"""Integration test for herness.harness.sql_guard (IT05-10) on the stand-in corpus.

Stand-in for `tests/fixtures/sql_ok/` on `small_build` (impl 11, not built yet): the schema
comes from a real read-only warehouse built in `tmp_path` via `WarehouseHandle.schema()`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.unit.harness._sql_guard_standin import (
    BLOCKED_COLUMNS,
    BUILD_ID,
    accepted_corpus,
    make_warehouse,
)

from herness.harness.llm.settings import SqlSettings
from herness.harness.sql_guard import SqlGuard
from herness.harness.warehouse import open_warehouse

pytestmark = pytest.mark.integration


def test_it05_10_sql_ok_corpus_all_accepted(tmp_path: Path) -> None:
    """IT05-10 every query of the >= 200-query accepted corpus passes the guard."""
    make_warehouse(tmp_path)
    handle = open_warehouse(BUILD_ID, warehouse_dir=tmp_path, sql=SqlSettings())
    try:
        guard = SqlGuard(handle.schema(), BLOCKED_COLUMNS)
        corpus = accepted_corpus()
        assert len(corpus) >= 200
        for sql in corpus:
            guarded = guard.check(sql)
            assert guarded.sql == sql
            # the guarded SQL also binds and runs on the locked read-only warehouse
            handle.cursor().execute(sql, _params(sql)).fetchall()
    finally:
        handle.close()


def _params(sql: str) -> dict[str, str]:
    names = ("metric", "record_id")
    return {name: "x" for name in names if f"${name}" in sql}
