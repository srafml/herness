"""Fakes shared by the job-handler tests (T01-11): a scripted runner, a config and contexts."""

from __future__ import annotations

import datetime
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue
from tests.support.build_harness import FakeJobContext
from tests.unit.connectors._runner_data import FakeConnector
from tests.unit.connectors._settings_data import files, jira, servicenow

import herness.connectors.jobs as jobs_module
from herness.connectors.runner import SyncResult
from herness.connectors.settings import SourcesConfig
from herness.connectors.settings_base import SourceSettings
from herness.core.errors import CircuitOpen, ConfigError
from herness.core.types import JobKind

NOW = datetime.datetime(2026, 3, 1, 12, 0, 0, tzinfo=datetime.UTC)
DATA = Path("/data")

type Script = SyncResult | BaseException | Callable[["ScriptedRunner"], SyncResult]


def result(source: str, entity: str, mode: str = "incremental", rows: int = 1) -> SyncResult:
    return SyncResult(source, entity, mode, rows, 0, 0, (), None, None)  # type: ignore[arg-type]


def circuit_open(key: str) -> CircuitOpen:
    return CircuitOpen("circuit open", key=key, retry_at=NOW + datetime.timedelta(minutes=5))


@dataclass
class Calls:
    """What the scripted runners saw: (source, method, entity, *args) per call and the
    constructor arguments of each runner."""

    runs: list[tuple[object, ...]] = field(default_factory=list)
    runners: list[ScriptedRunner] = field(default_factory=list)


class ScriptedRunner:
    """Stand-in for `SyncRunner`: `script[(source, entity)]` is returned, raised or called."""

    def __init__(  # noqa: PLR0913 - mirrors the SyncRunner keywords the handler passes
        self,
        connector: Any,
        cfg: SourceSettings,
        *,
        connector_factory: Callable[[], Any],
        progress: Callable[[str], None],
        should_stop: Callable[[], bool],
        script: Mapping[tuple[str, str], Script],
        calls: Calls,
    ) -> None:
        self.connector, self.cfg = connector, cfg
        self.connector_factory, self.progress, self.should_stop = (
            connector_factory,
            progress,
            should_stop,
        )
        self.script, self.calls = script, calls
        self.skipped_open: tuple[str, ...] = ()
        self.stopped = False
        self.data_root = DATA
        self.clock = lambda: NOW
        calls.runners.append(self)

    def _play(self, entity: str, *record: object) -> SyncResult:
        self.skipped_open, self.stopped = (), False
        self.calls.runs.append((self.connector.name, *record))
        step = self.script.get((self.connector.name, entity), result(self.connector.name, entity))
        if isinstance(step, BaseException):
            raise step
        return step if isinstance(step, SyncResult) else step(self)

    def run_incremental(self, entity: str) -> SyncResult:
        return self._play(entity, "incremental", entity)

    def run_backfill(
        self, entity: str, start: datetime.datetime, end: datetime.datetime
    ) -> SyncResult:
        return self._play(entity, "backfill", entity, start, end)

    def run_reconcile(self, entity: str) -> SyncResult:
        return self._play(entity, "reconcile", entity)


@dataclass
class FakeConfig:
    """The parts of `HernessConfig` the handlers read: `sources` and `paths.data`."""

    sources: SourcesConfig


def sources(**sections: dict[str, Any]) -> SourcesConfig:
    return SourcesConfig.model_validate({"version": 1, "sources": sections})


def two_sources() -> SourcesConfig:
    """servicenow (incident) and jira, both enabled, plus a disabled files section."""
    return sources(servicenow=servicenow(), jira=jira(), files=files(enabled=False))


def install(
    monkeypatch: pytest.MonkeyPatch,
    cfg: SourcesConfig,
    script: Mapping[tuple[str, str], Script] | None = None,
    entities: Mapping[str, tuple[str, ...]] | None = None,
) -> Calls:
    """Patch `get_config`, `build_connector` and `SyncRunner` of the jobs module."""
    calls = Calls()
    known = {"servicenow": ("incident",), "jira": ("issue",), "files": ("roster",)}
    known |= dict(entities or {})
    fake_cfg = FakeConfig(cfg)

    def build(name: str, config: object) -> FakeConnector:
        assert config is fake_cfg
        section = cfg.source(name)
        if not section.enabled:
            msg = f"source {name} is disabled"
            raise ConfigError(msg)
        return FakeConnector(name=name, entities=known[name])

    def runner(connector: Any, settings: SourceSettings, **kwargs: Any) -> ScriptedRunner:
        return ScriptedRunner(connector, settings, script=script or {}, calls=calls, **kwargs)

    monkeypatch.setattr(jobs_module, "get_config", lambda: fake_cfg)
    monkeypatch.setattr(jobs_module, "build_connector", build)
    monkeypatch.setattr(jobs_module, "SyncRunner", runner)
    return calls


def ctx(
    payload: Mapping[str, JsonValue] | None = None,
    *,
    kind: JobKind = "sync",
    yield_after: int | None = None,
) -> FakeJobContext:
    return FakeJobContext(payload or {}, kind=kind, yield_after=yield_after)
