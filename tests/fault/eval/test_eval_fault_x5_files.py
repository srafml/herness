"""Spec 11 fault case X5 (impl 11 FT11-05; F11-11; R-40, DD11-11; T11-33).

The files connector syncs the inbox the synthetic generator wrote for `tiny_build`
(`data/inbox/service_costs/service_costs.csv`, U11-21) in a real child process
(`tests.support.sync_kill`) whose JSON plan (`tests.support.faults`: `write_fault_plan`,
`fault_env`) kills it at `connector.before_watermark` for source `files`: after the lake
commit, before the `file_ingest` record (TH01-12). The rerun has no plan.

The files source keeps no `watermark` row (U01-50): its progress record is the `file_ingest`
row of the file's fingerprint, so "one watermark advance" is asserted as exactly one
`file_ingest` row written over the killed run and both reruns.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Final

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from tests.fault.connectors._files_env import load
from tests.support import faults
from tests.support.builds import BuildHandle
from tests.support.config_tree import REPO
from tests.support.fake_keyring import MemoryKeyring
from tests.support.ops_store import OpsStoreHandle
from tests.support.sync_env import SETTLED_S, write_sync_config
from tests.support.sync_kill import KILLED_RETURNCODE, RESULT_PREFIX

from herness.core.resilience import ProcessState
from herness.store.ops import read_all

pytestmark = pytest.mark.fault

CHILD_TIMEOUT_S: Final = 600  # F11-11 step 3
X5_RULE: Final = {"point": "connector.before_watermark", "action": "kill", "source": "files"}
SOURCES_YAML: Final = """\
version: 1
sources:
  files:
    enabled: true
    inbox: data/inbox
    entities:
      service_costs: {pattern: "*.csv", key_field: [service_name]}
"""
RAW: Final = ("raw", "files", "service_costs")


def _child(
    config_dir: Path, db_path: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """`python -m tests.support.sync_kill` (the `sync` job for `files`) to its end."""
    full = dict(os.environ, PYTHONUTF8="1", HERNESS_ENV="test")
    full.pop("HERNESS_FAULTS", None)
    full.update(env)
    argv = [sys.executable, "-m", "tests.support.sync_kill", str(config_dir), str(db_path)]
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        argv, cwd=REPO, env=full, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=CHILD_TIMEOUT_S, check=False,
    )  # fmt: skip


def _result(done: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert done.returncode == 0, done.stderr[-3000:]
    [line] = [x for x in done.stdout.splitlines() if x.startswith(RESULT_PREFIX)]
    outcome: dict[str, Any] = json.loads(line.removeprefix(RESULT_PREFIX))
    assert outcome["status"] == "done", outcome
    return outcome


def _ingest_rows() -> list[dict[str, Any]]:
    return [dict(r) for r in read_all("SELECT fingerprint, rows, files FROM file_ingest")]


def _lake(data: Path) -> pa.Table:
    files = sorted(p for p in data.joinpath(*RAW).rglob("*.parquet") if not p.name.startswith("."))
    return pa.concat_tables([pq.read_table(p) for p in files], promote_options="default")


def _generated_inbox(build: BuildHandle, data: Path) -> Path:
    """Copy the generator's inbox file under the test data root, past the settle time."""
    src = build.data_root / "inbox" / "service_costs" / "service_costs.csv"
    dst = data / "inbox" / "service_costs" / "service_costs.csv"
    dst.parent.mkdir(parents=True)
    shutil.copyfile(src, dst)
    past = time.time() - SETTLED_S - 60
    os.utime(dst, (past, past))
    return dst


def test_ft11_05_files_kill_before_watermark_nothing_ingested_twice(
    tmp_path: Path,
    ops_store: OpsStoreHandle,
    tiny_build: BuildHandle,
    reset_process_state: ProcessState,
    fake_keyring: MemoryKeyring,
) -> None:
    """FT11-05 X5 `connector.before_watermark kill source=files` over the generated inbox:
    the rerun ingests the file once (`file_ingest` fingerprint), a third run adds nothing,
    and exactly one progress record (`file_ingest` row) is written."""
    del reset_process_state, fake_keyring
    data = ops_store.data_root
    config_dir = write_sync_config(tmp_path, SOURCES_YAML)
    assert load(config_dir).paths.data == data
    inbox = _generated_inbox(tiny_build, data)
    fingerprint = hashlib.sha256(inbox.read_bytes()).hexdigest()
    n_services = len(inbox.read_text(encoding="utf-8").splitlines()) - 1
    assert n_services > 0
    plan = faults.write_fault_plan(tmp_path, [X5_RULE])

    killed = _child(config_dir, ops_store.db_path, faults.fault_env(plan))
    logs = killed.stdout + killed.stderr
    assert killed.returncode == KILLED_RETURNCODE, (killed.returncode, logs[-3000:])
    assert RESULT_PREFIX not in killed.stdout
    assert "resilience.faults.enabled" in logs  # the plan came from HERNESS_FAULTS
    assert "resilience.faults.kill" in logs  # died at the fault point
    assert _lake(data).num_rows == n_services  # committed before the kill
    assert _ingest_rows() == []  # ... and not recorded

    (second,) = _result(_child(config_dir, ops_store.db_path, {}))["results"]
    assert (second["entity"], second["rows"]) == ("service_costs", n_services)
    assert [(r["fingerprint"], r["rows"]) for r in _ingest_rows()] == [(fingerprint, n_services)]

    (third,) = _result(_child(config_dir, ops_store.db_path, {}))["results"]
    assert (third["rows"], third["files"]) == (0, [])  # the fingerprint is known: no re-ingest
    assert len(_ingest_rows()) == 1  # one progress record over all three runs
    assert read_all("SELECT entity FROM watermark WHERE source = 'files'") == []  # U01-50
    # the raw lake is at-least-once: each record from the killed commit and the rerun only
    counts = Counter(_lake(data).column("_record_id").to_pylist())
    assert len(counts) == n_services
    assert set(counts.values()) == {2}
