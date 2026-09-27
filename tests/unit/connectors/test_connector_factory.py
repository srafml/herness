"""Tests for herness.connectors.factory.build_connector (impl 01 U01-55, U01-16, U01-17;
T01-11). Connectors are built from a synthetic, fully loaded config. Only the files
connector exists at this card; `jira` and `monitoring` (with a `prometheus` adapter) are
covered by fake classes registered under those names, which records the keyword arguments
the factory passes (the real classes land with T01-17 / T01-19)."""

from __future__ import annotations

import datetime
import importlib.util
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
import respx
from tests.support.sync_env import write_sync_config

from herness.connectors.base import Connector, SupportsKeyListing
from herness.connectors.factory import build_connector
from herness.connectors.files import FilesConnector
from herness.core import config as c
from herness.core import registry
from herness.core.errors import ConfigError

pytestmark = pytest.mark.unit

SOURCES_YAML = """\
version: 1
sources:
  files:
    enabled: true
    inbox: {inbox}
    entities:
      teams: {{pattern: "*.csv", key_field: [team_code]}}
  jira:
    enabled: true
    flavor: cloud
    base_url: https://acme.atlassian.net
    auth: {{method: api_token, credentials: "secret:jira_token"}}
  monitoring:
    enabled: true
    adapters:
      prometheus:
        enabled: true
        base_url: https://prometheus.example.com
        auth: {{method: bearer, credentials: "secret:prometheus_key"}}
      datadog:
        enabled: false
        base_url: https://datadog.example.com
        auth: {{method: api_and_app_key, credentials: "secret:datadog_key"}}
  servicenow:
    enabled: false
    base_url: https://acme.service-now.com
    auth: {{method: oauth_client_credentials, credentials: "secret:sn_oauth"}}
    entities:
      incident: {{fields: [number]}}
"""

MAPPINGS_YAML = """\
version: 1
custom_fields:
  jira: {story_points: customfield_10016, epic_link: customfield_10014}
"""


def _fixed() -> datetime.datetime:
    return datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC)


class _Base:
    """Protocol-conforming fake connector that records its constructor arguments."""

    name = ""

    def __init__(self, settings: Any, *, clock: Callable[[], Any], **kwargs: Any) -> None:
        self.settings, self.clock, self.kwargs = settings, clock, kwargs
        self.entities: tuple[str, ...] = tuple(settings.entities)

    def check(self) -> None:
        return None

    def sync(self, entity: str, since: Any, until: Any = None) -> Iterator[pa.RecordBatch]:
        return iter(())

    def watermark_field(self, entity: str) -> str:
        return "updated"


class FakeJira(_Base):
    name = "jira"

    def list_keys(self, entity: str) -> Iterator[pa.RecordBatch]:
        return iter(())


class FakeMonitoring(_Base):
    name = "monitoring"


class FakeAdapter:
    def __init__(self, settings: Any, *, clock: Callable[[], Any]) -> None:
        self.settings, self.clock = settings, clock


@pytest.fixture
def cfg(tmp_path: Path) -> c.HernessConfig:
    """Synthetic config with files (relative inbox), jira, monitoring and a disabled source;
    registry entries for all three connectors and the prometheus adapter."""
    registry.register("connector", "files")(FilesConnector)
    registry.register("connector", "jira")(FakeJira)
    registry.register("connector", "monitoring")(FakeMonitoring)
    registry.register("monitoring_adapter", "prometheus")(FakeAdapter)
    sources = SOURCES_YAML.format(inbox="data/inbox")
    config_dir = write_sync_config(tmp_path, sources, MAPPINGS_YAML)
    return c.load_config("local", config_dir=config_dir, env={})


def test_ut01_94_every_registered_connector_builds_without_network(
    cfg: c.HernessConfig,
) -> None:
    """UT01-94 every registered connector builds from the synth config; each satisfies
    `Connector`, the key-listing ones `SupportsKeyListing`; no HTTP call is made."""
    with respx.mock(assert_all_called=False) as mock:
        built = {name: build_connector(name, cfg) for name in registry.available("connector")}
    assert not mock.calls
    assert set(built) == {"files", "jira", "monitoring"}
    for name, conn in built.items():
        assert conn.name == name
        for member in ("check", "sync", "watermark_field"):
            assert callable(getattr(conn, member))
        assert isinstance(conn.entities, tuple)
        _conforms(conn)
    assert isinstance(built["files"], SupportsKeyListing)
    assert isinstance(built["jira"], SupportsKeyListing)
    assert not isinstance(built["monitoring"], SupportsKeyListing)


def _conforms(conn: Connector) -> Connector:
    return conn


def test_ut01_94_files_inbox_relative_to_data_parent(cfg: c.HernessConfig) -> None:
    """UT01-94 a relative `inbox` resolves against `paths.data.parent`."""
    conn = build_connector("files", cfg, clock=_fixed)
    assert isinstance(conn, FilesConnector)
    assert conn._root == (cfg.paths.data.parent / "data" / "inbox").resolve()
    assert conn._clock is _fixed


def test_ut01_94_files_absolute_inbox_kept(tmp_path: Path) -> None:
    """UT01-94 an absolute `inbox` is used as is (resolved)."""
    registry.register("connector", "files")(FilesConnector)
    inbox = (tmp_path / "elsewhere").resolve()
    sources = SOURCES_YAML.format(inbox=inbox.as_posix())
    config = c.load_config("local", config_dir=write_sync_config(tmp_path, sources), env={})
    conn = build_connector("files", config)
    assert isinstance(conn, FilesConnector)
    assert conn._root == inbox


def test_ut01_94_jira_gets_non_empty_custom_fields_in_order(cfg: c.HernessConfig) -> None:
    """UT01-94 jira gets the mapped custom field ids in `story_points, team,
    estimate_cost_usd, epic_link` order, unmapped ones dropped."""
    conn = build_connector("jira", cfg, clock=_fixed)
    assert isinstance(conn, FakeJira)
    assert conn.kwargs == {"custom_field_ids": ("customfield_10016", "customfield_10014")}
    assert conn.settings is cfg.sources.source("jira")
    assert conn.clock is _fixed


def test_ut01_94_monitoring_gets_enabled_adapters(cfg: c.HernessConfig) -> None:
    """UT01-94 monitoring gets one adapter per enabled tool, built with the same clock."""
    conn = build_connector("monitoring", cfg, clock=_fixed)
    assert isinstance(conn, FakeMonitoring)
    (adapter,) = conn.kwargs["adapters"]
    assert isinstance(adapter, FakeAdapter)
    assert adapter.clock is _fixed
    assert str(adapter.settings.base_url) == "https://prometheus.example.com"


def test_ut01_94_disabled_unconfigured_or_unregistered(
    cfg: c.HernessConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT01-94 disabled, unconfigured and unregistered sources → ConfigError."""
    with pytest.raises(ConfigError, match="source servicenow is disabled"):
        build_connector("servicenow", cfg)
    with pytest.raises(ConfigError, match="source mongodb is not configured"):
        build_connector("mongodb", cfg)
    registry.reset_registry()
    monkeypatch.setattr(registry, "_BUILTINS", {})  # independent of later built-in rows
    with pytest.raises(ConfigError, match="unknown connector 'files'"):
        build_connector("files", cfg)


class FakeServiceNow(_Base):
    name = "servicenow"


def test_ut01_94_other_sources_get_no_extra_kwargs(tmp_path: Path) -> None:
    """UT01-94 a source other than files, jira or monitoring gets settings and clock only."""
    registry.register("connector", "servicenow")(FakeServiceNow)
    sources = SOURCES_YAML.format(inbox="data/inbox").replace(
        "  servicenow:\n    enabled: false", "  servicenow:\n    enabled: true"
    )
    config = c.load_config("local", config_dir=write_sync_config(tmp_path, sources), env={})
    conn = build_connector("servicenow", config, clock=_fixed)
    assert isinstance(conn, FakeServiceNow)
    assert conn.kwargs == {}
    assert conn.settings is config.sources.source("servicenow")


_FILES_ROW = ("connector", "files")


def _shipped_builtins() -> dict[tuple[str, str], str]:
    """`_BUILTINS` as shipped: a fresh load of registry.py (test_registry's autouse fixture
    clears the live table for the rest of the session)."""
    spec = importlib.util.spec_from_file_location("_registry_probe", registry.__file__)
    assert spec is not None
    assert spec.loader is not None
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    return dict(probe._BUILTINS)


def test_ut01_94_files_resolves_through_the_builtin_table(
    cfg: c.HernessConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT01-94 the shipped `_BUILTINS` row resolves `files` with no explicit registration."""
    row = _shipped_builtins()[_FILES_ROW]
    assert row == "herness.connectors.files:FilesConnector"
    registry.reset_registry()
    monkeypatch.setitem(registry._BUILTINS, _FILES_ROW, row)
    assert registry.get("connector", "files") is FilesConnector
    assert isinstance(build_connector("files", cfg), FilesConnector)


_PROBE = """\
import sys
from pathlib import Path
from herness.connectors.factory import build_connector
from herness.core import config as c
assert "herness.connectors.files" not in sys.modules
cfg = c.load_config("local", config_dir=Path(sys.argv[1]), env={})
conn = build_connector("files", cfg)
sys.stdout.write(type(conn).__module__ + ":" + type(conn).__name__)
"""


def test_ut01_94_build_connector_files_without_prior_import(tmp_path: Path) -> None:
    """UT01-94 in a fresh interpreter, `build_connector("files", cfg)` resolves the class
    through `_BUILTINS` although `herness.connectors.files` was never imported."""
    config_dir = write_sync_config(tmp_path, SOURCES_YAML.format(inbox="data/inbox"))
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERNESS_")}
    done = subprocess.run(  # noqa: S603 - fixed interpreter and script, test only
        [sys.executable, "-c", _PROBE, str(config_dir)],
        capture_output=True,
        text=True,
        check=False,
        env=env | {"HERNESS_ENV": "test"},
        cwd=Path(__file__).resolve().parents[3],
        timeout=120,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    # config warnings are logged to stdout too; the probe writes its answer last
    assert done.stdout.endswith("\nherness.connectors.files:FilesConnector")
