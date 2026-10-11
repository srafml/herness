"""Metrics of one decider on one question for the classifier eval (U11-67; T11-35).

Private sibling of `herness.eval.classifier` (§2 module map), which re-exports
`DeciderMetrics` and `ReliabilityBin`. Probabilistic deciders (cache distributions) go
through impl 03's public `question_metrics` (2-fold cross-fitted temperature on `gold.fold`,
ECE over 15 equal-mass bins, coverage and accuracy at the question threshold on calibrated
probabilities), then the calibrated matrix from `apply_temperature` gives the confusion
matrix, the reliability bins and the bootstrap CIs. Hard answers (human labels, the stored
`enrich.decision` answers of a synthetic build) get the same table without a temperature;
their ECE uses the stored top-label probability through `ece`. Macro-F1 is
`sklearn.metrics.f1_score(average="macro")` for choice and bool. CIs are bootstrap
percentile 95 % intervals over `resamples` draws from the caller's `numpy` generator.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Final, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict
from sklearn.metrics import f1_score  # type: ignore[import-untyped]  # sklearn ships no types

from herness.core.types import Question, QuestionType
from herness.enrich.calibrate import apply_temperature, ece
from herness.enrich.evaluate import question_metrics

__all__ = ["DeciderMetrics", "ReliabilityBin", "hard_metrics", "label_space", "matrix",
           "not_scored", "scored_metrics"]  # fmt: skip

N_BINS: Final = 15
_FIXED: Final[dict[str, tuple[str, ...]]] = {
    "bool": ("true", "false"),  # impl 03: column 0 is p(true)
    "score": ("0", "1", "2", "3"),  # column index == score value
}
_CI_LOW, _CI_HIGH = 2.5, 97.5

type DeciderStatus = Literal["ok", "absent", "insufficient"]
type Distribution = Mapping[str, float]
type _MetricFn = Callable[[np.ndarray, np.ndarray, np.ndarray], float | None]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ReliabilityBin(_Model):
    """One of the 15 equal-mass bins of top-label confidence: rows, mean confidence and
    accuracy."""

    n: int
    confidence: float
    accuracy: float


class DeciderMetrics(_Model):
    """Metrics of one decider (or the synthetic decisions) on one question (U11-67).

    `status` is `absent` without rows and `insufficient` for human labels with fewer than 30
    gold overlaps; the metric fields are then None and the lists empty. `ci` maps a metric to
    its bootstrap 95 % interval; `confusion[gold][predicted]` is over `labels`."""

    status: DeciderStatus
    decider_version: str | None = None
    n: int = 0
    accuracy: float | None = None
    macro_f1: float | None = None
    mae: float | None = None
    within_one: float | None = None
    ece: float | None = None
    temperature: float | None = None
    uncalibrated: bool | None = None
    coverage_at_threshold: float | None = None
    accuracy_at_threshold: float | None = None
    ci: dict[str, tuple[float, float]] = {}
    labels: list[str] = []
    confusion: list[list[int]] = []
    reliability: list[ReliabilityBin] = []


def not_scored(status: DeciderStatus, *, version: str | None = None, n: int = 0) -> DeciderMetrics:
    """An `absent` or `insufficient` entry."""
    return DeciderMetrics(status=status, decider_version=version, n=n)


def label_space(q: Question, answers: Sequence[str]) -> tuple[str, ...]:
    """Answer columns as impl 03 builds them: fixed for bool and score; for choice the static
    options, then any other seen answer sorted (carry-over T03-36: no public helper)."""
    if q.type in _FIXED:
        return _FIXED[q.type]
    base = tuple(q.options or ())
    return base + tuple(sorted(set(answers) - set(base)))


def matrix(dists: Sequence[Distribution], space: Sequence[str]) -> np.ndarray:
    """Rows of `dists` over `space`, normalized; an all-zero row becomes uniform (as impl 03)."""
    probs = np.array([[d.get(a, 0.0) for a in space] for d in dists], dtype=np.float64)
    probs = probs.reshape(len(dists), len(space))
    sums = probs.sum(axis=1, keepdims=True)
    out: np.ndarray = np.where(sums > 0, probs / np.where(sums > 0, sums, 1.0), 1.0 / len(space))
    return out


def _macro_f1(qtype: QuestionType) -> _MetricFn | None:
    if qtype == "score":
        return None
    return lambda pred, labels, _conf: float(
        f1_score(labels, pred, average="macro", zero_division=0.0)
    )


def _accuracy(pred: np.ndarray, labels: np.ndarray, _conf: np.ndarray) -> float:
    return float(np.mean(pred == labels))


def _mae(pred: np.ndarray, labels: np.ndarray, _conf: np.ndarray) -> float:
    return float(np.abs(pred - labels).mean())


def _within_one(pred: np.ndarray, labels: np.ndarray, _conf: np.ndarray) -> float:
    return float(np.mean(np.abs(pred - labels) <= 1))


def _ece(pred: np.ndarray, labels: np.ndarray, conf: np.ndarray) -> float:
    """Top-label ECE of (confidence, correct) through impl 03's `ece`: column 0 holds the
    confidence, so the argmax is column 0 and label 0 means correct."""
    top = np.column_stack([conf, np.zeros_like(conf)])
    return ece(top, np.where(pred == labels, 0, 1), N_BINS)


def _metric_fns(qtype: QuestionType, *, with_conf: bool) -> dict[str, _MetricFn]:
    fns: dict[str, _MetricFn | None] = {"accuracy": _accuracy, "macro_f1": _macro_f1(qtype)}
    if qtype == "score":
        fns |= {"mae": _mae, "within_one": _within_one}
    if with_conf:
        fns["ece"] = _ece
    return {name: fn for name, fn in fns.items() if fn is not None}


def _bootstrap(
    fns: Mapping[str, _MetricFn], arrays: tuple[np.ndarray, np.ndarray, np.ndarray],
    rng: np.random.Generator, resamples: int,
) -> dict[str, tuple[float, float]]:  # fmt: skip
    """Percentile 95 % intervals over `resamples` row resamples (with replacement)."""
    pred, labels, conf = arrays
    draws = rng.integers(0, len(labels), size=(resamples, len(labels)))
    values: dict[str, list[float]] = {name: [] for name in fns}
    for idx in draws:
        for name, fn in fns.items():
            value = fn(pred[idx], labels[idx], conf[idx])
            if value is not None:
                values[name].append(value)
    return {
        name: (float(np.percentile(v, _CI_LOW)), float(np.percentile(v, _CI_HIGH)))
        for name, v in values.items()
        if v
    }


def _reliability(pred: np.ndarray, labels: np.ndarray, conf: np.ndarray) -> list[ReliabilityBin]:
    """The 15 equal-mass bins of `ece` as data (fewer when there are fewer rows)."""
    correct = (pred == labels).astype(np.float64)
    order = np.argsort(conf, kind="stable")
    return [
        ReliabilityBin(
            n=len(chunk),
            confidence=float(conf[chunk].mean()),
            accuracy=float(correct[chunk].mean()),
        )
        for chunk in np.array_split(order, min(N_BINS, len(order)))
    ]


def _confusion(pred: np.ndarray, labels: np.ndarray, k: int) -> list[list[int]]:
    out = np.zeros((k, k), dtype=np.int64)
    np.add.at(out, (labels, pred), 1)
    return [[int(x) for x in row] for row in out]


def _table(
    q: Question, space: Sequence[str], arrays: tuple[np.ndarray, np.ndarray, np.ndarray],
    rng: np.random.Generator, resamples: int, *, with_conf: bool,
) -> dict[str, object]:  # fmt: skip
    """The fields every scored entry shares: point metrics, CIs, confusion, reliability."""
    pred, labels, conf = arrays
    fns = _metric_fns(q.type, with_conf=with_conf)
    out: dict[str, object] = {name: fn(pred, labels, conf) for name, fn in fns.items()}
    out |= {
        "n": len(labels),
        "ci": _bootstrap(fns, arrays, rng, resamples),
        "labels": list(space),
        "confusion": _confusion(pred, labels, len(space)),
    }
    if with_conf:
        covered = conf >= q.threshold
        out |= {
            "reliability": _reliability(pred, labels, conf),
            "coverage_at_threshold": float(np.mean(covered)),
            "accuracy_at_threshold": float(np.mean(pred[covered] == labels[covered]))
            if covered.any()
            else None,
        }
    return out


def scored_metrics(  # noqa: PLR0913 - the gold arrays plus the decider and bootstrap context
    q: Question, space: Sequence[str], probs: np.ndarray, labels: np.ndarray,
    folds: np.ndarray, *, version: str | None, rng: np.random.Generator, resamples: int,
) -> DeciderMetrics:  # fmt: skip
    """A probabilistic decider on gold: impl 03's `question_metrics` (cross-fitted T, ECE,
    coverage) plus macro-F1, confusion, reliability and CIs on the calibrated matrix."""
    if len(labels) == 0:
        return not_scored("absent", version=version)
    qm = question_metrics(probs, labels, folds, qtype=q.type, threshold=q.threshold)
    calibrated = apply_temperature(probs, qm.temperature, q.type)
    arrays = (calibrated.argmax(axis=1), np.asarray(labels, dtype=np.int64), calibrated.max(1))
    table = _table(q, space, arrays, rng, resamples, with_conf=True)
    table |= {  # impl 03's own values where it computes them
        "accuracy": qm.accuracy, "ece": qm.ece, "mae": qm.mae, "within_one": qm.within_one,
        "coverage_at_threshold": qm.coverage_at_threshold,
        "accuracy_at_threshold": qm.accuracy_at_threshold,
    }  # fmt: skip
    return DeciderMetrics.model_validate(
        table | {"status": "ok", "decider_version": version, "temperature": qm.temperature,
                 "uncalibrated": qm.uncalibrated},
        strict=False,
    )  # fmt: skip


def hard_metrics(  # noqa: PLR0913 - the answers plus the decider and bootstrap context
    q: Question, space: Sequence[str], pred: np.ndarray, labels: np.ndarray,
    conf: np.ndarray | None, *, version: str | None, rng: np.random.Generator, resamples: int,
) -> DeciderMetrics:  # fmt: skip
    """Hard answers against gold or truth; `conf` (stored top-label probabilities) adds ECE,
    reliability bins and coverage at the question threshold."""
    if len(labels) == 0:
        return not_scored("absent", version=version)
    with_conf = conf is not None
    top = np.zeros(len(labels)) if conf is None else np.asarray(conf, dtype=np.float64)
    arrays = (np.asarray(pred, dtype=np.int64), np.asarray(labels, dtype=np.int64), top)
    table = _table(q, space, arrays, rng, resamples, with_conf=with_conf)
    return DeciderMetrics.model_validate(
        table | {"status": "ok", "decider_version": version}, strict=False
    )
