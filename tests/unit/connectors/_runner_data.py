"""Fake connectors, batches, guard and runner builders shared by the sync-runner tests (T01-06)."""

from __future__ import annotations

import datetime
import json
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow as pa
from tests.support.fake_lake import FakeLake
from tests.unit.connectors._settings_data import adapter, monitoring, servicenow

from herness.connectors.base import METADATA_SCHEMA
from herness.connectors.runner import SyncRunner
from herness.connectors.settings import MonitoringSettings, ServiceNowSettings
from herness.connectors.settings_base import SourceSettings
from herness.core.errors import CircuitOpen
from herness.store.ops import read_all

NOW = datetime.datetime(2026, 3, 1, 12, 0, 0, tzinfo=datetime.UTC)
T = NOW - datetime.timedelta(hours=2)
UNTIL = NOW - datetime.timedelta(seconds=60)  # default settle_seconds
SINCE = T - datetime.timedelta(minutes=30)  # monitoring default overlap_minutes

type Step = pa.RecordBatch | BaseException | Callable[[], None]


def batch(  # noqa: PLR0913 - test data builder, keyword-only options
    source: str,
    entity: str,
    keys: Iterable[str],
    start: datetime.datetime,
    *,
    step: datetime.timedelta = datetime.timedelta(seconds=1),
    deleted: bool = False,
    extra: str | None = None,
) -> pa.RecordBatch:
    """Metadata-schema rows with ascending `_source_updated_at` from `start`."""
    rows: list[dict[str, Any]] = []
    for i, key in enumerate(keys):
        row: dict[str, Any] = {
            "_record_id": f"{source}:{entity}:{key}",
            "_source": source,
            "_entity": entity,
            "_source_key": key,
            "_source_updated_at": start + i * step,
            "_fetched_at": NOW,
            "_deleted": deleted,
            "_payload": None,
        }
        if extra is not None:
            row[extra] = "x"
        rows.append(row)
    schema = (
        METADATA_SCHEMA if extra is None else METADATA_SCHEMA.append(pa.field(extra, pa.string()))
    )
    return pa.RecordBatch.from_pylist(rows, schema=schema)


def _play(steps: Iterable[Step]) -> Iterator[pa.RecordBatch]:
    for item in steps:
        if isinstance(item, pa.RecordBatch):
            yield item
        elif isinstance(item, BaseException):
            raise item
        else:
            item()


@dataclass
class FakeConnector:
    """Single-stream connector replaying `steps` (batches, exceptions to raise, callbacks)."""

    name: str = "servicenow"
    entities: tuple[str, ...] = ("incident",)
    steps: list[Step] = field(default_factory=list)
    calls: list[tuple[str, datetime.datetime | None, datetime.datetime | None]] = field(
        default_factory=list
    )

    def check(self) -> None:
        return None

    def sync(
        self,
        entity: str,
        since: datetime.datetime | None,
        until: datetime.datetime | None = None,
    ) -> Iterator[pa.RecordBatch]:
        self.calls.append((entity, since, until))
        return _play(self.steps)

    def watermark_field(self, entity: str) -> str:
        return "sys_updated_on"


@dataclass
class FakeToolConnector:
    """`monitoring` fan-out connector; `steps[tool]` replayed by `sync_tool`."""

    steps: dict[str, list[Step]]
    name: str = "monitoring"
    entities: tuple[str, ...] = ("event", "metric_daily")
    calls: list[tuple[str, str, datetime.datetime | None, datetime.datetime]] = field(
        default_factory=list
    )

    def check(self) -> None:
        return None

    def sync(
        self,
        entity: str,
        since: datetime.datetime | None,
        until: datetime.datetime | None = None,
    ) -> Iterator[pa.RecordBatch]:
        msg = "the runner uses sync_tool for tool streams"
        raise AssertionError(msg)

    def watermark_field(self, entity: str) -> str:
        return "ts"

    def tools(self) -> tuple[str, ...]:
        return tuple(self.steps)

    def stream_key(self, tool: str) -> str:
        return f"monitoring:{tool}"

    def sync_tool(
        self,
        tool: str,
        entity: str,
        since: datetime.datetime | None,
        until: datetime.datetime,
    ) -> Iterator[pa.RecordBatch]:
        self.calls.append((tool, entity, since, until))
        return _play(self.steps[tool])


@dataclass
class FakeGuard:
    """Stand-in for `herness.core.resilience.guard`: keys in `open_keys` raise `CircuitOpen`."""

    open_keys: set[str] = field(default_factory=set)
    calls: list[str] = field(default_factory=list)

    def __call__(self, key: str) -> None:
        self.calls.append(key)
        if key in self.open_keys:
            msg = f"circuit open: {key}"
            raise CircuitOpen(msg, key=key, retry_at=NOW + datetime.timedelta(minutes=5))


def servicenow_cfg(**update: Any) -> ServiceNowSettings:
    cfg = ServiceNowSettings.model_validate(servicenow())
    return cfg.model_copy(update=update) if update else cfg


def monitoring_cfg(**update: Any) -> MonitoringSettings:
    cfg = MonitoringSettings.model_validate(
        monitoring(prometheus=adapter("prometheus"), datadog=adapter("datadog"))
    )
    return cfg.model_copy(update=update) if update else cfg


def make_runner(
    connector: Any,
    cfg: SourceSettings,
    lake: FakeLake,
    data_root: Path,
    *,
    clock: Callable[[], datetime.datetime] = lambda: NOW,
    progress: Callable[[str], None] | None = None,
) -> SyncRunner:
    return SyncRunner(
        connector,
        cfg,
        clock=clock,
        writer_factory=lake.factory(),
        data_root=data_root,
        progress=progress,
    )


def metrics() -> list[tuple[str, dict[str, str], float]]:
    """`(name, labels, value)` of every `metric_sample` row written so far, in order."""
    rows = read_all("SELECT name, labels, value FROM metric_sample ORDER BY rowid")
    return [(str(r["name"]), json.loads(r["labels"]), float(r["value"])) for r in rows]
