"""Classifier evaluation: the Laya gate cross-check, decider comparison and synthetic truth
check (U11-67; flow F11-09; design 11 §5.4).

`run_classifier` reads the active Laya version (`CURRENT`), its `manifest.json` (verified with
impl 03's `verify_model_dir`) and `eval.json`, recomputes `gold_sha256` over the gold
directory with `gold_digest`, and recomputes every stored gate metric from the gold rows and
the Laya cache rows with impl 03's `question_metrics`. A difference above
`classifier.gate_recompute_tolerance`, a gold hash mismatch (or an unreadable gold part),
missing cache rows or an accepted question with a failed criterion is a failed check
(`passed` false, exit 14), logged as `eval.classifier.mismatch`; it is never an error.
A missing `CURRENT`, manifest, `eval.json` or gold set is `ConfigError` (exit 3). The
decider comparison and the synthetic check (private siblings `_classifier_deciders` and
`_classifier_metrics`) are informational. The question set (types, thresholds,
fingerprints) and the decider settings come from the loaded config.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import structlog
from pydantic import BaseModel, ConfigDict, ValidationError

from herness.core.config import get_config
from herness.core.errors import ConfigError, FatalError
from herness.core.logging import get_logger
from herness.core.types import Question, QuestionSet
from herness.enrich.cache import DecisionCache
from herness.enrich.calibrate import CalibrationStore
from herness.enrich.evaluate import QuestionMetrics, question_metrics
from herness.enrich.labels import LabelStore, gold_digest
from herness.enrich.laya_models import LayaManifest, read_current, verify_model_dir
from herness.enrich.layout import EnrichPaths
from herness.enrich.questions import load_question_set
from herness.eval._classifier_deciders import (
    Bootstrap,
    GoldRow,
    compare_deciders,
    fingerprint,
    latest_dists,
    synthetic_metrics,
)
from herness.eval._classifier_metrics import DeciderMetrics, ReliabilityBin, label_space, matrix
from herness.eval.truth import load_truth_labels

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

    from herness.eval.settings import ClassifierSettings

__all__ = ["ClassifierReport", "DeciderMetrics", "GateMetricCheck", "ReliabilityBin",
           "default_deciders", "run_classifier"]  # fmt: skip

REPORT_VERSION: Final = 1
MAX_EVAL_BYTES: Final = 4 * 1024 * 1024
_LAYA: Final = "laya"
_GATE_FIELDS: Final = (  # the `laya` block of eval.json (impl 03 U03-129)
    "accuracy", "macro_f1", "mae", "within_one", "ece", "temperature",
    "coverage_at_threshold", "accuracy_at_threshold",
)  # fmt: skip

_log = get_logger("eval.classifier")

type CheckName = Literal[
    "question_set_version",
    "model_version",
    "gold_sha256",
    "question",
    "cache_rows",
    "metric",
    "calibration",
    "accepted_criteria",
]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class GateMetricCheck(_Model):
    """One gate cross-check (F11-09 steps 2-4): what was stored, what was recomputed."""

    check: CheckName
    passed: bool
    question: str | None = None
    metric: str | None = None
    stored: float | str | None = None
    recomputed: float | str | None = None
    detail: str = ""


class ClassifierReport(_Model):
    """`classifier.json` (U11-67): the Laya `version` checked, the gate checks, the decider
    comparison (decider -> question -> metrics) and the synthetic check (None on real
    builds). `passed` = every gate check passed."""

    version: str
    question_set_version: str
    gate_check: list[GateMetricCheck]
    deciders: dict[str, dict[str, DeciderMetrics]]
    synthetic: dict[str, DeciderMetrics] | None
    passed: bool

    def check(self, name: CheckName) -> GateMetricCheck | None:
        """The first check called `name`, if any."""
        return next((c for c in self.gate_check if c.check == name), None)


class _EvalEntry(BaseModel):
    """One question of `eval.json` (the keys this check reads; impl 03 writes more)."""

    question_fingerprint: str
    laya: dict[str, float | None]
    passed: dict[str, bool]


class _EvalDoc(BaseModel):
    """Eval-side reader of impl 03's `eval.json` (no public reader yet; carry-over T03-36)."""

    version: str
    question_set_version: str
    gold_sha256: str
    questions: dict[str, _EvalEntry]


def default_deciders() -> list[str]:
    """F11-09 step 5: laya, openjev, jev (when enabled), llm, ensemble, human."""
    jev = get_config().models.deciders.jev.enabled
    return [_LAYA, "openjev", *(["jev"] if jev else []), "llm", "ensemble", "human"]


def _paths(data_root: Path) -> EnrichPaths:
    cfg = get_config()
    return EnrichPaths(
        data_root=data_root.resolve(),
        embedding_path=cfg.decisions.embedding.path,
        laya_current_file=cfg.models.deciders.laya.current_file,
    )


def _read_eval(paths: EnrichPaths, version: str) -> _EvalDoc:
    path = paths.laya_dir(version) / "eval.json"
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_EVAL_BYTES + 1)
    except OSError:
        msg = f"{version}: eval.json missing or unreadable"
        raise ConfigError(msg, version=version) from None
    if len(raw) > MAX_EVAL_BYTES:
        msg = f"{version}: eval.json larger than 4 MB"
        raise ConfigError(msg, version=version)
    try:
        return _EvalDoc.model_validate(json.loads(raw))
    except (ValueError, ValidationError):
        msg = f"{version}: eval.json malformed"
        raise ConfigError(msg, version=version) from None


def _question_set(qset_version: str) -> QuestionSet:
    qs = load_question_set(get_config().decisions)
    if qs.version != qset_version:
        msg = f"question set {qset_version} is not the configured one"
        raise ConfigError(msg, question_set_version=qset_version)
    return qs


def _gold(store: LabelStore, qsv: str) -> pa.Table | None:
    """The whole gold table; None when a part cannot be read (a failed gold check, ST11-04).
    `ConfigError` when the gold directory holds no part."""
    gold_dir = store.paths.labels_dir(qsv, "gold")
    if not any(gold_dir.glob("part-*.parquet")):
        msg = f"classifier gold set missing for {qsv}"
        raise ConfigError(msg, question_set_version=qsv)
    try:
        return store.read("gold")
    except ConfigError:
        raise
    except (pa.ArrowException, OSError, FatalError):  # a corrupt part; StoreBusy propagates
        return None


def _frozen(store: LabelStore, gold: pa.Table, qs: QuestionSet) -> dict[str, list[GoldRow]]:
    """Gold rows (content_hash, answer, fold) of questions frozen at their current
    fingerprint, as impl 03's evaluate step selects them."""
    out: dict[str, list[GoldRow]] = {}
    for q in qs.questions:
        fp = fingerprint(q)
        if not store.is_gold_frozen(q.id, fp):
            continue
        mask = pc.and_(pc.equal(gold["question"], q.id), pc.equal(gold["question_fingerprint"], fp))
        rows = gold.filter(mask).select(["content_hash", "answer", "fold"]).to_pylist()
        if rows:
            out[q.id] = rows
    return out


class _Checks:
    """Collects gate checks; logs each failed one as `eval.classifier.mismatch` (run id from
    the bound log context; never ticket text)."""

    def __init__(self) -> None:
        self.rows: list[GateMetricCheck] = []

    def add(self, check: CheckName, passed: bool, **fields: object) -> None:
        row = GateMetricCheck.model_validate({"check": check, "passed": passed, **fields})
        self.rows.append(row)
        if not passed:
            run_id = structlog.contextvars.get_contextvars().get("run_id")
            _log.error("eval.classifier.mismatch", run_id=run_id, question=row.question,
                       metric=row.metric or check, stored=row.stored,
                       recomputed=row.recomputed)  # fmt: skip


def _close(stored: float | None, recomputed: float | None, tolerance: float) -> bool:
    if stored is None or recomputed is None:
        return stored is None and recomputed is None
    return abs(stored - recomputed) <= tolerance


def _recompute(
    q: Question, rows: Sequence[GoldRow], laya: Mapping[tuple[str, str], Mapping[str, float]],
    teacher: Mapping[tuple[str, str], Mapping[str, float]],
) -> tuple[QuestionMetrics, int]:  # fmt: skip
    """impl 03's metrics of Laya on one question; the label space also covers the teacher's
    answers, as `evaluate_candidate` builds it. Returns the metrics and the rows scored."""
    seen = [str(r["answer"]) for r in rows]
    seen += [a for c in (laya, teacher) for (qid, _), d in c.items() if qid == q.id for a in d]
    space = label_space(q, seen)
    present = [r for r in rows if (q.id, str(r["content_hash"])) in laya]
    probs = matrix([laya[(q.id, str(r["content_hash"]))] for r in present], space)
    labels = np.array([space.index(str(r["answer"])) for r in present], dtype=np.int64)
    folds = np.array([r["fold"] for r in present], dtype=np.int64)
    metrics = question_metrics(probs, labels, folds, qtype=q.type, threshold=q.threshold)
    return metrics, len(present)


type _Cache = Mapping[tuple[str, str], Mapping[str, float]]


@dataclass(frozen=True)
class _Gate:
    """What steps 3-4 check: the stored documents, the question set and the frozen gold."""

    doc: _EvalDoc
    manifest: LayaManifest
    qs: QuestionSet
    gold: Mapping[str, Sequence[GoldRow]]
    paths: EnrichPaths
    tolerance: float


def _question_checks(
    checks: _Checks, gate: _Gate, q: Question, caches: tuple[_Cache, _Cache],
    stored_t: float | None,
) -> None:  # fmt: skip
    """F11-09 step 3 for one question: cache coverage, every stored metric, the calibration
    file's temperature."""
    entry, rows = gate.doc.questions[q.id], gate.gold[q.id]
    metrics, n = _recompute(q, rows, *caches)
    missing = "" if n == len(rows) else "missing Laya cache rows"
    checks.add("cache_rows", not missing, question=q.id, metric="n", stored=float(len(rows)),
               recomputed=float(n), detail=missing)  # fmt: skip
    for name in _GATE_FIELDS:
        stored, recomputed = entry.laya.get(name), getattr(metrics, name)
        checks.add("metric", _close(stored, recomputed, gate.tolerance), question=q.id,
                   metric=name, stored=stored, recomputed=recomputed)  # fmt: skip
    t = entry.laya.get("temperature")
    checks.add("calibration", _close(t, stored_t, gate.tolerance), question=q.id,
               metric="temperature", stored=stored_t, recomputed=t,
               detail="" if stored_t is not None else "no calibration.json entry")  # fmt: skip


def _skip_reason(gate: _Gate, qid: str, current: str | None) -> str | None:
    if current is None:
        return "not in the question set"
    if current != gate.doc.questions[qid].question_fingerprint:
        return "fingerprint changed"
    return None if qid in gate.gold else "no frozen gold"


def _gate(checks: _Checks, gate: _Gate) -> None:
    """Steps 3 (every question of eval.json recomputed) and 4 (accepted questions)."""
    doc, manifest, qs = gate.doc, gate.manifest, gate.qs
    cache = DecisionCache(gate.paths, qs.version)
    known = {q.id: q for q in qs.questions}
    fingerprints = {qid: fingerprint(known[qid]) for qid in doc.questions if qid in known}
    laya = latest_dists(cache, _LAYA, manifest.version, fingerprints)  # the CURRENT version
    teacher = latest_dists(cache, manifest.teacher, manifest.teacher_version, fingerprints)
    temperatures = CalibrationStore(gate.paths).load(_LAYA, manifest.version, qs.version)
    for qid, entry in sorted(doc.questions.items()):
        reason = _skip_reason(gate, qid, fingerprints.get(qid))
        if reason is not None:
            checks.add("question", False, question=qid, stored=entry.question_fingerprint,
                       recomputed=fingerprints.get(qid), detail=reason)  # fmt: skip
            continue
        cal = temperatures.get(qid)
        _question_checks(checks, gate, known[qid], (laya, teacher),
                         None if cal is None else cal.temperature)  # fmt: skip
    for qid in manifest.accepted_questions:  # step 4
        stored = doc.questions.get(qid)
        if stored is None:
            checks.add("accepted_criteria", False, question=qid, detail="not in eval.json")
            continue
        failed = sorted(name for name, ok in stored.passed.items() if not ok)
        detail = ", ".join(failed) or ("" if stored.passed else "no criteria")
        checks.add("accepted_criteria", not detail, question=qid, detail=detail)


def run_classifier(  # noqa: PLR0913 - U11-67 signature
    qset_version: str,
    deciders: Sequence[str],
    *,
    data_root: Path,
    warehouse: DuckDBPyConnection | None,
    truth_dir: Path | None,
    settings: ClassifierSettings,
    rng_seed: int = 20261001,
) -> ClassifierReport:
    """Classifier eval (U11-67, flow F11-09 steps 1-6); the caller writes the files (step 7)."""
    qs = _question_set(qset_version)
    paths = _paths(data_root)
    version = read_current(paths)  # step 1
    manifest = verify_model_dir(paths, version)
    doc = _read_eval(paths, version)
    store = LabelStore(paths, qset_version)
    gold_table = _gold(store, qset_version)
    checks = _Checks()
    checks.add("model_version", doc.version == version, stored=doc.version, recomputed=version)
    checks.add("question_set_version", doc.question_set_version == qset_version,
               stored=doc.question_set_version, recomputed=qset_version)  # fmt: skip
    digest = None if gold_table is None else gold_digest(gold_table)  # step 2
    unreadable = "" if gold_table is not None else "gold unreadable"
    checks.add("gold_sha256", digest == doc.gold_sha256, stored=doc.gold_sha256,
               recomputed=digest, detail=unreadable)  # fmt: skip
    gold = {} if gold_table is None else _frozen(store, gold_table, qs)
    if gold_table is not None:
        tolerance = settings.gate_recompute_tolerance
        _gate(checks, _Gate(doc, manifest, qs, gold, paths, tolerance))
    boot = Bootstrap(np.random.default_rng(rng_seed), settings.bootstrap)
    fixed = {_LAYA: version} | (
        {"openjev": manifest.teacher_version} if manifest.teacher == "openjev" else {}
    )
    compared = compare_deciders(deciders, qs, gold, cache=DecisionCache(paths, qset_version),
                                store=store, fixed=fixed, boot=boot)  # fmt: skip
    synthetic = None
    if truth_dir is not None and warehouse is not None:  # step 6
        synthetic = synthetic_metrics(qs, qset_version, warehouse, load_truth_labels(truth_dir),
                                      sample=settings.synthetic_sample, boot=boot)  # fmt: skip
    return ClassifierReport(
        version=version, question_set_version=qset_version, gate_check=checks.rows,
        deciders=compared, synthetic=synthetic, passed=all(c.passed for c in checks.rows),
    )  # fmt: skip
