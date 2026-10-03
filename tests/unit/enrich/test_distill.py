"""Tests for herness.enrich.distill: report model and job handler (UT03-128, UT03-129; T03-32).

The flow tests of `run_distill` (IT03-15, FT03-05) live in
``tests/integration/enrich/test_distill_flow.py`` and ``tests/fault/enrich/``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import ValidationError

from herness.core.errors import ConfigError
from herness.core.jobs import JobContext
from herness.enrich import distill
from herness.enrich.distill import DistillReport, YieldRequested, make_distill_handler

pytestmark = pytest.mark.unit


def _report(**fields: object) -> dict[str, object]:
    base: dict[str, object] = {
        "version": "laya-20261004-1", "round_kind": "initial", "round": 0, "teacher": "openjev",
        "teacher_version": "openjev-latest", "n_sample": 10, "n_train": 8, "n_val": 2,
        "blocked_questions": [], "gold_frozen_questions": ["q_c"], "gold_items_created": 0,
        "accepted_proposed": [], "macro_metric": 0.5, "stopped": False, "stop_reason": "none",
        "durations_s": {"prepared": 0.1},
    }  # fmt: skip
    base.update(fields)
    return base


def _ctx(payload: dict[str, object]) -> JobContext:
    return cast(JobContext, SimpleNamespace(job=SimpleNamespace(payload=payload)))


# --- UT03-128: DistillReport ------------------------------------------------------------------


def test_ut03_128_stopped_with_a_version_is_rejected() -> None:
    """UT03-128 stopped with a version: validation error (``stopped`` => ``version is None``)."""
    with pytest.raises(ValidationError, match="stopped"):
        DistillReport.model_validate(_report(stopped=True, stop_reason="min_gain"))


def test_ut03_128_valid_reports_are_immutable() -> None:
    """UT03-128 a stopped report without a version and a candidate report validate; frozen."""
    stopped = DistillReport.model_validate(
        _report(
            version=None, teacher=None, teacher_version=None, stopped=True, stop_reason="max_rounds"
        )
    )
    candidate = DistillReport.model_validate(_report())
    assert stopped.version is None
    assert candidate.model_dump(mode="json")["macro_metric"] == 0.5
    with pytest.raises(ValidationError):
        candidate.version = "laya-20261004-2"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        DistillReport.model_validate(_report(extra_field=1))


# --- UT03-129: make_distill_handler -----------------------------------------------------------


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    def fake_run(**kwargs: Any) -> DistillReport:
        seen.append(kwargs)
        return DistillReport.model_validate(_report(round_kind=kwargs["round_kind"]))

    monkeypatch.setattr(distill, "run_distill", fake_run)
    return seen


def test_ut03_129_unknown_round_kind_raises_config_error(calls: list[dict[str, Any]]) -> None:
    """UT03-129 payload ``{"round_kind": "x"}`` from ``ctx.job.payload``: ConfigError (R-42)."""
    handler = make_distill_handler(None)
    with pytest.raises(ConfigError, match="round_kind"):
        handler(_ctx({"round_kind": "x"}))
    with pytest.raises(ConfigError, match="round_kind"):
        handler(_ctx({"round_kind": 3}))
    assert calls == []


def test_ut03_129_default_initial_and_done_outcome(calls: list[dict[str, Any]]) -> None:
    """UT03-129 no round_kind -> "initial"; the report is the job result (json mode)."""
    factory = object()
    handler = make_distill_handler(factory)  # type: ignore[arg-type]
    ctx = _ctx({})
    outcome = handler(ctx)
    assert calls == [{"round_kind": "initial", "ctx": ctx, "llm_factory": factory}]
    assert outcome.status == "done"
    assert outcome.result["round_kind"] == "initial"
    assert outcome.result["version"] == "laya-20261004-1"
    outcome = handler(_ctx({"round_kind": "active"}))
    assert calls[-1]["round_kind"] == "active"
    assert outcome.result["round_kind"] == "active"


def test_ut03_129_yield_requested_maps_to_yield(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT03-129 ``YieldRequested`` from run_distill -> ``JobOutcome(status="yield")``."""

    def yielding(**_: Any) -> DistillReport:
        stage = "train"
        raise YieldRequested(stage)

    monkeypatch.setattr(distill, "run_distill", yielding)
    outcome = make_distill_handler(None)(_ctx({"round_kind": "initial"}))
    assert outcome.status == "yield"
    assert outcome.result == {}


def test_ut03_129_herness_errors_propagate(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT03-129 other errors propagate to spec 08 unchanged."""

    def failing(**_: Any) -> DistillReport:
        msg = "boom"
        raise ConfigError(msg)

    monkeypatch.setattr(distill, "run_distill", failing)
    with pytest.raises(ConfigError, match="boom"):
        make_distill_handler(None)(_ctx({}))


def test_ut03_129_reexports() -> None:
    """UT03-129 module map: accept_model, rollback_model and YieldRequested are re-exported."""
    from herness.enrich import gpu, laya_admin  # noqa: PLC0415 - identity check

    assert distill.accept_model is laya_admin.accept_model
    assert distill.rollback_model is laya_admin.rollback_model
    assert distill.YieldRequested is gpu.YieldRequested
