"""Question set loading, fingerprints, dynamic options and acceptance lookup (U03-14 ... U03-20).

Design 03 §3.2 and §5.5. A question's fingerprint covers its defining fields (never its
dynamic options); one ``question_set_version`` never maps a question id to two fingerprints.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Final

import duckdb
import numpy as np
from pydantic import ValidationError

from herness.core.errors import ConfigError
from herness.core.ids import canonical_json, sha256_hex
from herness.core.logging import get_logger
from herness.core.types import Question, QuestionSet
from herness.enrich.layout import EnrichPaths
from herness.enrich.settings import AcceptanceCriteria, DecisionsConfig, QuestionConfig

PAIR_QUESTIONS: Final[frozenset[str]] = frozenset({"change_caused_pair"})

_log = get_logger("enrich.questions")

_FINGERPRINT_FIELDS: Final = (
    "id", "type", "instructions", "options", "options_source", "levels", "applies_to",
    "threshold", "scoring_use",
)  # fmt: skip
_REGISTRY_NAME: Final = "questions.json"
_REGISTRY_MAX_BYTES: Final = 64 * 1024
_MIN_OPTIONS: Final = 2
_MAX_DESCRIPTION: Final = 500
_FINGERPRINT_RE: Final = re.compile(r"^[0-9a-f]{16}$")
# U03-02 rule (e): a copy, since OWN041 forbids importing herness.core.types.decisions directly;
# a unit test pins it to the owner's constants
_OPTION_KEY_RE: Final = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_BOOL_WORDS: Final = frozenset({"true", "false", "yes", "no"})
_VECTOR_SHAPE: Final = (1024,)
_DYNAMIC_SQL: Final = {
    "core.team": "SELECT team_id, name FROM core.team WHERE active ORDER BY team_id",
    "core.service": "SELECT service_id, name FROM core.service ORDER BY service_id",
}


def question_fingerprint(q: QuestionConfig | Question, /) -> str:
    """sha256[:16] of the canonical JSON of the fingerprinted fields (U03-15)."""
    fields: dict[str, object] = {name: getattr(q, name) for name in _FINGERPRINT_FIELDS}
    if q.options_source != "static":
        fields["options"] = None  # dynamic options never enter the fingerprint
    fields["levels"] = None if q.levels is None else list(q.levels)
    fields["applies_to"] = list(q.applies_to)
    fields["threshold"] = format(q.threshold, ".6f")
    return sha256_hex(canonical_json(fields))[:16]


def _error_text(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())


def _build_question(qc: QuestionConfig) -> Question:
    fields = {name: getattr(qc, name) for name in _FINGERPRINT_FIELDS}
    try:
        return Question.model_validate({**fields, "fingerprint": question_fingerprint(qc)})
    except ValidationError as exc:
        msg = f"invalid question {qc.id}: {_error_text(exc)}"
        raise ConfigError(msg, question=qc.id) from exc


def _check_pair_shape(q: Question) -> None:
    if q.type != "bool" or q.scoring_use or q.applies_to != ("incident",):
        msg = f"pair question {q.id} must be bool, scoring_use false, applies_to [incident]"
        raise ConfigError(msg, question=q.id)


def load_question_set(cfg: DecisionsConfig, /) -> QuestionSet:
    """Turn ``cfg.questions`` into a validated, fingerprinted ``QuestionSet`` (U03-16)."""
    seen: set[str] = set()
    questions: list[Question] = []
    for qc in cfg.questions:
        if qc.id in seen:
            msg = f"duplicate question id {qc.id}"
            raise ConfigError(msg, question=qc.id)
        seen.add(qc.id)
        questions.append(_build_question(qc))
    try:
        qs = QuestionSet(version=cfg.question_set_version, questions=tuple(questions))
    except ValidationError as exc:
        msg = f"invalid question set (question_set_version or size): {_error_text(exc)}"
        raise ConfigError(msg) from exc
    for qid in sorted(PAIR_QUESTIONS & seen):
        _check_pair_shape(qs.get(qid))
    return qs


def _read_registry(path: Path) -> dict[str, str]:
    """Read ``questions.json`` once (≤ 64 KB, ``{qid: fingerprint}``); absent file → empty."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(_REGISTRY_MAX_BYTES + 1)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        msg = f"{_REGISTRY_NAME} is unreadable"
        raise ConfigError(msg, path=str(path)) from exc
    if len(raw) > _REGISTRY_MAX_BYTES:
        msg = f"{_REGISTRY_NAME} exceeds 64 KB"
        raise ConfigError(msg, path=str(path))
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        msg = f"{_REGISTRY_NAME} is unreadable"
        raise ConfigError(msg, path=str(path)) from exc
    valid = isinstance(data, dict) and all(
        isinstance(v, str) and _FINGERPRINT_RE.fullmatch(v) is not None for v in data.values()
    )
    if not valid:
        msg = f"{_REGISTRY_NAME} is unreadable: not an object of fingerprints"
        raise ConfigError(msg, path=str(path))
    return dict(data)


def _write_atomic(path: Path, text: str) -> None:
    """Write ``text`` to a temp file next to ``path``, fsync it and ``os.replace`` it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def check_fingerprint_registry(qs: QuestionSet, /, *, paths: EnrichPaths) -> None:
    """Refuse a changed question under an unchanged version; record new ids (U03-17)."""
    path = paths.cache_dir(qs.version) / _REGISTRY_NAME
    registry = _read_registry(path)
    for q in qs.questions:
        old = registry.get(q.id)
        if old is not None and old != q.fingerprint:
            _log.error(
                "enrich.config.fingerprint_drift", question=q.id, question_set_version=qs.version
            )
            msg = f"question {q.id} changed without a new question_set_version"
            raise ConfigError(msg, question=q.id)
    updated = {**registry, **{q.id: q.fingerprint for q in qs.questions}}
    if updated != registry or not path.exists():
        _write_atomic(path, canonical_json(updated) + "\n")


def _dynamic_options(
    qid: str, rows: Iterable[tuple[object, object]], redact: Callable[[str], str]
) -> dict[str, str]:
    """Label = id; description = redacted name (≤ 500 chars) or the id (U03-18 steps 2-4)."""
    options: dict[str, str] = {}
    skipped = 0
    for raw_id, name in rows:
        label = str(raw_id)
        if _OPTION_KEY_RE.fullmatch(label) is None or label.lower() in _BOOL_WORDS:
            skipped += 1
            continue
        description = redact(name)[:_MAX_DESCRIPTION] if isinstance(name, str) and name else ""
        options[label] = description or label
    if skipped:
        _log.warning("enrich.questions.option_skipped", question=qid, count=skipped)
    if len(options) < _MIN_OPTIONS:
        msg = f"dynamic options < 2 for {qid}"
        raise ConfigError(msg, question=qid)
    return options


def resolve_dynamic_options(
    qs: QuestionSet,
    /,
    *,
    wh: duckdb.DuckDBPyConnection,
    redact: Callable[[str], str],
) -> QuestionSet:
    """Fill ``options`` of dynamic-source choice questions from the warehouse (U03-18).

    ``redact`` is T10-10 (herness.core.redact.redact_text), injected until it lands.
    """
    rows_by_source: dict[str, list[tuple[object, object]]] = {}
    questions: list[Question] = []
    for q in qs.questions:
        source = q.options_source
        if q.type != "choice" or source == "static":
            questions.append(q)
            continue
        if source not in rows_by_source:
            rows_by_source[source] = [
                (r[0], r[1]) for r in wh.execute(_DYNAMIC_SQL[source]).fetchall()
            ]
        options = _dynamic_options(q.id, rows_by_source[source], redact)
        questions.append(q.model_copy(update={"options": options}))
    return QuestionSet(version=qs.version, questions=tuple(questions))


def _check_vector(shape: tuple[int, ...], what: str, qid: str) -> None:
    if shape != _VECTOR_SHAPE:
        msg = f"{what} of question {qid} must have shape (1024,)"
        raise ConfigError(msg, question=qid)


def shortlist_options(
    q: Question,
    text_vec: np.ndarray,
    option_vecs: Mapping[str, np.ndarray],
    *,
    k: int = 64,
) -> Question:
    """Keep the top-``k`` options by cosine similarity to ``text_vec`` (U03-19)."""
    if q.type != "choice" or not q.options:
        msg = f"shortlist needs a choice question with options: {q.id}"
        raise ConfigError(msg, question=q.id)
    options = q.options
    labels = sorted(options)
    _check_vector(np.shape(text_vec), "text vector", q.id)
    for label in labels:
        if label not in option_vecs:
            msg = f"no option vector for {label} of question {q.id}"
            raise ConfigError(msg, question=q.id)
        _check_vector(np.shape(option_vecs[label]), f"option vector {label}", q.id)
    matrix = np.stack([np.asarray(option_vecs[label], dtype=np.float32) for label in labels])
    sims = matrix @ np.asarray(text_vec, dtype=np.float32)
    order = sorted(range(len(labels)), key=lambda i: (-float(sims[i]), labels[i]))[:k]
    return q.model_copy(update={"options": {labels[i]: options[labels[i]] for i in order}})


def acceptance_for(cfg: DecisionsConfig, qid: str) -> AcceptanceCriteria:
    """Per-type default overlaid field by field by the question's own overrides (U03-20)."""
    qc = next((q for q in cfg.questions if q.id == qid), None)
    if qc is None:
        msg = f"unknown question {qid}"
        raise ConfigError(msg, question=qid)
    defaults = {
        "choice": cfg.acceptance.choice,
        "bool": cfg.acceptance.bool_,
        "score": cfg.acceptance.score,
    }[qc.type]
    if qc.acceptance is None:
        return defaults
    return defaults.model_copy(update=qc.acceptance.model_dump(exclude_none=True))
