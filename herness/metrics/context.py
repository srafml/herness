"""Step inputs and outputs shared by scoring step modules (design 04 §3.1, U04-54).

Exists so `funding.py`, `org.py`, `levers.py` and `portfolio.py` import step types without
importing `scoring.py` (no import cycle).
"""

import dataclasses
from collections.abc import Mapping
from datetime import date
from typing import cast, get_args

from pydantic import BaseModel, ConfigDict

from herness.metrics._binds import default_binds, weight_binds
from herness.metrics.catalog import MetricCatalog
from herness.metrics.settings import WEIGHT_USES, UsdModel, WeightsConfig, unconfirmed_blocks
from herness.metrics.windows import default_window

__all__ = ["ScoringReport", "StepContext", "StepResult"]


def _step_binds(catalog: MetricCatalog, weights: WeightsConfig) -> dict[str, object]:
    """`s_count_metrics`, `s_unconfirmed_models` and `unconfirmed` (U04-34)."""
    count_metrics = sorted(
        n for n in catalog.names() if catalog.get(n).aggregation in ("count", "snapshot")
    )
    unconfirmed_models = sorted(
        m for m in get_args(UsdModel) if unconfirmed_blocks(weights, WEIGHT_USES[f"lever_{m}"])
    )
    return {
        "s_count_metrics": count_metrics,
        "s_unconfirmed_models": unconfirmed_models,
        "unconfirmed": bool(unconfirmed_models),
    }


@dataclasses.dataclass(frozen=True, slots=True)
class StepContext:
    """Inputs shared by every scoring step (U04-54)."""

    build_id: str
    catalog: MetricCatalog
    weights: WeightsConfig
    as_of: date
    tz: str
    disabled_metrics: frozenset[str]

    def binds(self) -> dict[str, object]:
        """Union of `default_binds`, `weight_binds`, the t12w window binds and the step binds."""
        counts = cast("Mapping[str, int]", self.catalog.defaults.windows)
        window = default_window("t12w", self.as_of, self.tz, counts)
        return {
            **default_binds(self.catalog),
            **weight_binds(self.weights),
            **window.binds(),
            **_step_binds(self.catalog, self.weights),
        }


@dataclasses.dataclass(frozen=True, slots=True)
class StepResult:
    """Per-step outcome (U04-54)."""

    row_counts: dict[str, int]
    warnings: list[str]
    flags: list[str]
    failed_checks: list[str]


class ScoringReport(BaseModel):
    """`run_scoring`'s report (U04-54)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    build_id: str
    steps_done: list[str]
    row_counts: dict[str, int]
    duration_ms: dict[str, int]
    flags: list[str]
    warnings: list[str]
