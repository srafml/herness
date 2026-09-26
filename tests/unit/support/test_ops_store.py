"""Unit tests for tests.support.ops_store: fixture `ops_store`, `OpsStoreHandle` (UT11-117)."""

from __future__ import annotations

import os
import re
from importlib import resources
from pathlib import Path

import pytest
from tests.support.ops_store import OpsStoreHandle

pytestmark = pytest.mark.unit

_HIGHEST = max(
    int(m[1])
    for e in resources.files("herness.store.migrations").iterdir()
    if (m := re.fullmatch(r"([0-9]{3})_[a-z0-9_]+\.sql", e.name))
)

INNER = '''
from pathlib import Path

import pytest
from tests.support.config_tree import write_full_config

from herness.core.config import load_config
from herness.store.ops import connection, run_write
from herness.store.ops.migrate import pending_migrations, schema_version

pytestmark = pytest.mark.unit
HIGHEST = {highest}
RUN = (
    "INSERT INTO run (run_id, kind, depth, profile, config_hash, started_at)"
    " VALUES ('r1', 'funding_review', 'standard', 'default', 'h', '2026-09-26T10:00:00.000000Z')"
)
seen = []


def run_count():
    return int(connection().execute("SELECT count(*) AS n FROM run").fetchone()["n"])


def check_fresh(ops_store, tmp_path):
    assert ops_store.data_root == tmp_path / "data"
    assert ops_store.db_path == tmp_path / "data" / "ops.sqlite"
    assert ops_store.migration.applied
    assert pending_migrations() == []
    assert schema_version() == HIGHEST == ops_store.migration.version
    # the config tree lives outside tmp_path/data, so only HERNESS_PATHS__DATA can match
    cfg = load_config(config_dir=write_full_config(tmp_path / "cfgroot"))
    assert cfg.paths.data == ops_store.data_root


def test_ut99_11_first(ops_store, tmp_path: Path) -> None:
    """UT99-11"""
    check_fresh(ops_store, tmp_path)
    run_write(lambda conn: conn.execute(RUN), op="test_write")
    assert run_count() == 1
    seen.append(ops_store)


def test_ut99_12_second(ops_store, tmp_path: Path) -> None:
    """UT99-12"""
    check_fresh(ops_store, tmp_path)
    assert run_count() == 0
    first = seen[0]
    assert first.data_root != ops_store.data_root
    first.db_path.unlink()  # no open handle from the first test's teardown remains
    files = [row["file"] for row in connection().execute("PRAGMA database_list")]
    assert str(first.db_path) not in files
'''


def test_ut11_117_sequential_tests_get_fresh_isolated_stores(pytester: pytest.Pytester) -> None:
    """UT11-117 two tests using `ops_store` run in sequence (inner session): each `db_path` is
    under its own `tmp_path`; the store is fully migrated (`schema_version()` equals the highest
    migration file, nothing pending); the loaded config's `paths.data` is the data root via the
    override only; the first writes a `run` row, the second sees 0 rows, deletes the first
    test's file (no open handle) and `connection()` no longer points at it."""
    assert _HIGHEST > 0
    pytester.makepyfile(test_inner=INNER.format(highest=_HIGHEST))
    result = pytester.runpytest("-p", "tests.conftest", "-p", "no:asyncio")
    result.assert_outcomes(passed=2)


def test_ut11_117_handle_is_path_compatible(ops_store: OpsStoreHandle) -> None:
    """UT11-117 `OpsStoreHandle` behaves like the interim `ops_store: Path` API (merge-compat):
    `os.PathLike`, delegated attribute access and `/`; an uninitialised handle does not recurse."""
    assert os.fspath(ops_store) == str(ops_store.db_path)
    assert Path(ops_store) == ops_store.db_path
    assert ops_store.is_file()  # delegated to db_path via __getattr__
    assert ops_store.name == ops_store.db_path.name
    assert (ops_store / "sibling") == ops_store.db_path / "sibling"
    blank = object.__new__(OpsStoreHandle)
    with pytest.raises(AttributeError):
        _ = blank.db_path
    with pytest.raises(AttributeError):
        _ = blank.name
    with pytest.raises(AttributeError):
        _ = blank.__deepcopy__
