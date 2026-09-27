"""Security tests of the build pipeline (impl 02 ST02-12 TH02-12, ST02-14 TH02-14; T02-18).

ST02-12 runs the `lake_small` build under the spec 10 socket guard with a loopback-only
allowlist plus a recording audit hook, and records every DuckDB connection the build opens.
ST02-14 builds `lake_small` with sentinel strings in ticket text and a failing cast, then a
build whose SQL fails on a literal, and looks for the sentinels in logs, `meta.*` and the
error. The static scan for `INSTALL`/`LOAD` in build SQL stays in UT02-61.
"""

from __future__ import annotations

import json
import shutil
import socket
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import duckdb
import pytest
import structlog
from tests.support.build_harness import FakeJobContext
from tests.support.lake_small import Row, install, load_config, write_lake
from tests.support.ops_store import OpsStoreHandle

from herness.core import egress_socket as es
from herness.core.errors import EgressBlocked
from herness.core.types import JobOutcome
from herness.model import build
from herness.model.build import run_build_pipeline
from herness.model.errors import BuildSqlError
from herness.model.sqlfiles import discover_sql_files
from herness.store import warehouse
from herness.store.layout import DataLayout

pytestmark = pytest.mark.integration

SENTINEL = "zq-sentinel-8812"
SENTINEL_TS = "zq-sentinel-ts-5531"
_OFFLINE_VARS = ("HF_HUB_OFFLINE", "HF_HUB_DISABLE_TELEMETRY", "DO_NOT_TRACK")
_SETTINGS = (
    "SELECT current_setting('autoinstall_known_extensions'),"
    " current_setting('autoload_known_extensions')"
)

# sys.addaudithook cannot be removed: one process-wide recorder, active only while a test
# has switched it on.
_recorder: dict[str, Any] = {"on": False, "events": [], "installed": False}


def _record(event: str, args: tuple[object, ...]) -> None:
    if _recorder["on"] and event.startswith("socket."):
        _recorder["events"].append((event, repr(args)[:200]))


def _layout(ops_store: OpsStoreHandle) -> DataLayout:
    return DataLayout.from_root(ops_store.data_root)


def _meta_text(build_id: str, layout: DataLayout) -> str:
    with warehouse.open_readonly(build_id, layout=layout) as con:
        parts = [
            con.execute(f"SELECT CAST(t AS VARCHAR) FROM meta.{name} AS t").fetchall()  # noqa: S608 - fixed names
            for name in ("build", "dq_result", "evidence")
        ]
    return json.dumps(parts, default=str)


def test_st02_12_build_opens_no_socket_and_no_autoload(
    tmp_path: Path,
    ops_store: OpsStoreHandle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ST02-12 TH02-12 the lake_small build under the strict socket guard makes no network
    attempt; every DuckDB connection (writer, inspector, reader) has autoinstall and
    autoload off."""
    install(ops_store.data_root)
    cfg = load_config(tmp_path / "cfgroot", ops_store.data_root)
    for var in _OFFLINE_VARS:
        monkeypatch.delenv(var, raising=False)  # restored after the test
    es.install_socket_guard(cfg)  # local profile, no sources, egress off: loopback only
    with pytest.raises(EgressBlocked):
        socket.create_connection(("example.com", 443), timeout=1)
    connections: list[tuple[object, ...]] = []
    real_connect = duckdb.connect

    def recording_connect(*args: Any, **kwargs: Any) -> duckdb.DuckDBPyConnection:
        con = real_connect(*args, **kwargs)
        settings = con.execute(_SETTINGS).fetchone()
        assert settings is not None
        connections.append(tuple(settings))
        return con

    monkeypatch.setattr(duckdb, "connect", recording_connect)
    if not _recorder["installed"]:
        sys.addaudithook(_record)
        _recorder["installed"] = True
    _recorder["events"] = []
    _recorder["on"] = True
    try:
        outcome = run_build_pipeline(FakeJobContext({"stages": ["build"]}))
        build_id = str(outcome.result["build_id"])
        [info] = warehouse.list_builds(layout=_layout(ops_store))  # inspector connection
        with warehouse.open_readonly(build_id, layout=_layout(ops_store)) as con:  # reader
            con.execute("SELECT count(*) FROM core.incident").fetchall()
    finally:
        _recorder["on"] = False
    assert (outcome.status, info.status) == ("done", "building")
    assert _recorder["events"] == []
    assert len(connections) >= 3, "writer, inspector and reader connections expected"
    assert set(connections) == {(False, False)}


def _with_sentinels(raw: Path) -> None:
    fields = {
        "number": "INC0099",
        "opened_at": SENTINEL_TS,  # a failing cast: counted in stg.cast_stats, never an error
        "short_description": SENTINEL,
        "description": f"text {SENTINEL} text",
        "close_notes": SENTINEL,
        "priority": SENTINEL,
    }
    write_lake(raw, {("servicenow", "incident"): [Row("s9", 3, fields)]})


def test_st02_14_no_row_values_in_logs_meta_or_errors(
    tmp_path: Path,
    ops_store: OpsStoreHandle,
    monkeypatch: pytest.MonkeyPatch,
    fake_job_context: Callable[..., FakeJobContext],
) -> None:
    """ST02-14 TH02-14 sentinel ticket text and a failing cast literal never reach logs or
    `meta.*`; a SQL failure on a literal gives a BuildSqlError without it."""
    raw = install(ops_store.data_root)
    _with_sentinels(raw)
    load_config(tmp_path / "cfgroot", ops_store.data_root)
    layout = _layout(ops_store)
    with structlog.testing.capture_logs() as logs:
        outcome = run_build_pipeline(fake_job_context({"stages": ["build"]}))
    assert isinstance(outcome, JobOutcome)
    good = str(outcome.result["build_id"])
    with warehouse.open_readonly(good, layout=layout) as con:
        stored = con.execute(
            "SELECT short_description, opened_at FROM core.incident WHERE number = 'INC0099'"
        ).fetchall()
    assert stored == [(SENTINEL, None)]  # the text is data; the bad timestamp became NULL
    sql_copy = tmp_path / "sql"
    shutil.copytree(Path(build.__file__).parent / "sql", sql_copy)
    (sql_copy / "230_incident.sql").write_text(
        f"SELECT CAST('{SENTINEL}' AS INTEGER);\n", encoding="utf-8"
    )
    monkeypatch.setattr(build, "discover_sql_files", lambda: discover_sql_files(sql_dir=sql_copy))
    with (
        structlog.testing.capture_logs() as failing_logs,
        pytest.raises(BuildSqlError) as caught,
    ):
        run_build_pipeline(fake_job_context({"stages": ["build"]}))
    error = caught.value
    assert error.file == "230_incident.sql"
    exposed = [str(error), error.db_error, repr(dict(error.context)), repr(dict(error.details))]
    assert "'?'" in error.db_error
    failed = next(b.build_id for b in warehouse.list_builds(layout=layout) if b.status == "failed")
    haystacks = [
        json.dumps(logs, default=str),
        json.dumps(failing_logs, default=str),
        _meta_text(good, layout),
        _meta_text(failed, layout),
        *exposed,
    ]
    for text in haystacks:
        assert SENTINEL not in text
        assert SENTINEL_TS not in text
