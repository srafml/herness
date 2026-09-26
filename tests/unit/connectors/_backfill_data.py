"""Window-aware fake connectors, a concurrency gate and store readers for the backfill tests
(U01-41, U01-43; T01-07). Every fake is thread-safe: backfill slices run in worker threads."""

from __future__ import annotations

import datetime
import threading
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow as pa
from tests.support.fake_lake import FakeLake
from tests.unit.connectors._runner_data import NOW, Step, batch

from herness.connectors.runner import SyncRunner
from herness.connectors.settings_base import SourceSettings
from herness.store.ops import get_watermark, read_all

type Window = tuple[datetime.datetime, datetime.datetime]
type Plan = Callable[[str, datetime.datetime, datetime.datetime], Iterable[Step]]

_GATE_TIMEOUT_S = 10.0


def one_row(source: str, entity: str, since: datetime.datetime, _until: object) -> list[Step]:
    """Default plan: one row per window, stamped at the window start."""
    return [batch(source, entity, [since.strftime("%Y%m%d%H%M")], since)]


def no_rows(*_args: object) -> list[Step]:
    """Plan of a source with nothing in the range."""
    return []


class Gate:
    """Counts concurrent fetches; the first `meet` fetches wait at a barrier, so a pool that
    cannot run `meet` fetches at once breaks the barrier instead of hanging (no sleeps)."""

    def __init__(self, meet: int = 0) -> None:
        self.meet = meet
        self.barrier = threading.Barrier(meet) if meet else None
        self.lock = threading.Lock()
        self.active = self.peak = self.entered = 0
        self.threads: set[str] = set()

    def enter(self) -> None:
        with self.lock:
            self.active += 1
            self.entered += 1
            self.peak = max(self.peak, self.active)
            self.threads.add(threading.current_thread().name)
            first = self.entered <= self.meet
        if first and self.barrier is not None:
            self.barrier.wait(timeout=_GATE_TIMEOUT_S)

    def leave(self) -> None:
        with self.lock:
            self.active -= 1


@dataclass
class Source:
    """State shared by every connector instance of one fake source (the factory's output)."""

    plan: Plan = one_row
    gate: Gate = field(default_factory=Gate)
    calls: list[tuple[Any, ...]] = field(default_factory=list)
    instances: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def fetch(self, call: tuple[Any, ...], batches: Iterable[Step]) -> Iterator[pa.RecordBatch]:
        with self.lock:
            self.calls.append(call)
        self.gate.enter()
        try:
            for item in batches:
                if isinstance(item, BaseException):
                    raise item
                if isinstance(item, pa.RecordBatch):
                    yield item
        finally:
            self.gate.leave()

    def windows(self) -> list[Window]:
        """The `(since, until)` of every fetch, sorted."""
        return sorted((c[-2], c[-1]) for c in self.calls)


@dataclass
class WindowConnector:
    """Single-stream connector whose rows depend on the fetched window."""

    source: Source
    name: str = "servicenow"
    entities: tuple[str, ...] = ("incident",)

    def check(self) -> None:
        return None

    def sync(
        self,
        entity: str,
        since: datetime.datetime | None,
        until: datetime.datetime | None = None,
    ) -> Iterator[pa.RecordBatch]:
        assert since is not None
        assert until is not None
        steps = self.source.plan(self.name, since, until)
        return self.source.fetch((entity, since, until), steps)

    def watermark_field(self, entity: str) -> str:
        return "sys_updated_on"


@dataclass
class WindowToolConnector:
    """`monitoring` fan-out connector; `plans[tool]` gives each tool's rows per window."""

    source: Source
    plans: dict[str, Plan]
    name: str = "monitoring"
    entities: tuple[str, ...] = ("event", "metric_daily")

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
        return tuple(self.plans)

    def stream_key(self, tool: str) -> str:
        return f"monitoring:{tool}"

    def sync_tool(
        self,
        tool: str,
        entity: str,
        since: datetime.datetime | None,
        until: datetime.datetime,
    ) -> Iterator[pa.RecordBatch]:
        assert since is not None
        steps = self.plans[tool](self.name, since, until)
        return self.source.fetch((tool, entity, since, until), steps)


def factory_of(make: Callable[[], Any], source: Source) -> Callable[[], Any]:
    """`connector_factory` that counts the instances it builds."""

    def build() -> Any:
        with source.lock:
            source.instances += 1
        return make()

    return build


def backfill_runner(
    connector: Any,
    cfg: SourceSettings,
    lake: FakeLake,
    data_root: Path,
    *,
    factory: Callable[[], Any] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> SyncRunner:
    return SyncRunner(
        connector,
        cfg,
        clock=lambda: NOW,
        writer_factory=lake.factory(),
        connector_factory=factory,
        data_root=data_root,
        should_stop=should_stop,
    )


def stop_after(calls: int) -> Callable[[], bool]:
    """`should_stop` that turns true once it has been polled `calls` times."""
    polled = [0]
    lock = threading.Lock()

    def should_stop() -> bool:
        with lock:
            polled[0] += 1
            return polled[0] > calls

    return should_stop


def slices(source: str | None = None) -> list[dict[str, Any]]:
    """Every `sync_slice` row (optionally of one source) ordered by source and start."""
    sql = (
        "SELECT source, entity, slice_start, slice_end, status, rows, files, attempts,"
        " last_error FROM sync_slice ORDER BY source, slice_start"
    )
    rows = [dict(r) for r in read_all(sql)]
    return [r for r in rows if source is None or r["source"] == source]


def watermark(key: str, entity: str) -> datetime.datetime | None:
    wm = get_watermark(key, entity)
    return None if wm is None else wm.value
