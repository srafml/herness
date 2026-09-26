"""Gold evaluation of a Laya candidate: metrics, macro metric, ``eval.json`` (U03-127 ... U03-129).

Design 03 §4.4, §5.6 and §5.8 steps 6-7. The gate runs on frozen, held-out gold only (TH03-04);
an ``uncalibrated`` question is never proposed for acceptance (TH03-17). ``question_metrics`` is
pure and imported by spec 11's ``eval.json`` cross-check (ET03-02).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Collection, Mapping, Sequence
from datetime import datetime
from typing import Final

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.dataset as ds
from sklearn.metrics import f1_score  # type: ignore[import-untyped]  # sklearn ships no types

from herness.core import time as clock
from herness.core.ids import canonical_json
from herness.core.logging import get_logger
from herness.core.resilience.metrics import record_gauge
from herness.core.types import Question, QuestionSet, QuestionType
from herness.enrich.cache import DecisionCache, replace_atomic
from herness.enrich.calibrate import (
    CalibrationResult,
    CalibrationStore,
    apply_temperature,
    cross_fit,
)
from herness.enrich.labels import LabelStore, gold_digest
from herness.enrich.layout import EnrichPaths
from herness.enrich.questions import acceptance_for, question_fingerprint
from herness.enrich.settings import DecisionsConfig

_EVAL_FILE: Final = "eval.json"
_LAYA: Final = "laya"
_GAUGE: Final = "herness_enrich_gold_metric_ratio"
_FIXED_LABELS: Final[dict[str, tuple[str, ...]]] = {
    "bool": ("true", "false"),  # column 0 is p(true): apply_temperature calibrates column 0
    "score": ("0", "1", "2", "3"),  # column index == score value (MAE, within-one)
}
_CRITERION_METRIC: Final = {
    "min_accuracy": "accuracy", "min_macro_f1": "macro_f1", "max_ece": "ece",
    "min_coverage": "coverage_at_threshold", "min_within_one": "within_one", "max_mae": "mae",
}  # fmt: skip
_LAYA_FIELDS: Final = (
    "accuracy", "macro_f1", "mae", "within_one", "ece", "temperature",
    "coverage_at_threshold", "accuracy_at_threshold",
)  # fmt: skip
_GAUGE_FIELDS: Final = (
    "accuracy", "macro_f1", "within_one", "ece", "coverage_at_threshold",
    "accuracy_at_threshold",
)  # fmt: skip

_log = get_logger("enrich.evaluate")

type _Distribution = dict[str, float]


@dataclasses.dataclass(frozen=True, slots=True)
class QuestionMetrics:
    """Gold metrics of one decider on one question (U03-127)."""

    accuracy: float
    macro_f1: float | None
    mae: float | None
    within_one: float | None
    ece: float
    temperature: float
    coverage_at_threshold: float
    accuracy_at_threshold: float | None
    n: int
    uncalibrated: bool


def _empty_result() -> tuple[CalibrationResult, QuestionMetrics, np.ndarray]:
    cal = CalibrationResult(1.0, 0.0, 0.0, 0.0, 0, uncalibrated=True)
    metrics = QuestionMetrics(0.0, None, None, None, 0.0, 1.0, 0.0, None, 0, uncalibrated=True)
    return cal, metrics, np.zeros((0, 0))


def _fit_and_measure(
    probs: np.ndarray, labels: np.ndarray, folds: np.ndarray, qtype: QuestionType, threshold: float
) -> tuple[CalibrationResult, QuestionMetrics, np.ndarray]:
    """Cross-fit calibration, the metrics and the calibrated matrix P'."""
    labels = np.asarray(labels, dtype=np.int64)
    if len(labels) == 0:
        return _empty_result()
    cal = cross_fit(probs, labels, folds, qtype)
    calibrated = apply_temperature(probs, cal.temperature, qtype)
    pred = calibrated.argmax(axis=1)
    macro_f1 = mae = within_one = None
    if qtype == "choice":
        macro_f1 = float(f1_score(labels, pred, average="macro", zero_division=0.0))
    elif qtype == "score":
        distance = np.abs(pred - labels)
        mae, within_one = float(distance.mean()), float(np.mean(distance <= 1))
    covered = calibrated.max(axis=1) >= threshold
    metrics = QuestionMetrics(
        accuracy=float(np.mean(pred == labels)),
        macro_f1=macro_f1,
        mae=mae,
        within_one=within_one,
        ece=cal.ece,
        temperature=cal.temperature,
        coverage_at_threshold=float(np.mean(covered)),
        accuracy_at_threshold=float(np.mean(pred[covered] == labels[covered]))
        if covered.any()
        else None,
        n=len(labels),
        uncalibrated=cal.uncalibrated,
    )
    return cal, metrics, calibrated


def question_metrics(
    probs: np.ndarray,
    labels: np.ndarray,
    folds: np.ndarray,
    *,
    qtype: QuestionType,
    threshold: float,
) -> QuestionMetrics:
    """Gold metrics of one decider on one question (U03-127; design 03 §5.8 step 6).

    ``probs`` (n, K) are raw; they are calibrated with the cross-fitted T before measuring.
    """
    return _fit_and_measure(probs, labels, folds, qtype, threshold)[1]


def macro_metric(metrics: Mapping[str, QuestionMetrics], qs: QuestionSet) -> float:
    """Mean primary metric over ``scoring_use`` questions present in ``metrics`` (U03-128).

    Primary metric (OI-11): ``accuracy`` for choice and bool, ``within_one`` for score.
    """
    values = [
        (m.within_one or 0.0) if q.type == "score" else m.accuracy
        for q in qs.questions
        if q.scoring_use and (m := metrics.get(q.id)) is not None
    ]
    return float(np.mean(values)) if values else 0.0


def _fingerprint(q: Question) -> str:
    return q.fingerprint or question_fingerprint(q)


def _frozen_gold(
    store: LabelStore, qs: QuestionSet
) -> tuple[pa.Table, dict[str, list[dict[str, object]]]]:
    """The whole gold table (digested), and by qid the rows of frozen questions at the current
    fingerprint (evaluated)."""
    gold = store.read("gold")
    keep = [
        (q.id, _fingerprint(q)) for q in qs.questions if store.is_gold_frozen(q.id, _fingerprint(q))
    ]
    mask = np.zeros(gold.num_rows, dtype=bool)
    for qid, fp in keep:
        match = pc.and_(pc.equal(gold["question"], qid), pc.equal(gold["question_fingerprint"], fp))
        mask |= np.asarray(match.to_numpy(zero_copy_only=False), dtype=bool)
    frozen = gold.filter(pa.array(mask, type=pa.bool_()))
    by_qid: dict[str, list[dict[str, object]]] = {qid: [] for qid, _ in keep}
    for row in frozen.select(["question", "content_hash", "answer", "fold"]).to_pylist():
        by_qid[str(row["question"])].append(row)
    return gold, by_qid


def _cache_rows(
    cache: DecisionCache, decider: tuple[str, str], qs: QuestionSet
) -> dict[tuple[str, str], _Distribution]:
    """Latest distribution per (question, content_hash) at the current fingerprints."""
    dataset = cache.dataset()
    if dataset is None:
        return {}
    condition = (
        (ds.field("decider") == decider[0])
        & (ds.field("decider_version") == decider[1])
        & ds.field("question_fingerprint").isin([_fingerprint(q) for q in qs.questions])
    )
    columns = ["question", "question_fingerprint", "content_hash", "distribution", "decided_at"]
    table = dataset.to_table(columns=columns, filter=condition)
    current = {q.id: _fingerprint(q) for q in qs.questions}
    latest: dict[tuple[str, str], tuple[datetime, _Distribution]] = {}
    for row in table.sort_by([("decided_at", "ascending")]).to_pylist():
        if current.get(row["question"]) == row["question_fingerprint"]:
            key = (row["question"], row["content_hash"])
            latest[key] = (row["decided_at"], dict(row["distribution"] or []))
    return {key: dist for key, (_, dist) in latest.items()}


def _label_space(
    q: Question, gold: Sequence[Mapping[str, object]], dists: Sequence[_Distribution]
) -> tuple[str, ...]:
    """Answer columns: fixed for bool and score; options then any other seen answer for choice."""
    if q.type in _FIXED_LABELS:
        return _FIXED_LABELS[q.type]
    base = tuple(q.options or ())
    seen = {str(row["answer"]) for row in gold} | {key for d in dists for key in d}
    return base + tuple(sorted(seen - set(base)))


def _matrix(dists: Sequence[_Distribution], space: Sequence[str]) -> np.ndarray:
    """Rows of ``dists`` over ``space``, normalized; an all-zero row becomes uniform."""
    probs = np.array([[d.get(a, 0.0) for a in space] for d in dists], dtype=np.float64)
    probs = probs.reshape(len(dists), len(space))
    sums = probs.sum(axis=1, keepdims=True)
    return np.where(sums > 0, probs / np.where(sums > 0, sums, 1.0), 1.0 / len(space))


@dataclasses.dataclass(frozen=True, slots=True)
class _Scored:
    """One decider on one question: calibration, metrics and calibrated rows by content hash."""

    cal: CalibrationResult
    metrics: QuestionMetrics
    rows: dict[str, np.ndarray]
    space: tuple[str, ...]


def _score(
    q: Question,
    gold: Sequence[Mapping[str, object]],
    cached: Mapping[tuple[str, str], _Distribution],
    space: tuple[str, ...],
) -> _Scored:
    present = [row for row in gold if (q.id, row["content_hash"]) in cached]
    dists = [cached[(q.id, str(row["content_hash"]))] for row in present]
    labels = np.array([space.index(str(row["answer"])) for row in present], dtype=np.int64)
    folds = np.array([row["fold"] for row in present], dtype=np.int64)
    probs = _matrix(dists, space)
    cal, metrics, calibrated = _fit_and_measure(probs, labels, folds, q.type, q.threshold)
    hashes = [str(row["content_hash"]) for row in present]
    return _Scored(cal, metrics, dict(zip(hashes, calibrated, strict=True)), space)


def _system_accuracy(
    gold: Sequence[Mapping[str, object]], laya: _Scored, teacher: _Scored, q: Question
) -> float:
    """Gate plus simulated escalation: Laya when calibrated p >= threshold, else the teacher."""
    space_index = {a: i for i, a in enumerate(laya.space)}
    correct = 0
    for row in gold:
        h, gold_index = str(row["content_hash"]), space_index.get(str(row["answer"]))
        p = laya.rows.get(h)
        if p is None or p.max() < q.threshold:
            p = teacher.rows.get(h)
        correct += p is not None and int(p.argmax()) == gold_index
    return correct / len(gold) if gold else 0.0


def _passed(
    criteria: Mapping[str, float], laya: QuestionMetrics, teacher_accuracy: float | None
) -> dict[str, bool]:
    """``min_*`` -> value >= bound, ``max_*`` -> value <= bound; a missing metric fails."""
    passed: dict[str, bool] = {}
    for name, bound in criteria.items():
        if name == "max_gap_to_teacher":
            value = None if teacher_accuracy is None else teacher_accuracy - laya.accuracy
        else:
            value = getattr(laya, _CRITERION_METRIC[name])
        passed[name] = value is not None and (
            value <= bound if name.startswith("max_") else value >= bound
        )
    return passed


def _question_entry(
    q: Question, cfg: DecisionsConfig, gold: Sequence[Mapping[str, object]],
    laya: _Scored, teacher: _Scored, teacher_name: str,
) -> dict[str, object]:  # fmt: skip
    has_teacher = teacher.metrics.n > 0
    teacher_accuracy = teacher.metrics.accuracy if has_teacher else None
    criteria = acceptance_for(cfg, q.id).model_dump(exclude_none=True)
    passed = _passed(criteria, laya.metrics, teacher_accuracy)
    return {
        "type": q.type,
        "question_fingerprint": _fingerprint(q),
        "n_gold": len(gold),
        "laya": {name: getattr(laya.metrics, name) for name in _LAYA_FIELDS},
        "teacher": {
            "decider": teacher_name,
            "accuracy": teacher_accuracy,
            "ece": teacher.metrics.ece if has_teacher else None,
        },
        "system": {"accuracy": _system_accuracy(gold, laya, teacher, q)},
        "criteria": criteria,
        "passed": passed,
        # bool(passed): no criterion at all never proposes (all([]) would be True)
        "accepted_proposed": bool(passed)
        and all(passed.values())
        and not laya.metrics.uncalibrated,
    }


def _record_gauges(qid: str, laya: QuestionMetrics, teacher: _Scored, teacher_name: str) -> None:
    """``herness_enrich_gold_metric_ratio`` per (question, metric, decider) (impl 03 §8.2)."""
    values = [(_LAYA, name, getattr(laya, name)) for name in _GAUGE_FIELDS]
    if teacher.metrics.n > 0:
        values += [
            (teacher_name, name, getattr(teacher.metrics, name)) for name in ("accuracy", "ece")
        ]
    for decider, metric, value in values:
        if value is not None:
            labels = {"question": qid, "metric": metric, "decider": decider}
            record_gauge(_GAUGE, float(value), component="enrich", labels=labels)


def _write_json(path_dir: EnrichPaths, version: str, doc: Mapping[str, object]) -> None:
    text = canonical_json(dict(doc)) + "\n"
    target = path_dir.laya_dir(version) / _EVAL_FILE
    replace_atomic(target, lambda tmp: tmp.write_text(text, encoding="utf-8"), kind="eval file")


def evaluate_candidate(  # noqa: PLR0913 - U03-129 keyword-only signature (+ blocked, see report)
    *,
    version: str,
    qs: QuestionSet,
    cfg: DecisionsConfig,
    store: LabelStore,
    cache: DecisionCache,
    calibration: CalibrationStore,
    teacher: tuple[str, str],
    paths: EnrichPaths,
    now: datetime,
    blocked: Collection[str] = (),
) -> dict[str, object]:
    """Gate a Laya candidate on frozen gold; write ``eval.json`` and calibration (U03-129).

    Questions without frozen gold at the current fingerprint, or in ``blocked`` (blocked from
    training, design 03 §5.8 step 3), are absent from ``questions``. Returns the document.
    """
    all_gold, gold_by_qid = _frozen_gold(store, qs)
    laya_cache = _cache_rows(cache, (_LAYA, version), qs)
    teacher_cache = _cache_rows(cache, teacher, qs)
    entries: dict[str, dict[str, object]] = {}
    metrics: dict[str, QuestionMetrics] = {}
    laya_cal: dict[str, CalibrationResult] = {}
    teacher_cal: dict[str, CalibrationResult] = {}
    for q in qs.questions:
        gold = gold_by_qid.get(q.id)
        if gold is None or q.id in blocked:
            continue
        dists = [d for c in (laya_cache, teacher_cache) for (qid, _), d in c.items() if qid == q.id]
        space = _label_space(q, gold, dists)
        laya = _score(q, gold, laya_cache, space)
        scored_teacher = _score(q, gold, teacher_cache, space)
        entries[q.id] = _question_entry(q, cfg, gold, laya, scored_teacher, teacher[0])
        metrics[q.id], laya_cal[q.id] = laya.metrics, laya.cal
        if scored_teacher.metrics.n > 0:
            teacher_cal[q.id] = scored_teacher.cal
        _record_gauges(q.id, laya.metrics, scored_teacher, teacher[0])
    gold_dir = paths.labels_dir(qs.version, "gold")
    doc: dict[str, object] = {
        "version": version,
        "question_set_version": qs.version,
        "gold_path": gold_dir.relative_to(paths.data_root.parent).as_posix() + "/",
        "gold_sha256": gold_digest(all_gold),  # whole gold directory (F11-09, U03-138)
        "evaluated_at": clock.format_utc(now),
        "questions": entries,
        "macro_metric": macro_metric(metrics, qs),
    }
    _write_json(paths, version, doc)
    calibration.save(_LAYA, version, qs.version, laya_cal)
    if teacher_cal:
        calibration.save(teacher[0], teacher[1], qs.version, teacher_cal)
    proposed = sorted(qid for qid, entry in entries.items() if entry["accepted_proposed"])
    _log.info(
        "enrich.distill.candidate_evaluated",
        version=version,
        accepted_proposed=proposed,
        macro_metric=doc["macro_metric"],
    )
    return doc
