"""Integration tests for herness.store.warehouse: hardened reader (ST02-04), locked builds."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest

from herness.store.layout import DataLayout
from herness.store.warehouse import build_path, list_builds, open_readonly

pytestmark = pytest.mark.integration

ID_A = "20260101-000000-AAAAAA"
ID_B = "20260102-000000-BBBBBB"
_WRITER = (
    "import duckdb, sys\n"
    "con = duckdb.connect(sys.argv[1])\n"
    "print('ready', flush=True)\n"
    "sys.stdin.read()\n"
)


@pytest.fixture
def layout(tmp_path: Path) -> DataLayout:
    lay = DataLayout.from_root(tmp_path / "data")
    lay.warehouse.mkdir(parents=True)
    for build_id in (ID_A, ID_B):
        con = duckdb.connect(str(build_path(build_id, layout=lay)))
        con.execute("CREATE SCHEMA meta")
        con.execute(
            "CREATE TABLE meta.build (build_id VARCHAR, started_at TIMESTAMPTZ,"
            " finished_at TIMESTAMPTZ, status VARCHAR)"
        )
        con.execute("INSERT INTO meta.build VALUES (?, now(), now(), 'promoted')", [build_id])
        con.close()
    (lay.warehouse / "CURRENT").write_text(f"{ID_A}\n", encoding="ascii")
    lay.ops_db.write_text("a,b\n1,2\n", encoding="ascii")
    return lay


def _attacks(lay: DataLayout) -> list[str]:
    ops = lay.ops_db.as_posix()
    other = build_path(ID_B, layout=lay).as_posix()
    out = (lay.root / "out.csv").as_posix()
    return [
        f"COPY meta.build TO '{out}'",
        f"ATTACH '{other}' AS other",
        f"SELECT * FROM read_csv('{ops}')",  # noqa: S608 - fixed test path
        "SELECT * FROM read_csv('ops.sqlite')",
        "SET enable_external_access = true",
        "RESET enable_external_access",
        "SET lock_configuration = false",
        "INSTALL httpfs",
        "LOAD httpfs",
        "CREATE TABLE meta.x (a INTEGER)",
    ]


def test_st02_04_readonly_blocks_escapes(layout: DataLayout) -> None:
    """ST02-04 COPY, ATTACH, read_csv, SET enable_external_access, INSTALL httpfs all fail."""
    con = open_readonly(layout=layout)
    try:
        for statement in _attacks(layout):
            with pytest.raises(duckdb.Error):
                con.execute(statement).fetchall()
        assert not (layout.root / "out.csv").exists()
        row = con.execute("SELECT current_setting('enable_external_access')").fetchone()
        assert row == (False,)
    finally:
        con.close()


def test_st02_04_cursor_inherits_hardening(layout: DataLayout) -> None:
    """ST02-04 a per-thread cursor keeps the locked, read-only settings."""
    con = open_readonly(layout=layout)
    try:
        cur = con.cursor()
        query = f"SELECT * FROM read_csv('{layout.ops_db.as_posix()}')"  # noqa: S608 - test path
        with pytest.raises(duckdb.Error):
            cur.execute(query).fetchall()
        with pytest.raises(duckdb.Error):
            cur.execute("SET enable_external_access = true")
    finally:
        con.close()


@pytest.fixture
def writer_process(layout: DataLayout) -> Iterator[subprocess.Popen[str]]:
    path = str(build_path(ID_B, layout=layout))
    proc = subprocess.Popen(  # noqa: S603 - fixed interpreter and inline script
        [sys.executable, "-c", _WRITER, path],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stdin is not None
    try:
        assert proc.stdout.readline().strip() == "ready"
        yield proc
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)


def test_ut02_21_list_builds_locked_by_writer(
    layout: DataLayout, writer_process: subprocess.Popen[str]
) -> None:
    """UT02-21 a file held by a writer in a subprocess -> locked."""
    statuses = {b.build_id: b.status for b in list_builds(layout=layout)}
    assert statuses == {ID_B: "locked", ID_A: "promoted"}
