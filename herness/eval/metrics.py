"""Aggregate metrics, skeptic catch and threshold checks of a golden eval run (U11-64, U11-65).

Repeats of one question aggregate by strict majority (design 11 §5.3.1). Trace metrics read
the JSONL events of `data/traces/<run_id>.jsonl` as written by `herness.harness.tracing`
(`type` plus flat fields: `verifier_verdict.passed`, `tool_call.ok`, `retry`, `fallback`,
`repair`, `guard_stop`). `unsupported_number_rate` comes from the `count_unsupported` counts
stored on each result, never from Verifier events (TH11-12). Threshold rows follow design
§5.3.6 in table order; the per-row checks and `ThresholdResult` live in the private sibling
`herness.eval._metrics_checks` (380-line budget) and are re-exported here.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, ConfigDict

from herness.eval._metrics_checks import (
    ThresholdResult,
    correctness_check,
    cost_check,
    latency_check,
    planted_check,
    suite_error_check,
    tool_check,
    unsupported_check,
)
from herness.eval.results import QuestionResult, ResultStatus

if TYPE_CHECKING:
    from herness.core.types import Finding
    from herness.eval.golden import EvalQuestion
    from herness.eval.invoke import PipelineOutput
    from herness.eval.settings import Thresholds
    from herness.eval.truth import TruthManifest
    from herness.store.ops.runs import RunRow

__all__ = [
    "EvalMetrics",
    "ThresholdResult",
    "aggregate_metrics",
    "check_thresholds",
    "skeptic_catch",
]

DEGRADED_RE: Final = re.compile(r"(?i)\b(degrad\w*|worse|deteriorat\w*|declin\w*|slower)\b")
_UNGRADED: Final = frozenset({"skipped", "suite_error"})
_PER_RUN: Final = {
    "retry": "retries_per_run",
    "fallback": "fallbacks_per_run",
    "repair": "repairs_per_run",
    "guard_stop": "guard_stops_per_run",
}


class EvalMetrics(BaseModel):
    """The design §5.3.4 metrics of one eval run (U11-64); rates are None without data."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    question_status: dict[str, ResultStatus] = {}
    answer_correctness: float | None = None
    correctness_by_pipeline: dict[str, float] = {}
    correctness_by_tag: dict[str, float] = {}
    unsupported_number_rate: float = 0.0
    verifier_rejection_rate: float | None = None
    tool_call_success_rate: float | None = None
    retries_per_run: float = 0.0
    fallbacks_per_run: float = 0.0
    repairs_per_run: float = 0.0
    guard_stops_per_run: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: Decimal = Decimal(0)
    chat_latency_p50_s: float | None = None
    chat_latency_p95_s: float | None = None
    review_latency_p50_s: float | None = None
    review_latency_p95_s: float | None = None
    judge_mean: float | None = None
    skeptic_catch: bool | None = None
    notes: list[str] = []


# --- aggregate_metrics (U11-64) -------------------------------------------------------------


def aggregate_metrics(
    results: Sequence[QuestionResult],
    traces: Mapping[str, Sequence[Mapping[str, object]]],
    runs: Mapping[str, RunRow],
    *,
    skeptic: bool | None,
    questions: Mapping[str, EvalQuestion] | None = None,
) -> EvalMetrics:
    """Aggregate per-question results, traces and run rows into `EvalMetrics`.

    `questions` (id → suite question) supplies pipeline and tags; without it the per-pipeline
    and per-tag correctness stay empty and a result with no draft counts as a chat question.
    """
    known = questions or {}
    status = question_statuses(results)
    by_pipeline, by_tag = _grouped(status, known)
    trace_values, notes = _trace_metrics(results, traces)
    tokens_in, tokens_out, cost = _usage(runs.values())
    chat, review = _latencies(results, known)
    return EvalMetrics(
        question_status=status,
        answer_correctness=_rate(status.values()),
        correctness_by_pipeline=by_pipeline,
        correctness_by_tag=by_tag,
        unsupported_number_rate=_unsupported_rate(results),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost,
        chat_latency_p50_s=_nearest_rank(chat, 50),
        chat_latency_p95_s=_nearest_rank(chat, 95),
        review_latency_p50_s=_nearest_rank(review, 50),
        review_latency_p95_s=_nearest_rank(review, 95),
        judge_mean=_judge_mean(results),
        skeptic_catch=skeptic,
        notes=notes,
    ).model_copy(update=trace_values)  # trace rates and per-run counts, all float | None


def question_statuses(results: Iterable[QuestionResult]) -> dict[str, ResultStatus]:
    """Per question id, the status of its repeats by strict majority (first-seen order)."""
    by_id: dict[str, list[ResultStatus]] = defaultdict(list)
    for r in results:
        by_id[r.id].append(r.status)
    return {qid: _majority(statuses) for qid, statuses in by_id.items()}


def _majority(statuses: Sequence[ResultStatus]) -> ResultStatus:
    """`suite_error` wins (a suite defect); all skipped → skipped; else passed by strict
    majority of the repeats that ran, `error` when every one errored, otherwise failed."""
    if "suite_error" in statuses:
        return "suite_error"
    ran = [s for s in statuses if s != "skipped"]
    if not ran:
        return "skipped"
    if 2 * ran.count("passed") > len(ran):
        return "passed"
    return "error" if all(s == "error" for s in ran) else "failed"


def _rate(statuses: Iterable[ResultStatus]) -> float | None:
    """passed / graded, graded excluding `skipped` and `suite_error`; None when none graded."""
    graded = [s for s in statuses if s not in _UNGRADED]
    return graded.count("passed") / len(graded) if graded else None


def _grouped(
    status: Mapping[str, ResultStatus], questions: Mapping[str, EvalQuestion]
) -> tuple[dict[str, float], dict[str, float]]:
    by_pipeline: dict[str, list[ResultStatus]] = defaultdict(list)
    by_tag: dict[str, list[ResultStatus]] = defaultdict(list)
    for qid, s in status.items():
        question = questions.get(qid)
        if question is None:
            continue
        by_pipeline[question.pipeline].append(s)
        for tag in question.tags:
            by_tag[tag].append(s)
    return _rates(by_pipeline), _rates(by_tag)


def _rates(groups: Mapping[str, list[ResultStatus]]) -> dict[str, float]:
    return {key: rate for key, values in groups.items() if (rate := _rate(values)) is not None}


def _unsupported_rate(results: Iterable[QuestionResult]) -> float:
    """Σ unsupported / Σ total over every result with a report (0 when nothing counted)."""
    reports = [r.unsupported for r in results if r.unsupported is not None]
    total = sum(report.total for report in reports)
    return sum(report.unsupported for report in reports) / total if total else 0.0


def _trace_metrics(
    results: Iterable[QuestionResult],
    traces: Mapping[str, Sequence[Mapping[str, object]]],
) -> tuple[dict[str, float | None], list[str]]:
    """Rates and per-run counts over the traces of the distinct pipeline runs; a run without
    a trace is left out of both and named in the notes (flow F11-07 step 10)."""
    run_ids = sorted({r.pipeline_run_id for r in results if r.pipeline_run_id is not None})
    present = [rid for rid in run_ids if rid in traces]
    notes = [f"trace missing: {rid}" for rid in run_ids if rid not in traces]
    events = [event for rid in present for event in traces[rid]]
    counts = Counter(event.get("type") for event in events)
    values: dict[str, float | None] = {
        name: counts[kind] / len(present) if present else 0.0 for kind, name in _PER_RUN.items()
    }
    values["verifier_rejection_rate"] = _share(events, "verifier_verdict", "passed", value=False)
    values["tool_call_success_rate"] = _share(events, "tool_call", "ok", value=True)
    return values, notes


def _share(
    events: Sequence[Mapping[str, object]], kind: str, field: str, *, value: bool
) -> float | None:
    hits = [event.get(field) is value for event in events if event.get("type") == kind]
    return sum(hits) / len(hits) if hits else None


def _usage(runs: Iterable[RunRow]) -> tuple[int, int, Decimal]:
    """Token totals of every ledger snapshot in `run.token_usage`, and Σ `run.cost_usd`."""
    tokens_in = tokens_out = 0
    cost = Decimal(0)
    for run in runs:
        cost += run.cost_usd
        for ledger in run.token_usage.values():
            if isinstance(ledger, Mapping):
                tokens_in += _count(ledger.get("tokens_in"))
                tokens_out += _count(ledger.get("tokens_out"))
    return tokens_in, tokens_out, cost


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _latencies(
    results: Iterable[QuestionResult], questions: Mapping[str, EvalQuestion]
) -> tuple[list[float], list[float]]:
    """Wall time per chat question result, and once per review run (shared runs)."""
    chat: list[float] = []
    review: dict[str, float] = {}
    for r in results:
        if r.status in _UNGRADED:
            continue
        question = questions.get(r.id)
        is_chat = question.pipeline == "chat" if question is not None else r.draft_mode is None
        if is_chat:
            chat.append(r.latency_s)
        elif r.pipeline_run_id is not None:
            review.setdefault(r.pipeline_run_id, r.latency_s)
    return chat, list(review.values())


def _nearest_rank(values: Sequence[float], pct: int) -> float | None:
    """Nearest-rank percentile: the value at rank ⌈pct/100 x n⌉ of the sorted values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = -(-pct * len(ordered) // 100)
    return ordered[max(rank, 1) - 1]


def _judge_mean(results: Iterable[QuestionResult]) -> float | None:
    """Mean of the rubric means of every rubric check that was not skipped."""
    scores = [
        float(mean)
        for r in results
        for check in r.checks
        if check.check == "rubric" and check.status != "skipped"
        if isinstance(mean := check.detail.get("mean"), int | float)
    ]
    return sum(scores) / len(scores) if scores else None


# --- skeptic_catch (U11-64) -----------------------------------------------------------------


def skeptic_catch(
    truth: TruthManifest | None, org_outputs: Sequence[PipelineOutput]
) -> bool | None:
    """True when every org run kept the T5 confounder out (design 11 §5.1.5); None on real
    data (no truth) or without an org run that completed (outputs with `error` are left out)."""
    runs = [out for out in org_outputs if out.error is None]
    if truth is None or not runs:
        return None
    team = truth.plants.T5_confounder.team_id
    return all(_caught(out, team) for out in runs)


def _caught(out: PipelineOutput, team: str) -> bool:
    """(a) no verified T5 degraded claim, (b) T5 not in the top 5, (c) every rejected or
    revised T5 degraded claim carries a seasonality concern or fail."""
    t5 = [f for f in out.findings if f.entity_id == team and DEGRADED_RE.search(f.claim)]
    fooled = any(f.status == "verified" for f in t5)
    unchallenged = any(f.status in {"rejected", "revised"} and not _seasonal(f) for f in t5)
    return not fooled and team not in out.ranked_ids[:5] and not unchallenged


def _seasonal(finding: Finding) -> bool:
    return any(
        check.check == "seasonality" and check.result in {"concern", "fail"}
        for challenge in finding.challenge
        for check in challenge.checks
    )


# --- check_thresholds (U11-65) --------------------------------------------------------------


def check_thresholds(  # noqa: PLR0913 - signature fixed by U11-65 (keyword-only)
    m: EvalMetrics,
    *,
    profile: str,
    depth: str,
    baseline: EvalMetrics | None,
    results: Sequence[QuestionResult],
    dataset_kind: str,
    dataset_scale: str,
    settings: Thresholds,
) -> list[ThresholdResult]:
    """One result per design §5.3.6 row; a missing baseline skips the baseline-relative
    parts with `no baseline` in `detail`."""
    return [
        unsupported_check(m, settings),
        correctness_check(m, profile, depth, baseline, settings),
        planted_check(m, depth, question_statuses(results), dataset_kind),
        tool_check(m, profile, settings),
        latency_check(m, (profile, depth, dataset_scale), baseline, settings),
        cost_check(m, profile, baseline, settings),
        suite_error_check(results),
    ]
