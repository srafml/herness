"""Tests for herness.core.registry (impl 10 UT10-25 … UT10-27, ST10-46 registry part)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from tests.support._fixture_registry_target import (
    FakeDistribution,
    FakeEntryPoint,
    FixtureBuiltin,
    entry_points_stub,
)

from herness.core import registry as reg
from herness.core.errors import ConfigError
from herness.core.logging import configure_logging, reset_logging

pytestmark = pytest.mark.unit

_BUILTIN_TARGET = "tests.support._fixture_registry_target:FixtureBuiltin"


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    saved = dict(reg._BUILTINS)
    reg.reset_registry()
    yield
    reg._BUILTINS.clear()
    reg._BUILTINS.update(saved)  # restore the shipped rows so later modules see them
    reg.reset_registry()
    reset_logging()


def _err_lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    return [json.loads(text) for text in capsys.readouterr().err.splitlines() if text.strip()]


def _event(lines: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return next(line for line in lines if line.get("event") == name)


def test_ut10_25_builtin_lazy_import_same_object() -> None:
    """UT10-25 get() lazily imports a `_BUILTINS` row and returns the same object twice."""
    reg._BUILTINS[("tool", "fixture_builtin")] = _BUILTIN_TARGET

    first = reg.get("tool", "fixture_builtin")
    second = reg.get("tool", "fixture_builtin")
    assert first is FixtureBuiltin
    assert first is second


def test_ut10_26_unknown_name_lists_available_and_register_rejects_conflict() -> None:
    """UT10-26 get() on an unknown name lists available(); register() rejects a conflict."""

    @reg.register("tool", "alpha")
    class Alpha:
        pass

    with pytest.raises(ConfigError) as unknown:
        reg.get("tool", "missing")
    assert "unknown tool 'missing'" in str(unknown.value)
    assert "alpha" in str(unknown.value)

    with pytest.raises(ConfigError) as duplicate:
        reg.register("tool", "alpha")(object())
    assert "duplicate registration tool:alpha" in str(duplicate.value)

    # Re-registering the identical object is a no-op, not a conflict.
    assert reg.register("tool", "alpha")(Alpha) is Alpha


def test_ut10_27_available_lists_plugin_and_logs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT10-27 available() loads a fixture entry point once and logs registry.plugin.loaded."""
    configure_logging("INFO")

    def _load() -> type:
        @reg.register("tool", "fixture_plugin_tool")
        class _PluginTool:
            pass

        return _PluginTool

    ep = FakeEntryPoint("fixture_plugin_tool", _load, FakeDistribution("fixture-dist", "1.2.3"))
    monkeypatch.setattr(reg.importlib.metadata, "entry_points", entry_points_stub([ep]))

    assert "fixture_plugin_tool" not in reg.available("connector")
    names = reg.available("tool")

    assert names == ["fixture_plugin_tool"]
    line = _event(_err_lines(capsys), "registry.plugin.loaded")
    assert line["level"] == "warning"
    assert line["entry_point"] == "fixture_plugin_tool"
    assert line["distribution"] == "fixture-dist"
    assert line["version"] == "1.2.3"

    # Entry points load once per process: a second call does not repeat the WARNING line.
    reg.available("tool")
    assert _err_lines(capsys) == []


def test_rf_entry_point_failure_logged_and_skipped(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """RF a plugin whose load raises is logged as registry.plugin.failed and skipped."""
    configure_logging("INFO")

    def _boom() -> object:
        msg = "broken plugin"
        raise RuntimeError(msg)

    ep = FakeEntryPoint("broken_plugin", _boom, FakeDistribution("broken-dist", "0.0.1"))
    monkeypatch.setattr(reg.importlib.metadata, "entry_points", entry_points_stub([ep]))

    assert reg.available("tool") == []
    line = _event(_err_lines(capsys), "registry.plugin.failed")
    assert line["level"] == "error"
    assert line["entry_point"] == "broken_plugin"
    assert line["error_type"] == "RuntimeError"


def test_rf_invalid_kind_and_name_raise_config_error() -> None:
    """RF register() and get() reject an unknown kind or a badly shaped name."""
    with pytest.raises(ConfigError, match="invalid registry kind"):
        reg.register("bogus", "ok")  # type: ignore[arg-type]
    with pytest.raises(ConfigError, match="invalid registry name"):
        reg.register("tool", "Bad Name!")
    with pytest.raises(ConfigError, match="invalid registry name"):
        reg.get("tool", "")


def test_rf_builtin_import_failure_raises_config_error() -> None:
    """RF get() wraps an ImportError of a `_BUILTINS` module as ConfigError."""
    reg._BUILTINS[("tool", "no_such_module")] = "herness.core._no_such_module:Thing"
    with pytest.raises(ConfigError, match="cannot import"):
        reg.get("tool", "no_such_module")


def test_rf_get_resolves_via_entry_point_after_loading(monkeypatch: pytest.MonkeyPatch) -> None:
    """RF get() falls through to entry-point loading (U10-24 step 3) and returns the plugin."""

    def _load() -> type:
        @reg.register("tool", "plugin_only_tool")
        class _PluginOnlyTool:
            pass

        return _PluginOnlyTool

    ep = FakeEntryPoint("plugin_only_tool", _load, FakeDistribution("plugin-dist", "1.0.0"))
    monkeypatch.setattr(reg.importlib.metadata, "entry_points", entry_points_stub([ep]))

    resolved = reg.get("tool", "plugin_only_tool")
    assert resolved.__name__ == "_PluginOnlyTool"


def test_rf_reset_registry_keeps_builtins() -> None:
    """RF reset_registry clears registrations and the entry-point flag, keeping `_BUILTINS`."""
    reg._BUILTINS[("tool", "fixture_builtin")] = _BUILTIN_TARGET
    reg.get("tool", "fixture_builtin")
    reg.reset_registry()
    assert ("tool", "fixture_builtin") not in reg._REGISTRY
    assert ("tool", "fixture_builtin") in reg._BUILTINS
    assert reg._entry_points_loaded is False
