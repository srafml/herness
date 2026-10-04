"""Crash safety of the ServiceNow replay and its sliced backfill (impl 01 FT01-01, FT01-03; T01-25).

A real child process (``tests.fault.connectors._sn_child``) runs the `sync` job in backfill
mode over the seeded ``incident_table`` cassette (244 incidents in the first 60 hours from
2026-02-20) with the JSON plan rule ``connector.before_watermark kill nth=N source=servicenow``,
loaded only from ``HERNESS_FAULTS`` under ``HERNESS_ENV=test``. The kill lands after a slice's
``LakeWriter.commit()`` and before ``mark_slice_done`` (TH01-12), so that slice's rows are in the
lake while the slice is not `done`. One-day slices and one worker make the order deterministic.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest
from tests.fault.connectors import _sn_env as sn
from tests.support.fake_keyring import MemoryKeyring
from tests.support.ops_store import OpsStoreHandle
from tests.support.sn_cassettes import T0
from tests.support.sync_kill import KILLED_RETURNCODE

from herness.core.resilience import ProcessState
from herness.store.ops import get_watermark, reset_connections

pytestmark = pytest.mark.fault

_PLAN = {"point": "connector.before_watermark", "action": "kill", "source": "servicenow"}
_LAST = T0 + datetime.timedelta(hours=59, minutes=45)  # the newest cassette record
_COMPLETED = "connectors.backfill.slice_completed"


def _plan(tmp_path: Path, nth: int) -> Path:
    plan = tmp_path / f"kill_{nth}.json"
    plan.write_text(json.dumps([_PLAN | {"nth": nth}]), encoding="utf-8")
    return plan


def _versions(rows: list[dict[str, object]]) -> set[tuple[object, object]]:
    return {(r["_source_key"], r["_source_updated_at"]) for r in rows}


def test_ft01_01_kill_before_watermark_on_the_replay_reruns_to_the_same_core(
    tmp_path: Path,
    ops_store: OpsStoreHandle,
    reset_process_state: ProcessState,
    fake_keyring: MemoryKeyring,
) -> None:
    """FT01-01 `connector.before_watermark kill nth=2` on the ServiceNow replay (subprocess):
    the rerun's lake is a superset of the no-crash run's rows (the killed slice is read
    twice) and after the build `core.*` and the staging equal those of a no-crash run."""
    del reset_process_state, fake_keyring
    span = ("2026-02-20", "2026-02-23")  # three one-day slices, all with rows
    config_dir = sn.make_tree(tmp_path, ops_store.db_path)
    crash_root = tmp_path
    base_root = tmp_path / "base"
    base_db = base_root / "data" / "ops.sqlite"
    base_cfg = sn.make_tree(base_root, base_db)

    # 1. the killed run: it dies after slice 2 committed, before slice 2 is marked done
    killed = sn.child(config_dir, ops_store.db_path, span, _plan(tmp_path, 2))
    assert killed.returncode == KILLED_RETURNCODE, (killed.returncode, killed.stderr[-3000:])
    assert sn.RESULT_PREFIX not in killed.stdout
    logs = killed.stdout + killed.stderr
    assert "resilience.faults.enabled" in logs  # the plan came from HERNESS_FAULTS
    assert "resilience.faults.kill" in logs  # died at the fault point, not elsewhere
    after_kill = sn.lake_rows(crash_root / "data")
    assert after_kill, "the kill must land after LakeWriter.commit()"
    assert [s[1] for s in sn.slices(ops_store.db_path)] == ["done", "running", "pending"]
    reset_connections(path=ops_store.db_path)
    assert get_watermark("servicenow", "incident") is None  # backfill: set once, at the end

    # 2. the rerun (no plan): slices 2 and 3 run, the job is done
    rerun = sn.result(sn.child(config_dir, ops_store.db_path, span, None))
    assert rerun["result"]["partial"] is False
    assert [s[1] for s in sn.slices(ops_store.db_path)] == ["done"] * 3
    crashed_rows = sn.lake_rows(crash_root / "data")

    # 3. the no-crash run over its own tree
    clean = sn.result(sn.child(base_cfg, base_db, span, None))
    assert clean["result"]["partial"] is False
    clean_rows = sn.lake_rows(base_root / "data")
    assert len(clean_rows) == 244  # every cassette record, once

    # the rerun's lake is a superset: the killed run's slice is there twice, nothing is lost
    assert _versions(clean_rows) <= _versions(crashed_rows)
    day_two = _in_day(after_kill, 1)
    assert day_two, "the killed slice must hold rows"
    assert len(crashed_rows) == len(clean_rows) + day_two

    # 4. after the build the crashed lake equals the no-crash lake in `core.*` and staging
    crashed_state = sn.core_state(crash_root)
    clean_state = sn.core_state(base_root)
    assert [t for t in clean_state if t.startswith("core.")], "the build produced no core.*"
    assert crashed_state == clean_state
    assert len(clean_state["stg.sn_incident"]) == 240  # the 4 audited deletes name unseen keys


def _in_day(rows: list[dict[str, object]], day: int) -> int:
    """Rows of ``rows`` whose ``_source_updated_at`` lies in day ``day`` (0-based) of the span."""
    lo = T0 + datetime.timedelta(days=day)
    hi = lo + datetime.timedelta(days=1)
    stamps = [r["_source_updated_at"] for r in rows]
    return sum(1 for t in stamps if isinstance(t, datetime.datetime) and lo <= t < hi)


def test_ft01_03_kill_after_three_of_ten_slices_restarts_with_seven(
    tmp_path: Path,
    ops_store: OpsStoreHandle,
    reset_process_state: ProcessState,
    fake_keyring: MemoryKeyring,
) -> None:
    """FT01-03 kill the job process after 3 of 10 slices are `done`: the restart runs 7
    slices (the killed one included, not the 3 done ones) and the final watermark is
    `min(end, max)` of the committed records."""
    del reset_process_state, fake_keyring
    span = ("2026-02-17", "2026-02-27")  # ten one-day slices; rows only in slices 4 to 6
    end = datetime.datetime(2026, 2, 27, tzinfo=datetime.UTC)
    config_dir = sn.make_tree(tmp_path, ops_store.db_path)

    # 1. the 4th slice's commit is followed by the kill: slices 1-3 are done
    killed = sn.child(config_dir, ops_store.db_path, span, _plan(tmp_path, 4))
    assert killed.returncode == KILLED_RETURNCODE, (killed.returncode, killed.stderr[-3000:])
    assert sn.RESULT_PREFIX not in killed.stdout
    assert (killed.stdout + killed.stderr).count(_COMPLETED) == 3
    status = [s[1] for s in sn.slices(ops_store.db_path)]
    assert status == ["done"] * 3 + ["running"] + ["pending"] * 6
    reset_connections(path=ops_store.db_path)
    assert get_watermark("servicenow", "incident") is None

    # 2. the restart runs the 7 slices that are not done
    restart = sn.child(config_dir, ops_store.db_path, span, None)
    outcome = sn.result(restart)
    assert (restart.stdout + restart.stderr).count(_COMPLETED) == 7
    assert [s[1] for s in sn.slices(ops_store.db_path)] == ["done"] * 10
    (done,) = outcome["result"]["results"]
    assert done["rows"] == 244  # the cassette's incidents, all in slices 4 to 6
    assert outcome["result"]["partial"] is False

    # 3. watermark = min(end, max committed _source_updated_at of the restart): the newest record
    reset_connections(path=ops_store.db_path)
    wm = get_watermark("servicenow", "incident")
    assert wm is not None
    assert wm.value == min(end, _LAST)
    assert wm.value < end
