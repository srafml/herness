"""Security tests for herness.harness.warehouse (ST05-06, ST05-17; TH05-04, TH05-06, TH05-17)."""

from __future__ import annotations

import inspect
from pathlib import Path

import duckdb
import pytest

from herness.core.errors import ConfigError
from herness.harness import warehouse as wh
from herness.harness.llm.settings import SqlSettings

pytestmark = pytest.mark.unit

BUILD_ID = "20260925-101500-ABCDEF"


def _make_tiny_build(warehouse_dir: Path, build_id: str = BUILD_ID) -> Path:
    warehouse_dir.mkdir(parents=True, exist_ok=True)
    path = warehouse_dir / f"wh-{build_id}.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE SCHEMA core")
    con.execute("CREATE TABLE core.incident (id INTEGER)")
    con.close()
    return path


# --- ST05-06: every listed statement fails at the agent's read-only, locked connection ------


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE core.evil (a INT)",
        "INSERT INTO core.incident VALUES (1)",
        "COPY core.incident TO 'C:/tmp/evil.csv'",
        "SELECT * FROM read_csv('C:/Windows/win.ini')",
        "SET enable_external_access = true",
        "ATTACH ':memory:' AS mem2",
    ],
    ids=["create", "insert", "copy", "read_csv_absolute", "set_external_access", "attach"],
)
def test_st05_06_blocked_statements_fail_at_connection(tmp_path: Path, sql: str) -> None:
    """ST05-06 CREATE, INSERT, COPY, read_csv(abs path), SET external_access, ATTACH all fail."""
    _make_tiny_build(tmp_path)
    handle = wh.open_warehouse(BUILD_ID, warehouse_dir=tmp_path, sql=SqlSettings())
    try:
        with pytest.raises(duckdb.Error):
            handle.cursor().execute(sql)
    finally:
        handle.close()


# --- ST05-17: path traversal through build_id, absolute paths, symlinks --------------------


@pytest.mark.parametrize(
    "bad_id",
    [
        "../../x",
        "../../../etc/passwd",
        "/etc/passwd",
        "C:/Windows/win.ini",
        "20260925-101500-AB/CDEF",
        "20260925-101500-AB\\CDEF",
    ],
)
def test_st05_17_build_id_traversal_raises_config_error(tmp_path: Path, bad_id: str) -> None:
    """ST05-17 build_id with separators or absolute-path shapes raises ConfigError."""
    with pytest.raises(ConfigError):
        wh.open_warehouse(bad_id, warehouse_dir=tmp_path, sql=SqlSettings())


def test_st05_17_symlinked_warehouse_file_raises_config_error(tmp_path: Path) -> None:
    """ST05-17 a symlinked warehouse file raises ConfigError instead of being opened."""
    real_dir = tmp_path / "real"
    _make_tiny_build(real_dir)
    warehouse_dir = tmp_path / "warehouse"
    warehouse_dir.mkdir()
    link = warehouse_dir / f"wh-{BUILD_ID}.duckdb"
    try:
        link.symlink_to(real_dir / f"wh-{BUILD_ID}.duckdb")
    except OSError as exc:
        pytest.skip(f"platform denied symlink creation: {exc}")
    with pytest.raises(ConfigError):
        wh.open_warehouse(BUILD_ID, warehouse_dir=warehouse_dir, sql=SqlSettings())


def test_st05_17_run_id_not_a_path_input_of_this_unit() -> None:
    """ST05-17 this unit takes no run_id, so a run_id with separators cannot reach a path here.

    `open_warehouse` and `WarehousePool.get` are parameterised only by `build_id`,
    `warehouse_dir` and `sql`; `run_id` never enters this unit's filesystem paths.
    """
    open_params = set(inspect.signature(wh.open_warehouse).parameters)
    pool_get_params = set(inspect.signature(wh.WarehousePool.get).parameters)
    assert "run_id" not in open_params
    assert "run_id" not in pool_get_params
