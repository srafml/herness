"""Deep-mode ensemble stage: band selection and pooling (impl 03 U03-88, U03-89; F03-08).

Design 03 §5.9. `ensemble_band` reads ``enrich_resolved`` and the ``enrich_laya_cal`` temp
table that ``resolve_decisions.sql`` (U03-78) leaves behind; `run_ensemble_pool` pools the
members' cached rows only through the `EnsembleDecider` (U03-67), with temperatures from the
`CalibrationStore`; it calls no model or client. The caller (`run_enrichment`, T03-28) holds
the GPU class and runs the members first. Restartable: pairs with a current ensemble row are
not pooled again, and reviews are selected from the cached ensemble rows.
Carry-over: `report` is typed by the private `_Report` protocol until `StageReport` (U03-142,
T03-28) lands. Logs, errors and payloads carry counts, ids, hashes and codes only (TH03-03).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from itertools import groupby
from typing import Final, Protocol

import duckdb
import pyarrow as pa
import pyarrow.dataset as ds

from herness.core import time as clock
from herness.core.errors import SchemaViolation
from herness.core.logging import get_logger
from herness.core.types import DecisionInput, QuestionSet
from herness.enrich.cache import DecisionCache, io_error
from herness.enrich.calibrate import CalibrationStore
from herness.enrich.deciders.ensemble import EnsembleDecider
from herness.enrich.questions import PAIR_QUESTIONS, question_fingerprint
from herness.enrich.resolve import QueueItem
from herness.enrich.review_items import create_if_absent
from herness.enrich.settings import DecisionsConfig

__all__ = ["ensemble_band", "run_ensemble_pool"]

POOL_CHUNK: Final = 2_000  # band items per `decide` call and cache rows per flush (U03-89)
AGREEMENT_MIN: Final = 2 / 3  # pooled rows below it get a disagreement review (design §5.9)
_PURPOSE: Final = "ensemble_disagreement"
# U03-83's keys, statuses and scope; qsv matched via `scope` only (T03-20 note on U03-148).
_KEYS: Final = ("purpose", "question", "content_hash")
_BLOCKING: Final = ("pending", "approved", "rejected")
_CACHE, _BAND = "_ens_cache", "_ens_band"  # registered views of the disagreement query
_SCHEMA_ERRORS: Final = (duckdb.CatalogException, duckdb.BinderException)
_BAND_COLS: Final = ("record_id", "entity", "content_hash", "question", "fingerprint")
_BAND_SCHEMA: Final = pa.schema([(column, pa.string()) for column in _BAND_COLS])

_BAND_SQL: Final = """
WITH low AS (
    SELECT r.record_id, r.entity, r.content_hash, r.question, r.opened_at
    FROM enrich_resolved AS r
    JOIN enrich_laya_cal AS l ON l.record_id = r.record_id AND l.question = r.question
    WHERE r.scoring_use AND list_contains(CAST($questions AS VARCHAR[]), r.question)
        AND l.p_cal < $band
),
rec AS (
    SELECT record_id, entity, content_hash, list_sort(list(DISTINCT question)) AS question_ids,
        max(opened_at) AS opened_at
    FROM low GROUP BY record_id, entity, content_hash
    ORDER BY opened_at DESC NULLS LAST, record_id, entity
    LIMIT $max_rows
)
SELECT r.record_id, r.entity, r.content_hash, t.text, r.question_ids
FROM rec AS r
JOIN enrich.text_redacted AS t ON t.record_id = r.record_id AND t.entity = r.entity
ORDER BY r.opened_at DESC NULLS LAST, r.record_id, r.entity
"""
# Latest current-version ensemble row per (content_hash, question) of the band (lowest record
# per hash, the review item match key), agreement < 2/3, in the hash order of U03-81, capped.
_DISAGREE_SQL: Final = f"""
WITH latest AS (
    SELECT b.record_id, b.content_hash, b.question, b.fingerprint, c.answer, c.probability,
        c.backend_confidence
    FROM {_BAND} AS b
    JOIN {_CACHE} AS c ON c.content_hash = b.content_hash AND c.question = b.question
        AND c.question_fingerprint = b.fingerprint
    WHERE c.decider = 'ensemble' AND c.decider_version = $version
    QUALIFY row_number() OVER (PARTITION BY b.content_hash, b.question
        ORDER BY c.decided_at DESC, b.record_id, b.entity) = 1
)
SELECT record_id, content_hash, question, fingerprint, answer, probability
FROM latest WHERE backend_confidence < $agreement_min
ORDER BY sha256($build_id || '|' || content_hash || '|' || question)
LIMIT $cap
"""  # noqa: S608 - fixed view names

_log = get_logger("enrich.ensemble")


class _Report(Protocol):
    """Fields the stage mutates. T03-28: retype to StageReport (U03-142)."""

    status: str
    note: str | None
    decided: int


def _duck_error(exc: duckdb.Error, *, where: str) -> SchemaViolation:
    """SchemaViolation naming only a catalog/binder message or the class (never row values)."""
    reason = str(exc).splitlines()[0] if isinstance(exc, _SCHEMA_ERRORS) else type(exc).__name__
    return SchemaViolation(f"{where}: {reason}")


def ensemble_band(
    wh: duckdb.DuckDBPyConnection, *, cfg: DecisionsConfig, qs: QuestionSet
) -> list[QueueItem]:
    """Records with a ``scoring_use`` question whose calibrated Laya p < ``ensemble.band``.

    U03-88: each item lists those questions; at most ``ensemble.max_rows`` records, ordered by
    ``opened_at DESC`` (NULLs last), ``record_id``. Needs ``enrich_resolved`` and
    ``enrich_laya_cal`` (U03-78). Raises SchemaViolation on a DuckDB error.
    """
    scoring = [q.id for q in qs.questions if q.scoring_use and q.id not in PAIR_QUESTIONS]
    if not scoring or cfg.ensemble.max_rows == 0:
        return []
    params = {"questions": scoring, "band": cfg.ensemble.band, "max_rows": cfg.ensemble.max_rows}
    try:
        rows = wh.execute(_BAND_SQL, params).fetchall()
    except duckdb.Error as exc:
        raise _duck_error(exc, where="ensemble_band") from exc
    return [QueueItem(rid, entity, ch, text, tuple(qids)) for rid, entity, ch, text, qids in rows]


def _present(
    cache: DecisionCache, members: Sequence[tuple[str, str]], band: Sequence[QueueItem]
) -> list[tuple[str, str]]:
    """Members with at least one cached row for a band pair; the others drop out (U03-89)."""
    dataset = cache.dataset()
    if dataset is None or not band or not members:
        return []
    condition = (
        ds.field("content_hash").isin(sorted({i.content_hash for i in band}))
        & ds.field("question").isin(sorted({q for i in band for q in i.question_ids}))
        & ds.field("decider").isin(sorted({d for d, _ in members}))
    )
    try:
        table = dataset.to_table(columns=["decider", "decider_version"], filter=condition)
    except OSError as exc:
        raise io_error(exc, "cannot read decision cache", decider="ensemble") from exc
    seen = {(r["decider"], r["decider_version"]) for r in table.to_pylist()}
    return [(d, v) for d, v in members if (d, v) in seen]


def _weights(
    gold_accuracy: Mapping[tuple[str, str], float], present: Sequence[tuple[str, str]],
    band: Sequence[QueueItem],
) -> dict[tuple[str, str], float]:  # fmt: skip
    """Gold accuracy of the present members; warn per band question that lacks any of it."""
    names = {d for d, _ in present}
    weights = {key: w for key, w in gold_accuracy.items() if key[0] in names}
    for qid in sorted({q for item in band for q in item.question_ids}):
        if any((d, qid) not in weights for d in sorted(names)):
            _log.warning("enrich.ensemble.weights_default", question=qid)  # F03-08 step 4
    return weights


def _todo(band: Sequence[QueueItem], done: set[tuple[str, str]]) -> list[DecisionInput]:
    """Band items trimmed to the pairs without a current ensemble row (restart)."""
    todo: list[DecisionInput] = []
    for item in band:
        qids = tuple(q for q in item.question_ids if (item.content_hash, q) not in done)
        if qids:
            todo.append(DecisionInput.model_validate({"record_id": item.record_id,
                "entity": item.entity, "content_hash": item.content_hash, "text": item.text,
                "question_ids": qids}))  # fmt: skip
    return todo


def _pool(
    decider: EnsembleDecider, todo: Sequence[DecisionInput], qs: QuestionSet, cache: DecisionCache
) -> int:
    """U03-89 step 2: `decide` per chunk of 2,000 items, rows under (`ensemble`, version)."""
    writer = cache.writer(decider.name, decider.version, questions=qs, flush_rows=POOL_CHUNK)
    pooled = 0
    try:
        for start in range(0, len(todo), POOL_CHUNK):
            outputs = decider.decide(todo[start : start + POOL_CHUNK], qs)
            pooled += writer.add(outputs, samples=None)
    finally:
        writer.flush()  # a failed chunk keeps the earlier ones: the rerun skips them
    return pooled


def _band_table(band: Sequence[QueueItem], qs: QuestionSet) -> pa.Table:
    fps = {q.id: q.fingerprint or question_fingerprint(q) for q in qs.questions}
    rows = [
        {"record_id": i.record_id, "entity": i.entity, "content_hash": i.content_hash,
         "question": qid, "fingerprint": fps[qid]}
        for i in band for qid in i.question_ids if qid in fps
    ]  # fmt: skip
    return pa.Table.from_pylist(rows, schema=_BAND_SCHEMA)


def _disagreements(  # noqa: PLR0913 - the query's inputs
    wh: duckdb.DuckDBPyConnection, band: Sequence[QueueItem], qs: QuestionSet,
    cache: DecisionCache, version: str, *, build_id: str, cap: int,
) -> list[dict[str, object]]:  # fmt: skip
    """U03-89 step 3: ``label_check`` payloads for pooled rows with agreement < 2/3, capped."""
    params = {"version": version, "agreement_min": AGREEMENT_MIN, "build_id": build_id,
              "cap": cap}  # fmt: skip
    cache.register(wh, _CACHE)
    wh.register(_BAND, _band_table(band, qs))
    try:
        rows = wh.execute(_DISAGREE_SQL, params).fetchall()
    except duckdb.Error as exc:
        raise _duck_error(exc, where="run_ensemble_pool") from exc
    finally:
        wh.unregister(_BAND)
        wh.unregister(_CACHE)
    return [
        {"record_id": rid, "content_hash": ch, "question": qid, "question_fingerprint": fp,
         "question_set_version": qs.version, "answer": answer, "probability": p,
         "decider": "ensemble", "decider_version": version, "purpose": _PURPOSE,
         "text_ref": "enrich.text_redacted"}
        for rid, ch, qid, fp, answer, p in rows
    ]  # fmt: skip


def _create_reviews(payloads: list[dict[str, object]], qsv: str) -> int:
    """Create the missing review items per question (U03-148); return the created count."""
    total = 0
    now = clock.now()
    for question, group in groupby(sorted(payloads, key=lambda p: str(p["question"])),
                                   key=lambda p: p["question"]):  # fmt: skip
        created, _ = create_if_absent("label_check", list(group), match_keys=_KEYS,
            blocking_statuses=_BLOCKING, scope={"question_set_version": qsv}, now=now)  # fmt: skip
        total += created
        _log.info("enrich.spot_check.created", question=question, count=created,
                  purpose=_PURPOSE)  # fmt: skip
    return total


def run_ensemble_pool(  # noqa: PLR0913 - U03-89's keyword-only signature is binding
    wh: duckdb.DuckDBPyConnection, *, band: Sequence[QueueItem], qs: QuestionSet,
    cfg: DecisionsConfig, cache: DecisionCache, calibration: CalibrationStore,
    members: Sequence[tuple[str, str]], gold_accuracy: Mapping[tuple[str, str], float],
    build_id: str, report: _Report,
) -> str:  # fmt: skip
    """Stage 4b pooling (U03-89): pool the band, cache ensemble rows, queue disagreements.

    Members without a cached band row drop out (weights renormalize); a question lacking a
    present member's gold accuracy pools with equal weights (warning). Returns the version for
    ``versions["ensemble"]``. Cache, DuckDB (SchemaViolation) and ops errors propagate.
    """
    present = _present(cache, members, band)
    weights = _weights(gold_accuracy, present, band) if present else {}
    decider = EnsembleDecider(cache, calibration, members=present, weights=weights, qsv=qs.version)
    if not present:
        report.status, report.note = "skipped", "no_work"
        _log.info("enrich.ensemble.pooled", band=len(band), members=0, pooled=0, reviews=0)
        return decider.version
    done = cache.existing_keys(decider.name, decider.version, qs)
    pooled = _pool(decider, _todo(band, done), qs, cache)
    report.decided += pooled
    cap = cfg.ensemble.disagreement_review_cap
    payloads = _disagreements(wh, band, qs, cache, decider.version, build_id=build_id, cap=cap)
    created = _create_reviews(payloads, qs.version) if payloads else 0
    _log.info("enrich.ensemble.pooled", band=len(band), members=len(present), pooled=pooled,
              reviews=created)  # fmt: skip
    return decider.version
