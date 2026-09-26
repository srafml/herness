"""UT06-11 … UT06-13, ST06-15: `config/pipelines.yaml` model and knob resolution (U06-22-24)."""

import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from herness.core.errors import ConfigError
from herness.core.types.swarm import TaskBudget
from herness.harness.pipelines.settings import (
    DepthConfig,
    DepthKnobs,
    KSamples,
    PipelinesConfig,
    resolve_knobs,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[4]
SHIPPED = ROOT / "config" / "pipelines.yaml"


def _raw() -> dict[str, Any]:
    data = yaml.safe_load(SHIPPED.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _cfg() -> PipelinesConfig:
    return PipelinesConfig.model_validate(_raw())


def _error_locs(exc: pytest.ExceptionInfo[ValidationError]) -> list[tuple[int | str, ...]]:
    return [tuple(e["loc"]) for e in exc.value.errors()]


# --- UT06-11 -------------------------------------------------------------------------------


def test_ut06_11_shipped_file_loads_with_design_values() -> None:
    """UT06-11 the shipped file loads and equals the §9 defaults."""
    cfg = _cfg()
    assert cfg == PipelinesConfig()
    assert cfg.version == 1
    assert cfg.swarm.oversubscribe == 1.5
    assert cfg.swarm.skeptic.min_weeks_seasonality == 13
    assert cfg.swarm.crosscheck.abs_tol_ratio == 0.001
    assert set(cfg.depth) == {"fast", "standard", "deep"}
    assert cfg.depth["deep"].large_stage is True
    assert cfg.depth["standard"].large_stage is False
    assert cfg.depth["standard"].k_samples == KSamples(default=1, on_reject=3)
    assert cfg.pipelines.funding_review.portfolio_scenario == "base"
    assert cfg.pipelines.org_review.org_specialties["deep"] == ["ops", "change", "delivery"]
    assert cfg.pipelines.chat.budget.wall_clock_s == 120
    assert cfg.pipelines.chat.escalation["run_tokens"] == 400_000
    assert cfg.hybrid.max_cost_usd_per_run == Decimal(15)


def test_ut06_11_unknown_key_reports_path() -> None:
    """UT06-11 an unknown key is rejected with its key path (TH06-15)."""
    raw = _raw()
    raw["swarm"]["dedup"]["bogus"] = 1
    with pytest.raises(ValidationError) as exc:
        PipelinesConfig.model_validate(raw)
    assert ("swarm", "dedup", "bogus") in _error_locs(exc)

    raw = _raw()
    raw["depth"]["fast"]["surprise"] = True
    with pytest.raises(ValidationError) as exc:
        PipelinesConfig.model_validate(raw)
    assert ("depth", "fast", "surprise") in _error_locs(exc)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("version",), 2),
        (("swarm", "max_task_attempts"), 11),
        (("swarm", "oversubscribe"), 0.5),
        (("swarm", "child_budget_factor"), 0),
        (("swarm", "writer_reserve"), 1.0),
        (("swarm", "verifier_batch"), "20"),
        (("depth", "fast", "skeptic_rounds"), 6),
        (("depth", "fast", "max_spawn_depth"), 3),
        (("depth", "fast", "k_samples"), 10),
        (("depth", "fast", "k_samples"), {"reject": 0}),
        (("depth", "fast", "k_samples"), {"other": 3}),
        (("depth", "fast", "k_samples"), True),
        (("depth", "fast", "analyst_budget", "max_steps"), 0),
        (("pipelines", "funding_review", "portfolio_scenario"), ""),
        (("pipelines", "funding_review", "K_candidates"), {"fast": 1, "standard": 2}),
        (("pipelines", "org_review", "org_specialties", "fast"), []),
        (("pipelines", "chat", "escalation"), {"analyst_budget": 1}),
        (("pipelines", "chat", "escalation"), {"max_tasks_per_run": -1}),
        (("hybrid", "max_input_tokens_per_call"), 9_999),
        (("hybrid", "max_cost_usd_per_run"), -1),
    ],
)
def test_ut06_11_bound_violations_rejected(path: tuple[str, ...], value: object) -> None:
    """UT06-11 values outside the §9 bounds fail validation."""
    raw = _raw()
    node = raw
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    with pytest.raises(ValidationError):
        PipelinesConfig.model_validate(raw)


def test_ut06_11_depth_needs_all_three_keys() -> None:
    """UT06-11 `depth` must name exactly fast, standard and deep."""
    raw = _raw()
    del raw["depth"]["deep"]
    with pytest.raises(ValidationError, match="depth must have exactly"):
        PipelinesConfig.model_validate(raw)
    raw = _raw()
    raw["depth"]["turbo"] = raw["depth"]["fast"]
    with pytest.raises(ValidationError):
        PipelinesConfig.model_validate(raw)


def test_ut06_11_settings_import_loads_no_other_harness_module() -> None:
    """UT06-11 importing the settings leaf loads no other `herness.harness` module (D06-34)."""
    code = (
        "import sys, herness.harness.pipelines.settings\n"
        "extra = sorted(m for m in sys.modules if m.startswith('herness.harness') and m not in "
        "{'herness.harness', 'herness.harness.pipelines', 'herness.harness.pipelines.settings'})\n"
        "print(','.join(extra))\n"
    )
    out = subprocess.run(  # noqa: S603 - fixed interpreter and literal code
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=ROOT
    )
    assert out.stdout.strip() == ""


def test_ut06_11_package_lazy_getattr_unknown_name() -> None:
    """UT06-11 the package re-exports lazily; an unknown name raises AttributeError."""
    import herness.harness.pipelines as pkg  # noqa: PLC0415 - exercised on purpose

    with pytest.raises(AttributeError, match="no attribute 'Nope'"):
        _ = pkg.Nope


def test_ut06_11_package_lazy_getattr_mapped_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT06-11 a mapped name is imported from its owner on first access and then cached."""
    import herness.harness.pipelines as pkg  # noqa: PLC0415 - exercised on purpose

    monkeypatch.setitem(pkg._EXPORTS, "PipelinesConfig", "herness.harness.pipelines.settings")
    try:
        assert pkg.PipelinesConfig is PipelinesConfig
        assert "PipelinesConfig" in vars(pkg)
        assert "PipelinesConfig" in dir(pkg)
    finally:
        vars(pkg).pop("PipelinesConfig", None)


# --- UT06-12 -------------------------------------------------------------------------------


def _depth_raw(k_samples: object) -> dict[str, Any]:
    raw = dict(_raw()["depth"]["standard"])
    raw["k_samples"] = k_samples
    return raw


def test_ut06_12_int_k_samples_normalizes_to_pair() -> None:
    """UT06-12 `k_samples: 3` becomes (3, 3)."""
    k = DepthConfig.model_validate(_depth_raw(3)).k_samples
    assert (k.default, k.on_reject) == (3, 3)


def test_ut06_12_reject_k_samples_normalizes_to_pair() -> None:
    """UT06-12 `{reject: 3}` becomes (1, 3)."""
    k = DepthConfig.model_validate(_depth_raw({"reject": 3})).k_samples
    assert (k.default, k.on_reject) == (1, 3)


def test_ut06_12_k_samples_bounds() -> None:
    """UT06-12 KSamples values are 1-9 and `on_reject >= default`."""
    with pytest.raises(ValidationError, match="on_reject"):
        KSamples(default=3, on_reject=2)
    with pytest.raises(ValidationError):
        KSamples(default=0, on_reject=1)
    with pytest.raises(ValidationError):
        DepthConfig.model_validate(_depth_raw({"reject": 10}))
    assert KSamples(default=9, on_reject=9).on_reject == 9


def test_ut06_12_depth_knobs_frozen_with_defaults() -> None:
    """UT06-12 DepthKnobs is frozen and has the U06-23 defaults."""
    knobs = resolve_knobs(_cfg(), "funding_review", "fast")
    assert knobs.large_stage is False
    assert knobs.K_teams == 0
    assert knobs.org_specialties == []
    with pytest.raises(ValidationError):
        knobs.max_tasks_per_run = 1  # type: ignore[misc]


# --- UT06-13 -------------------------------------------------------------------------------


def test_ut06_13_standard_funding_with_override() -> None:
    """UT06-13 standard funding + `{"max_tasks_per_run": 60}` gives 60 and `K_candidates` 25."""
    knobs = resolve_knobs(_cfg(), "funding_review", "standard", override={"max_tasks_per_run": 60})
    assert isinstance(knobs, DepthKnobs)
    assert knobs.max_tasks_per_run == 60
    assert knobs.K_candidates == 25
    assert knobs.K_clusters == 10
    assert knobs.M_must == 10
    assert knobs.H_wildcards == 3
    assert knobs.window_days == 365
    assert knobs.run_tokens == 6_000_000
    assert knobs.k_samples == KSamples(default=1, on_reject=3)
    assert knobs.analyst_budget == TaskBudget(
        max_steps=20, max_tokens=80_000, wall_clock_s=600, max_cost_usd=Decimal(0)
    )
    assert knobs.K_teams == 0


def test_ut06_13_org_deep_keys() -> None:
    """UT06-13 org_review deep takes its per-depth keys and `large_stage`."""
    knobs = resolve_knobs(_cfg(), "org_review", "deep")
    assert knobs.K_teams == 60
    assert knobs.K_candidates == 0
    assert knobs.org_specialties == ["ops", "change", "delivery"]
    assert knobs.window_days == 180
    assert knobs.large_stage is True
    assert knobs.k_samples == KSamples(default=5, on_reject=5)


def test_ut06_13_analyst_budget_override_rejected() -> None:
    """UT06-13 override key `analyst_budget` → ConfigError."""
    with pytest.raises(ConfigError, match="unknown budget_override key: analyst_budget"):
        resolve_knobs(_cfg(), "funding_review", "standard", override={"analyst_budget": 1})


def test_ut06_13_chat_escalation_resolves() -> None:
    """UT06-13 the shipped chat escalation map is a valid override."""
    cfg = _cfg()
    knobs = resolve_knobs(cfg, "funding_review", "fast", override=cfg.pipelines.chat.escalation)
    assert (knobs.max_tasks_per_run, knobs.skeptic_rounds) == (8, 1)
    assert (knobs.skeptic_top_n, knobs.run_tokens) == (3, 400_000)


def test_ut06_13_override_producing_invalid_knobs() -> None:
    """UT06-13 an allowed key whose value breaks a DepthKnobs bound → ConfigError."""
    with pytest.raises(ConfigError, match="max_tasks_per_run"):
        resolve_knobs(_cfg(), "funding_review", "fast", override={"max_tasks_per_run": 0})


# --- ST06-15 -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key", ["analyst_budget", "k_samples", "org_specialties", "large_stage", "nope", "window"]
)
def test_st06_15_structural_or_unknown_override_key(key: str) -> None:
    """ST06-15 structural and unknown override keys → ConfigError (TH06-15)."""
    with pytest.raises(ConfigError, match=f"unknown budget_override key: {key}"):
        resolve_knobs(_cfg(), "org_review", "standard", override={key: 1})


@pytest.mark.parametrize("value", [-1, True, 1.5, "3"])
def test_st06_15_override_value_must_be_non_negative_int(value: object) -> None:
    """ST06-15 a non-int or negative override value → ConfigError."""
    with pytest.raises(ConfigError, match="budget_override skeptic_rounds must be a non-negative"):
        resolve_knobs(
            _cfg(),
            "funding_review",
            "deep",
            override={"skeptic_rounds": value},  # type: ignore[dict-item]
        )
