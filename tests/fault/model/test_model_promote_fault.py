"""Crash safety of build promotion and cleanup (impl 02 FT02-03 … FT02-05; T02-21, R-40).

FT02-03 / FT02-04: a real child process (`tests.support.build_kill`) runs the full
`build_pipeline` on `lake_small` with a JSON fault plan loaded only from `HERNESS_FAULTS`
under `HERNESS_ENV=test`. `build.mid_sql kill nth=5` kills it inside stage `build`: the
file is an orphan, `CURRENT` unchanged, and the next run deletes the orphan and promotes.
`pipeline.before_promote kill` kills it at the promotion: `CURRENT` unchanged, and the retry
of the same job (same saved state) resumes at `promote`, re-runs DQ and promotes.
FT02-05: the old build is held open by a reader process (a DuckDB read-only connection,
which blocks the retire update, plus a plain file handle, which blocks deletion on Windows;
DuckDB 1.5 readers open the file with delete sharing, T02-21 spec note under U02-105).
The promotion defers the deletion; after the reader closes, the next promotion deletes it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog
from tests.support.lake_small import install, load_config
from tests.support.ops_store import OpsStoreHandle
from tests.support.promote_builds import build_id, make_build, statuses
from tests.support.sync_kill import KILLED_RETURNCODE

from herness.core import time as clock
from herness.core.resilience import ProcessState
from herness.model import _build_support as support
from herness.model.promote import promote_build
from herness.model.settings import BuildSettings
from herness.store import warehouse
from herness.store._warehouse_rw import open_for_build, write_current
from herness.store.layout import DataLayout

pytestmark = pytest.mark.fault

REPO = Path(__file__).resolve().parents[3]
CHILD_TIMEOUT_S = 300
RESULT_PREFIX = "BUILD_RESULT "
_READER = (
    "import duckdb, sys\n"
    "con = duckdb.connect(sys.argv[1], read_only=True)\n"
    "handle = open(sys.argv[1], 'rb')\n"
    "print('ready', flush=True)\n"
    "sys.stdin.read()\n"
)


class Lake:
    """`lake_small` under the ops store's data root and the config tree pointing at it."""

    def __init__(self, tmp_path: Path, ops_store: OpsStoreHandle) -> None:
        self.data = ops_store.data_root
        install(self.data)
        load_config(tmp_path / "cfgroot", self.data)
        self.config_dir = tmp_path / "cfgroot" / "config"
        self.layout = DataLayout.from_root(self.data)
        self.tmp = tmp_path

    def child(
        self, state: str, plan: list[dict[str, Any]] | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Run the full pipeline in a child process with job state file ``state``."""
        env = dict(os.environ)
        env["HERNESS_ENV"] = "test"
        env["PYTHONUTF8"] = "1"
        env.pop("HERNESS_FAULTS", None)
        if plan is not None:
            plan_path = self.tmp / f"plan-{state}.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            env["HERNESS_FAULTS"] = str(plan_path)
        argv = [
            sys.executable,
            "-m",
            "tests.support.build_kill",
            str(self.config_dir),
            str(self.data),
            str(self.tmp / f"{state}.json"),
        ]
        return subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv,
            cwd=REPO,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=CHILD_TIMEOUT_S,
            check=False,
        )

    def state(self, state: str) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads((self.tmp / f"{state}.json").read_text("utf-8"))
        return loaded

    def current(self) -> str | None:
        return warehouse.read_current(layout=self.layout)


@pytest.fixture
def lake(tmp_path: Path, ops_store: OpsStoreHandle, reset_process_state: ProcessState) -> Lake:
    del reset_process_state
    return Lake(tmp_path, ops_store)


def _result(done: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    [line] = [x for x in done.stdout.splitlines() if x.startswith(RESULT_PREFIX)]
    outcome: dict[str, Any] = json.loads(line.removeprefix(RESULT_PREFIX))
    assert outcome["status"] == "done", outcome
    return outcome["result"]  # type: ignore[no-any-return]


def _killed(done: subprocess.CompletedProcess[str]) -> str:
    assert done.returncode == KILLED_RETURNCODE, (done.returncode, done.stderr[-3000:])
    assert RESULT_PREFIX not in done.stdout
    logs = done.stdout + done.stderr
    assert "resilience.faults.enabled" in logs  # the plan came from HERNESS_FAULTS
    assert "resilience.faults.kill" in logs  # died at the fault point, not elsewhere
    return logs


def test_ft02_03_kill_mid_sql_leaves_orphan_next_run_promotes(lake: Lake) -> None:
    """FT02-03 `build.mid_sql kill nth=5` in a subprocess job: the killed build is an orphan
    (`building`, no `finished_at`), `CURRENT` unchanged; the next run deletes the orphan and
    promotes its own build."""
    first = _result(lake.child("job1"))["build_id"]
    assert lake.current() == first

    _killed(lake.child("job2", [{"point": "build.mid_sql", "action": "kill", "nth": 5}]))
    assert lake.current() == first
    [orphan] = [b for b in warehouse.list_builds(layout=lake.layout) if b.build_id != first]
    assert (orphan.status, orphan.finished_at) == ("building", None)

    rerun = lake.child("job2")
    result = _result(rerun)
    assert result["build_id"] not in (first, orphan.build_id)
    assert result["promoted"] is True
    assert lake.current() == result["build_id"]
    assert statuses(lake.layout) == {result["build_id"]: "promoted", first: "retired"}
    logs = (rerun.stdout + rerun.stderr).splitlines()
    deleted = [line for line in logs if "model.build.orphan_deleted" in line]
    assert len(deleted) == 1
    assert orphan.build_id in deleted[0]


def test_ft02_04_kill_before_promote_retry_resumes_at_promote(lake: Lake) -> None:
    """FT02-04 `pipeline.before_promote kill`: `CURRENT` unchanged after the kill; the retry
    (same job state) resumes at `promote`, re-runs DQ and promotes (spec 08 F8)."""
    first = _result(lake.child("job1"))["build_id"]

    _killed(lake.child("job2", [{"point": "pipeline.before_promote", "action": "kill"}]))
    assert lake.current() == first
    saved = lake.state("job2")
    assert saved["stages_done"] == ["build", "enrich", "score", "dq"]
    killed_id = saved["build_id"]
    assert statuses(lake.layout)[killed_id] == "building"

    retry = lake.child("job2")
    result = _result(retry)
    assert result["build_id"] == killed_id
    assert list(result["durations_ms"]) == ["promote"]
    assert result["dq"]["checks"] > 0  # DQ ran again in the retry
    assert "model.dq.evaluated" in retry.stdout + retry.stderr
    assert lake.current() == killed_id
    assert statuses(lake.layout) == {killed_id: "promoted", first: "retired"}


@pytest.fixture
def reader(tmp_path: Path) -> Iterator[Any]:
    """Factory: hold a build file open in a reader process until the returned stop()."""
    procs: list[subprocess.Popen[str]] = []

    def start(path: Path) -> Any:
        proc = subprocess.Popen(  # noqa: S603 - fixed interpreter and inline script
            [sys.executable, "-c", _READER, str(path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        procs.append(proc)
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "ready"

        def stop() -> None:
            assert proc.stdin is not None
            proc.stdin.close()
            proc.wait(timeout=30)

        return stop

    yield start
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=30)


def _promote(layout: DataLayout, build: str) -> None:
    make_build(layout, build, "building", finished=False)
    view = support.DbSettings(threads=1, memory_limit="512MiB")
    con = open_for_build(build, create=False, cfg=view, layout=layout)
    promote_build(con, build, build_cfg=BuildSettings(keep_last=1), layout=layout, now=clock.now())


@pytest.mark.skipif(os.name != "nt", reason="POSIX unlink succeeds while a reader holds the file")
def test_ft02_05_reader_defers_deletion_until_closed(
    ops_store: OpsStoreHandle, reader: Any
) -> None:
    """FT02-05 the old build is held open by a reader process: the promotion defers its
    retirement and its deletion (`deferred`); after the reader closes, the next promotion
    deletes it."""
    layout = DataLayout.from_root(ops_store.data_root)
    old = build_id(1)
    make_build(layout, old, "promoted")
    write_current(old, layout=layout)
    stop = reader(warehouse.build_path(old, layout=layout))
    with structlog.testing.capture_logs() as logs:
        _promote(layout, build_id(2))
    assert warehouse.read_current(layout=layout) == build_id(2)
    [deferred] = [e for e in logs if e["event"] == "model.build.retire_deferred"]
    assert deferred["build_id"] == old
    [cleanup] = [e for e in logs if e["event"] == "model.build.cleanup"]
    assert (cleanup["deleted"], cleanup["deferred"]) == (0, 1)
    assert [e["build_id"] for e in logs if e["event"] == "store.warehouse.delete_deferred"] == [old]
    assert statuses(layout) == {build_id(2): "promoted", old: "promoted"}
    stop()
    with structlog.testing.capture_logs() as logs:
        _promote(layout, build_id(3))
    assert statuses(layout) == {build_id(3): "promoted"}
    deleted = sorted(e["build_id"] for e in logs if e["event"] == "store.warehouse.deleted")
    assert deleted == [old, build_id(2)]
    assert not warehouse.build_path(old, layout=layout).exists()
