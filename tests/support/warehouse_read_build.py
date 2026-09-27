"""Stand-in builds for the warehouse tools part 2 tests and benchmarks (T05-18).

Spec 11's `tiny_build` and `full` fixtures do not exist yet. `make_build` extends the T05-17
stand-in (`tests.support.warehouse_tools_build`) with `core.problem`, `enrich.cluster`,
`enrich.cluster_member`, `enrich.decision`, more `enrich.text_redacted` rows and a work item
whose summary holds a planted name. `MemoryWarehouse` is a `WarehouseHandle` over an in-memory
DuckDB such as `metrics_tiny` (for `get_metric`, which needs the spec 02 DDL and `meta.build`).
`patch_redaction` routes every redaction call site to a stub that masks the planted name and
e-mail and records what it saw. Re-point to `tiny_build` / `full` when spec 11 lands.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import duckdb
import pytest
from tests.support import warehouse_tools_build as wb

from herness.harness import _tools_record as rec
from herness.harness import _warehouse_tools_sql as ws
from herness.harness import warehouse_tools as wt
from herness.harness.llm.settings import SqlSettings
from herness.harness.warehouse import DuckWarehouse, open_warehouse

BUILD_ID = wb.BUILD_ID
PERSONAL = wb.PERSONAL
INJECTION = wb.INJECTION
PLANTED_NAME = "Priya Raman"
CLUSTER_ID = "cl_01HZX3K7M2" + "0" * 16
EMPTY_CLUSTER_ID = "cl_01HZX3K7M2" + "1" * 16
WORK_ITEM_ID = "jira:issue:PAY-7"
# also a work item id: `get_record` must resolve it deterministically (incident first)
DUPLICATE_ID = "sn:incident:5"
PROBLEM_ID = "sn:problem:1"
MEMBERS = 8  # sn:incident:0 ... 7 are cluster members

_STATEMENTS = (
    "CREATE TABLE core.problem (record_id VARCHAR, number VARCHAR, state VARCHAR,"
    " service_id VARCHAR, root_cause_text VARCHAR)",
    "INSERT INTO core.problem VALUES ('sn:problem:1', 'PRB0001', 'open', 'svc_1',"
    " 'raw root cause')",
    "INSERT INTO core.work_item VALUES ($wid, 'Call $name about $personal login', 'open')",
    "INSERT INTO core.work_item VALUES ('sn:incident:5', 'duplicate id', 'open')",
    "CREATE TABLE enrich.cluster (cluster_id VARCHAR, label VARCHAR, root_cause_category VARCHAR,"
    " size INTEGER, first_seen TIMESTAMP, last_seen TIMESTAMP, top_terms VARCHAR[],"
    " service_ids VARCHAR[], algorithm_version VARCHAR)",
    "INSERT INTO enrich.cluster VALUES ($cid, $injection, 'capacity', 8,"
    " TIMESTAMP '2024-01-01', TIMESTAMP '2024-03-30', ['disk', 'full'], ['svc_0', 'svc_1'],"
    " 'v1'), ($empty, 'empty cluster', 'other', 0, NULL, NULL, [], [], 'v1')",
    "CREATE TABLE enrich.cluster_member (record_id VARCHAR, cluster_id VARCHAR,"
    " membership_prob DOUBLE)",
    # probabilities tie in pairs, so the record_id tie-break of the sample order is exercised
    "INSERT INTO enrich.cluster_member SELECT 'sn:incident:' || i, $cid, 1.0 - (i // 2) / 10.0"
    " FROM range($members) t(i)",
    "INSERT INTO enrich.cluster_member VALUES ($wid, $empty, 0.4)",
    "CREATE TABLE enrich.decision (record_id VARCHAR, question VARCHAR, answer VARCHAR,"
    " probability DOUBLE)",
    "INSERT INTO enrich.decision VALUES ($wid, 'severity', 'high', 0.9),"
    " ($wid, 'root_cause', 'config', 0.7), ('sn:incident:1', 'root_cause', 'disk', 0.8)",
    "INSERT INTO enrich.text_redacted VALUES ($wid, 'redacted work item text')",
    "UPDATE enrich.text_redacted SET text = $injection WHERE record_id = 'sn:incident:3'",
)


def make_build(warehouse_dir: Path, **sizes: int) -> Path:
    """The T05-17 stand-in plus the tables `get_cluster` and `get_record` read."""
    path = wb.make_build(warehouse_dir, **sizes)
    values: dict[str, object] = {
        "wid": WORK_ITEM_ID,
        "cid": CLUSTER_ID,
        "empty": EMPTY_CLUSTER_ID,
        "injection": INJECTION,
        "members": MEMBERS,
        "name": PLANTED_NAME,
        "personal": PERSONAL,
    }
    con = duckdb.connect(str(path))
    try:
        for statement in _STATEMENTS:
            text = statement.replace("$name", PLANTED_NAME).replace("$personal", PERSONAL)
            used = {k: v for k, v in values.items() if f"${k}" in text}
            con.execute(text, used)
    finally:
        con.close()
    return path


@contextmanager
def opened(warehouse_dir: Path, *, extended: bool = True) -> Iterator[DuckWarehouse]:
    """Build (extended or the plain T05-17 stand-in) and open it read-only; closed on exit."""
    (make_build if extended else wb.make_build)(warehouse_dir)
    handle = open_warehouse(BUILD_ID, warehouse_dir=warehouse_dir, sql=SqlSettings())
    try:
        yield handle
    finally:
        handle.close()


def patch_redaction(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stub `redact_text` at every call site (config has no redaction keys); returns the texts
    it received. It masks `PERSONAL` and `PLANTED_NAME`."""
    seen: list[str] = []

    def fake_redact(text: str | None) -> str | None:
        if text is None:
            return None
        seen.append(text)
        return text.replace(PERSONAL, "[EMAIL]").replace(PLANTED_NAME, "[NAME]")

    for module in (ws, rec, wt):
        monkeypatch.setattr(module, "redact_text", fake_redact)
    rec.clear_guard_cache()
    return seen


class MemoryWarehouse:
    """`WarehouseHandle` over an in-memory DuckDB connection (one cursor per thread, UTC)."""

    def __init__(self, con: duckdb.DuckDBPyConnection, build_id: str) -> None:
        self._con = con
        self._build_id = build_id
        self._local = threading.local()

    @property
    def build_id(self) -> str:
        return self._build_id

    @property
    def path(self) -> Path:
        return Path(":memory:")

    def cursor(self) -> duckdb.DuckDBPyConnection:
        cur: duckdb.DuckDBPyConnection | None = getattr(self._local, "cursor", None)
        if cur is None:
            cur = self._con.cursor()
            cur.execute("SET TimeZone = 'UTC'")
            self._local.cursor = cur
        return cur

    def schema(self) -> dict[str, dict[str, dict[str, str]]]:
        return {}

    def table_comment(self, qualified: str) -> str:
        del qualified
        return ""
