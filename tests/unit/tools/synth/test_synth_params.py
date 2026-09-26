"""Tests for tools.synth.params (U11-01, U11-02): UT11-01..UT11-03 and PT11-04."""

import dataclasses
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from herness.core.errors import ConfigError
from tools.synth import GENERATOR_VERSION
from tools.synth.params import (
    SCALE_PRESETS,
    ScalePreset,
    SynthParams,
    SynthUsageError,
    load_params,
    params_hash,
)

pytestmark = pytest.mark.unit

_ALL_SOURCES = ("servicenow", "jira", "monitoring", "files")
_START = date(2023, 9, 1)
_END = date(2026, 8, 31)


def _load(scale: str = "small", params_file: Path | None = None, **kw: Any) -> SynthParams:
    args: dict[str, Any] = {
        "start": _START,
        "end": _END,
        "sources": _ALL_SOURCES,
        "dirty": "default",
        "fetch_mode": "initial",
        "params_file": params_file,
    }
    args.update(kw)
    return load_params(scale, **args)


def _write(tmp_path: Path, text: str, name: str = "params.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# --- UT11-01 ---------------------------------------------------------------------------


def test_ut11_01_scale_presets_match_design() -> None:
    """UT11-01 SCALE_PRESETS equals design §5.1.1; small and full share the catalog class."""
    assert set(SCALE_PRESETS) == {"tiny", "small", "full"}
    assert "5m" not in SCALE_PRESETS
    expected = {
        "tiny": (3, 12, 20, 1_200, 250, 40, 400, 150, 20, 90, "tiny"),
        "small": (12, 150, 400, 100_000, 10_000, 1_500, 40_000, 4_000, 400, None, "standard"),
        "full": (
            12,
            150,
            400,
            5_000_000,
            500_000,
            60_000,
            2_000_000,
            200_000,
            400,
            None,
            "standard",
        ),
    }
    for name, values in expected.items():
        preset = SCALE_PRESETS[name]
        assert isinstance(preset, ScalePreset)
        assert preset.name == name
        got = dataclasses.astuple(preset)[1:]
        assert got == values
    assert SCALE_PRESETS["small"].catalog_class == SCALE_PRESETS["full"].catalog_class
    with pytest.raises(dataclasses.FrozenInstanceError):
        SCALE_PRESETS["tiny"].incidents = 1  # type: ignore[misc]


# --- UT11-02 ---------------------------------------------------------------------------


def test_ut11_02_5m_alias_normalizes_to_full() -> None:
    """UT11-02 load_params("5m") yields scale full with the full preset."""
    p = _load("5m")
    assert p.scale == "full"
    assert p.preset == SCALE_PRESETS["full"]
    assert (p.start, p.end) == (_START, _END)


def test_ut11_02_tiny_span_ignores_start() -> None:
    """UT11-02 tiny start = end - 89 days; the --start value is ignored."""
    p = _load("tiny", start=date(2020, 1, 1), end=_END)
    assert p.scale == "tiny"
    assert p.end == _END
    assert p.start == _END - timedelta(days=89)
    assert p.preset.span_days == 90


def test_ut11_02_defaults_equal_design() -> None:
    """UT11-02 defaults equal design §5.1.3, §5.1.4 and §5.1.6 plus delta DD11-05."""
    assert GENERATOR_VERSION == "2.0.0"  # fixtures regenerate when this or a default changes
    p = _load()
    assert p.sources == _ALL_SOURCES
    assert (p.dirty, p.fetch_mode, p.business_timezone) == ("default", "initial", "UTC")
    assert (p.org.teams_per_org_mean, p.org.teams_per_org_min, p.org.teams_per_org_max) == (
        12.5,
        6,
        20,
    )
    assert p.org.criticality == {1: 0.10, 2: 0.25, 3: 0.40, 4: 0.25}
    assert (p.incident.pareto_alpha, p.incident.top_service_share) == (1.2, 0.05)
    assert (p.incident.top_share_target, p.incident.criticality1_weight) == (0.40, 1.5)
    assert p.priority.shares == {1: 0.01, 2: 0.06, 3: 0.38, 4: 0.50, 5: 0.05}
    assert p.priority.criticality1_high_multiplier == 2.0
    assert (p.arrival.saturday_factor, p.arrival.sunday_factor) == (0.35, 0.30)
    assert max(p.arrival.hour_curve) == 2.2
    assert min(p.arrival.hour_curve) == 0.3
    assert p.arrival.hour_curve[10:16] == (2.2,) * 6
    assert (p.arrival.annual_amplitude, len(p.arrival.holidays)) == (0.1, 10)
    assert p.ack.share == 0.85
    assert (p.ack.median_minutes[1], p.ack.median_minutes[3]) == (5.0, 45.0)
    assert p.mttr.median_hours == {1: 3.0, 2: 8.0, 3: 30.0, 4: 72.0, 5: 240.0}
    assert (p.mttr.sigma, p.mttr.team_multiplier_sigma) == (0.9, 0.2)
    assert (p.reassign.base_mean, p.reassign.extra_mean, p.reassign.extra_threshold) == (
        0.6,
        0.8,
        1.3,
    )
    assert (p.reassign.reopen_rate, p.reassign.reopen_rate_p4_p5) == (0.04, 0.06)
    assert p.sla.limit_hours == {1: 4.0, 2: 12.0, 3: 72.0, 4: 168.0, 5: 720.0}
    assert (p.impact.share, p.impact.min_factor, p.impact.max_factor) == (0.70, 0.3, 1.0)
    assert p.change.types == {"standard": 0.60, "normal": 0.35, "emergency": 0.05}
    assert p.change.close_codes == {
        "successful": 0.90,
        "successful_with_issues": 0.05,
        "unsuccessful": 0.03,
        "backed_out": 0.02,
    }
    assert p.change.emergency_failure_multiplier == 3.0
    assert (p.problem.incidents_per_problem, p.problem.known_error_rate) == (80, 0.40)
    assert p.event.severities == {
        "critical": 0.05,
        "major": 0.15,
        "minor": 0.30,
        "warning": 0.35,
        "info": 0.15,
    }
    assert (p.event.near_incident_share, p.event.near_incident_ref_share) == (0.50, 0.90)
    assert (p.metric_daily.availability_min, p.metric_daily.availability_max) == (99.5, 99.99)
    assert p.jira.issue_types["initiative"] == 0.01
    assert p.jira.issue_types["epic"] == 0.05
    assert p.jira.issue_types["feature"] == 0.14
    story_like = ("story", "bug", "task")
    assert sum(p.jira.issue_types[k] for k in story_like) == pytest.approx(0.80)
    assert p.jira.story_points == {1: 0.15, 2: 0.25, 3: 0.25, 5: 0.20, 8: 0.10, 13: 0.05}
    assert (p.jira.cycle_time_median_days, p.jira.reenter_in_progress_rate) == (6.0, 0.12)
    assert (p.jira.carry_over_rate, p.jira.ticket_mention_rate) == (0.15, 0.03)
    assert (p.text.typo_rate, p.text.spanish_share, p.text.slot_variation) == (0.01, 0.05, 0.20)
    assert (p.pii.incident_share, p.pii.jira_share) == (0.03, 0.01)
    assert (p.pii.spans_min, p.pii.spans_max, p.pii.person_names) == (1, 3, 500)
    d = p.dirty_rates
    assert (d.bad_timestamp, d.future_ts, d.resolved_before_opened) == (0.002, 0.0002, 0.0005)
    assert (d.missing_service, d.missing_component, d.duplicate_rows) == (0.08, 0.25, 0.01)
    assert (d.later_versions, d.tombstones, d.unknown_enum) == (0.05, 0.003, 0.001)
    assert d.heavy_multiplier == 5.0


def test_ut11_02_params_hash_format_and_sensitivity(tmp_path: Path) -> None:
    """UT11-02 params_hash is sha256:<64 hex>, stable, and changes with any value."""
    base = params_hash(_load())
    assert base.startswith("sha256:")
    assert len(base) == len("sha256:") + 64
    assert params_hash(_load()) == base
    changed = _load(params_file=_write(tmp_path, "text: {typo_rate: 0.02}\n"))
    assert changed.text.typo_rate == 0.02
    assert params_hash(changed) != base
    assert params_hash(_load("full")) != base


def test_ut11_02_params_file_merges_maps_and_replaces_lists(tmp_path: Path) -> None:
    """UT11-02 maps merge over the defaults; lists replace."""
    text = "mttr:\n  median_hours: {1: 2}\narrival:\n  holidays: ['12-25']\n"
    p = _load(params_file=_write(tmp_path, text))
    assert p.mttr.median_hours == {1: 2.0, 2: 8.0, 3: 30.0, 4: 72.0, 5: 240.0}
    assert p.arrival.holidays == ("12-25",)
    assert p.mttr.sigma == 0.9


def test_ut11_02_sources_normalized() -> None:
    """UT11-02 sources are de-duplicated into the canonical order."""
    p = _load(sources=("files", "jira", "files"))
    assert p.sources == ("jira", "files")


# --- UT11-03 ---------------------------------------------------------------------------


def test_ut11_03_daily_with_full_is_rejected() -> None:
    """UT11-03 fetch_mode daily with full (also via 5m) raises SynthUsageError."""
    for scale in ("full", "5m"):
        with pytest.raises(SynthUsageError, match="fetch_mode"):
            _load(scale, fetch_mode="daily")
    assert _load("small", fetch_mode="daily").fetch_mode == "daily"


def test_ut11_03_unknown_key_names_path(tmp_path: Path) -> None:
    """UT11-03 an unknown key raises SynthUsageError naming its key path."""
    path = _write(tmp_path, "priority:\n  sharez: {1: 1.0}\n")
    with pytest.raises(SynthUsageError, match=r"priority\.sharez") as info:
        _load(params_file=path)
    assert isinstance(info.value, ConfigError)
    assert info.value.context["key"] == "priority.sharez"
    with pytest.raises(SynthUsageError, match="bogus_group"):
        _load(params_file=_write(tmp_path, "bogus_group: 1\n", "b.yaml"))


def test_ut11_03_oversized_file_rejected(tmp_path: Path) -> None:
    """UT11-03 a 300 KB params file raises SynthUsageError before parsing."""
    path = _write(tmp_path, "# " + "x" * (300 * 1024) + "\n")
    with pytest.raises(SynthUsageError, match="params_file"):
        _load(params_file=path)


def test_ut11_03_probabilities_must_sum_to_one(tmp_path: Path) -> None:
    """UT11-03 probabilities summing to 0.9 raise SynthUsageError naming the key."""
    text = "priority:\n  shares: {1: 0.01, 2: 0.06, 3: 0.28, 4: 0.50, 5: 0.05}\n"
    with pytest.raises(SynthUsageError, match=r"priority\.shares"):
        _load(params_file=_write(tmp_path, text))
    sev = "event:\n  severities: {critical: 0.0}\n"
    with pytest.raises(SynthUsageError, match=r"event\.severities"):
        _load(params_file=_write(tmp_path, sev, "sev.yaml"))


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("text:\n  typo_rate: 1.5\n", "text.typo_rate"),
        ("dirty_rates:\n  tombstones: -0.1\n", "dirty_rates.tombstones"),
        ("- 1\n- 2\n", "params_file"),
        ("text: [1, 2]\n", "text"),
        ("a: [\n", "params_file"),
        ("scale: tiny\n", "scale"),
        ("business_timezone: Mars/Olympus\n", "business_timezone"),
        ("arrival:\n  hour_curve: [1.0, 2.0]\n", "arrival.hour_curve"),
        ("pii:\n  spans_min: 4\n", "pii"),
    ],
)
def test_ut11_03_bad_values_name_the_key(tmp_path: Path, text: str, key: str) -> None:
    """UT11-03 bad values, non-mapping and malformed YAML raise SynthUsageError with the key."""
    with pytest.raises(SynthUsageError) as info:
        _load(params_file=_write(tmp_path, text))
    assert info.value.context["key"] == key
    assert key in info.value.message


@pytest.mark.parametrize(
    ("scale", "kw", "key"),
    [
        ("huge", {}, "scale"),
        ("small", {"start": _END, "end": _START}, "start"),
        ("small", {"sources": ()}, "sources"),
        ("small", {"sources": ("servicenow", "email")}, "sources"),
        ("small", {"dirty": "extreme"}, "dirty"),
        ("small", {"fetch_mode": "hourly"}, "fetch_mode"),
    ],
)
def test_ut11_03_bad_arguments_rejected(scale: str, kw: dict[str, Any], key: str) -> None:
    """UT11-03 bad arguments raise SynthUsageError naming the argument."""
    with pytest.raises(SynthUsageError) as info:
        _load(scale, **kw)
    assert info.value.context["key"] == key


def test_ut11_03_missing_params_file(tmp_path: Path) -> None:
    """UT11-03 a missing params file raises SynthUsageError."""
    with pytest.raises(SynthUsageError, match="params_file"):
        _load(params_file=tmp_path / "missing.yaml")


def test_ut11_03_yaml_alias_bomb_hits_node_cap(tmp_path: Path) -> None:
    """UT11-03 an alias-expansion bomb raises SynthUsageError instead of exhausting memory."""
    lines = ["a0: &a0 [x, x, x, x, x, x, x, x, x, x]"]
    for i in range(1, 9):
        refs = ", ".join([f"*a{i - 1}"] * 10)
        lines.append(f"a{i}: &a{i} [{refs}]")
    with pytest.raises(SynthUsageError, match="params_file") as info:
        _load(params_file=_write(tmp_path, "\n".join(lines) + "\n"))
    assert info.value.context["key"] == "params_file"


@pytest.mark.parametrize("text", ["a: &a [*a]\n", "a: &a {b: *a}\n"])
def test_ut11_03_self_referential_alias_rejected(tmp_path: Path, text: str) -> None:
    """UT11-03 a self-referential YAML alias raises SynthUsageError, not RecursionError."""
    with pytest.raises(SynthUsageError, match="params_file") as info:
        _load(params_file=_write(tmp_path, text))
    assert info.value.context["key"] == "params_file"


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("event:\n  severities: {critical: 0.0, critcal: 0.05}\n", "event.severities"),
        ("jira:\n  story_points: {4: 0.15, 1: 0.0}\n", "jira.story_points"),
        ("text:\n  typo_rate: true\n", "text.typo_rate"),
        ("mttr:\n  sigma: .inf\n", "mttr.sigma"),
        ("pii:\n  person_names: '500'\n", "pii.person_names"),
        ("mttr:\n  median_hours: {1: 2.0, '1': 3.0}\n", "mttr.median_hours.1"),
    ],
)
def test_ut11_03_strict_values_and_fixed_keys(tmp_path: Path, text: str, key: str) -> None:
    """UT11-03 unknown map keys, bool/str/inf numbers and colliding keys name the key path."""
    with pytest.raises(SynthUsageError) as info:
        _load(params_file=_write(tmp_path, text))
    assert info.value.context["key"].startswith(key)


# --- PT11-04 ---------------------------------------------------------------------------

_RATE_KEYS = [
    ("text", "typo_rate"),
    ("text", "spanish_share"),
    ("pii", "incident_share"),
    ("problem", "known_error_rate"),
    ("reassign", "reopen_rate"),
    ("ack", "share"),
    ("dirty_rates", "tombstones"),
    ("event", "near_incident_share"),
]
_rates = st.floats(min_value=0.0, max_value=1.0, allow_nan=False)


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    overrides=st.dictionaries(st.sampled_from(_RATE_KEYS), _rates, min_size=1),
    order=st.randoms(use_true_random=False),
    style=st.sampled_from([None, True, False]),
    indent=st.integers(min_value=2, max_value=6),
)
def test_pt11_04_params_hash_invariant_to_key_order_and_yaml_format(
    tmp_path: Path, overrides: dict[tuple[str, str], float], order: Any, style: Any, indent: int
) -> None:
    """PT11-04 params_hash ignores key order and YAML formatting of the params file."""
    nested: dict[str, dict[str, float]] = {}
    for (group, key), value in overrides.items():
        nested.setdefault(group, {})[key] = value
    shuffled_groups = list(nested.items())
    order.shuffle(shuffled_groups)
    shuffled = {}
    for group, values in shuffled_groups:
        items = list(values.items())
        order.shuffle(items)
        shuffled[group] = dict(items)
    a = _write(tmp_path, yaml.safe_dump(nested, sort_keys=True), "a.yaml")
    b = _write(
        tmp_path,
        yaml.safe_dump(shuffled, sort_keys=False, default_flow_style=style, indent=indent),
        "b.yaml",
    )
    c = _write(tmp_path, json.dumps(shuffled, indent=indent), "c.yaml")
    hashes = {params_hash(_load(params_file=path)) for path in (a, b, c)}
    assert len(hashes) == 1
