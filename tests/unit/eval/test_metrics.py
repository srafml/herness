"""Tests for herness.eval.metrics (U11-64, U11-65): UT11-96..UT11-99, UT11-102..UT11-106."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from tests.unit.eval._golden_fixtures import truth

from herness.core.types import SKEPTIC_CHECKS, Finding
from herness.eval.golden import EvalQuestion
from herness.eval.grading import GradeResult, UnsupportedReport
from herness.eval.invoke import PipelineOutput
from herness.eval.metrics import (
    EvalMetrics,
    ThresholdResult,
    aggregate_metrics,
    check_thresholds,
    skeptic_catch,
)
from herness.eval.results import QuestionResult
from herness.eval.settings import Thresholds
from herness.store.ops.runs import RunRow

pytestmark = pytest.mark.unit

RUN_A = "run_" + "0" * 25 + "A"
RUN_B = "run_" + "0" * 25 + "B"
RUN_C = "run_" + "0" * 25 + "C"
_TASK = "task_" + "0" * 25 + "X"
_Q = "q_0123456789abcdef"
_T0 = datetime(2026, 10, 1, tzinfo=UTC)
T5 = truth().plants.T5_confounder.team_id
OTHER = "servicenow:sys_user_group:9f1"


def _unsupported(total: int, bad: int) -> UnsupportedReport:
    return UnsupportedReport(
        total=total, unsupported=bad, stray_numerals=[], orphan_markers=[], stale_refs=[],
        rate=bad / total if total else 0.0,
    )  # fmt: skip


def _result(qid: str = "G01", repeat: int = 1, **overrides: Any) -> QuestionResult:
    values: dict[str, Any] = {
        "id": qid, "repeat": repeat, "status": "passed", "checks": [], "reference": {},
        "final_text": "", "numbers": [], "unsupported": _unsupported(2, 0),
        "tokens_in": 0, "tokens_out": 0, "cost_usd": "0", "latency_s": 1.0,
        "retries": 0, "fallbacks": 0, "repairs": 0, "pipeline_run_id": None,
        "draft_mode": None, "trace_path": None, "error": None,
    }  # fmt: skip
    values.update(overrides)
    return QuestionResult.model_validate(values)


def _question(qid: str, pipeline: str, tags: Sequence[str] = ()) -> EvalQuestion:
    return EvalQuestion.model_validate({
        "id": qid, "pipeline": pipeline, "question": "q?", "tags": list(tags),
        "expected": {"rubric": {"criteria": ["clarity"], "min_score": 3}},
    })  # fmt: skip


def _run(run_id: str, *, tokens: tuple[int, int], cost: str, kind: str = "chat") -> RunRow:
    usage: dict[str, object] = {
        "analysis": {"tokens_in": tokens[0], "tokens_out": tokens[1]},
        "by_role": {"analyst": {"calls": 3}},
    }
    return RunRow(
        run_id=run_id, kind=kind, depth="standard", profile="local", build_id=None,
        status="done", started_at=_T0, finished_at=None, token_usage=usage,
        cost_usd=Decimal(cost), config_hash="h", meta={},
    )  # fmt: skip


def _metrics(**overrides: Any) -> EvalMetrics:
    values: dict[str, Any] = {
        "question_status": {}, "answer_correctness": 0.95, "unsupported_number_rate": 0.0,
        "tool_call_success_rate": 0.99, "chat_latency_p95_s": 5.0, "cost_usd": Decimal(10),
    }  # fmt: skip
    values.update(overrides)
    return EvalMetrics.model_validate(values)


def _settings() -> Thresholds:
    return Thresholds(
        unsupported_number_rate_max=0.0, correctness_drop_max_pp=3.0,
        correctness_floor={"local-standard": 0.80, "local-deep": 0.85, "hybrid": 0.88,
                           "premium": 0.90},
        tool_success_min={"local": 0.90, "hybrid": 0.95, "premium": 0.95},
        latency_p95_ratio_max=1.25, cost_ratio_max=1.20, bench_regression_max=0.20,
    )  # fmt: skip


def _check(
    m: EvalMetrics, *, profile: str = "local", depth: str = "standard", **kw: Any
) -> dict[str, ThresholdResult]:
    args: dict[str, Any] = {
        "baseline": None,
        "results": [],
        "dataset_kind": "synthetic",
        "dataset_scale": "small",
    }
    args.update(kw)
    rows = check_thresholds(m, profile=profile, depth=depth, settings=_settings(), **args)
    return {row.check: row for row in rows}


# --- U11-64 aggregate_metrics -------------------------------------------------------------


def test_ut11_96_strict_majority_of_repeats() -> None:
    """UT11-96 repeats pass/fail/pass pass the question; fail/fail/pass fail it."""
    results = [
        _result("G01", 1), _result("G01", 2, status="failed"), _result("G01", 3),
        _result("G02", 1, status="failed"), _result("G02", 2, status="failed"),
        _result("G02", 3),
    ]  # fmt: skip
    m = aggregate_metrics(results, {}, {}, skeptic=None, questions={})
    assert m.question_status == {"G01": "passed", "G02": "failed"}
    assert m.answer_correctness == pytest.approx(0.5)


def test_ut11_96_even_split_is_not_a_majority() -> None:
    """UT11-96 a 1-1 split of two repeats is not a strict majority: the question fails."""
    results = [_result("G01", 1), _result("G01", 2, status="error")]
    m = aggregate_metrics(results, {}, {}, skeptic=None, questions={})
    assert m.question_status == {"G01": "failed"}


def test_ut11_96_graded_excludes_skipped_and_suite_error_per_pipeline_and_tag() -> None:
    """UT11-96 graded excludes skipped and suite_error; correctness per pipeline and tag."""
    results = [
        _result("G01"), _result("G02", status="failed"), _result("G03", status="skipped"),
        _result("G04", status="suite_error"), _result("F01"), _result("O01", status="error"),
    ]  # fmt: skip
    questions = {
        "G01": _question("G01", "chat", ["numeric"]),
        "G02": _question("G02", "chat", ["numeric", "planted"]),
        "G03": _question("G03", "chat", ["planted"]),
        "G04": _question("G04", "chat"),
        "F01": _question("F01", "funding", ["planted"]),
        "O01": _question("O01", "org"),
    }
    m = aggregate_metrics(results, {}, {}, skeptic=None, questions=questions)
    assert m.answer_correctness == pytest.approx(2 / 4)
    assert m.correctness_by_pipeline == pytest.approx({"chat": 0.5, "funding": 1.0, "org": 0.0})
    assert m.correctness_by_tag == pytest.approx({"numeric": 0.5, "planted": 0.5})
    assert m.question_status["G03"] == "skipped"
    assert m.question_status["G04"] == "suite_error"


def test_ut11_97_trace_rates_and_per_run_counts() -> None:
    """UT11-97 verifier and tool rates and per-run event counts are exact; tokens and cost
    come from the run rows; unsupported rate sums over results."""
    traces = {
        RUN_A: [
            {"type": "tool_call", "ok": True}, {"type": "tool_call", "ok": True},
            {"type": "tool_call", "ok": False}, {"type": "verifier_verdict", "passed": False},
            {"type": "verifier_verdict", "passed": True}, {"type": "retry"},
            {"type": "repair"}, {"type": "llm_call"},
        ],
        RUN_B: [
            {"type": "tool_call", "ok": True}, {"type": "verifier_verdict", "passed": True},
            {"type": "fallback"}, {"type": "guard_stop", "cause": "budget"}, {"type": "retry"},
        ],
    }  # fmt: skip
    results = [
        _result("G01", pipeline_run_id=RUN_A, unsupported=_unsupported(4, 1)),
        _result("G02", pipeline_run_id=RUN_B, unsupported=_unsupported(6, 0)),
        _result("G02", 2, pipeline_run_id=RUN_B, unsupported=None),
    ]
    runs = {
        RUN_A: _run(RUN_A, tokens=(100, 40), cost="0.50"),
        RUN_B: _run(RUN_B, tokens=(20, 5), cost="0.25"),
    }
    m = aggregate_metrics(results, traces, runs, skeptic=True, questions={})
    assert m.tool_call_success_rate == pytest.approx(3 / 4)
    assert m.verifier_rejection_rate == pytest.approx(1 / 3)
    assert m.retries_per_run == pytest.approx(1.0)
    assert m.fallbacks_per_run == pytest.approx(0.5)
    assert m.repairs_per_run == pytest.approx(0.5)
    assert m.guard_stops_per_run == pytest.approx(0.5)
    assert m.unsupported_number_rate == pytest.approx(1 / 10)
    assert (m.tokens_in, m.tokens_out, m.cost_usd) == (120, 45, Decimal("0.75"))
    assert m.skeptic_catch is True
    assert m.notes == []


def test_ut11_98_nearest_rank_latency_and_judge_mean() -> None:
    """UT11-98 latencies 1..20 give p50 = 10 and p95 = 19 (nearest rank); judge_mean
    excludes skipped rubric scores; review latency is per run."""
    rubric = [
        GradeResult(check="rubric", status="passed", detail={"mean": 4.0}),
        GradeResult(check="rubric", status="skipped", detail={"reason": "no_judge"}),
        GradeResult(check="numeric:n", status="passed", detail={"mean": 1.0}),
    ]
    chat = [
        _result(f"G{i:02d}", latency_s=float(i), checks=rubric if i == 1 else [])
        for i in range(1, 21)
    ]
    failed_rubric = [GradeResult(check="rubric", status="failed", detail={"mean": 3.0})]
    review = [
        _result("F01", latency_s=500.0, pipeline_run_id=RUN_C, draft_mode="full",
                checks=failed_rubric),
        _result("F02", latency_s=500.0, pipeline_run_id=RUN_C, draft_mode="full"),
        _result("O01", latency_s=900.0, pipeline_run_id=RUN_A, draft_mode="findings_only"),
    ]  # fmt: skip
    m = aggregate_metrics([*chat, *review], {RUN_C: [], RUN_A: []}, {}, skeptic=None, questions={})
    assert m.chat_latency_p50_s == pytest.approx(10.0)
    assert m.chat_latency_p95_s == pytest.approx(19.0)
    assert m.review_latency_p50_s == pytest.approx(500.0)
    assert m.review_latency_p95_s == pytest.approx(900.0)
    assert m.judge_mean == pytest.approx(3.5)


def test_ut11_99_skeptic_catch() -> None:
    """UT11-99 a verified T5 degraded claim → False; without it → True; real data → None."""
    clean = PipelineOutput(run_id=RUN_A, findings=[_finding(OTHER, "MTTR got worse")])
    caught = PipelineOutput(
        run_id=RUN_B,
        findings=[_finding(T5, "T5 MTTR is worse", status="rejected", seasonality="concern")],
    )
    fooled = PipelineOutput(run_id=RUN_C, findings=[_finding(T5, "MTTR has degraded badly")])
    assert skeptic_catch(truth(), [clean, caught]) is True
    assert skeptic_catch(truth(), [clean, fooled]) is False
    assert skeptic_catch(None, [clean]) is None
    assert skeptic_catch(truth(), []) is None


def _finding(
    entity: str, claim: str, *, status: str = "verified", seasonality: str | None = None
) -> Finding:
    challenge = []
    if seasonality is not None:
        checks = [
            {"check": c, "result": seasonality if c == "seasonality" else "pass", "note": "",
             "query_ids": [_Q]}
            for c in SKEPTIC_CHECKS
        ]  # fmt: skip
        challenge = [{"finding_id": "fnd_01J9ZQ4Y8M6V3K2N1P0R5T7W9F", "checks": checks,
                      "verdict": "reject"}]  # fmt: skip
    return Finding.model_validate({
        "finding_id": "fnd_01J9ZQ4Y8M6V3K2N1P0R5T7W9F", "run_id": RUN_A, "task_id": _TASK,
        "author_role": "analyst", "claim": claim, "entity_type": "team", "entity_id": entity,
        "numbers": [{"id": "n1", "value": 3, "unit": "count", "query_id": _Q,
                     "column": "n", "row_key": None}],
        "query_ids": [_Q], "confidence": 0.7, "status": status, "challenge": challenge,
        "created_at": _T0,
    })  # fmt: skip


def test_ut11_99_ranked_top5_and_unchallenged_rejection_fail() -> None:
    """UT11-99 T5 in ranked_ids[:5] fails (b); a rejected T5 degraded claim without a
    seasonality concern or fail fails (c); T5 at rank 6 passes."""
    top5 = PipelineOutput(run_id=RUN_A, ranked_ids=[OTHER, T5])
    rank6 = PipelineOutput(run_id=RUN_A, ranked_ids=[OTHER] * 5 + [T5])
    passed_challenge = _finding(T5, "declining", status="revised", seasonality="pass")
    no_challenge = _finding(T5, "slower response", status="rejected")
    unrelated = _finding(T5, "volume rose", status="rejected")
    assert skeptic_catch(truth(), [top5]) is False
    assert skeptic_catch(truth(), [rank6]) is True
    assert skeptic_catch(truth(), [PipelineOutput(findings=[passed_challenge])]) is False
    assert skeptic_catch(truth(), [PipelineOutput(findings=[no_challenge])]) is False
    assert skeptic_catch(truth(), [PipelineOutput(findings=[unrelated])]) is True
    fail_check = _finding(T5, "deteriorating", status="rejected", seasonality="fail")
    assert skeptic_catch(truth(), [PipelineOutput(findings=[fail_check])]) is True


# --- U11-65 check_thresholds ---------------------------------------------------------------


def test_ut11_102_unsupported_numbers_fails() -> None:
    """UT11-102 unsupported_number_rate 0.01 fails the `unsupported_numbers` check."""
    rows = _check(_metrics(unsupported_number_rate=0.01))
    assert rows["unsupported_numbers"].passed is False
    assert rows["unsupported_numbers"].actual == pytest.approx(0.01)
    assert rows["unsupported_numbers"].limit == pytest.approx(0.0)
    assert _check(_metrics())["unsupported_numbers"].passed is True


def test_ut11_102_every_design_row_reported() -> None:
    """UT11-102 one result per design §5.3.6 row, in table order."""
    rows = check_thresholds(
        _metrics(), profile="local", depth="standard", baseline=None, results=[],
        dataset_kind="synthetic", dataset_scale="small", settings=_settings(),
    )  # fmt: skip
    assert [r.check for r in rows] == [
        "unsupported_numbers", "correctness", "planted_truth", "tool_calls", "latency", "cost",
        "suite_errors",
    ]  # fmt: skip


def test_ut11_103_correctness_floor_and_drop() -> None:
    """UT11-103 0.79 local-standard is below the floor; 0.86 vs baseline 0.90 drops 4 pp."""
    floor = _check(_metrics(answer_correctness=0.79))["correctness"]
    assert floor.passed is False
    assert floor.limit == pytest.approx(0.80)
    base = _metrics(answer_correctness=0.90)
    drop = _check(_metrics(answer_correctness=0.86), baseline=base)["correctness"]
    assert drop.passed is False
    assert "4.0 pp" in drop.detail
    ok = _check(_metrics(answer_correctness=0.88), baseline=base)["correctness"]
    assert ok.passed is True


def test_ut11_103_floor_keys_by_profile() -> None:
    """UT11-103 floor key `<profile>-<depth>` for local, `<profile>` for hybrid/premium;
    a missing key has no floor."""
    m = _metrics(answer_correctness=0.87)
    assert _check(m, profile="hybrid", depth="fast")["correctness"].passed is False
    assert _check(m, profile="local", depth="deep")["correctness"].passed is True
    fast = _check(_metrics(answer_correctness=0.1), profile="local", depth="fast")
    assert fast["correctness"].passed is True
    assert fast["correctness"].limit is None


def test_ut11_104_planted_truth_fails() -> None:
    """UT11-104 G06 failed on synthetic fails `planted_truth`; so does skeptic False at
    standard; skeptic False at fast and real datasets do not."""
    results = [_result("G03"), _result("G06", status="failed"), _result("F01")]
    rows = _check(_metrics(skeptic_catch=True), results=results)
    assert rows["planted_truth"].passed is False
    assert "G06" in rows["planted_truth"].detail
    ok = [_result("G03"), _result("G06")]
    assert _check(_metrics(skeptic_catch=True), results=ok)["planted_truth"].passed is True
    assert _check(_metrics(skeptic_catch=False), results=ok)["planted_truth"].passed is False
    deep = _check(_metrics(skeptic_catch=False), depth="deep", results=ok)
    assert deep["planted_truth"].passed is False
    fast = _check(_metrics(skeptic_catch=False), depth="fast", results=ok)
    assert fast["planted_truth"].passed is True
    real = _check(_metrics(skeptic_catch=False), results=results, dataset_kind="real")
    assert real["planted_truth"].passed is True


def test_ut11_105_tool_success_and_chat_latency_fail() -> None:
    """UT11-105 tool success 0.89 local and chat p95 31 s local-standard both fail."""
    rows = _check(_metrics(tool_call_success_rate=0.89, chat_latency_p95_s=31.0))
    assert rows["tool_calls"].passed is False
    assert rows["tool_calls"].limit == pytest.approx(0.90)
    assert rows["latency"].passed is False
    synth = _check(_metrics(tool_call_success_rate=0.89), profile="synth")
    assert synth["tool_calls"].passed is False
    hybrid = _check(_metrics(tool_call_success_rate=0.93), profile="hybrid")
    assert hybrid["tool_calls"].passed is False
    assert _check(_metrics(chat_latency_p95_s=31.0), depth="deep")["latency"].passed is True


def test_ut11_105_latency_vs_baseline_and_full_review() -> None:
    """UT11-105 chat p95 over 1.25 x baseline fails; standard review > 5,400 s fails only
    on a full-scale dataset."""
    base = _metrics(chat_latency_p95_s=4.0)
    assert _check(_metrics(chat_latency_p95_s=5.1), baseline=base)["latency"].passed is False
    assert _check(_metrics(chat_latency_p95_s=4.9), baseline=base)["latency"].passed is True
    slow = _metrics(review_latency_max_s=5401.0)
    assert _check(slow, dataset_scale="full")["latency"].passed is False
    assert _check(slow, dataset_scale="small")["latency"].passed is True
    assert _check(slow, dataset_scale="full", depth="deep")["latency"].passed is True


def test_ut11_106_cost_suite_errors_and_no_baseline() -> None:
    """UT11-106 premium cost 1.3 x baseline fails; one suite error fails; without a
    baseline the baseline-relative parts say `no baseline`."""
    base = _metrics(cost_usd=Decimal(10))
    cost = _check(_metrics(cost_usd=Decimal(13)), profile="premium", baseline=base)["cost"]
    assert cost.passed is False
    errors = _check(_metrics(), results=[_result("G01", status="suite_error")])
    assert errors["suite_errors"].passed is False
    assert errors["suite_errors"].actual == pytest.approx(1.0)
    none = _check(_metrics(cost_usd=Decimal(13)), profile="premium", depth="deep")
    assert none["cost"].passed is True
    assert none["cost"].detail == "no baseline"
    assert none["correctness"].detail == "no baseline"
    assert none["latency"].detail == "no baseline"


# --- controller-verification coverage (T11-29) ---------------------------------------------


def test_cv_t11_29_missing_trace_noted_and_left_out() -> None:
    """CV-T11-29 a run without a trace is named in the notes and left out of the per-run
    denominators; odd ledgers and boolean token counts add nothing."""
    results = [_result("G01", pipeline_run_id=RUN_A), _result("G02", pipeline_run_id=RUN_B)]
    odd = RunRow(
        run_id=RUN_C, kind="chat", depth="fast", profile="local", build_id=None, status="done",
        started_at=_T0, finished_at=None, cost_usd=Decimal("0.1"), config_hash="h", meta={},
        token_usage={"chat": {"tokens_in": True, "tokens_out": "9"}, "flag": 1},
    )  # fmt: skip
    m = aggregate_metrics(
        results, {RUN_A: [{"type": "retry"}]}, {RUN_C: odd}, skeptic=None, questions={}
    )
    assert m.notes == [f"trace missing: {RUN_B}"]
    assert m.retries_per_run == pytest.approx(1.0)
    assert m.tool_call_success_rate is None
    assert m.verifier_rejection_rate is None
    assert (m.tokens_in, m.tokens_out, m.cost_usd) == (0, 0, Decimal("0.1"))


def test_cv_t11_29_empty_results() -> None:
    """CV-T11-29 no results: rates None, per-run counts 0, unsupported rate 0."""
    m = aggregate_metrics([], {}, {}, skeptic=None, questions={})
    assert m.answer_correctness is None
    assert m.retries_per_run == pytest.approx(0.0)
    assert m.unsupported_number_rate == pytest.approx(0.0)
    assert m.chat_latency_p95_s is None
    assert m.judge_mean is None


def test_cv_t11_29_repeat_aggregation_edges() -> None:
    """CV-T11-29 all-skipped stays skipped, any suite_error wins, all-error stays error;
    a review result without a run id adds no review latency; questions drive chat latency."""
    results = [
        _result("G01", 1, status="skipped"), _result("G01", 2, status="skipped"),
        _result("G02", 1), _result("G02", 2, status="suite_error"),
        _result("G03", 1, status="error", latency_s=1.0),
        _result("G03", 2, status="error", latency_s=1.0),
        _result("F01", 1, draft_mode=None, latency_s=7.0),
        _result("O01", 1, status="error", draft_mode=None, latency_s=3.0),
    ]  # fmt: skip
    questions = {"F01": _question("F01", "funding"), "O01": _question("O01", "org")}
    m = aggregate_metrics(results, {}, {}, skeptic=None, questions=questions)
    assert m.question_status == {
        "G01": "skipped", "G02": "suite_error", "G03": "error", "F01": "passed", "O01": "error",
    }  # fmt: skip
    assert m.review_latency_p95_s is None
    assert m.chat_latency_p95_s == pytest.approx(1.0)


def test_cv_t11_29_skeptic_ignores_errored_outputs() -> None:
    """CV-T11-29 an org output with `error` is not a run: only errored outputs give None."""
    broken = PipelineOutput(error="RuntimeError: boom")
    assert skeptic_catch(truth(), [broken]) is None
    fooled = PipelineOutput(run_id=RUN_C, findings=[_finding(T5, "MTTR is worse")])
    assert skeptic_catch(truth(), [broken, fooled]) is False


def test_cv_t11_29_threshold_edges() -> None:
    """CV-T11-29 no graded questions, a baseline without correctness or latency, no tool
    calls, a profile without a tool minimum, and a passing cost all pass."""
    empty = _metrics(answer_correctness=None, tool_call_success_rate=None, chat_latency_p95_s=None)
    base = _metrics(answer_correctness=None, chat_latency_p95_s=None, cost_usd=Decimal(10))
    rows = _check(empty, profile="premium", baseline=base)
    assert rows["correctness"].detail == "no graded questions"
    assert rows["tool_calls"].detail == "no tool calls"
    assert rows["latency"].passed is True
    assert rows["latency"].limit is None
    assert rows["cost"].passed is True
    assert rows["cost"].limit == pytest.approx(12.0)
    ok = _check(_metrics(answer_correctness=0.95), profile="premium", baseline=base)
    assert ok["correctness"].passed is True
    assert ok["correctness"].detail == "no baseline correctness"
    other = _check(_metrics(), profile="mystery")
    assert other["tool_calls"].detail == "no minimum"
    assert other["cost"].detail == "not premium"
    assert other["suite_errors"].passed is True


def test_ut11_104_planted_ids_not_run_are_named() -> None:
    """UT11-104 a filtered run that skipped planted questions passes `planted_truth` and the
    detail names the planted ids that were not run."""
    row = _check(_metrics(skeptic_catch=True), results=[_result("G06"), _result("F01")])
    assert row["planted_truth"].passed is True
    assert row["planted_truth"].detail == "not run: G03, O01"
    every = [_result(qid) for qid in ("G03", "G06", "F01", "O01")]
    assert _check(_metrics(skeptic_catch=True), results=every)["planted_truth"].detail == "ok"


def test_ut11_105_slowest_of_many_review_runs_fails() -> None:
    """UT11-105 with 21 review runs where only the slowest exceeds 5,400 s, the standard
    review check on a full dataset fails (max, not the nearest-rank p95)."""
    runs = ["run_" + f"{i:026d}" for i in range(21)]
    review = [
        _result(f"F{i:02d}", latency_s=6000.0 if i == 20 else 100.0, pipeline_run_id=rid,
                draft_mode="full")
        for i, rid in enumerate(runs)
    ]  # fmt: skip
    m = aggregate_metrics(review, {}, {}, skeptic=None, questions={})
    assert m.review_latency_p95_s == pytest.approx(100.0)
    assert m.review_latency_max_s == pytest.approx(6000.0)
    row = _check(m, dataset_scale="full")["latency"]
    assert row.passed is False
    assert "review 6000 s" in row.detail


def test_ut11_103_baseline_without_correctness_reported() -> None:
    """UT11-103 a baseline without `answer_correctness` skips the drop check visibly:
    `no baseline` appears in the correctness detail."""
    base = _metrics(answer_correctness=None)
    row = _check(_metrics(answer_correctness=0.5), baseline=base)["correctness"]
    assert row.passed is False
    assert "below floor" in row.detail
    assert "no baseline" in row.detail
