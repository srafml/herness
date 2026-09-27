"""Tests for herness.metrics.settings (U04-14 … U04-22, U04-82)."""

import ast
import copy
import dataclasses
import sys
import typing
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from herness.core.errors import ConfigError
from herness.metrics import settings as s
from herness.metrics.settings import (
    WEIGHT_USES,
    MetricDef,
    MetricsCatalogConfig,
    MetricsDefaults,
    ScoringConfig,
    WeightChangePayload,
    WeightIssue,
    WeightsConfig,
    check_weight_confirmations,
    unconfirmed_blocks,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
HASH = "cfg_0123456789abcdef"
OTHER_HASH = "cfg_fedcba9876543210"
BLOCKS = [
    "cost_per_downtime_hour",
    "cost_per_engineer_hour",
    "hours_per_story_point",
    "priority_impact_multiplier",
    "impact_fallback",
    "toil",
    "change",
    "sla_penalty_usd",
    "strategic_weights",
    "expected_reduction",
    "cluster_fix",
    "team_capacity_points_per_quarter",
]


def _yaml(name: str) -> dict[str, Any]:
    data = yaml.safe_load((ROOT / "config" / name).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _weights_raw() -> dict[str, Any]:
    return copy.deepcopy(_yaml("weights.yaml"))


def _weights(**flags: bool) -> WeightsConfig:
    raw = _weights_raw()
    for block, value in flags.items():
        raw[block]["unconfirmed"] = value
    return WeightsConfig.model_validate(raw)


def _metric_raw() -> dict[str, Any]:
    return {
        "name": "mttr_hours",
        "description": "Mean wall-clock hours from opened_at to resolved_at.",
        "domain": "ops",
        "grains": ["service", "team", "org", "cluster"],
        "unit": "hours",
        "better": "lower",
        "aggregation": "mean",
        "min_sample_size": 10,
        "owner": "sre-analytics",
        "estimate": False,
        "uses_weights": [],
        "usd_model": "mttr",
        "filters": ["priority", "service_id"],
        "enabled": True,
        "requires_columns": [],
        "sql": "SELECT 1",
    }


def _payload(blocks: list[str], config_hash: str | None = HASH) -> WeightChangePayload:
    return WeightChangePayload.model_validate(
        {"blocks": blocks, "proposed_config_hash": config_hash, "changes": [], "origin": "operator"}
    )


def _locs(exc: ValidationError) -> list[tuple[int | str, ...]]:
    return [tuple(err["loc"]) for err in exc.errors()]


# --- UT04-15: literals and MetricDef -------------------------------------------------------


def test_ut04_15_literals_match_spec() -> None:
    """UT04-15 closed vocabularies equal U04-14 (Unit equals design 00 §12.1)."""
    assert typing.get_args(s.Unit) == (
        "count", "usd", "pct", "ratio", "hours", "minutes", "seconds",
        "days", "score", "rank", "other",
    )  # fmt: skip
    assert typing.get_args(s.Period) == ("week", "month", "quarter", "t12w", "t12m")
    assert typing.get_args(s.EntityType) == ("service", "team", "org", "work_item", "cluster")
    assert typing.get_args(s.Better) == ("higher", "lower")
    assert typing.get_args(s.Aggregation) == (
        "count", "sum", "mean", "median", "ratio", "snapshot",
    )  # fmt: skip
    assert typing.get_args(s.Domain) == ("ops", "change", "delivery", "monitoring", "cost")
    assert typing.get_args(s.UsdModel) == (
        "mttr", "repeat", "reopen", "reassign", "sla", "cfr", "noise",
    )  # fmt: skip
    assert typing.get_args(s.FilterKey) == (
        "priority", "service_id", "team_id", "org_id", "cluster_id",
        "work_item_type", "severity", "change_type",
    )  # fmt: skip
    assert typing.get_args(s.Source) == (
        "incident", "change", "event", "work_item", "metric_daily",
    )  # fmt: skip


def test_ut04_15_metric_def_rejects_unit_percent_and_better_up() -> None:
    """UT04-15 unit `percent`, better `up` → pydantic error on both fields."""
    assert MetricDef.model_validate(_metric_raw()).unit == "hours"
    raw = _metric_raw() | {"unit": "percent", "better": "up"}
    with pytest.raises(ValidationError) as info:
        MetricDef.model_validate(raw)
    locs = _locs(info.value)
    assert ("unit",) in locs
    assert ("better",) in locs


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "Bad-Name"),
        ("name", "ab"),
        ("description", ""),
        ("description", "x" * 301),
        ("grains", []),
        ("grains", ["team", "team"]),
        ("min_sample_size", 0),
        ("min_sample_size", 100_001),
        ("min_sample_size", True),
        ("owner", "Owner!"),
        ("filters", ["priority", "priority"]),
        ("filters", ["description"]),
        ("requires_columns", ["incident.team_id"]),
        ("sql", ""),
        ("sql", "x" * 20_001),
        ("usd_model", "dollars"),
        ("extra_key", 1),
    ],
)
def test_ut04_15_metric_def_field_constraints(field: str, value: object) -> None:
    """UT04-15 each MetricDef field constraint rejects a bad value at its own path."""
    with pytest.raises(ValidationError) as info:
        MetricDef.model_validate(_metric_raw() | {field: value})
    assert any(loc[0] == field for loc in _locs(info.value))


def test_ut04_15_metric_def_is_frozen() -> None:
    """UT04-15 MetricDef is immutable."""
    metric = MetricDef.model_validate(_metric_raw() | {"requires_columns": ["core.incident.x"]})
    with pytest.raises(ValidationError):
        metric.name = "other"  # type: ignore[misc]


# --- UT04-19: section models ---------------------------------------------------------------


def test_ut04_19_shipped_sections_validate() -> None:
    """UT04-19 shipped weights.yaml and the metrics.yaml defaults/scoring sections validate."""
    weights = WeightsConfig.model_validate(_weights_raw())
    assert list(weights.blocks()) == BLOCKS
    assert all(block.unconfirmed for block in weights.blocks().values())
    assert weights.cost_per_downtime_hour.by_criticality[1] == Decimal(50000)
    assert weights.strategic_weights.clip == (0.5, 2.0)
    assert [sc.name for sc in weights.portfolio.scenarios] == ["lean", "base", "stretch"]
    raw = _yaml("metrics.yaml")
    assert raw["version"] == 1
    defaults = MetricsDefaults.model_validate(raw["defaults"])
    assert defaults == MetricsDefaults()
    scoring = ScoringConfig.model_validate(raw["scoring"])
    assert scoring.as_of is None
    # T04-09/10/11: the design 04 §7.1 weights sum to 1.0 once every scorecard metric ships.
    assert scoring.org.metrics == {"mttr_hours": 0.15}
    assert set(scoring.levers.templates) == set(typing.get_args(s.UsdModel))


def test_ut04_19_catalog_root_model() -> None:
    """UT04-19 MetricsCatalogConfig validates and rejects duplicate metric names."""
    raw = _yaml("metrics.yaml")
    raw["metrics"] = [_metric_raw()]
    catalog = MetricsCatalogConfig.model_validate(raw)
    assert catalog.metrics[0].name == "mttr_hours"
    raw["metrics"] = [_metric_raw(), _metric_raw()]
    with pytest.raises(ValidationError, match="duplicate metric mttr_hours"):
        MetricsCatalogConfig.model_validate(raw)
    raw["metrics"] = []
    with pytest.raises(ValidationError):
        MetricsCatalogConfig.model_validate(raw)


def _mutate(raw: dict[str, Any], path: tuple[str, ...], value: object) -> None:
    node = raw
    for key in path[:-1]:
        node = node[key]
    if value is _DELETE:
        del node[path[-1]]
    else:
        node[path[-1]] = value


_DELETE = object()


@pytest.mark.parametrize(
    ("path", "value", "loc"),
    [
        (
            ("priority_impact_multiplier", "values", 5),
            _DELETE,
            ("priority_impact_multiplier", "values"),
        ),
        (("strategic_weights", "clip"), [2.0, 0.5], ("strategic_weights", "clip")),
        (("strategic_weights", "clip"), [0.0, 0.5], ("strategic_weights", "clip")),
        (("toil", "effort_factor", 6), 1.0, ("toil", "effort_factor")),
        (("toil", "business_share"), 1.5, ("toil", "business_share")),
        (
            ("cost_per_downtime_hour", "by_criticality", 5),
            1,
            ("cost_per_downtime_hour", "by_criticality"),
        ),
        (("cost_per_downtime_hour", "default"), -1, ("cost_per_downtime_hour", "default")),
        (("cost_per_engineer_hour", "value"), 0, ("cost_per_engineer_hour", "value")),
        (("cost_per_engineer_hour", "value"), 95.125, ("cost_per_engineer_hour", "value")),
        (("cost_per_engineer_hour", "value"), float("nan"), ("cost_per_engineer_hour", "value")),
        (("cost_per_engineer_hour", "value"), "ninety", ("cost_per_engineer_hour", "value")),
        (("cost_per_engineer_hour", "value"), True, ("cost_per_engineer_hour", "value")),
        (("impact_fallback", "outage_fraction", 2), _DELETE, ("impact_fallback",)),
        (("impact_fallback", "max_priority"), 6, ("impact_fallback", "max_priority")),
        (
            ("expected_reduction", "overrides", "X-1"),
            1.5,
            ("expected_reduction", "overrides", "X-1"),
        ),
        (("business_timezone",), "Mars/Olympus", ("business_timezone",)),
        (("business_timezone",), "../etc", ("business_timezone",)),
        (("version",), 2, ("version",)),
        (("portfolio", "mandatory"), ["PAY-1"], ("portfolio",)),
        (("portfolio", "scenarios", 0, "name"), "unconstrained", ("portfolio", "scenarios")),
        (("portfolio", "scenarios", 1, "name"), "lean", ("portfolio", "scenarios")),
        (
            ("portfolio", "scenarios", 0, "budget_usd"),
            10**13,
            ("portfolio", "scenarios", 0, "budget_usd"),
        ),
        (("portfolio", "solver", "num_workers"), 2, ("portfolio", "solver", "num_workers")),
        (("change", "unknown"), 1, ("change", "unknown")),
    ],
)
def test_ut04_19_weights_invalid_values_give_paths(
    path: tuple[Any, ...], value: object, loc: tuple[str | int, ...]
) -> None:
    """UT04-19 invalid weights (missing priority key, clip reversed, ...) → errors with paths."""
    raw = _weights_raw()
    if path == ("portfolio", "mandatory"):
        raw["portfolio"]["excluded"] = ["PAY-1"]
    _mutate(raw, path, value)
    with pytest.raises(ValidationError) as info:
        WeightsConfig.model_validate(raw)
    assert any(found[: len(loc)] == loc for found in _locs(info.value)), _locs(info.value)


def test_ut04_19_weights_decimal_inputs() -> None:
    """UT04-19 Decimal fields accept YAML ints, 2-decimal floats and decimal strings."""
    raw = _weights_raw()
    raw["cost_per_engineer_hour"]["value"] = 95.5
    raw["sla_penalty_usd"]["value"] = "12.345"
    raw["cluster_fix"]["min_annual_pain_usd"] = 50000
    weights = WeightsConfig.model_validate(raw)
    assert weights.cost_per_engineer_hour.value == Decimal("95.5")
    assert weights.sla_penalty_usd.value == Decimal("12.345")
    assert weights.cluster_fix.min_annual_pain_usd == Decimal(50000)
    raw["strategic_weights"]["clip"] = (1, 1)
    assert WeightsConfig.model_validate(raw).strategic_weights.clip == (1.0, 1.0)


def test_ut04_19_unconfirmed_defaults_true() -> None:
    """UT04-19 a block without `unconfirmed` counts as unconfirmed (safe default)."""
    raw = _weights_raw()
    del raw["change"]["unconfirmed"]
    assert WeightsConfig.model_validate(raw).change.unconfirmed is True


@pytest.mark.parametrize(
    ("path", "value", "loc"),
    [
        (("defaults", "windows", "week"), _DELETE, ("defaults", "windows")),
        (("defaults", "windows", "week"), 521, ("defaults", "windows", "week")),
        (("defaults", "noise_severities"), [], ("defaults", "noise_severities")),
        (("defaults", "failure_outcomes"), ["ok"], ("defaults", "failure_outcomes", 0)),
        (("defaults", "compute_timeout_s"), 0.5, ("defaults", "compute_timeout_s")),
        (("scoring", "funding", "tier_weights", "cluster_weight"), 0.0, ("scoring", "funding")),
        (("scoring", "funding", "window_days"), 29, ("scoring", "funding", "window_days")),
        (("scoring", "org", "metrics"), {}, ("scoring", "org", "metrics")),
        (("scoring", "org", "metrics", "mttr_hours"), 0.0, ("scoring", "org", "metrics")),
        (("scoring", "levers", "templates", "noise"), _DELETE, ("scoring", "levers", "templates")),
        (("scoring", "levers", "templates", "mttr"), "x" * 501, ("scoring", "levers", "templates")),
        (("scoring", "as_of"), "2026-01-01", ("scoring", "as_of")),
    ],
)
def test_ut04_19_metrics_sections_invalid_values_give_paths(
    path: tuple[str, ...], value: object, loc: tuple[str, ...]
) -> None:
    """UT04-19 invalid defaults/scoring values → errors with paths."""
    raw = _yaml("metrics.yaml")
    raw["metrics"] = [_metric_raw()]
    _mutate(raw, path, value)
    with pytest.raises(ValidationError) as info:
        MetricsCatalogConfig.model_validate(raw)
    assert any(found[: len(loc)] == loc for found in _locs(info.value)), _locs(info.value)


# --- U04-20: WEIGHT_USES and unconfirmed_blocks --------------------------------------------


def test_ut04_99_weight_uses_table() -> None:
    """UT04-99 (U04-20 part) WEIGHT_USES entries per consumer; unions keep first-seen order."""
    incident = (
        "cost_per_downtime_hour", "priority_impact_multiplier", "impact_fallback",
        "toil", "cost_per_engineer_hour",
    )  # fmt: skip
    funding = (
        *incident, "change", "hours_per_story_point", "strategic_weights",
        "expected_reduction", "cluster_fix",
    )  # fmt: skip
    assert WEIGHT_USES["incident_cost"] == incident
    assert WEIGHT_USES["toil"] == ("toil", "cost_per_engineer_hour")
    assert WEIGHT_USES["impact"] == ("impact_fallback",)
    assert WEIGHT_USES["noise_cost"] == ("toil", "cost_per_engineer_hour")
    assert WEIGHT_USES["backout_cost"] == ("change", "cost_per_engineer_hour")
    assert WEIGHT_USES["funding"] == funding
    for lever in ("lever_mttr", "lever_repeat", "lever_reopen"):
        assert WEIGHT_USES[lever] == incident
    assert WEIGHT_USES["lever_reassign"] == ("toil", "cost_per_engineer_hour")
    assert WEIGHT_USES["lever_sla"] == ("sla_penalty_usd",)
    assert WEIGHT_USES["lever_cfr"] == (*incident, "change")
    assert WEIGHT_USES["lever_noise"] == ("toil", "cost_per_engineer_hour")
    assert WEIGHT_USES["portfolio"] == (*funding, "team_capacity_points_per_quarter")
    assert all(set(uses) <= set(BLOCKS) for uses in WEIGHT_USES.values())
    with pytest.raises(TypeError):
        WEIGHT_USES["x"] = ()  # type: ignore[index]


def test_ut04_68_unconfirmed_blocks() -> None:
    """UT04-68 (U04-20 part) unconfirmed_blocks returns named unconfirmed blocks, sorted, unique."""
    weights = _weights(toil=False)
    names = ["toil", "cost_per_engineer_hour", "change", "cost_per_engineer_hour"]
    assert unconfirmed_blocks(weights, names) == ["change", "cost_per_engineer_hour"]
    assert unconfirmed_blocks(weights, []) == []
    with pytest.raises(ConfigError, match="unknown weight block nope"):
        unconfirmed_blocks(weights, ["toil", "nope"])


# --- UT04-20 / UT04-119 / ST04-05: confirmation gate --------------------------------------


def test_ut04_20_confirmation_needs_matching_payload() -> None:
    """UT04-20 previous unconfirmed, current confirmed: issue without payload, none with."""
    previous = _weights()
    current = _weights(toil=False, change=False)
    issues = check_weight_confirmations(previous, current, HASH, [])
    assert [i.path for i in issues] == ["weights.toil.unconfirmed", "weights.change.unconfirmed"]
    assert all(i.severity == "error" for i in issues)
    assert issues[0].message == (
        f"confirming toil needs an approved weight_change review item for {HASH}"
    )
    approved = [_payload(["toil"]), _payload(["change", "toil"])]
    assert check_weight_confirmations(previous, current, HASH, approved) == []


def test_ut04_20_confirmation_edge_cases() -> None:
    """UT04-20 already-confirmed blocks pass; first load gates every confirmed block."""
    confirmed = _weights(toil=False)
    assert check_weight_confirmations(confirmed, confirmed, HASH, []) == []
    issues = check_weight_confirmations(None, confirmed, HASH, [])
    assert [i.path for i in issues] == ["weights.toil.unconfirmed"]
    assert check_weight_confirmations(_weights(), _weights(), HASH, []) == []
    with pytest.raises(ConfigError):
        check_weight_confirmations(None, confirmed, "cfg_XYZ", [])


def test_ut04_119_gate_returns_weight_issues() -> None:
    """UT04-119 gate returns WeightIssue (not ConfigIssue); empty message → ConfigError."""
    issues = check_weight_confirmations(_weights(), _weights(cluster_fix=False), HASH, [])
    assert len(issues) == 1
    assert type(issues[0]) is WeightIssue
    assert issues[0].path == "weights.cluster_fix.unconfirmed"
    assert dataclasses.is_dataclass(issues[0])
    with pytest.raises(dataclasses.FrozenInstanceError):
        issues[0].path = "x"  # type: ignore[misc]
    with pytest.raises(ConfigError, match="bad weight issue"):
        WeightIssue(severity="error", path="weights.toil.unconfirmed", message="")


@pytest.mark.parametrize(
    ("severity", "path", "message"),
    [
        ("fatal", "weights.toil.unconfirmed", "m"),
        ("error", "", "m"),
        ("error", "p" * 201, "m"),
        ("warn", "weights.toil.unconfirmed", "m" * 301),
    ],
)
def test_ut04_119_weight_issue_constraints(severity: Any, path: str, message: str) -> None:
    """UT04-119 WeightIssue field constraints are checked in __post_init__."""
    with pytest.raises(ConfigError, match="bad weight issue"):
        WeightIssue(severity=severity, path=path, message=message)


_SIBLING = "herness.metrics._weights_settings"
_ALLOWED_HERNESS = {"herness.core.types", "herness.core.errors", _SIBLING}


def _imported_modules(path: Path) -> list[str]:
    modules: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            assert node.module is not None
            modules.append(node.module)
    return modules


@pytest.mark.parametrize("name", ["settings.py", "_weights_settings.py"])
def test_ut04_119_settings_imports_are_restricted(name: str) -> None:
    """UT04-119 settings modules import only stdlib, pydantic, core.types, core.errors (R-03).

    settings.py may also import its private sibling _weights_settings, which obeys the same rule.
    """
    modules = _imported_modules(ROOT / "herness" / "metrics" / name)
    assert modules
    for module in modules:
        top = module.split(".")[0]
        ok = top in sys.stdlib_module_names or top == "pydantic" or module in _ALLOWED_HERNESS
        assert ok, module
    assert (_SIBLING in modules) == (name == "settings.py")


@pytest.mark.filterwarnings("error")
def test_ut04_19_models_round_trip_json_and_deepcopy() -> None:
    """UT04-19 loaded configs survive model_dump_json/model_validate_json and deepcopy unchanged."""
    weights = WeightsConfig.model_validate(_weights_raw())
    assert WeightsConfig.model_validate_json(weights.model_dump_json()) == weights
    assert weights.model_dump(mode="json")["cost_per_engineer_hour"]["value"] == "95"
    raw = _yaml("metrics.yaml")
    raw["metrics"] = [_metric_raw() | {"requires_columns": ["core.incident.team_id"]}]
    catalog = MetricsCatalogConfig.model_validate(raw)
    assert MetricsCatalogConfig.model_validate_json(catalog.model_dump_json()) == catalog
    payload = _payload(["toil"])
    assert WeightChangePayload.model_validate_json(payload.model_dump_json()) == payload
    for model in (weights, catalog, catalog.defaults, catalog.scoring, payload):
        assert copy.deepcopy(model) == model
        assert model.model_copy(deep=True) == model


def test_st04_05_flip_without_or_with_wrong_approval_refused() -> None:
    """ST04-05 flipping `unconfirmed` without approval, or approval for another hash, is refused."""
    previous = _weights()
    current = _weights(cost_per_engineer_hour=False)
    expected = ["weights.cost_per_engineer_hour.unconfirmed"]
    no_approval = check_weight_confirmations(previous, current, HASH, [])
    assert [i.path for i in no_approval] == expected
    other = [
        _payload(["cost_per_engineer_hour"], OTHER_HASH),
        _payload(["cost_per_engineer_hour"], None),
    ]
    wrong_hash = check_weight_confirmations(previous, current, HASH, other)
    assert [i.path for i in wrong_hash] == expected
    other_block = [_payload(["toil"])]
    assert [
        i.path for i in check_weight_confirmations(previous, current, HASH, other_block)
    ] == expected
    for issue in no_approval + wrong_hash:
        assert "95" not in issue.message


# --- U04-21: payload schema ----------------------------------------------------------------


def test_ut04_20_weight_change_payload_schema() -> None:
    """UT04-20 WeightChangePayload has no free text and enforces its patterns."""
    good = {
        "blocks": ["toil"],
        "proposed_config_hash": HASH,
        "changes": [{"path": "weights.toil.effort_factor.1", "old": "1.5", "new": "2"}],
        "origin": "memory",
        "memory_id": "mem_01J8ZQ3V4W5X6Y7Z8A9B0C1D2E",
    }
    payload = WeightChangePayload.model_validate(good)
    assert payload.changes[0].new == "2"
    bad_cases: list[dict[str, Any]] = [
        good | {"blocks": []},
        good | {"blocks": ["nope"]},
        good | {"proposed_config_hash": "cfg_short"},
        good | {"origin": "model"},
        good | {"memory_id": None},
        good | {"origin": "operator"},
        good | {"memory_id": "mem_bad"},
        good | {"note": "free text"},
        good | {"changes": [{"path": "weights.toil", "old": "two hours", "new": None}]},
        good | {"changes": [{"path": "toil.x", "old": None, "new": "1"}]},
        good | {"changes": [{"path": "weights.toil", "old": None, "new": "1" * 33}]},
    ]
    for bad in bad_cases:
        with pytest.raises(ValidationError):
            WeightChangePayload.model_validate(bad)
