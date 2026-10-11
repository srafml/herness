"""CV-T11-32: the latency ratio skips a baseline whose chat p95 is below a floor (U11-65).

A mock baseline answers in about no time, so `ratio x baseline p95` would fail every later
run. Below `thresholds.latency_baseline_floor_s` (default 1.0 s) the ratio part is skipped
with `baseline latency below floor` in `detail`; the absolute caps still apply (controller
ruling, T11-32 spec note after U11-65).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from herness.eval.metrics import EvalMetrics, check_thresholds
from herness.eval.settings import Thresholds

pytestmark = pytest.mark.unit


def _metrics(p95: float) -> EvalMetrics:
    return EvalMetrics.model_validate({
        "question_status": {}, "answer_correctness": 0.95, "unsupported_number_rate": 0.0,
        "tool_call_success_rate": 0.99, "chat_latency_p95_s": p95, "cost_usd": Decimal(10),
    })  # fmt: skip


def _settings(**extra: Any) -> Thresholds:
    return Thresholds(
        unsupported_number_rate_max=0.0, correctness_drop_max_pp=3.0,
        correctness_floor={"local-standard": 0.80}, tool_success_min={"local": 0.90},
        latency_p95_ratio_max=1.25, cost_ratio_max=1.20, bench_regression_max=0.20, **extra,
    )  # fmt: skip


def _latency(m: EvalMetrics, base: EvalMetrics, settings: Thresholds) -> Any:
    rows = check_thresholds(
        m, profile="local", depth="standard", baseline=base, results=[],
        dataset_kind="synthetic", dataset_scale="small", settings=settings,
    )  # fmt: skip
    return next(r for r in rows if r.check == "latency")


def test_cv_t11_32_latency_ratio_skipped_below_baseline_floor() -> None:
    """CV-T11-32 a baseline chat p95 below the floor (default 1.0 s) skips the ratio part
    with a visible detail; the 30 s local-standard cap still applies; at or above the floor
    the ratio applies as before."""
    assert _settings().latency_baseline_floor_s == 1.0
    near_zero = _metrics(0.01)
    row = _latency(_metrics(2.5), near_zero, _settings())
    assert row.passed is True
    assert "baseline latency below floor" in row.detail
    assert row.limit == 30.0
    assert _latency(_metrics(31.0), near_zero, _settings()).passed is False  # absolute cap
    row = _latency(_metrics(2.5), _metrics(1.0), _settings())  # ratio applies at the floor
    assert (row.passed, row.limit) == (False, 1.25)
    assert (
        _latency(_metrics(2.5), near_zero, _settings(latency_baseline_floor_s=0.001)).passed
        is False
    )
