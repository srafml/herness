"""Tests for herness.core.config.load_config and HernessConfig (impl 10 U10-01, U10-08, U10-09)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.support.config_tree import write_full_config
from tests.unit.connectors.test_settings_config import EXAMPLE

from herness.core import config as c
from herness.core import config_sources as cs
from herness.core.config_validate import reset_owner_validators
from herness.core.errors import ConfigError

pytestmark = pytest.mark.unit

SENTINEL = "SENTINEL-9f3a"


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    yield
    c.reset_config()  # local reset until T11-40 wires reset_config into tests/conftest.py (R3)
    reset_owner_validators()  # reset_core (impl 10 §11): owner registrations


@pytest.fixture
def cfg_dir(tmp_path: Path) -> Path:
    return write_full_config(tmp_path)


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _append(path: Path, text: str) -> None:
    _write(path, path.read_text(encoding="utf-8") + text)


def _load(cfg_dir: Path, profile: c.ProfileName | None = None, **kw: object) -> c.HernessConfig:
    env = kw.pop("env", {})
    return c.load_config(profile, config_dir=cfg_dir, env=env, **kw)  # type: ignore[arg-type]


# --- U10-01, U10-08 ------------------------------------------------------------------------


def test_ut10_01_profile_names_and_reexports() -> None:
    """UT10-01 ProfileName, GATED_PROFILES and the re-exports come from one declaration."""
    assert c.ProfileName is cs.ProfileName
    assert c.SDK_SOURCE_KINDS is cs.SDK_SOURCE_KINDS
    assert frozenset({"hybrid", "premium"}) == c.GATED_PROFILES
    assert c.SecurityConfig.__module__ == "herness.core.settings"


def test_ut10_01_root_model_needs_load_context() -> None:
    """UT10-01 constructing HernessConfig outside load_config is a ConfigError."""
    for build in (c.HernessConfig, lambda: c.HernessConfig.model_validate({})):
        with pytest.raises(ConfigError, match=r"^HernessConfig must be built by load_config$"):
            build()


def test_ut10_01_file_value_loses_to_profile_and_paths_are_absolute(cfg_dir: Path) -> None:
    """UT10-01 key in herness.yaml and profiles/local.yaml: the profile wins; paths absolute."""
    _append(cfg_dir / "herness.yaml", "retention: {traces_days: 30}\n")
    _write(cfg_dir / "profiles" / "local.yaml", "version: 1\nretention: {traces_days: 45}\n")
    cfg = _load(cfg_dir)
    assert cfg.retention.traces_days == 45
    assert cfg.retention.chat_days == 180
    base = cfg_dir.resolve().parent
    assert cfg.paths.data == base / "data"
    assert cfg.paths.logs == base / "data" / "logs"
    assert cfg.paths.backup_target == base / "backup"
    assert all(p.is_absolute() for _, p in cfg.paths)


def test_ut10_01_absolute_paths_kept_and_model_frozen(cfg_dir: Path, tmp_path: Path) -> None:
    """UT10-01 absolute paths stay as written; the root model and its sections are frozen."""
    target = (tmp_path / "elsewhere").resolve()
    _append(cfg_dir / "profiles" / "local.yaml", f"paths: {{data: '{target.as_posix()}'}}\n")
    cfg = _load(cfg_dir)
    assert cfg.paths.data == target
    with pytest.raises(ValueError, match="frozen"):
        cfg.profile = "hybrid"  # type: ignore[misc]
    with pytest.raises(ValueError, match="frozen"):
        cfg.logging.level = "DEBUG"  # type: ignore[misc]


def test_ut10_02_env_beats_profile(cfg_dir: Path) -> None:
    """UT10-02 HERNESS_LOGGING__LEVEL=DEBUG wins over the file and profile layers."""
    _write(cfg_dir / "profiles" / "local.yaml", "version: 1\nlogging: {level: WARNING}\n")
    assert _load(cfg_dir).logging.level == "WARNING"
    assert _load(cfg_dir, env={"HERNESS_LOGGING__LEVEL": "DEBUG"}).logging.level == "DEBUG"


def test_ut10_03_cli_beats_env(cfg_dir: Path) -> None:
    """UT10-03 --set logging.level=ERROR wins over the environment."""
    env = {"HERNESS_LOGGING__LEVEL": "DEBUG"}
    cfg = _load(cfg_dir, env=env, overrides=["logging.level=ERROR"])
    assert cfg.logging.level == "ERROR"


def test_ut10_04_maps_merge_lists_replace(cfg_dir: Path) -> None:
    """UT10-04 an overlay merges a map key by key and replaces a list."""
    _append(cfg_dir / "herness.yaml", "retention: {traces_days: 30, chat_days: 60}\n")
    _write(
        cfg_dir / "profiles" / "local.yaml",
        "version: 1\nretention: {traces_days: 45}\n"
        "security:\n  redaction: {extra_names: [Zed Q]}\n",
    )
    base = (cfg_dir / "herness.yaml").read_text(encoding="utf-8")
    names = "redaction: {directory_file: null, extra_names: [Ann Lee, Bo Chan]}"
    _write(cfg_dir / "herness.yaml", base.replace("redaction: {directory_file: null}", names))
    cfg = _load(cfg_dir)
    assert (cfg.retention.traces_days, cfg.retention.chat_days) == (45, 60)
    assert cfg.security.redaction.extra_names == ("Zed Q",)
    assert cfg.security.redaction.directory_file is None


def test_ut10_06_unknown_nested_field_names_path_not_value(cfg_dir: Path) -> None:
    """UT10-06 unknown nested field: ConfigError naming the key path; no value echoed."""
    _append(cfg_dir / "herness.yaml", f"retention: {{bogus_days: {SENTINEL!r}}}\n")
    with pytest.raises(ConfigError) as info:
        _load(cfg_dir)
    err = info.value
    assert "retention.bogus_days (herness.yaml): Extra inputs are not permitted" in err.message
    assert err.hint == "herness config validate"
    assert [(i.path, i.file) for i in err.issues] == [("retention.bogus_days", "herness.yaml")]  # type: ignore[attr-defined]
    assert SENTINEL not in str(err)
    assert SENTINEL not in repr(err.issues)
    assert err.__cause__ is None
    assert err.__suppress_context__


def test_ut10_06_invalid_values_never_echoed(cfg_dir: Path) -> None:
    """UT10-06 invalid values in many sections: at most 20 listed, inputs never echoed."""
    fields = ", ".join(f"f{i}: {SENTINEL}" for i in range(25))
    _append(cfg_dir / "herness.yaml", f"retention: {{{fields}}}\n")
    _write(cfg_dir / "mappings.yaml", f"version: 1\nenums: {SENTINEL}\n")
    with pytest.raises(ConfigError) as info:
        _load(cfg_dir, overrides=[f"bogus={SENTINEL}"])
    err = info.value
    assert len(err.issues) == 27
    assert err.message.startswith("invalid config (27 issues, first 20 listed): retention.")
    assert len(err.message) <= 1001  # HernessError bounds the message; issues keep all
    assert SENTINEL not in str(err)
    assert SENTINEL not in repr(err.issues)
    files = {i.file for i in err.issues}  # type: ignore[attr-defined]
    assert files == {"herness.yaml", "mappings.yaml", None}


def test_ut10_01_config_dir_and_profile_errors(tmp_path: Path) -> None:
    """UT10-01 missing config dir and unknown profile are ConfigErrors."""
    with pytest.raises(ConfigError, match=r"^config dir not found: "):
        c.load_config(config_dir=tmp_path / "nope", env={})
    cfg_dir = write_full_config(tmp_path)
    with pytest.raises(ConfigError, match=r"^unknown profile: cloud$"):
        c.load_config(config_dir=cfg_dir, env={"HERNESS_PROFILE": "cloud"})


def test_ut10_01_profile_from_env_and_os_environ(
    cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-01 profile comes from HERNESS_PROFILE; env defaults to os.environ."""
    assert _load(cfg_dir, env={"HERNESS_PROFILE": "premium"}).profile == "premium"
    monkeypatch.setenv("HERNESS_PROFILE", "synth")
    monkeypatch.setenv("HERNESS_LOGGING__LEVEL", "ERROR")
    cfg = c.load_config(config_dir=cfg_dir)
    assert (cfg.profile, cfg.logging.level) == ("synth", "ERROR")


# --- U10-09 steps 3, 5, 6 ------------------------------------------------------------------


def test_ut10_09_hybrid_with_gate(cfg_dir: Path) -> None:
    """UT10-09 gate set and --profile hybrid: profile hybrid, writer routed to claude-opus."""
    cfg = _load(cfg_dir, "hybrid")
    assert cfg.profile == "hybrid"
    assert cfg.models.models.roles["writer"] == "claude-opus"
    assert cfg.security.egress.destinations == ("api.anthropic.com",)
    assert _load(cfg_dir).models.models.roles["writer"] == "local-30b"


@pytest.mark.parametrize(
    "override",
    ["security.egress.enabled=true", "profile=hybrid", "security={}", "profile.x=1"],
)
def test_ut10_10_set_security_and_profile_are_file_only(cfg_dir: Path, override: str) -> None:
    """UT10-10 --set security.* or profile is a 'file-only' ConfigError naming the path."""
    with pytest.raises(ConfigError, match="file-only") as info:
        _load(cfg_dir, overrides=[override])
    key = override.split("=", maxsplit=1)[0]
    assert info.value.message == f"security.* and profile are file-only: {key}"


def test_ut10_10_env_security_is_file_only(cfg_dir: Path) -> None:
    """UT10-10 HERNESS_SECURITY__EGRESS__ENABLED=true through load_config: 'file-only'."""
    with pytest.raises(ConfigError, match="file-only"):
        _load(cfg_dir, env={"HERNESS_SECURITY__EGRESS__ENABLED": "true"})


_POLICY = "  data_policy: {hybrid_approved: true, premium_approved: true, approved_by: ops-lead,\n"


@pytest.mark.parametrize(
    ("profile", "policy"),
    [
        (
            "hybrid",
            "  data_policy: {premium_approved: true, approved_by: a, approved_on: 2026-09-01}",
        ),
        ("hybrid", "  data_policy: {hybrid_approved: true, approved_on: 2026-09-01}"),
        ("premium", "  data_policy: {premium_approved: true, approved_by: a}"),
        (
            "premium",
            "  data_policy: {hybrid_approved: true, approved_by: a, approved_on: 2026-09-01}",
        ),
        ("chat", "  data_policy: {chat_approved: true, approved_by: a}"),
    ],
)
def test_ut10_11_gate_requires_recorded_approval(cfg_dir: Path, profile: str, policy: str) -> None:
    """UT10-11 hybrid/premium without the flag or record, chat without record: ConfigError."""
    text = (cfg_dir / "herness.yaml").read_text(encoding="utf-8")
    start = text.index(_POLICY)
    end = text.index("\n", start + len(_POLICY)) + 1
    _write(cfg_dir / "herness.yaml", text[:start] + policy + "\n" + text[end:])
    name = "local" if profile == "chat" else profile
    with pytest.raises(ConfigError, match="requires recorded approval") as info:
        _load(cfg_dir, name)  # type: ignore[arg-type]
    assert info.value.message == (
        f"profile {profile} requires recorded approval in herness.yaml security.data_policy"
    )


def test_ut10_11_chat_approval_with_record_loads(cfg_dir: Path) -> None:
    """UT10-11 chat_approved with approved_by and approved_on loads."""
    text = (cfg_dir / "herness.yaml").read_text(encoding="utf-8")
    _write(cfg_dir / "herness.yaml", text.replace("hybrid_approved: true,", "chat_approved: true,"))
    assert _load(cfg_dir).security.data_policy.chat_approved is True


@pytest.mark.parametrize(
    "egress",
    ["{enabled: true}", "{destinations: [a.example.com]}", "{purposes: [reasoning]}"],
)
def test_ut10_12_local_forbids_egress(cfg_dir: Path, egress: str) -> None:
    """UT10-12 local with egress enabled in herness.yaml: ConfigError 'forbids egress'."""
    text = (cfg_dir / "herness.yaml").read_text(encoding="utf-8")
    _write(
        cfg_dir / "herness.yaml", text.replace("security:\n", f"security:\n  egress: {egress}\n")
    )
    with pytest.raises(ConfigError, match=r"^profile local forbids egress$"):
        _load(cfg_dir)
    with pytest.raises(ConfigError, match=r"^profile synth forbids egress$"):
        _load(cfg_dir, "synth")


def test_ut10_12_synth_overlay_enabling_egress(cfg_dir: Path) -> None:
    """UT10-12 a synth overlay enabling egress: ConfigError; local and synth default to none."""
    for profile in ("local", "synth"):
        egress = _load(cfg_dir, profile).security.egress  # type: ignore[arg-type]
        assert (egress.enabled, egress.destinations, egress.purposes) == (False, (), ())
    _write(
        cfg_dir / "profiles" / "synth.yaml", "version: 1\nsecurity:\n  egress: {enabled: true}\n"
    )
    with pytest.raises(ConfigError, match=r"^profile synth cannot enable egress$"):
        _load(cfg_dir, "synth")


# --- U10-08 composite files (R-69, R-76) and the shipped owner files ------------------------

# Design 01 §7 example (connector tests) plus the impl 02 sibling sections (R-69).
_SOURCES = EXAMPLE + "dq: {row_count_drop_max: 0.1}\nbuild: {threads: 8}\n"


def test_ut10_84_sibling_sections_composed(cfg_dir: Path) -> None:
    """UT10-84 sources.yaml dq/build and models.yaml deciders reach the owners' models."""
    _write(cfg_dir / "sources.yaml", _SOURCES)
    cfg = _load(cfg_dir)
    assert cfg.sources.dq.row_count_drop_max == 0.1
    assert cfg.sources.dq.cast_fail_warn == 0.005
    assert cfg.sources.build.threads == 8
    enabled = [name for name, _ in cfg.sources.enabled_sources()]
    assert enabled == ["servicenow", "jira", "monitoring", "files"]
    shown = c.effective_dict(cfg)["sources"]  # carry-over 7: dumping sources never re-validates
    assert shown["sources"]["jira"]["flavor"] == "cloud"
    assert shown["build"]["threads"] == 8
    assert cfg.models.models.roles["planner"] == "local-30b"
    assert cfg.models.harness.tools.max_parallel >= 1
    assert cfg.models.deciders.openjev.base_url.startswith("http://127.0.0.1")
    assert isinstance(cfg.sources, c.SourcesFileConfig)
    assert isinstance(cfg.models, c.ModelsFileConfig)


@pytest.mark.parametrize("stem", ["sources", "models", "decisions", "weights", "metrics"])
def test_ut10_84_unknown_top_level_key_names_file_and_key(cfg_dir: Path, stem: str) -> None:
    """UT10-84 an unknown top-level key in an owner file: ConfigError naming file and key."""
    _append(cfg_dir / f"{stem}.yaml", f"\nstray_key: {SENTINEL}\n")
    with pytest.raises(ConfigError) as info:
        _load(cfg_dir)
    assert f"{stem}.stray_key ({stem}.yaml)" in info.value.message
    assert SENTINEL not in str(info.value)


def test_ut10_84_owner_sections_are_closed(cfg_dir: Path) -> None:
    """UT10-84 the pipelines, memory and app owner models (T10-03b) reject unknown keys."""
    for stem in ("pipelines", "memory", "app"):
        _write(cfg_dir / f"{stem}.yaml", "version: 1\nanything: 1\n")
        with pytest.raises(ConfigError, match=rf"{stem}\.anything \({stem}\.yaml\)"):
            _load(cfg_dir)
        _write(cfg_dir / f"{stem}.yaml", "version: 1\n")
    assert _load(cfg_dir).memory.injection_patterns == ("ignore previous",)


def test_rf_shipped_owner_files_load_yaml_shaped(cfg_dir: Path) -> None:
    """RF carry-over 4/9: shipped decisions, eval, metrics, models, weights load (lists)."""
    cfg = _load(cfg_dir)
    assert cfg.decisions.escalation_chain == ("openjev", "llm")
    assert cfg.weights.business_timezone == "America/New_York"
    assert cfg.weights.version == 1
    assert cfg.metrics.version == 1
    assert cfg.metrics.metrics[0].name == "mttr_hours"
    assert cfg.eval is not None
    assert cfg.eval.baseline == "synthetic-42-small"
    assert cfg.resilience.resilience.jobs.lease_s > 0
    (cfg_dir / "eval.yaml").unlink()
    assert _load(cfg_dir).eval is None


def test_rf_owner_config_error_passes_through(cfg_dir: Path) -> None:
    """RF an owner validator's own ConfigError reaches the caller unchanged."""
    text = (cfg_dir / "models.yaml").read_text(encoding="utf-8")
    _write(
        cfg_dir / "models.yaml", text.replace("    writer: local-30b\n", "    writer: nobody\n", 1)
    )
    with pytest.raises(ConfigError, match=r"models\."):
        _load(cfg_dir)
