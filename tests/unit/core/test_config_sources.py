"""Tests for herness.core.config_sources (impl 10 U10-15 … U10-19, U10-21; T10-02)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.support.config_harness import Harness, load, write_config

from herness.core import config_sources as cs
from herness.core.errors import ConfigError
from herness.core.settings import SecurityConfig

pytestmark = pytest.mark.unit


@pytest.fixture
def cfg(tmp_path: Path) -> Path:
    return write_config(tmp_path)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --- U10-15 load_yaml_file -------------------------------------------------------------


def test_ut10_07_duplicate_key_names_line(tmp_path: Path) -> None:
    """UT10-07 a duplicate key at line 7 gives 'duplicate key' and ':7'."""
    text = "version: 1\na: 1\nb: 2\nc: 3\nd:\n  x: 1\n  x: 2\n"
    path = _write(tmp_path / "metrics.yaml", text)
    with pytest.raises(ConfigError) as info:
        cs.load_yaml_file(path)
    assert "duplicate key" in info.value.message
    assert ":7" in info.value.message
    assert "metrics.yaml" in info.value.message


def test_ut10_08_anchor_and_alias_rejected(tmp_path: Path) -> None:
    """UT10-08 an anchor and alias in metrics.yaml is rejected."""
    path = _write(tmp_path / "metrics.yaml", "version: 1\na: &x 1\nb: *x\n")
    with pytest.raises(ConfigError, match="anchors and aliases") as info:
        cs.load_yaml_file(path)
    assert info.value.message == "YAML anchors and aliases are not allowed: metrics.yaml:2"


def test_ut10_08_alias_without_anchor_rejected(tmp_path: Path) -> None:
    """UT10-08 a bare alias is an error too (anchor check never reached)."""
    path = _write(tmp_path / "metrics.yaml", "a: *x\n")
    with pytest.raises(ConfigError) as info:
        cs.load_yaml_file(path)
    assert info.value.message == "YAML anchors and aliases are not allowed: metrics.yaml:1"


def test_ut10_08_merge_keys_rejected(tmp_path: Path) -> None:
    """UT10-08 the << merge key is rejected with an alias and with an inline mapping."""
    aliased = _write(tmp_path / "a.yaml", "base: &b {x: 1}\nuse:\n  <<: *b\n")
    with pytest.raises(ConfigError, match=r"^YAML anchors and aliases are not allowed: a\.yaml:1$"):
        cs.load_yaml_file(aliased)
    inline = _write(tmp_path / "m.yaml", "use:\n  <<: {x: 1}\n  y: 2\n")
    with pytest.raises(ConfigError, match=r"^m\.yaml:[0-9]+: invalid YAML$"):
        cs.load_yaml_file(inline)


def test_ut10_07_missing_and_too_large(tmp_path: Path) -> None:
    """UT10-07 missing file and st_size cap give the U10-15 messages."""
    with pytest.raises(ConfigError, match=r"^config file missing: nope\.yaml$"):
        cs.load_yaml_file(tmp_path / "nope.yaml")
    path = _write(tmp_path / "big.yaml", "a: 1\n" * 10)
    with pytest.raises(ConfigError, match=r"^config file too large: big\.yaml$"):
        cs.load_yaml_file(path, max_bytes=10)


def test_ut10_07_bom_empty_and_shapes(tmp_path: Path) -> None:
    """UT10-07 BOM stripped; empty gives {}; non-mapping and bad YAML rejected."""
    bom = tmp_path / "bom.yaml"
    bom.write_bytes(b"\xef\xbb\xbfa: 1\n")
    assert cs.load_yaml_file(bom) == {"a": 1}
    assert cs.load_yaml_file(_write(tmp_path / "empty.yaml", "# only a comment\n")) == {}
    with pytest.raises(ConfigError, match=r"^list\.yaml: top level must be a mapping$"):
        cs.load_yaml_file(_write(tmp_path / "list.yaml", "- 1\n- 2\n"))
    with pytest.raises(ConfigError, match=r"^bad\.yaml:2: invalid YAML$"):
        cs.load_yaml_file(_write(tmp_path / "bad.yaml", "a: 1\nb: c: d\n"))
    latin = tmp_path / "latin.yaml"
    latin.write_bytes(b"a: \xff\n")
    with pytest.raises(ConfigError, match=r"latin\.yaml"):
        cs.load_yaml_file(latin)


def test_ut10_07_unhashable_key_is_invalid_yaml(tmp_path: Path) -> None:
    """UT10-07 a complex (unhashable) key becomes the invalid-YAML error."""
    path = _write(tmp_path / "k.yaml", "? [1, 2]\n: 3\n")
    with pytest.raises(ConfigError, match="invalid YAML"):
        cs.load_yaml_file(path)


# --- U10-19 parse_overrides ------------------------------------------------------------


def test_ut10_23_parse_overrides_rejections() -> None:
    """UT10-23 missing '=', descent into a scalar, bad segment, 13 segments: key path each."""
    with pytest.raises(ConfigError, match=r"^--set needs key=value: a\.b$"):
        cs.parse_overrides(["a.b"])
    with pytest.raises(ConfigError) as info:
        cs.parse_overrides(["a.b=2", "a.b.c=1"])
    assert info.value.message == "--set a.b.c: a.b is already set to a non-mapping value"
    with pytest.raises(ConfigError, match="non-mapping"):
        cs.parse_overrides(["a.b=[1]", "a.b.c=1"])
    with pytest.raises(ConfigError, match="bad seg"):
        cs.parse_overrides(["bad seg=1"])
    thirteen = ".".join(f"s{i}" for i in range(13))
    with pytest.raises(ConfigError, match=thirteen.replace(".", r"\.")):
        cs.parse_overrides([f"{thirteen}=1"])


def test_ut10_23_parse_overrides_values_and_limits() -> None:
    """UT10-23 YAML values, later override wins, empty segment, value and count limits."""
    got = cs.parse_overrides(
        ["logging.level=ERROR", "a.b=[1, 2]", "a.c={x: 1}", "a.b=3", "e=", "d.q=a=b"]
    )
    assert got == {
        "logging": {"level": "ERROR"},
        "a": {"b": 3, "c": {"x": 1}},
        "e": None,
        "d": {"q": "a=b"},
    }
    assert cs.parse_overrides(["a.b.c=1", "a.b=2"]) == {"a": {"b": 2}}
    assert cs.parse_overrides(["models.models.depth.default=3"]) == {
        "models": {"models": {"depth": {"default": 3}}}
    }
    with pytest.raises(ConfigError, match=r"a\.\.b"):
        cs.parse_overrides(["a..b=1"])
    with pytest.raises(ConfigError, match=r"^--set a: value too long$"):
        cs.parse_overrides(["a=" + "x" * 4097])
    with pytest.raises(ConfigError, match="100"):
        cs.parse_overrides([f"k{i}=1" for i in range(101)])
    long_key = "k" * 5000
    for item in (long_key, long_key + "=1"):
        with pytest.raises(ConfigError) as info:
            cs.parse_overrides([item])
        assert len(info.value.message) < 200


def test_ut10_23_deeply_nested_value_is_config_error() -> None:
    """UT10-23 a deeply nested --set value is a ConfigError, never RecursionError."""
    for text in ("[" * 2000 + "]" * 2000, "[" * 65 + "]" * 65):
        with pytest.raises(ConfigError) as info:
            cs.parse_overrides(["a=" + text])
        assert info.value.message == "--set a: invalid YAML value"
    assert cs.parse_overrides(["a=" + "[" * 10 + "]" * 10])["a"] == [[[[[[[[[[]]]]]]]]]]


def test_ut10_23_parse_overrides_bad_value_names_key_only() -> None:
    """UT10-23 an invalid, aliased or non-UTF-8-safe value names the key path, not the value."""
    for item in ("a.b=[1, 2", "a.b=&x 1", "a.b={k: 1, k: 2}"):
        with pytest.raises(ConfigError) as info:
            cs.parse_overrides([item])
        assert info.value.message == "--set a.b: invalid YAML value"


_SEG = st.from_regex(r"[A-Za-z0-9_-]{1,8}", fullmatch=True)
_SCALAR = st.one_of(
    st.integers(min_value=-(10**12), max_value=10**12),
    st.booleans(),
    st.none(),
    st.text(st.characters(codec="utf-8", categories=["L", "N", "Zs", "P"]), max_size=20),
)
_TREE = st.recursive(
    _SCALAR,
    lambda inner: st.dictionaries(_SEG, inner, min_size=1, max_size=4),
    max_leaves=12,
)


def _flatten(tree: dict[str, Any], prefix: str = "") -> list[str]:
    items: list[str] = []
    for key, value in tree.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            items += _flatten(value, path + ".")
        else:
            items.append(f"{path}={json.dumps(value, ensure_ascii=False)}")
    return items


@settings(max_examples=150, deadline=None)
@given(st.dictionaries(_SEG, _TREE, min_size=1, max_size=4))
def test_pt10_06_round_trip(tree: dict[str, Any]) -> None:
    """PT10-06 generated key paths and YAML scalars parse back to the same nested dict."""
    assert cs.parse_overrides(_flatten(tree)) == tree


# --- U10-16 FilesYamlSource ------------------------------------------------------------


def test_ut10_05_missing_sources_yaml(cfg: Path) -> None:
    """UT10-05 removing sources.yaml gives 'config file missing: sources.yaml'."""
    (cfg / "sources.yaml").unlink()
    with pytest.raises(ConfigError, match=r"^config file missing: sources\.yaml$"):
        load(cfg)


def test_ut10_05_missing_eval_yaml_gives_none(cfg: Path) -> None:
    """UT10-05 removing eval.yaml leaves cfg.eval None; present, it is loaded."""
    assert load(cfg).eval == {"golden": {}}
    (cfg / "eval.yaml").unlink()
    assert load(cfg).eval is None


def test_ut10_05_files_layout(cfg: Path) -> None:
    """UT10-05 sections per stem, version kept only for sources, injection patterns read."""
    got = load(cfg)
    assert got.sources == {"version": 1}
    assert got.metrics == {}
    assert got.logging == {"level": "INFO"}
    assert got.memory == {"injection_patterns": ["ignore previous", "system:"]}


@pytest.mark.parametrize(
    ("name", "text", "message"),
    [
        ("herness.yaml", "version: 2\n", "herness.yaml: version must be 1"),
        ("herness.yaml", "logging: {}\n", "herness.yaml: version must be 1"),
        ("herness.yaml", "version: 1\nsources: {}\n", "herness.yaml: unknown root key sources"),
        ("weights.yaml", "version: true\n", "weights.yaml: version must be 1"),
        ("eval.yaml", "version: 3\n", "eval.yaml: version must be 1"),
        ("extra.yaml", "version: 1\n", "unexpected config file extra.yaml"),
    ],
)
def test_ut10_05_file_rules(cfg: Path, name: str, text: str, message: str) -> None:
    """UT10-05 version, root-key and unexpected-file rules."""
    _write(cfg / name, text)
    with pytest.raises(ConfigError) as info:
        load(cfg)
    assert info.value.message == message


def test_ut10_06_unknown_keys_name_path_without_value(cfg: Path) -> None:
    """UT10-06 unknown root key and unknown overlay section: key path named, value not echoed."""
    _write(cfg / "herness.yaml", "version: 1\nbogus_root: SENTINEL-9f3a\n")
    with pytest.raises(ConfigError) as info:
        load(cfg)
    assert info.value.message == "herness.yaml: unknown root key bogus_root"
    _write(cfg / "herness.yaml", "version: 1\n")
    _write(cfg / "profiles" / "hybrid.yaml", "version: 1\nbogus: {x: SENTINEL-9f3a}\n")
    with pytest.raises(ConfigError) as info:
        load(cfg, "hybrid")
    assert info.value.message == "profiles/hybrid.yaml: unknown section bogus"
    assert "SENTINEL" not in str(info.value)


def test_ut10_05_injection_patterns_rules(cfg: Path) -> None:
    """UT10-05 injection_patterns.txt is required and capped at 1 MiB."""
    patterns = cfg / "injection_patterns.txt"
    patterns.write_bytes(b"x" * (1_048_576 + 1))
    with pytest.raises(ConfigError, match=r"too large: injection_patterns\.txt"):
        load(cfg)
    patterns.unlink()
    with pytest.raises(ConfigError, match=r"missing: injection_patterns\.txt"):
        load(cfg)


def test_ut10_05_sources_need_load_context() -> None:
    """UT10-05 a source used outside the load context raises ConfigError."""
    with pytest.raises(ConfigError, match="load context"):
        Harness()


# --- U10-17 ProfileYamlSource ----------------------------------------------------------


def test_rf_profile_overlay_merges_over_files(cfg: Path) -> None:
    """RF the overlay replaces lists and merges maps over the file layer."""
    _write(cfg / "models.yaml", "version: 1\nmodels:\n  roles: {writer: local, judge: j}\n")
    _write(
        cfg / "profiles" / "hybrid.yaml",
        "version: 1\nmodels:\n  models:\n    roles: {writer: claude-opus}\n"
        "security:\n  egress: {destinations: [api.anthropic.com]}\n",
    )
    got = load(cfg, "hybrid")
    assert got.models == {"models": {"roles": {"writer": "claude-opus", "judge": "j"}}}
    assert got.security.egress.destinations == ("api.anthropic.com",)


def test_ut10_13_overlay_sets_data_policy(cfg: Path) -> None:
    """UT10-13 an overlay setting security.data_policy.hybrid_approved is rejected."""
    _write(
        cfg / "profiles" / "hybrid.yaml",
        "version: 1\nsecurity:\n  data_policy:\n    hybrid_approved: true\n",
    )
    with pytest.raises(ConfigError, match=r"^profiles may not set security\.data_policy$"):
        load(cfg, "hybrid")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("version: 2\n", "profiles/hybrid.yaml: version must be 1"),
        ("version: 1\nprofile: local\n", "profiles/hybrid.yaml: unknown section profile"),
        ("version: 1\nsecurity: 3\n", "profiles/hybrid.yaml: security must be a mapping"),
    ],
)
def test_ut10_13_overlay_shape_rules(cfg: Path, text: str, message: str) -> None:
    """UT10-13 overlay version and section-name rules."""
    _write(cfg / "profiles" / "hybrid.yaml", text)
    with pytest.raises(ConfigError) as info:
        load(cfg, "hybrid")
    assert info.value.message == message


def test_ut10_13_overlay_missing(cfg: Path) -> None:
    """UT10-13 a missing profile file is a ConfigError."""
    (cfg / "profiles" / "premium.yaml").unlink()
    with pytest.raises(ConfigError, match=r"missing: premium\.yaml"):
        load(cfg, "premium")


@pytest.mark.parametrize(
    "egress",
    ["{enabled: true}", "{destinations: [a.example.com]}", "{purposes: [reasoning]}"],
)
def test_rf_synth_overlay_cannot_enable_egress(cfg: Path, egress: str) -> None:
    """RF a synth overlay enabling egress is rejected (U10-17 step 4)."""
    _write(cfg / "profiles" / "synth.yaml", f"version: 1\nsecurity:\n  egress: {egress}\n")
    with pytest.raises(ConfigError, match=r"^profile synth cannot enable egress$"):
        load(cfg, "synth")


def test_ut10_14_synth_config_fragment(cfg: Path, tmp_path: Path) -> None:
    """UT10-14 HERNESS_SYNTH_CONFIG: mappings merged; sources rejected; local rejected."""
    _write(cfg / "mappings.yaml", "version: 1\nfields: {a: 1, b: 2}\n")
    _write(cfg / "profiles" / "synth.yaml", "version: 1\nmappings:\n  fields: {b: 3}\n")
    frag = _write(tmp_path / "frag.yaml", "mappings:\n  fields: {c: 4}\n")
    env = {"HERNESS_SYNTH_CONFIG": str(frag)}
    assert load(cfg, "synth", env).mappings == {"fields": {"a": 1, "b": 3, "c": 4}}
    _write(frag, "mappings: {}\nsources: {}\n")
    with pytest.raises(ConfigError, match="only mappings"):
        load(cfg, "synth", env)
    _write(frag, "mappings: {}\n")
    with pytest.raises(ConfigError, match=r"^HERNESS_SYNTH_CONFIG requires profile synth$"):
        load(cfg, "local", env)


# --- U10-18 GuardedEnvSource, FilteredDotEnvSource ---------------------------------------


def test_ut10_10_env_security_is_file_only(cfg: Path) -> None:
    """UT10-10 HERNESS_SECURITY__EGRESS__ENABLED=true gives a 'file-only' ConfigError."""
    env = {"HERNESS_SECURITY__EGRESS__ENABLED": "true"}
    with pytest.raises(ConfigError, match="file-only") as info:
        load(cfg, env=env)
    assert info.value.message == "security.* is file-only; remove HERNESS_SECURITY__EGRESS__ENABLED"


def test_ut10_10_env_mixed_case_security_is_file_only(cfg: Path) -> None:
    """UT10-10 the file-only rule does not depend on the case of the name."""
    with pytest.raises(ConfigError, match="file-only"):
        load(cfg, env={"HERNESS_Security__egress__enabled": "true"})
    with pytest.raises(ConfigError, match="file-only"):
        load(cfg, env={"HERNESS_PROFILE__X": "1"})


def test_rf_env_layer_and_skips(cfg: Path) -> None:
    """RF env values parse as YAML; reserved and secret names are skipped."""
    env = {
        "HERNESS_LOGGING__LEVEL": "ERROR",
        "HERNESS_APP__LIMITS": "[1, 2]",
        "HERNESS_PROFILE": "hybrid",
        "HERNESS_ENV": "prod",
        "HERNESS_FAULTS": "x",
        "HERNESS_WORKER": "1",
        "HERNESS_SECRET__ANTHROPIC": "sk-not-a-real-key",
        "PATH": "/usr/bin",
    }
    got = load(cfg, env=env)
    assert got.logging == {"level": "ERROR"}
    assert got.app == {"limits": [1, 2]}
    assert got.profile == "local"


def test_rf_init_overrides_env(cfg: Path) -> None:
    """RF init (CLI) beats env, env beats files."""
    env = {"HERNESS_LOGGING__LEVEL": "WARNING"}
    over = cs.parse_overrides(["logging.level=ERROR"])
    assert load(cfg, env=env, **over).logging == {"level": "ERROR"}
    assert load(cfg, env=env).logging == {"level": "WARNING"}


def test_ut10_10_env_bad_value_and_too_many(cfg: Path) -> None:
    """UT10-10 a bad env value names the variable only; more than 500 variables rejected."""
    with pytest.raises(ConfigError) as info:
        load(cfg, env={"HERNESS_APP__X": "[1, 2"})
    assert info.value.message == "HERNESS_APP__X: invalid YAML value"
    many = {f"HERNESS_APP__K{i}": "1" for i in range(501)}
    with pytest.raises(ConfigError, match="500"):
        load(cfg, env=many)


def test_rf_dotenv_only_in_dev(cfg: Path) -> None:
    """RF .env is read only with HERNESS_ENV=dev; env beats .env."""
    _write(
        cfg.parent / ".env",
        '# dev settings\n\nHERNESS_LOGGING__LEVEL="DEBUG"\nHERNESS_APP__X=1\n'
        "HERNESS_SECRET__TOKEN=abc\nOTHER=1\n",
    )
    assert load(cfg).logging == {"level": "INFO"}
    got = load(cfg, env={"HERNESS_ENV": "dev"})
    assert got.logging == {"level": "DEBUG"}
    assert got.app == {"x": 1}
    got = load(cfg, env={"HERNESS_ENV": "dev", "HERNESS_APP__X": "2"})
    assert got.app == {"x": 2}


def test_rf_dotenv_missing_malformed_and_large(cfg: Path) -> None:
    """RF a missing .env is empty; malformed lines give the line number; 64 KiB cap."""
    dev = {"HERNESS_ENV": "dev"}
    assert load(cfg, env=dev).logging == {"level": "INFO"}
    dotenv = _write(cfg.parent / ".env", "A=1\nnot a line\n")
    with pytest.raises(ConfigError) as info:
        load(cfg, env=dev)
    assert info.value.message == ".env line 2: malformed"
    dotenv.write_bytes(b"A=1\n" * 20_000)
    with pytest.raises(ConfigError, match="too large"):
        load(cfg, env=dev)
    dotenv.write_bytes(b"A=\xff\n")
    with pytest.raises(ConfigError, match=r"\.env"):
        load(cfg, env=dev)


# --- U10-09 steps 1 and 6 helpers, U10-21 load_bootstrap --------------------------------


def test_ut10_24_resolve_profile() -> None:
    """UT10-24 profile: argument, else HERNESS_PROFILE, else local; unknown rejected."""
    assert cs.resolve_profile("premium", {"HERNESS_PROFILE": "hybrid"}) == "premium"
    assert cs.resolve_profile(None, {"HERNESS_PROFILE": "hybrid"}) == "hybrid"
    assert cs.resolve_profile(None, {}) == "local"
    with pytest.raises(ConfigError, match=r"^unknown profile: cloud$"):
        cs.resolve_profile(None, {"HERNESS_PROFILE": "cloud"})


def test_rf_check_profile_egress() -> None:
    """RF local and synth forbid egress; hybrid allows it."""
    on = SecurityConfig.model_validate({"egress": {"enabled": True}})
    dest = SecurityConfig.model_validate({"egress": {"destinations": ["a.example.com"]}})
    cs.check_profile_egress("hybrid", on)
    cs.check_profile_egress("local", SecurityConfig())
    for profile in ("local", "synth"):
        for sec in (on, dest):
            with pytest.raises(ConfigError, match=f"^profile {profile} forbids egress$"):
                cs.check_profile_egress(profile, sec)


_SOURCES_UT10_24 = """\
version: 1
sources:
  servicenow:
    base_url: https://ACME.service-now.com/api
    hosts: [SSO.example.com]
  jira:
    enabled: false
    base_url: https://jira.example.com
    hosts: [jira-sso.example.com]
  snowflake:
    base_url: https://evil.example.net
    hosts: [acme.snowflakecomputing.com]
  mongodb:
    hosts: [db.example.com]
  confluence:
    base_url: https://wiki.example.com
    attachments:
      enabled: false
      base_url: https://files.example.com
    mirrors:
      - base_url: https://mirror.example.com
      - base_url: not a url
  broken: 3
dq: {}
"""


def test_ut10_24_load_bootstrap_hosts(cfg: Path) -> None:
    """UT10-24 enabled-source hosts only, lower-cased; SDK base_url ignored (R-06)."""
    _write(cfg / "sources.yaml", _SOURCES_UT10_24)
    boot = cs.load_bootstrap(config_dir=cfg, env={})
    assert boot.profile == "local"
    assert boot.source_hosts == (
        "acme.service-now.com",
        "acme.snowflakecomputing.com",
        "db.example.com",
        "mirror.example.com",
        "sso.example.com",
        "wiki.example.com",
    )
    assert "evil.example.net" not in boot.source_hosts
    assert "jira.example.com" not in boot.source_hosts
    assert "files.example.com" not in boot.source_hosts


def test_ut10_24_load_bootstrap_security_merge(cfg: Path) -> None:
    """UT10-24 security = herness.yaml merged with the overlay's security subtree."""
    _write(
        cfg / "herness.yaml",
        "version: 1\nsecurity:\n  data_policy: {hybrid_approved: true}\n"
        "  egress: {max_request_bytes: 5}\n",
    )
    _write(
        cfg / "profiles" / "hybrid.yaml",
        "version: 1\nsecurity:\n  egress: {enabled: true, destinations: [API.anthropic.com]}\n",
    )
    boot = cs.load_bootstrap("hybrid", cfg, {})
    assert boot.profile == "hybrid"
    assert boot.security.data_policy.hybrid_approved is True
    assert boot.security.egress.enabled is True
    assert boot.security.egress.max_request_bytes == 5
    assert boot.security.egress.destinations == ("api.anthropic.com",)
    assert boot.source_hosts == ()
    _write(cfg / "profiles" / "local.yaml", "version: 1\nsecurity:\n  egress: {enabled: true}\n")
    with pytest.raises(ConfigError, match=r"^profile local forbids egress$"):
        cs.load_bootstrap("local", cfg, {})


def test_ut10_24_load_bootstrap_errors(cfg: Path) -> None:
    """UT10-24 invalid security, data_policy overlay and bad hosts are ConfigError."""
    _write(cfg / "herness.yaml", "version: 1\nsecurity:\n  egress: {enabled: 'yes-please'}\n")
    with pytest.raises(ConfigError) as info:
        cs.load_bootstrap(config_dir=cfg, env={"HERNESS_PROFILE": "local"})
    assert "security.egress.enabled" in info.value.message
    assert "yes-please" not in info.value.message
    _write(cfg / "herness.yaml", "version: 1\n")
    _write(cfg / "profiles" / "local.yaml", "version: 1\nsecurity: {data_policy: {}}\n")
    with pytest.raises(ConfigError, match="data_policy"):
        cs.load_bootstrap(config_dir=cfg, env={})
    _write(cfg / "profiles" / "local.yaml", "version: 1\n")
    _write(cfg / "sources.yaml", "version: 1\nsources:\n  s:\n    hosts: a.example.com\n")
    with pytest.raises(ConfigError, match=r"sources\.s\.hosts"):
        cs.load_bootstrap(config_dir=cfg, env={})
    _write(cfg / "sources.yaml", "version: 1\nsources:\n  s:\n    hosts: [1]\n")
    with pytest.raises(ConfigError, match=r"sources\.s\.hosts"):
        cs.load_bootstrap(config_dir=cfg, env={})


@pytest.mark.parametrize("flag", ['"false"', "0", '"no"', "0.0"])
def test_ut10_24_non_bool_enabled_rejected(cfg: Path, flag: str) -> None:
    """UT10-24 an enabled flag that is not a boolean is rejected, never read as enabled."""
    text = f"version: 1\nsources:\n  jira:\n    enabled: {flag}\n    hosts: [a.example.com]\n"
    _write(cfg / "sources.yaml", text)
    with pytest.raises(ConfigError, match=r"^sources\.yaml: sources\.jira\.enabled must be"):
        cs.load_bootstrap(config_dir=cfg, env={})
    nested = "version: 1\nsources:\n  wiki:\n    extra:\n      enabled: 0\n"
    _write(cfg / "sources.yaml", nested)
    with pytest.raises(ConfigError, match=r"sources\.wiki\.extra\.enabled"):
        cs.load_bootstrap(config_dir=cfg, env={})


def test_ut10_24_bootstrap_config_is_frozen(cfg: Path) -> None:
    """UT10-24 BootstrapConfig is an immutable dataclass."""
    boot = cs.load_bootstrap(config_dir=cfg, env={})
    with pytest.raises(AttributeError):
        boot.profile = "hybrid"  # type: ignore[misc]
