"""Tests for herness.core.types.jobs (T08-01)."""

from datetime import UTC, datetime
from typing import get_args

import pytest
from pydantic import ValidationError

import herness.core.types as shared
from herness.core.types.jobs import (
    BreakerState,
    ChatMode,
    GpuClass,
    JobKind,
    JobOutcome,
    JobSpec,
    MetricSample,
    PolicyName,
    ServiceName,
)

pytestmark = pytest.mark.unit

_BASE = {"kind": "sync", "payload": {}, "gpu_class": "none"}


def test_ut08_01_jobspec_priority_bounds_and_none() -> None:
    """UT08-01 JobSpec priority rejects -1 and 101, and accepts None (R-41)."""
    with pytest.raises(ValidationError):
        JobSpec(**_BASE, priority=-1)
    with pytest.raises(ValidationError):
        JobSpec(**_BASE, priority=101)
    spec = JobSpec(**_BASE, priority=None)
    assert spec.priority is None


def test_ut08_01_jobspec_rejects_extra_field() -> None:
    """UT08-01 an extra field on JobSpec raises ValidationError (extra='forbid')."""
    with pytest.raises(ValidationError):
        JobSpec(**_BASE, nope="not a field")


def test_ut08_01_jobspec_rejects_naive_scheduled_for() -> None:
    """UT08-01 a naive scheduled_for raises ValidationError (ENG §3.2)."""
    with pytest.raises(ValidationError):
        JobSpec(**_BASE, scheduled_for=datetime(2026, 1, 1))  # noqa: DTZ001


def test_ut08_01_jobspec_valid_is_frozen() -> None:
    """UT08-01 a valid JobSpec is frozen; attribute assignment raises."""
    spec = JobSpec(**_BASE, scheduled_for=datetime(2026, 1, 1, tzinfo=UTC))
    with pytest.raises(ValidationError):
        spec.kind = "chat"  # type: ignore[misc]


def test_ut08_01_literal_sets_match_design_08() -> None:
    """UT08-01 the six 08 literal aliases equal design 08 §3.1, §3.4, §3.6 exactly."""
    assert set(get_args(GpuClass.__value__)) == {"none", "reasoning", "decider", "large"}
    assert set(get_args(JobKind.__value__)) == {
        "sync",
        "reconcile",
        "build_pipeline",
        "distill",
        "review",
        "chat",
        "outcome_measure",
        "memory_maintenance",
        "maintenance",
        "eval",
    }
    assert set(get_args(ServiceName.__value__)) == {
        "vllm-reasoning",
        "openjev",
        "llamacpp-large",
    }
    assert set(get_args(ChatMode.__value__)) == {"live", "small_model", "defer", "cloud"}
    assert set(get_args(BreakerState.__value__)) == {"closed", "open", "half_open"}
    assert set(get_args(PolicyName.__value__)) == {
        "source_http_page",
        "llm_local",
        "llm_large",
        "llm_cloud",
        "decider_local",
        "decider_cloud",
        "embed_batch",
        "tool_store",
        "warehouse_read",
        "sqlite_write",
        "gpu_health",
    }


def test_ut08_01_types_package_exports_no_jobcontext() -> None:
    """UT08-01 herness.core.types exports no JobContext (R-02)."""
    assert "JobContext" not in shared.__all__
    assert not hasattr(shared, "JobContext")


def test_ut08_02_joboutcome_default_result_is_empty() -> None:
    """UT08-02 JobOutcome.result defaults to {}."""
    outcome = JobOutcome(status="done")
    assert outcome.result == {}


def test_ut08_02_joboutcome_oversize_result_raises() -> None:
    """UT08-02 a result over 1 MiB of canonical JSON raises ValidationError."""
    big = {"blob": "x" * 1_100_000}
    with pytest.raises(ValidationError):
        JobOutcome(status="done", result=big)


def test_joboutcome_keeps_a_valid_small_result() -> None:
    """Supplementary (T08-01): a small, JSON-safe result passes through unchanged."""
    outcome = JobOutcome(status="done", result={"count": 3, "note": "ok"})
    assert outcome.result == {"count": 3, "note": "ok"}


def test_joboutcome_rejects_non_finite_number_in_result() -> None:
    """Supplementary (T08-01): a NaN inside result is not canonical-JSON encodable."""
    with pytest.raises(ValidationError):
        JobOutcome(status="done", result={"v": float("nan")})


def test_ut08_02_joboutcome_frozen_and_extra_forbidden() -> None:
    """UT08-02 JobOutcome is frozen and rejects extra fields."""
    outcome = JobOutcome(status="yield")
    with pytest.raises(ValidationError):
        outcome.status = "done"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        JobOutcome(status="done", nope=1)


def test_metricsample_valid_round_trip() -> None:
    """Supplementary (T08-01): a valid MetricSample constructs and is frozen (U08-101)."""
    sample = MetricSample(
        ts=datetime(2026, 1, 1, tzinfo=UTC),
        name="herness_job_started_total",
        kind="counter",
        value=1.0,
        labels={"kind": "sync"},
        component="jobs",
    )
    assert sample.value == 1.0
    with pytest.raises(ValidationError):
        sample.value = 2.0  # type: ignore[misc]


def test_metricsample_rejects_naive_ts() -> None:
    """Supplementary (T08-01): MetricSample.ts must be timezone-aware."""
    with pytest.raises(ValidationError):
        MetricSample(
            ts=datetime(2026, 1, 1),  # noqa: DTZ001
            name="herness_job_started_total",
            kind="counter",
            value=1.0,
            component="jobs",
        )


def test_metricsample_rejects_bad_name() -> None:
    """Supplementary (T08-01): MetricSample.name must match the herness_* metric regex."""
    with pytest.raises(ValidationError):
        MetricSample(
            ts=datetime(2026, 1, 1, tzinfo=UTC),
            name="not_a_herness_metric",
            kind="counter",
            value=1.0,
            component="jobs",
        )


def test_metricsample_rejects_negative_counter_value() -> None:
    """Supplementary (T08-01): counter and histogram values must be >= 0."""
    with pytest.raises(ValidationError):
        MetricSample(
            ts=datetime(2026, 1, 1, tzinfo=UTC),
            name="herness_job_started_total",
            kind="counter",
            value=-1.0,
            component="jobs",
        )


def test_metricsample_allows_negative_gauge_value() -> None:
    """Supplementary (T08-01): gauge values may be negative."""
    sample = MetricSample(
        ts=datetime(2026, 1, 1, tzinfo=UTC),
        name="herness_queue_depth_delta",
        kind="gauge",
        value=-1.0,
        component="jobs",
    )
    assert sample.value == -1.0


def test_metricsample_rejects_non_finite_value() -> None:
    """Supplementary (T08-01): non-finite values (nan, inf) are rejected."""
    with pytest.raises(ValidationError):
        MetricSample(
            ts=datetime(2026, 1, 1, tzinfo=UTC),
            name="herness_job_started_total",
            kind="gauge",
            value=float("nan"),
            component="jobs",
        )


def test_metricsample_rejects_too_many_labels() -> None:
    """Supplementary (T08-01): labels caps at 6 keys."""
    labels = {f"k{i}": "v" for i in range(7)}
    with pytest.raises(ValidationError):
        MetricSample(
            ts=datetime(2026, 1, 1, tzinfo=UTC),
            name="herness_job_started_total",
            kind="counter",
            value=1.0,
            labels=labels,
            component="jobs",
        )


def test_metricsample_rejects_bad_label_key_and_value() -> None:
    """Supplementary (T08-01): label keys and values must match their regexes."""
    with pytest.raises(ValidationError):
        MetricSample(
            ts=datetime(2026, 1, 1, tzinfo=UTC),
            name="herness_job_started_total",
            kind="counter",
            value=1.0,
            labels={"Bad-Key": "v"},
            component="jobs",
        )
    with pytest.raises(ValidationError):
        MetricSample(
            ts=datetime(2026, 1, 1, tzinfo=UTC),
            name="herness_job_started_total",
            kind="counter",
            value=1.0,
            labels={"key": "bad value with spaces"},
            component="jobs",
        )


def test_metricsample_rejects_bad_component() -> None:
    """Supplementary (T08-01): component must match ^[a-z][a-z0-9_]{0,31}$."""
    with pytest.raises(ValidationError):
        MetricSample(
            ts=datetime(2026, 1, 1, tzinfo=UTC),
            name="herness_job_started_total",
            kind="counter",
            value=1.0,
            component="Not-Valid",
        )


def test_jobspec_rejects_bad_idem_key() -> None:
    """Supplementary (T08-01): idem_key must match its allowlist regex."""
    with pytest.raises(ValidationError):
        JobSpec(**_BASE, idem_key="bad key with spaces")
    spec = JobSpec(**_BASE, idem_key="a.valid-key_1:2/3")
    assert spec.idem_key == "a.valid-key_1:2/3"


def test_jobspec_max_attempts_bounds() -> None:
    """Supplementary (T08-01): max_attempts is None or in [1, 20]."""
    with pytest.raises(ValidationError):
        JobSpec(**_BASE, max_attempts=0)
    with pytest.raises(ValidationError):
        JobSpec(**_BASE, max_attempts=21)
    assert JobSpec(**_BASE, max_attempts=None).max_attempts is None
    assert JobSpec(**_BASE, max_attempts=20).max_attempts == 20
