"""Crash safety of the files ingest path (impl 01 FT01-02; spec 11 X5, DD11-11, R-40; T01-13).

A real child process (`tests.support.sync_kill`) runs the `sync` job for the files source
with the JSON plan rule `connector.before_watermark kill nth=1 source=files`, loaded only
from `HERNESS_FAULTS` under `HERNESS_ENV=test`. The kill lands after `LakeWriter.commit()`
and before `record_file_ingest` (TH01-12): the lake holds the file's rows, `file_ingest`
does not know the file. The rerun (a child without a plan) re-ingests it exactly once, a
third run adds nothing, and the `core.*` build over the lake has no duplicates.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from tests.fault.connectors._files_env import (
    build,
    committed_files,
    core_duplicates,
    lake_table,
    load,
    open_build,
    staged_teams,
    write_config,
)
from tests.support.fake_keyring import MemoryKeyring
from tests.support.lake_small import GOLDEN_CORE, GOLDEN_COUNTS, core_snapshot, install
from tests.support.ops_store import OpsStoreHandle
from tests.support.sync_env import drop_inbox
from tests.support.sync_kill import KILLED_RETURNCODE, RESULT_PREFIX

from herness.core.resilience import ProcessState
from herness.store.ops import read_all

pytestmark = pytest.mark.fault

REPO = Path(__file__).resolve().parents[3]
CHILD_TIMEOUT_S = 240
X5_RULE = {"point": "connector.before_watermark", "action": "kill", "nth": 1, "source": "files"}
KEYS = ["T1", "T2", "T3"]


def _child(config_dir: Path, db_path: Path, plan: Path | None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["HERNESS_ENV"] = "test"
    env.pop("HERNESS_FAULTS", None)
    if plan is not None:
        env["HERNESS_FAULTS"] = str(plan)
    env["PYTHONUTF8"] = "1"
    argv = [sys.executable, "-m", "tests.support.sync_kill", str(config_dir), str(db_path)]
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


def _result(done: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert done.returncode == 0, done.stderr[-3000:]
    [line] = [x for x in done.stdout.splitlines() if x.startswith(RESULT_PREFIX)]
    outcome: dict[str, Any] = json.loads(line.removeprefix(RESULT_PREFIX))
    assert outcome["status"] == "done", outcome
    return outcome


def _ingest_rows() -> list[dict[str, Any]]:
    return [dict(r) for r in read_all("SELECT fingerprint, rows, files FROM file_ingest")]


def test_ft01_02_kill_before_record_reingests_the_file_once(
    tmp_path: Path,
    ops_store: OpsStoreHandle,
    reset_process_state: ProcessState,
    fake_keyring: MemoryKeyring,
) -> None:
    """FT01-02 `connector.before_watermark kill nth=1` on files (spec 11 X5): the child dies
    after the lake commit; the rerun re-ingests the file once, `file_ingest` has one row per
    fingerprint and `core.*` has no duplicates."""
    del reset_process_state, fake_keyring
    data = ops_store.data_root
    config_dir = write_config(tmp_path)
    assert load(config_dir).paths.data == data
    install(data)  # lake_small: every core.* table gets rows
    inbox_file = drop_inbox(data)
    fingerprint = hashlib.sha256(inbox_file.read_bytes()).hexdigest()
    plan = tmp_path / "x5_plan.json"
    plan.write_text(json.dumps([X5_RULE]), encoding="utf-8")

    # 1. the killed run: the kill lands after commit, before the file_ingest row
    killed = _child(config_dir, ops_store.db_path, plan)
    assert killed.returncode == KILLED_RETURNCODE, (killed.returncode, killed.stderr[-3000:])
    assert RESULT_PREFIX not in killed.stdout
    logs = killed.stdout + killed.stderr
    assert "resilience.faults.enabled" in logs  # the plan came from HERNESS_FAULTS
    assert "resilience.faults.kill" in logs  # died at the fault point, not elsewhere
    first_files = committed_files(data)
    assert first_files, "the kill must land after LakeWriter.commit()"
    assert sorted(lake_table(first_files).column("_source_key").to_pylist()) == KEYS
    assert _ingest_rows() == []
    # files never writes a watermark (U01-50): this is a guard, not the crash-safety proof
    assert read_all("SELECT entity FROM watermark WHERE source = 'files'") == []
    teams_dir = data / "raw" / "files" / "teams"
    assert [p.name for p in teams_dir.rglob(".*")] == []  # committed: no temp file left

    # 2. the rerun (no plan) re-ingests the file exactly once
    (second,) = _result(_child(config_dir, ops_store.db_path, None))["results"]
    assert (second["entity"], second["rows"], second["skipped_deleted"]) == ("teams", 3, 0)
    rows = _ingest_rows()
    assert [(r["fingerprint"], r["rows"]) for r in rows] == [(fingerprint, 3)]
    rerun_files = [p for p in committed_files(data) if p not in first_files]
    recorded = sorted(json.loads(str(rows[0]["files"])))
    assert recorded == sorted(p.relative_to(data).as_posix() for p in rerun_files)

    # 3. a third run finds the fingerprint and adds nothing
    (third,) = _result(_child(config_dir, ops_store.db_path, None))["results"]
    assert (third["rows"], third["files"]) == (0, [])
    assert len(_ingest_rows()) == 1
    # guard only: the files path never writes a watermark, killed or not
    assert read_all("SELECT entity FROM watermark WHERE source = 'files'") == []

    # the raw lake is at-least-once: each record twice (killed commit + rerun), never thrice
    lake = lake_table(committed_files(data))
    assert Counter(lake.column("_record_id").to_pylist()) == {f"files:teams:{k}": 2 for k in KEYS}

    # 4. the core.* build over the lake: no duplicates anywhere, core unchanged
    outcome = build()
    assert outcome.result["row_counts"] == json.loads(GOLDEN_COUNTS.read_text(encoding="utf-8"))
    with open_build(data, str(outcome.result["build_id"])) as con:
        duplicates = core_duplicates(con)
        assert duplicates, "the build produced no core.* tables"
        assert all(count == 0 for count in duplicates.values()), duplicates
        assert core_snapshot(con) == json.loads(GOLDEN_CORE.read_text(encoding="utf-8"))
        assert staged_teams(con) == [(f"files:teams:{k}", k) for k in KEYS]
