"""Security test for herness.core.registry (impl 10 ST10-46 registry part, TH10-38)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from tests.support._fixture_registry_target import (
    FakeDistribution,
    FakeEntryPoint,
    entry_points_stub,
)

from herness.core import registry as reg
from herness.core.logging import configure_logging, reset_logging

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    reg.reset_registry()
    yield
    reg.reset_registry()
    reset_logging()


def _err_lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    return [json.loads(text) for text in capsys.readouterr().err.splitlines() if text.strip()]


def test_st10_46_plugin_distribution_logged_at_warning(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ST10-46 (registry part) a fixture plugin distribution loads with a WARNING line (TH10-38)."""
    configure_logging("INFO")

    def _load() -> type:
        @reg.register("connector", "malicious_source")
        class _Plugin:
            pass

        return _Plugin

    ep = FakeEntryPoint(
        "malicious_source", _load, FakeDistribution("untrusted-plugin-dist", "0.1.0")
    )
    monkeypatch.setattr(reg.importlib.metadata, "entry_points", entry_points_stub([ep]))

    names = reg.available("connector")

    assert "malicious_source" in names
    lines = [line for line in _err_lines(capsys) if line.get("event") == "registry.plugin.loaded"]
    assert len(lines) == 1
    assert lines[0]["level"] == "warning"
    assert lines[0]["distribution"] == "untrusted-plugin-dist"
    assert lines[0]["version"] == "0.1.0"
