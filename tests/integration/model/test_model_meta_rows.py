"""`meta.build` row writers and row counts (impl 02 U02-90 … U02-92; IT02-21 parts).

The full IT02-21 pipeline run lives in `test_model_build_pipeline.py`; these tests pin the
row writers on a DuckDB file with the `000_settings.sql` tables.
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest
import structlog

from herness.core.errors import ConfigError, SchemaViolation
from herness.model.meta import collect_row_counts, insert_build_row, update_build_row

pytestmark = pytest.mark.integration

SETTINGS_SQL = (
    Path(__file__).resolve().parents[3] / "herness" / "model" / "sql" / "000_settings.sql"
)
T0 = datetime.datetime(2026, 9, 1, 6, 0, tzinfo=datetime.UTC)
T1 = T0 + datetime.timedelta(minutes=5)
BUILD_ID = "20260901-060000-01ABCD"
CFG_HASH = "cfg_0123456789abcdef"
_ROW_SQL = (
    "SELECT build_id, started_at, finished_at, git_sha, config_hash, dataset_kind,"
    " CAST(source_watermarks AS VARCHAR), CAST(row_counts AS VARCHAR), status FROM meta.build"
)


@pytest.fixture
def con() -> Iterator[duckdb.DuckDBPyConnection]:
    connection = duckdb.connect(config={"TimeZone": "UTC"})
    connection.execute(SETTINGS_SQL.read_text(encoding="utf-8"))
    try:
        yield connection
    finally:
        connection.close()


def _insert(con: duckdb.DuckDBPyConnection, **overrides: object) -> None:
    values: dict[str, object] = {
        "build_id": BUILD_ID,
        "started_at": T0,
        "git_sha": "0123456789ab",
        "config_hash": CFG_HASH,
        "dataset_kind": "real",
        "source_watermarks": {"servicenow/incident": "2026-09-01T05:00:00.000000Z"},
    }
    values.update(overrides)
    insert_build_row(con, **values)  # type: ignore[arg-type]


def test_it02_21_insert_build_row(con: duckdb.DuckDBPyConnection) -> None:
    """IT02-21 (U02-90) one `building` row, `finished_at` NULL, `row_counts` `{}`, sorted JSON."""
    _insert(con, source_watermarks={"b/x": "2", "a/y": "1"})
    row = con.execute(_ROW_SQL).fetchall()
    assert row == [
        (
            BUILD_ID,
            T0,
            None,
            "0123456789ab",
            CFG_HASH,
            "real",
            '{"a/y": "1", "b/x": "2"}',
            "{}",
            "building",
        )
    ]


def test_it02_21_insert_build_row_rejects(con: duckdb.DuckDBPyConnection) -> None:
    """IT02-21 (U02-90) a second row, a bad config hash or a naive start raise SchemaViolation."""
    _insert(con)
    with pytest.raises(SchemaViolation, match="already has a row"):
        _insert(con)
    fresh = duckdb.connect()
    try:
        with pytest.raises(SchemaViolation):  # no meta.build table
            _insert(fresh)
    finally:
        fresh.close()
    con.execute("DELETE FROM meta.build")
    with pytest.raises(SchemaViolation, match="config_hash"):
        _insert(con, config_hash="cfg_XYZ")
    with pytest.raises(SchemaViolation, match="started_at"):
        _insert(con, started_at=T0.replace(tzinfo=None))
    with pytest.raises(SchemaViolation, match="dataset_kind"):
        _insert(con, dataset_kind="other")
    assert con.execute("SELECT count(*) FROM meta.build").fetchone() == (0,)


def test_it02_21_update_build_row(con: duckdb.DuckDBPyConnection) -> None:
    """IT02-21 (U02-91) status, finished_at, clear_finished and row_counts update in place."""
    _insert(con)
    update_build_row(con, row_counts={"core.b": 2, "core.a": 1})
    update_build_row(con, finished_at=T1)
    got = con.execute("SELECT finished_at, CAST(row_counts AS VARCHAR), status FROM meta.build")
    assert got.fetchall() == [(T1, '{"core.a": 1, "core.b": 2}', "building")]
    update_build_row(con, status="failed", clear_finished=True)
    got = con.execute("SELECT finished_at, status FROM meta.build")
    assert got.fetchall() == [(None, "failed")]


def test_it02_21_update_build_row_errors(con: duckdb.DuckDBPyConnection) -> None:
    """IT02-21 (U02-91) conflicting or empty requests are ConfigError; no row is SchemaViolation."""
    with pytest.raises(SchemaViolation, match=r"no meta.build row"):
        update_build_row(con, status="failed")
    _insert(con)
    with pytest.raises(ConfigError):
        update_build_row(con)
    with pytest.raises(ConfigError):
        update_build_row(con, finished_at=T1, clear_finished=True)
    with pytest.raises(ConfigError):
        update_build_row(con, status="gone")  # type: ignore[arg-type]
    with pytest.raises(SchemaViolation, match="finished_at"):
        update_build_row(con, finished_at=T1.replace(tzinfo=None))
    con.execute("DROP TABLE meta.build")
    with pytest.raises(SchemaViolation):
        update_build_row(con, status="failed")


def test_it02_21_collect_row_counts(con: duckdb.DuckDBPyConnection) -> None:
    """IT02-21 (U02-92) base tables of the given schemas only, sorted; views are not counted."""
    con.execute("CREATE TABLE core.b AS SELECT * FROM range(3)")
    con.execute("CREATE TABLE core.a AS SELECT * FROM range(0)")
    con.execute("CREATE VIEW core.v AS SELECT 1")
    con.execute('CREATE TABLE core."Bad Name" AS SELECT 1')
    con.execute("INSERT INTO enrich.cluster_member VALUES ('r', 'c', 0.5)")
    with structlog.testing.capture_logs() as logs:
        counts = collect_row_counts(con, ["enrich", "core"])
    assert [(e["event"], e["log_level"], e["count"]) for e in logs] == [
        ("model.build.row_count_skipped", "warning", 1)
    ]
    assert "Bad Name" not in repr(logs)
    assert counts == {
        "core.a": 0,
        "core.b": 3,
        "enrich.cluster": 0,
        "enrich.cluster_member": 1,
        "enrich.decision": 0,
        "enrich.incident_change_link": 0,
        "enrich.text_redacted": 0,
    }
    assert list(counts) == sorted(counts)
    assert json.dumps(collect_row_counts(con, ["score"])) == "{}"


def test_it02_21_collect_row_counts_error(con: duckdb.DuckDBPyConnection) -> None:
    """IT02-21 (U02-92) a DuckDB failure becomes SchemaViolation; a bad schema is ConfigError."""
    con.close()
    with pytest.raises(SchemaViolation):
        collect_row_counts(con, ["core"])
    with pytest.raises(ConfigError):
        collect_row_counts(con, ["main"])  # type: ignore[list-item]
