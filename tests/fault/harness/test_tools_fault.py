"""Fault tests for herness.harness.tools.execute_recorded (FT05-02, FT05-04; flow F05-04).

`run_sql` (U05-40) and `dispatch` (U05-34) are later cards: "error result" is checked as the
`ToolResult.from_error` that dispatch builds from the raised error (design §6), and "loop
continues" as the next call on the same context succeeding.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path

import pytest
from tests.support.fault_env import FaultEnv
from tests.support.harness_fakes import FakeOps
from tests.support.ops_store import OpsStoreHandle
from tests.unit.harness import _tools_standin as sd

from herness.core.errors import QueryError, StoreBusy
from herness.core.resilience import ProcessState
from herness.core.types import Evidence, ToolResult
from herness.harness import tools
from herness.harness.warehouse import DuckWarehouse
from herness.store.ops import core as ops_core
from herness.store.ops import evidence as ops_evidence

pytestmark = pytest.mark.fault

SQL = "SELECT count(*) AS c FROM core.big"


@pytest.fixture
def wh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reset_process_state: ProcessState
) -> Iterator[DuckWarehouse]:
    del reset_process_state
    sd.patch_config(monkeypatch)
    with sd.opened(tmp_path / "wh") as handle:
        yield handle


def test_ft05_02_sql_query_timeout_fault(wh: DuckWarehouse, fault_env: FaultEnv) -> None:
    """FT05-02 plan `sql.query` timeout: QueryError timeout result; the next call succeeds."""
    fault_env([{"point": "sql.query", "action": "timeout", "count": 1}])
    ops = FakeOps()
    ctx = sd.make_ctx(wh, ops)
    with pytest.raises(QueryError) as info:
        tools.execute_recorded(ctx, SQL, {})
    assert info.value.message == "timeout after 30.0s"
    error = ToolResult.from_error(info.value)
    assert error.ok is False
    assert error.error is not None
    assert error.error.type == "QueryError"
    assert error.content.startswith("ERROR QueryError: timeout after")
    assert ops.evidence == {}
    result = tools.execute_recorded(ctx, SQL, {})  # the loop continues
    assert result.rows == [(sd.BIG_ROWS,)]
    assert list(ops.evidence) == [result.query_id]


def test_ft05_02_other_injected_query_error_passes(wh: DuckWarehouse, fault_env: FaultEnv) -> None:
    """FT05-02 an injected non-timeout QueryError propagates unchanged."""
    fault_env([{"point": "sql.query", "action": "error:QueryError"}])
    with pytest.raises(QueryError, match="fault: injected"):
        tools.execute_recorded(sd.make_ctx(wh, FakeOps()), SQL, {})


class _StoreOps(FakeOps):
    """`OpsHandle` over the real ops store `evidence` area (R-13)."""

    def record_evidence(self, ev: Evidence) -> bool:
        return ops_evidence.record_evidence(ev)

    def record_evidence_use(
        self, query_id: str, run_id: str, task_id: str | None, used_at: datetime
    ) -> bool:
        return ops_evidence.record_evidence_use(query_id, run_id, task_id, used_at)


def _counting(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    real: Callable[..., object] = vars(tools)["retry_call"]

    def counting(name: str, fn: Callable[..., object], /, *a: object, **kw: object) -> object:
        calls.append(name)
        return real(name, fn, *a, **kw)

    monkeypatch.setattr(tools, "retry_call", counting)
    return calls


def test_ft05_04_locked_ops_store_retried_then_error(
    wh: DuckWarehouse, ops_store: OpsStoreHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FT05-04 ops store locked by another connection: retried per sqlite_write, then error."""
    monkeypatch.setattr(ops_core, "_BUSY_TIMEOUT_MS", 20)
    ops_core.reset_connections(path=ops_store.db_path)
    attempts: list[int] = []
    real_record = ops_evidence.record_evidence

    def counted(ev: Evidence) -> bool:
        attempts.append(1)
        return real_record(ev)

    monkeypatch.setattr(ops_evidence, "record_evidence", counted)
    policies = _counting(monkeypatch)
    blocker = sqlite3.connect(ops_store.db_path, isolation_level=None)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(StoreBusy) as info:
            tools.execute_recorded(sd.make_ctx(wh, _StoreOps()), SQL, {})
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    assert policies == ["sqlite_write"]
    assert len(attempts) == 6  # the sqlite_write policy's attempts (design 08 §7)
    error = ToolResult.from_error(info.value)
    assert error.ok is False
    assert error.error is not None
    assert error.error.type == "StoreBusy"
    result = tools.execute_recorded(sd.make_ctx(wh, _StoreOps()), SQL, {})  # lock released
    assert ops_evidence.get_evidence(result.query_id) is not None
    assert policies == ["sqlite_write", "sqlite_write", "sqlite_write"]


def test_ft05_04_store_busy_on_use_write(wh: DuckWarehouse) -> None:
    """FT05-04 a busy evidence_use write is retried too, then StoreBusy propagates."""

    class _BusyUse(FakeOps):
        def __init__(self) -> None:
            super().__init__()
            self.use_attempts = 0

        def record_evidence_use(
            self, query_id: str, run_id: str, task_id: str | None, used_at: datetime
        ) -> bool:
            self.use_attempts += 1
            msg = "ops store busy in record_evidence_use"
            raise StoreBusy(msg)

    ops = _BusyUse()
    with pytest.raises(StoreBusy):
        tools.execute_recorded(sd.make_ctx(wh, ops), SQL, {})
    assert ops.use_attempts == 6
    assert len(ops.evidence) == 1
