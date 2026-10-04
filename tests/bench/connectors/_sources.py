"""One fixture-replay source per live connector for BT01-05 (impl 01 §8; T01-25).

Every source is the production connector (and its ``SourceHttp``, auth or SDK seam) over an
in-memory stand-in that serves N new records: ServiceNow through the egress mock pool, Jira
and Dataverse through ``httpx2.MockTransport`` clients, Snowflake through ``FakeSnowflake``,
MongoDB through ``mongomock``, monitoring through fake adapters, files through a CSV inbox.
``Source.run`` is one ``SyncRunner.run_incremental`` per entity of the source against a
watermark set before the first record, so each run reads exactly the new records.
"""

from __future__ import annotations

import datetime
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import httpx2
import pyarrow as pa
import pytest
from tests.support import jira_pages
from tests.support.fake_snowflake import FakeSnowflake
from tests.support.sn_cassettes import T0 as SN_T0
from tests.unit.connectors import _dataverse_data as dv
from tests.unit.connectors import _jira_data as jr
from tests.unit.connectors import _mongo_data as mg
from tests.unit.connectors import _servicenow_env as sn
from tests.unit.connectors import _snowflake_data as sf
from tests.unit.connectors._http_data import mock_client
from tests.unit.connectors._monitoring_data import FETCHED, FakeAdapter, event_row
from tests.unit.connectors._settings_data import adapter, monitoring

from herness.connectors.files import FilesConnector
from herness.connectors.http import SourceHttp
from herness.connectors.jira import JiraConnector
from herness.connectors.monitoring.base import MonitoringConnector, event_batch
from herness.connectors.runner import SyncRunner
from herness.connectors.settings import FilesSettings, MonitoringSettings, ServiceNowSettings
from herness.connectors.snowflake import SnowflakeConnector
from herness.store.ops import set_watermark

UTC = datetime.UTC
AFTER = datetime.datetime(2026, 12, 1, tzinfo=UTC)  # every record is older than "now"
BEFORE = datetime.datetime(2025, 1, 1, tzinfo=UTC)  # the preset watermark: all records are new


@dataclass
class Source:
    """A source with ``rows`` new records and the function that syncs them."""

    name: str
    rows: int
    run: Callable[[], int]


def _later(*parts: int) -> Callable[[], datetime.datetime]:
    return lambda: datetime.datetime(*parts, tzinfo=UTC)  # type: ignore[misc]


def _sync(runner: SyncRunner, entities: list[str], streams: list[str]) -> int:
    """Set the pre-data watermark of every stream, then sync every entity; committed rows."""
    conn = runner.connector
    for entity in entities:
        for stream in streams:
            set_watermark(stream, entity, conn.watermark_field(entity), BEFORE, now=AFTER)
    return sum(runner.run_incremental(entity).rows for entity in entities)


def servicenow(settings: ServiceNowSettings, rows: int, data_root: Path) -> Source:
    """``rows`` incidents (T0 + i s); the caller installs ``SyntheticServiceNow`` as the pool."""
    clock = sn.Clock(SN_T0 + datetime.timedelta(days=30))
    runner = sn.runner(settings, clock, data_root)
    return Source("servicenow", rows, lambda: _sync(runner, ["incident"], ["servicenow"]))


def _jira_now() -> datetime.datetime:
    return jr.NOW


def jira(rows: int, data_root: Path, monkeypatch: pytest.MonkeyPatch) -> Source:
    """``rows`` Cloud issues, 100 per search page, with two changelog histories each."""
    jr.Seams().install(monkeypatch)
    size = 100
    stamp = datetime.datetime(2026, 9, 1, 10, tzinfo=UTC)

    def issue(n: int) -> dict[str, Any]:
        raw = jira_pages.issue(n)
        at = stamp + datetime.timedelta(seconds=n)
        raw["fields"]["updated"] = at.strftime("%Y-%m-%dT%H:%M:%S.000+0000")
        return raw

    def serve(request: httpx2.Request) -> httpx2.Response:
        body = httpx2.Response(200, content=request.content).json()
        page = int(str(body.get("nextPageToken", "p0"))[1:])
        lo, hi = page * size + 1, min(rows, (page + 1) * size) + 1
        last = hi > rows
        out: dict[str, Any] = {"issues": [issue(n) for n in range(lo, hi)], "isLast": last}
        if not last:
            out["nextPageToken"] = f"p{page + 1}"
        return httpx2.Response(200, json=out)

    cfg = jr.settings(page_size=size)
    http = SourceHttp(mock_client(serve, jr.BASE), breaker_key="jira", auth=None, clock=_jira_now)
    conn = JiraConnector(
        cfg, custom_field_ids=jira_pages.CUSTOM_FIELD_IDS, http=http, clock=_jira_now
    )
    runner = SyncRunner(conn, cfg, clock=_later(2026, 12, 1), data_root=data_root)
    return Source("jira", rows, lambda: _sync(runner, ["issue"], ["jira"]))


def dataverse(rows: int, data_root: Path) -> Source:
    """``rows`` projects in OData pages of 500 linked by ``@odata.nextLink``."""
    size, pages = 500, []
    base = datetime.datetime(2026, 8, 1, tzinfo=UTC)
    for first in range(0, rows, size):
        chunk = []
        for n in range(first, min(rows, first + size)):
            item = dv.row(n)
            item["modifiedon"] = (base + datetime.timedelta(seconds=n)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            chunk.append(item)
        more = first + size < rows
        pages.append(dv.page(chunk, dv.next_link(first + size) if more else None))
    server = dv.Server.of(*pages)
    conn = dv.connector(server)
    runner = SyncRunner(conn, dv.settings(), clock=_later(2026, 12, 1), data_root=data_root)
    return Source("dataverse", rows, lambda: _sync(runner, ["project"], ["dataverse"]))


def snowflake(rows: int, data_root: Path) -> Source:
    """``rows`` cost-center rows, one Arrow table of 10,000 rows per fetched batch."""
    chunk = 10_000
    tables = [sf.table(lo, min(chunk, rows - lo)) for lo in range(0, rows, chunk)]
    server = FakeSnowflake(tables=tables)
    cfg = sf.settings()
    conn = SnowflakeConnector(cfg, clock=sf.fixed_now, connect=server.connect)
    runner = SyncRunner(conn, cfg, clock=sf.fixed_now, data_root=data_root)
    runner_run = lambda: _sync(runner, ["cost_center"], ["snowflake"])  # noqa: E731
    return Source("snowflake", rows, runner_run)


def mongodb(rows: int, data_root: Path) -> Source:
    """``rows`` documents (one per second from T0) in a ``mongomock`` collection."""
    factory = mg.Factory()
    docs = [{"ts": mg.T0 + datetime.timedelta(seconds=i), "a": i} for i in range(rows)]
    factory.collection().insert_many(docs)
    conn = mg.connector(factory)
    runner = SyncRunner(conn, mg.settings(), clock=mg.fixed_now, data_root=data_root)
    return Source("mongodb", rows, lambda: _sync(runner, ["orders"], ["mongodb"]))


class _ChunkedAdapter(FakeAdapter):
    """`FakeAdapter` whose events come in batches of 5,000 rows (a batch holds at most
    ``batch_rows`` rows)."""

    def events(
        self, since: datetime.datetime, until: datetime.datetime
    ) -> Iterator[pa.RecordBatch]:
        self.calls.append(("events", since, until))
        times = [ts for ts in self.event_times if since <= ts < until]
        for lo in range(0, len(times), 5_000):
            rows = [
                event_row(f"{self.tool[:2]}{lo + i}", ts)
                for i, ts in enumerate(times[lo : lo + 5_000])
            ]
            yield event_batch(self.tool, rows, fetched_at=FETCHED)


def monitoring_events(rows: int, data_root: Path) -> Source:
    """``rows`` alert events, split over a Prometheus and a Datadog fake adapter."""
    half = rows // 2
    start = datetime.datetime(2026, 2, 1, tzinfo=UTC)
    prom = _ChunkedAdapter(
        "prometheus", [start + datetime.timedelta(seconds=i) for i in range(half)]
    )
    dd = _ChunkedAdapter(
        "datadog", [start + datetime.timedelta(seconds=i) for i in range(rows - half)]
    )
    cfg = MonitoringSettings.model_validate(
        monitoring(prometheus=adapter("prometheus"), datadog=adapter("datadog"))
    )
    now = _later(2026, 3, 1)
    conn = MonitoringConnector(cfg, adapters=(prom, dd), clock=now)
    runner = SyncRunner(conn, cfg, clock=now, data_root=data_root)
    streams = [conn.stream_key(tool) for tool in conn.tools()]
    return Source("monitoring", rows, lambda: _sync(runner, ["event"], streams))


def files(rows: int, root: Path, data_root: Path) -> Source:
    """One CSV of ``rows`` rows (DuckDB ``COPY``) in an inbox, ingested as a delta entity."""
    inbox = root / "inbox" / "bench"
    inbox.mkdir(parents=True)
    path = inbox / "data.csv"
    select = (
        "SELECT i AS id, 'team_' || (i % 97) AS name, i % 1000 AS size, "  # noqa: S608 - `rows` is an int
        f"TIMESTAMP '2026-01-01' + to_seconds(i) AS updated FROM range({rows}) t(i)"
    )
    con = duckdb.connect(":memory:")
    try:
        target = str(path).replace("'", "''")
        con.execute(f"COPY ({select}) TO '{target}' (FORMAT csv)")
    finally:
        con.close()
    old = int((datetime.datetime(2026, 9, 1, 11, tzinfo=UTC)).timestamp()) * 10**9
    os.utime(path, ns=(old, old))
    entity = {"pattern": "*.csv", "key_field": ["id"], "updated_field": "updated"}
    cfg = FilesSettings.model_validate(
        {"enabled": True, "batch_rows": 100_000, "entities": {"bench": entity}}
    )
    now = _later(2026, 9, 1, 12)
    conn = FilesConnector(cfg, inbox_root=root / "inbox", clock=now)
    runner = SyncRunner(conn, cfg, clock=now, data_root=data_root)
    return Source("files", rows, lambda: runner.run_incremental("bench").rows)
