"""Tests for herness.metrics.context (U04-54; context part of UT04-110)."""

import dataclasses
from typing import get_args

import pytest
from pydantic import ValidationError
from tests.support.metrics_render import AS_OF, TZ, FakeCatalog, metric, weights

from herness.metrics._binds import default_binds, weight_binds
from herness.metrics.context import ScoringReport, StepContext, StepResult
from herness.metrics.settings import WEIGHT_USES, UsdModel, unconfirmed_blocks
from herness.metrics.windows import default_window

pytestmark = pytest.mark.unit


def _context(*, catalog: FakeCatalog | None = None) -> StepContext:
    return StepContext(
        build_id="b1",
        catalog=catalog or FakeCatalog(),
        weights=weights(),
        as_of=AS_OF,
        tz=TZ,
        disabled_metrics=frozenset(),
    )


def test_ut04_110_step_context_binds_is_union() -> None:
    """UT04-110 binds() unions default_binds, weight_binds, the t12w window binds and the
    three step binds (s_count_metrics, s_unconfirmed_models, unconfirmed)."""
    sc = _context()
    binds = sc.binds()
    window = default_window("t12w", AS_OF, TZ, sc.catalog.defaults.windows)
    expected_base = {**default_binds(sc.catalog), **weight_binds(sc.weights), **window.binds()}
    for key, value in expected_base.items():
        assert binds[key] == value
    extra_keys = {"s_count_metrics", "s_unconfirmed_models", "unconfirmed"}
    assert set(binds) - set(expected_base) == extra_keys
    assert binds["s_count_metrics"] == []  # the default FakeCatalog metric aggregation is "mean"


def test_ut04_110_step_context_count_metrics_from_aggregation() -> None:
    """UT04-110 s_count_metrics lists enabled metrics whose aggregation is count or snapshot."""
    catalog = FakeCatalog(
        [
            metric(name="mttr_hours"),
            metric(name="n_incidents", aggregation="count"),
            metric(name="last_seen", aggregation="snapshot"),
            metric(name="disabled_count", aggregation="count", enabled=False),
        ]
    )
    sc = _context(catalog=catalog)
    assert sc.binds()["s_count_metrics"] == ["last_seen", "n_incidents"]


def test_ut04_110_step_context_unconfirmed_models_match_weight_gate() -> None:
    """UT04-110 s_unconfirmed_models and unconfirmed follow unconfirmed_blocks per lever model."""
    sc = _context()
    binds = sc.binds()
    expected = sorted(
        model
        for model in get_args(UsdModel)
        if unconfirmed_blocks(sc.weights, WEIGHT_USES[f"lever_{model}"])
    )
    assert binds["s_unconfirmed_models"] == expected
    assert binds["unconfirmed"] == bool(expected)


def test_ut04_110_step_context_is_immutable() -> None:
    """UT04-110 StepContext is a frozen dataclass."""
    sc = _context()
    with pytest.raises(dataclasses.FrozenInstanceError):
        sc.build_id = "other"  # type: ignore[misc]


def test_ut04_110_step_result_is_immutable() -> None:
    """UT04-110 StepResult is a frozen dataclass."""
    result = StepResult(row_counts={}, warnings=[], flags=[], failed_checks=[])
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.warnings = ["x"]  # type: ignore[misc]


def test_ut04_110_scoring_report_is_frozen_forbid_strict() -> None:
    """UT04-110 ScoringReport is a frozen, closed (extra=forbid), strict pydantic model."""
    report = ScoringReport(
        build_id="b1",
        steps_done=["validate"],
        row_counts={"metric_value": 3},
        duration_ms={"validate": 12},
        flags=[],
        warnings=[],
    )
    with pytest.raises(ValidationError):
        report.build_id = "other"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        ScoringReport.model_validate(report.model_dump() | {"extra": 1})
    with pytest.raises(ValidationError):
        ScoringReport.model_validate(report.model_dump() | {"row_counts": {"x": "3"}})
