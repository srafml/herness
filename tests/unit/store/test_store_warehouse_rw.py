"""Tests for herness.store._warehouse_rw (U02-34, U02-35): the warehouse write side."""

from __future__ import annotations

import ast
import datetime
import os
from pathlib import Path
from typing import Any

import duckdb
import pytest
from structlog.testing import capture_logs

from herness.core.errors import ConfigError, SchemaViolation, StoreBusy
from herness.model.settings import BuildSettings
from herness.store import _warehouse_rw as rw
from herness.store.errors import NotFoundError
from herness.store.layout import DataLayout
from herness.store.warehouse import build_path, new_build_id

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
ID_A = "20260101-000000-AAAAAA"
ID_B = "20260102-000000-BBBBBB"
NOW = datetime.datetime(2026, 1, 5, 12, 0, tzinfo=datetime.UTC)


@pytest.fixture
def layout(tmp_path: Path) -> DataLayout:
    lay = DataLayout.from_root(tmp_path / "data")
    lay.warehouse.mkdir(parents=True)
    return lay


def make_build(lay: DataLayout, build_id: str) -> Path:
    path = build_path(build_id, layout=lay)
    duckdb.connect(str(path)).close()
    return path


def set_current(lay: DataLayout, content: str | bytes) -> None:
    data = content.encode("ascii") if isinstance(content, str) else content
    (lay.warehouse / "CURRENT").write_bytes(data)


def cfg(*, threads: int | None = None, memory_limit: str = "1GB") -> BuildSettings:
    # "1GB", not the BuildSettings default "75%": DuckDB 1.5.5 rejects a percentage
    # memory_limit outright (see test_ut02_20_open_for_build_percent_memory_limit_rejected).
    return BuildSettings(threads=threads, memory_limit=memory_limit)


# --- UT02-20 (build part): open_for_build -----------------------------------------


def test_ut02_20_open_for_build_settings(layout: DataLayout) -> None:
    """UT02-20 build: temp dir set, time zone UTC, extensions off, external access on."""
    build_id = new_build_id(NOW)
    with capture_logs() as logs:
        con = rw.open_for_build(build_id, create=True, cfg=cfg(threads=2), layout=layout)
    try:

        def setting(name: str) -> object:
            row = con.execute(f"SELECT current_setting('{name}')").fetchone()
            assert row is not None
            return row[0]

        assert setting("TimeZone") == "UTC"
        assert setting("autoinstall_known_extensions") is False
        assert setting("autoload_known_extensions") is False
        assert setting("enable_external_access") is True
        assert setting("threads") == 2
        assert setting("access_mode") != "read_only"
        spill = layout.warehouse / "tmp" / build_id
        assert Path(str(setting("temp_directory"))) == spill
        cursor_tz = con.cursor().execute("SELECT current_setting('TimeZone')").fetchone()
        assert cursor_tz == ("UTC",)
    finally:
        con.close()
    assert build_path(build_id, layout=layout).is_file()
    assert (layout.warehouse / "tmp" / build_id).is_dir()
    [event] = [e for e in logs if e["event"] == "store.warehouse.opened_for_build"]
    assert (event["build_id"], event["create"]) == (build_id, True)


def test_ut02_20_open_for_build_default_threads(layout: DataLayout) -> None:
    """UT02-20 threads None falls back to os.cpu_count()."""
    build_id = new_build_id(NOW)
    con = rw.open_for_build(build_id, create=True, cfg=cfg(), layout=layout)
    try:
        row = con.execute("SELECT current_setting('threads')").fetchone()
        assert row is not None
        assert row[0] == (os.cpu_count() or 1)
    finally:
        con.close()


def test_ut02_20_open_for_build_create_conflict(layout: DataLayout) -> None:
    """UT02-20 create=True on an existing build file -> ConfigError, nothing opened."""
    make_build(layout, ID_A)
    with pytest.raises(ConfigError, match="already exists"):
        rw.open_for_build(ID_A, create=True, cfg=cfg(), layout=layout)


def test_ut02_20_open_for_build_missing_required(layout: DataLayout) -> None:
    """UT02-20 create=False on a missing build file -> ConfigError."""
    with pytest.raises(ConfigError, match="does not exist"):
        rw.open_for_build(ID_A, create=False, cfg=cfg(), layout=layout)


def test_ut02_20_open_for_build_reopen_existing(layout: DataLayout) -> None:
    """UT02-20 create=False on an existing build file opens it writable."""
    make_build(layout, ID_A)
    con = rw.open_for_build(ID_A, create=False, cfg=cfg(), layout=layout)
    try:
        con.execute("CREATE TABLE t (x INTEGER)")
        con.execute("INSERT INTO t VALUES (1)")
    finally:
        con.close()


def test_ut02_20_open_for_build_invalid_id(layout: DataLayout) -> None:
    """UT02-20 an invalid build ID -> SchemaViolation before any path is opened."""
    with pytest.raises(SchemaViolation):
        rw.open_for_build("../x", create=True, cfg=cfg(), layout=layout)


def test_ut02_20_open_for_build_lock_error(
    layout: DataLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT02-20 a lock IOException on connect -> StoreBusy."""

    def fail(*args: Any, **kwargs: Any) -> None:
        msg = 'IO Error: Could not set lock on file "x": Conflicting lock is held'
        raise duckdb.IOException(msg)

    monkeypatch.setattr(duckdb, "connect", fail)
    with pytest.raises(StoreBusy):
        rw.open_for_build(new_build_id(NOW), create=True, cfg=cfg(), layout=layout)


def test_ut02_20_open_for_build_corrupt_file(layout: DataLayout) -> None:
    """UT02-20 an existing non-DuckDB file -> SchemaViolation."""
    build_path(ID_A, layout=layout).write_bytes(b"junk" * 100)
    with pytest.raises(SchemaViolation, match="cannot open warehouse"):
        rw.open_for_build(ID_A, create=False, cfg=cfg(), layout=layout)


def test_ut02_20_open_for_build_percent_memory_limit_rejected(layout: DataLayout) -> None:
    """UT02-20 deviation: the pinned DuckDB 1.5.5 rejects BuildSettings' default '75%'.

    ``memory_limit`` is passed through verbatim (U02-34 algorithm step 3); DuckDB 1.5.5
    has no '%' unit for ``memory_limit`` in the config dict, so the *default*
    ``BuildSettings()`` cannot open a build today. Recorded as a concern, not fixed here:
    converting a percentage to an absolute size is outside this card's unit spec.
    """
    with pytest.raises(SchemaViolation, match="cannot open warehouse"):
        rw.open_for_build(new_build_id(NOW), create=True, cfg=BuildSettings(), layout=layout)


def test_ut02_20_open_for_build_hardening_fails(
    layout: DataLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT02-20 a failing time-zone statement -> SchemaViolation; connection closed."""
    monkeypatch.setattr(rw, "_SET_TZ", "SET no_such_setting = 1")
    with pytest.raises(SchemaViolation, match="cannot open warehouse"):
        rw.open_for_build(new_build_id(NOW), create=True, cfg=cfg(), layout=layout)


# --- UT02-23: write_current --------------------------------------------------------


def test_ut02_23_write_current_first_switch(layout: DataLayout) -> None:
    """UT02-23 no prior CURRENT: content is <id>\\n, no temp left, previous is None."""
    make_build(layout, ID_A)
    with capture_logs() as logs:
        previous = rw.write_current(ID_A, layout=layout)
    assert previous is None
    assert (layout.warehouse / "CURRENT").read_bytes() == f"{ID_A}\n".encode("ascii")
    assert list(layout.warehouse.glob(".CURRENT.tmp-*")) == []
    [event] = [e for e in logs if e["event"] == "store.warehouse.current_switched"]
    assert (event["previous"], event["build_id"]) == (None, ID_A)


def test_ut02_23_write_current_returns_previous(layout: DataLayout) -> None:
    """UT02-23 an existing CURRENT is returned and replaced."""
    make_build(layout, ID_A)
    make_build(layout, ID_B)
    set_current(layout, f"{ID_A}\n")
    previous = rw.write_current(ID_B, layout=layout)
    assert previous == ID_A
    assert (layout.warehouse / "CURRENT").read_bytes() == f"{ID_B}\n".encode("ascii")
    assert list(layout.warehouse.glob(".CURRENT.tmp-*")) == []


def test_ut02_23_write_current_missing_build(layout: DataLayout) -> None:
    """UT02-23 build file missing -> NotFoundError, CURRENT untouched."""
    with pytest.raises(NotFoundError) as info:
        rw.write_current(ID_A, layout=layout)
    assert (info.value.kind, info.value.key) == ("build", ID_A)
    assert not (layout.warehouse / "CURRENT").exists()


def test_ut02_23_write_current_invalid_id(layout: DataLayout) -> None:
    """UT02-23 an invalid build ID -> SchemaViolation before any write."""
    with pytest.raises(SchemaViolation):
        rw.write_current("../x", layout=layout)


def test_ut02_23_write_current_previous_read_error_is_none(layout: DataLayout) -> None:
    """UT02-23 a tampered CURRENT makes the previous read fail; previous is None."""
    make_build(layout, ID_A)
    set_current(layout, b"../evil")
    previous = rw.write_current(ID_A, layout=layout)
    assert previous is None
    assert (layout.warehouse / "CURRENT").read_bytes() == f"{ID_A}\n".encode("ascii")


def test_ut02_23_write_current_permission_error(
    layout: DataLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT02-23 os.replace raising PermissionError -> StoreBusy; temp removed."""
    make_build(layout, ID_A)

    def locked(*args: Any, **kwargs: Any) -> None:
        msg = "sharing violation"
        raise PermissionError(msg)

    monkeypatch.setattr(os, "replace", locked)
    with pytest.raises(StoreBusy):
        rw.write_current(ID_A, layout=layout)
    assert list(layout.warehouse.glob(".CURRENT.tmp-*")) == []


def test_ut02_23_write_current_other_oserror(
    layout: DataLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT02-23 another OSError from os.replace -> SchemaViolation; temp removed."""
    make_build(layout, ID_A)

    def broken(*args: Any, **kwargs: Any) -> None:
        msg = "device error"
        raise OSError(msg)

    monkeypatch.setattr(os, "replace", broken)
    with pytest.raises(SchemaViolation):
        rw.write_current(ID_A, layout=layout)
    assert list(layout.warehouse.glob(".CURRENT.tmp-*")) == []


def test_ut02_23_write_current_directory_fsync_on_posix(
    layout: DataLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT02-23 the POSIX branch fsyncs the directory (forced here for CI on Windows)."""
    make_build(layout, ID_A)
    calls: list[Path] = []
    monkeypatch.setattr(rw, "_fsync", lambda p, _flags: calls.append(p))
    monkeypatch.setattr(os, "name", "posix")
    rw.write_current(ID_A, layout=layout)
    assert layout.warehouse in calls


def test_ut02_23_write_current_skips_dir_fsync_on_windows(
    layout: DataLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT02-23 the Windows branch skips the directory fsync."""
    make_build(layout, ID_A)
    calls: list[Path] = []
    monkeypatch.setattr(rw, "_fsync", lambda p, _flags: calls.append(p))
    monkeypatch.setattr(os, "name", "nt")
    rw.write_current(ID_A, layout=layout)
    assert layout.warehouse not in calls


# --- ST02-05: import contract ------------------------------------------------------


def _module_name(path: Path, root: Path) -> str:
    parts = path.relative_to(root).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _importers_of(target: str, tree_root: Path, repo_root: Path) -> set[str]:
    found: set[str] = set()
    for path in tree_root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        node = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for stmt in ast.walk(node):
            hit = False
            if isinstance(stmt, ast.Import):
                hit = any(a.name == target or a.name.startswith(f"{target}.") for a in stmt.names)
            elif isinstance(stmt, ast.ImportFrom) and stmt.module:
                hit = stmt.module == target or stmt.module.startswith(f"{target}.")
            if hit:
                found.add(_module_name(path, repo_root))
    return found


def test_st02_05_only_model_build_and_promote_import_rw() -> None:
    """ST02-05 an AST scan of herness/ and app/ finds no other importer of _warehouse_rw."""
    allowed = {"herness.model.build", "herness.model.promote"}
    importers = _importers_of("herness.store._warehouse_rw", ROOT / "herness", ROOT)
    if (ROOT / "app").is_dir():
        importers |= _importers_of("herness.store._warehouse_rw", ROOT / "app", ROOT)
    assert importers - allowed == set()
