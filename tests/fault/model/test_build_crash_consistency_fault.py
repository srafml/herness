"""Lake write crash consistency through the build (impl 02 FT02-06; U02-16, T02-22).

A real child process (`tests.support.lake_kill`) re-writes the `servicenow/incident` rows of
`lake_small` through a `LakeWriter` as three files and kills itself after the first rename of
`commit`: one file is final, two are still temps. The same write is then rerun to completion
(the retried sync), which leaves duplicate rows in the lake. A pipeline build of that lake
must ignore the temps, collapse the duplicates in staging and produce `core.*` row counts
(and digests) equal to the build of the clean `lake_small`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest
from tests.support.lake_small import FIXTURE, install, load_config
from tests.support.ops_store import OpsStoreHandle
from tests.support.sync_kill import KILLED_RETURNCODE
from tests.support.synth_build import core_digests

from herness.core.resilience import ProcessState
from herness.store import warehouse
from herness.store.layout import DataLayout

pytestmark = [pytest.mark.fault, pytest.mark.slow]

REPO = Path(__file__).resolve().parents[3]
CHILD_TIMEOUT_S = 300
BUILD_PREFIX = "BUILD_RESULT "
LAKE_PREFIX = "LAKE_RESULT "
SOURCE, ENTITY = "servicenow", "incident"


def _run(module: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.update({"HERNESS_ENV": "test", "PYTHONUTF8": "1"})
    env.pop("HERNESS_FAULTS", None)
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-m", module, *args],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=CHILD_TIMEOUT_S,
        check=False,
    )


def _line(done: subprocess.CompletedProcess[str], prefix: str) -> dict[str, Any]:
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    [line] = [x for x in done.stdout.splitlines() if x.startswith(prefix)]
    loaded: dict[str, Any] = json.loads(line.removeprefix(prefix))
    return loaded


def _build(tmp: Path, config_dir: Path, data: Path, state: str) -> str:
    done = _run("tests.support.build_kill", str(config_dir), str(data), str(tmp / f"{state}.json"))
    outcome = _line(done, BUILD_PREFIX)
    assert outcome["status"] == "done", outcome
    assert outcome["result"]["promoted"] is True
    return str(outcome["result"]["build_id"])


def _digests(layout: DataLayout, build_id: str) -> dict[str, dict[str, Any]]:
    con = warehouse.open_readonly(build_id, layout=layout)
    try:
        return core_digests(con)
    finally:
        con.close()


def _entity_files(raw: Path) -> tuple[list[Path], list[Path]]:
    base = raw / SOURCE / ENTITY
    final = sorted(base.glob("dt=*/part-*.parquet"))
    temps = sorted(p for p in base.glob("dt=*/.part-*") if p.suffix != ".parquet")
    return final, temps


def _record_ids(files: list[Path]) -> list[str]:
    ids: list[str] = []
    for f in files:
        ids.extend(pq.read_table(f, columns=["_record_id"]).column(0).to_pylist())
    return ids


def test_ft02_06_kill_between_renames_rerun_build_equals_clean_run(
    tmp_path: Path, ops_store: OpsStoreHandle, reset_process_state: ProcessState
) -> None:
    """FT02-06 a writer killed between the renames of `commit` leaves final and temp files;
    after the rerun into the same entity a build ignores the temps, collapses the duplicates
    and has `core.*` row counts equal to the build of the clean lake."""
    del reset_process_state
    data = ops_store.data_root
    raw = install(data)
    load_config(tmp_path / "cfgroot", data)
    config_dir = tmp_path / "cfgroot" / "config"
    layout = DataLayout.from_root(data)
    clean_ids = _record_ids(_entity_files(raw)[0])
    clean = _digests(layout, _build(tmp_path, config_dir, data, "clean"))
    assert clean["core.incident"]["rows"] > 0

    source_dir = FIXTURE / "raw"
    args = (str(raw), str(source_dir), SOURCE, ENTITY)
    killed = _run("tests.support.lake_kill", *args, "kill")
    assert killed.returncode == KILLED_RETURNCODE, (killed.returncode, killed.stderr[-3000:])
    assert "lake_kill.after_first_rename" in killed.stderr
    assert LAKE_PREFIX not in killed.stdout
    final, temps = _entity_files(raw)
    assert len(final) == 1 + 1  # the fixture's file and the one renamed file
    assert len(temps) == 2  # the other two files are still temps

    rerun = _line(_run("tests.support.lake_kill", *args), LAKE_PREFIX)
    assert rerun["files"] == 3
    final, temps = _entity_files(raw)
    assert len(final) == 1 + 1 + 3
    assert len(temps) == 2  # stale temps stay (spec 01 sweeps old ones); readers ignore them
    ids = _record_ids(final)
    assert len(ids) > len(set(ids)) == len(set(clean_ids))  # duplicates in the lake

    crashed = _digests(layout, _build(tmp_path, config_dir, data, "crashed"))
    assert {t: d["rows"] for t, d in crashed.items()} == {t: d["rows"] for t, d in clean.items()}
    assert crashed == clean
