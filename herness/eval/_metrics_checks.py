"""Design 11 §5.3.6 threshold rows of `check_thresholds` (U11-65).

Private sibling of `herness.eval.metrics`, split off for its 380-line budget; `ThresholdResult`
is re-exported there. One function per table row; a missing baseline skips the
baseline-relative part of a row with `no baseline` in `detail`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from herness.eval.metrics import EvalMetrics
    from herness.eval.results import QuestionResult
    from herness.eval.settings import Thresholds

__all__ = [
    "ThresholdResult", "correctness_check", "cost_check", "latency_check", "planted_check",
    "suite_error_check", "tool_check", "unsupported_check",
]  # fmt: skip

PLANTED_IDS: Final = ("G03", "G06", "F01", "O01")
_CHAT_P95_LOCAL_STANDARD_S: Final = 30.0
_REVIEW_STANDARD_FULL_S: Final = 5_400.0
_NO_BASELINE: Final = "no baseline"
_SUITE_CHANGED: Final = "suite changed"


class ThresholdResult(BaseModel):
    """One design §5.3.6 regression check (U11-65)."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    check: str
    passed: bool
    actual: float | bool | None
    limit: float | None
    detail: str


def _row(
    check: str,
    fails: Sequence[str],
    notes: Sequence[str],
    actual: float | bool | None,
    limit: float | None,
) -> ThresholdResult:
    detail = "; ".join([*fails, *notes]) or "ok"
    return ThresholdResult(check=check, passed=not fails, actual=actual, limit=limit, detail=detail)


def unsupported_check(m: EvalMetrics, settings: Thresholds) -> ThresholdResult:
    rate, limit = m.unsupported_number_rate, settings.unsupported_number_rate_max
    fails = [f"rate {rate:.4f} above {limit:.4f}"] if rate > limit else []
    return _row("unsupported_numbers", fails, [], rate, limit)


def correctness_check(
    m: EvalMetrics, profile: str, depth: str, baseline: EvalMetrics | None, settings: Thresholds
) -> ThresholdResult:
    floor = settings.correctness_floor.get(f"{profile}-{depth}" if profile == "local" else profile)
    actual = m.answer_correctness
    if actual is None:
        changed = [_SUITE_CHANGED] if baseline is not None and baseline.suite_changed else []
        return _row("correctness", [], ["no graded questions", *changed], None, floor)
    fails = (
        [f"{actual:.3f} below floor {floor:.2f}"] if floor is not None and actual < floor else []
    )
    notes: list[str] = []
    if baseline is None:
        notes.append(_NO_BASELINE)
    elif baseline.suite_changed:  # TH11-03: a baseline of another suite is not compared
        notes.append(_SUITE_CHANGED)
    elif baseline.answer_correctness is None:
        notes.append(f"{_NO_BASELINE} correctness")
    else:
        drop_pp = round((baseline.answer_correctness - actual) * 100, 6)
        if drop_pp > settings.correctness_drop_max_pp:
            fails.append(f"dropped {drop_pp:.1f} pp vs baseline")
    return _row("correctness", fails, notes, actual, floor)


def planted_check(
    m: EvalMetrics, depth: str, status: Mapping[str, str], dataset_kind: str
) -> ThresholdResult:
    """Synthetic only: a planted question in `status` (id → aggregated status) that did not
    pass, or T5 not caught at standard or deep depth; planted ids not run are named only."""
    if dataset_kind != "synthetic":
        return _row("planted_truth", [], ["not synthetic"], None, None)
    missed = [qid for qid in PLANTED_IDS if qid in status and status[qid] != "passed"]
    not_run = [qid for qid in PLANTED_IDS if qid not in status]
    fails = [f"not passed: {', '.join(missed)}"] if missed else []
    if depth in {"standard", "deep"} and m.skeptic_catch is False:
        fails.append("T5 confounder not caught")
    notes = [f"not run: {', '.join(not_run)}"] if not_run else []
    return _row("planted_truth", fails, notes, not fails, None)


def tool_check(m: EvalMetrics, profile: str, settings: Thresholds) -> ThresholdResult:
    floor = settings.tool_success_min.get("local" if profile in {"local", "synth"} else profile)
    actual = m.tool_call_success_rate
    if actual is None or floor is None:
        return _row(
            "tool_calls", [], ["no tool calls" if actual is None else "no minimum"], actual, floor
        )
    fails = [f"{actual:.3f} below {floor:.2f}"] if actual < floor else []
    return _row("tool_calls", fails, [], actual, floor)


def latency_check(
    m: EvalMetrics, run: tuple[str, str, str], baseline: EvalMetrics | None, settings: Thresholds
) -> ThresholdResult:
    """Chat p95 vs ratio x baseline and the 30 s local-standard cap; any standard review run
    (the slowest, `review_latency_max_s`) over 5,400 s, only on a full-scale dataset."""
    profile, depth, scale = run
    p95 = m.chat_latency_p95_s
    fails: list[str] = []
    notes: list[str] = []
    limits: list[float] = []
    if baseline is None:
        notes.append(_NO_BASELINE)
    elif baseline.chat_latency_p95_s is not None:
        limits.append(settings.latency_p95_ratio_max * baseline.chat_latency_p95_s)
    if profile == "local" and depth == "standard":
        limits.append(_CHAT_P95_LOCAL_STANDARD_S)
    limit = min(limits) if limits else None
    if p95 is not None and limit is not None and p95 > limit:
        fails.append(f"chat p95 {p95:.1f} s above {limit:.1f} s")
    review = m.review_latency_max_s
    if (
        depth == "standard"
        and scale == "full"
        and review is not None
        and review > _REVIEW_STANDARD_FULL_S
    ):
        fails.append(f"review {review:.0f} s above {_REVIEW_STANDARD_FULL_S:.0f} s")
    return _row("latency", fails, notes, p95, limit)


def cost_check(
    m: EvalMetrics, profile: str, baseline: EvalMetrics | None, settings: Thresholds
) -> ThresholdResult:
    actual = float(m.cost_usd)
    if profile != "premium":
        return _row("cost", [], ["not premium"], actual, None)
    if baseline is None:
        return _row("cost", [], [_NO_BASELINE], actual, None)
    limit = Decimal(str(settings.cost_ratio_max)) * baseline.cost_usd
    fails = [f"cost {m.cost_usd} above {limit}"] if m.cost_usd > limit else []
    return _row("cost", fails, [], actual, float(limit))


def suite_error_check(results: Sequence[QuestionResult]) -> ThresholdResult:
    n = sum(1 for r in results if r.status == "suite_error")
    return _row("suite_errors", [f"{n} suite errors"] if n else [], [], float(n), 0.0)
