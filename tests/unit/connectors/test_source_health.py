"""Tests for herness.connectors.health: the per-stream health report of `herness doctor`
(impl 01 U01-57; T01-12). Breaker rows and watermarks live in a real migrated ops store
bound as the resilience backend; the report makes no source call."""

from __future__ import annotations

import datetime
from typing import Any

import pytest
from tests.support.ops_store import OpsStoreHandle
from tests.unit.connectors._settings_data import adapter, files, jira, monitoring, servicenow

from herness.connectors.health import SourceHealth, source_health_report
from herness.connectors.settings import SourcesConfig
from herness.core.errors import SchemaViolation
from herness.core.resilience import ProcessState, bind_ops_backend
from herness.core.resilience.ports import HealthRow
from herness.core.types import BreakerState
from herness.store.ops import set_watermark
from herness.store.ops.resilience import SqliteResilienceBackend

pytestmark = pytest.mark.unit

NOW = datetime.datetime(2026, 9, 26, 12, 0, tzinfo=datetime.UTC)


@pytest.fixture
def backend(ops_store: OpsStoreHandle, reset_process_state: ProcessState) -> None:
    """The migrated ops store bound as the resilience backend of the breakers."""
    del ops_store, reset_process_state
    bind_ops_backend(SqliteResilienceBackend())


def _breaker(key: str, state: BreakerState) -> None:
    row = HealthRow(key, state, 3, 1, NOW, "boom", NOW)
    SqliteResilienceBackend().health_apply(key, lambda _before: row, NOW)


def _mark(source: str, entity: str, age_hours: float) -> None:
    value = NOW - datetime.timedelta(hours=age_hours)
    set_watermark(source, entity, "updated", value, now=value)


def _cfg(**sections: dict[str, Any]) -> SourcesConfig:
    return SourcesConfig.model_validate({"version": 1, "sources": sections})


@pytest.mark.usefixtures("backend")
def test_ut01_59_open_breaker_stale_watermark_and_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT01-59 breaker open for jira, stale SN watermark, fresh files: down, degraded, ok."""
    monkeypatch.setattr("herness.core.time.now", lambda: NOW)
    cfg = _cfg(servicenow=servicenow(), jira=jira(), files=files())
    _breaker("jira", "open")
    _mark("jira", "issue", 1)
    _mark("servicenow", "incident", 30)
    assert source_health_report(cfg, now=NOW) == [
        SourceHealth("servicenow", "degraded", "watermark stale for incident: 30 h"),
        SourceHealth("jira", "down", "breaker open"),
        SourceHealth("files", "ok", ""),
    ]


@pytest.mark.usefixtures("backend")
def test_ut01_59_half_open_missing_and_fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT01-59 half-open is `breaker probing`; a missing watermark and a fresh one."""
    monkeypatch.setattr("herness.core.time.now", lambda: NOW)
    two = {
        "incident": {"fields": ["number"]},
        "problem": {"fields": ["number"]},
    }
    cfg = _cfg(servicenow=servicenow(entities=two), jira=jira(), files=files(enabled=False))
    _breaker("servicenow", "half_open")
    _mark("jira", "issue", 23)
    assert source_health_report(cfg, now=NOW) == [
        SourceHealth("servicenow", "degraded", "breaker probing"),
        SourceHealth("jira", "ok", ""),
    ]
    _breaker("servicenow", "closed")
    monkeypatch.setattr("herness.core.time.now", lambda: NOW + datetime.timedelta(seconds=10))
    _mark("servicenow", "problem", 1)
    report = source_health_report(cfg, now=NOW)
    assert report[0] == SourceHealth("servicenow", "degraded", "no watermark for incident")


@pytest.mark.usefixtures("backend")
def test_ut01_59_monitoring_keys_per_enabled_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT01-59 one key per enabled monitoring tool; the monitoring limit is 48 h."""
    monkeypatch.setattr("herness.core.time.now", lambda: NOW)
    mon = monitoring(prometheus=adapter("prometheus"), datadog=adapter("datadog", enabled=False))
    cfg = _cfg(monitoring=mon)
    _mark("monitoring:prometheus", "event", 47)
    _mark("monitoring:prometheus", "metric_daily", 47)
    assert source_health_report(cfg, now=NOW) == [SourceHealth("monitoring:prometheus", "ok", "")]
    later = NOW + datetime.timedelta(hours=2)
    assert source_health_report(cfg, now=later) == [
        SourceHealth("monitoring:prometheus", "degraded", "watermark stale for event: 49 h")
    ]


@pytest.mark.usefixtures("backend")
def test_ut01_59_no_enabled_source_and_naive_now() -> None:
    """UT01-59 no enabled source gives an empty report; a naive `now` is rejected."""
    assert source_health_report(_cfg(), now=NOW) == []
    with pytest.raises(SchemaViolation):
        source_health_report(_cfg(), now=NOW.replace(tzinfo=None))
