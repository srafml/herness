"""Unit tests for tests.support.ops_store: fixture `ops_store`, `OpsStoreHandle` (UT11-117)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from tests.support.config_tree import write_full_config
from tests.support.ops_store import OpsStoreHandle

from herness.core.config import load_config
from herness.store.ops import connection, run_write
from herness.store.ops.migrate import schema_version

pytestmark = pytest.mark.unit

_RUN = (
    "INSERT INTO run (run_id, kind, depth, profile, config_hash, started_at)"
    " VALUES ('r1', 'funding_review', 'standard', 'default', 'h', '2026-09-26T10:00:00.000000Z')"
)
_seen: list[OpsStoreHandle] = []


def _run_count() -> int:
    row = connection().execute("SELECT count(*) AS n FROM run").fetchone()
    return int(row["n"])


def test_ut11_117_first_test_writes_a_run_row(ops_store: OpsStoreHandle, tmp_path: Path) -> None:
    """UT11-117 the first test's store is fresh and migrated, and a config loaded during the
    test has `paths.data` at the store's data root (U11-78 step 2)."""
    assert schema_version() == ops_store.migration.version
    cfg = load_config(config_dir=write_full_config(tmp_path))
    assert cfg.paths.data == ops_store.data_root
    run_write(lambda conn: conn.execute(_RUN), op="test_write")
    assert _run_count() == 1
    _seen.append(ops_store)


def test_ut11_117_second_test_has_its_own_fresh_store(ops_store: OpsStoreHandle) -> None:
    """UT11-117 the second test's store lives under its own `tmp_path`, is current and empty;
    the first test's file has no open handle left (deletable), and `connection()` no longer
    points at it."""
    first = _seen[0]
    assert ops_store.data_root != first.data_root
    assert ops_store.db_path != first.db_path
    assert schema_version() == ops_store.migration.version
    assert _run_count() == 0
    assert os.environ["HERNESS_PATHS__DATA"] == str(ops_store.data_root)
    first.db_path.unlink()  # no open handle from the first test's teardown remains
    files = [row["file"] for row in connection().execute("PRAGMA database_list")]
    assert str(first.db_path) not in files


def test_ut11_117_handle_is_path_compatible(ops_store: OpsStoreHandle) -> None:
    """UT11-117 `OpsStoreHandle` behaves like the interim `ops_store: Path` API (merge-compat):
    `os.PathLike`, delegated attribute access and `/`."""
    assert os.fspath(ops_store) == str(ops_store.db_path)
    assert Path(ops_store) == ops_store.db_path
    assert ops_store.is_file()  # delegated to db_path via __getattr__
    assert ops_store.name == ops_store.db_path.name
    assert (ops_store / "sibling") == ops_store.db_path / "sibling"
