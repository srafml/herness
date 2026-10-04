"""Shared set-up of the ServiceNow crash tests (impl 01 FT01-01, FT01-03; T01-25).

Each run lives in its own tree: a full config tree (ServiceNow on the synthetic host, one
worker, one-day backfill slices) whose ``paths.data`` is ``<root>/data``, with its own
migrated ops store. ``child`` runs ``tests.fault.connectors._sn_child`` (a real process, the
fault plan only from ``HERNESS_FAULTS``); the lake and ops readers work on any root.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
from tests.fault.connectors._sn_child import RESULT_PREFIX
from tests.integration.model._stg_lake import build
from tests.support.build_harness import BuildHarness
from tests.support.sync_env import write_sync_config
from tests.unit.connectors import _servicenow_env as env

from herness.store.ops import read_all, reset_connections
from herness.store.ops.migrate import migrate

REPO = Path(__file__).resolve().parents[3]
CHILD_TIMEOUT_S = 300
INC_ENUM = {"servicenow.incident_state": {"1": "open", "2": "in_progress", "6": "resolved"}}
INC_ENUM["servicenow.incident_state"] |= {"7": "closed"}
KEY_QUERY = 'SELECT source_key, "number", state, source_updated_at FROM stg.sn_incident ORDER BY 1'


def sources_yaml() -> str:
    """ServiceNow enabled with one worker and one-day incident backfill slices."""
    text = env.sources_yaml()
    text = text.replace(
        "        window_hours: 48\n",
        "        window_hours: 48\n        backfill: {slice_days: 1}\n",
    )
    return text.replace("    entities:\n", "    max_concurrency: 1\n    entities:\n")


def make_tree(root: Path, db_path: Path) -> Path:
    """Write the config tree under ``root`` and migrate the ops store at ``db_path``."""
    config_dir = write_sync_config(root, sources_yaml())
    db_path.parent.mkdir(parents=True, exist_ok=True)
    reset_connections(path=db_path)
    migrate()
    return config_dir


def child(
    config_dir: Path, db_path: Path, span: tuple[str, str], plan: Path | None
) -> subprocess.CompletedProcess[str]:
    """Run the backfill job over ``span`` in a real process (``plan`` = a fault plan file)."""
    environ = dict(os.environ, HERNESS_ENV="test", PYTHONUTF8="1")
    environ.pop("HERNESS_FAULTS", None)
    if plan is not None:
        environ["HERNESS_FAULTS"] = str(plan)
    argv = [sys.executable, "-m", "tests.fault.connectors._sn_child", str(config_dir)]
    argv += [str(db_path), *span]
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        argv,
        cwd=REPO,
        env=environ,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=CHILD_TIMEOUT_S,
        check=False,
    )


def result(done: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    """The decoded result line of a child that ran to the end with a `done` job."""
    assert done.returncode == 0, done.stderr[-3000:]
    [line] = [x for x in done.stdout.splitlines() if x.startswith(RESULT_PREFIX)]
    outcome: dict[str, Any] = json.loads(line.removeprefix(RESULT_PREFIX))
    assert outcome["status"] == "done", outcome
    return outcome


def lake_rows(data_root: Path) -> list[dict[str, Any]]:
    """Every row of the committed lake files of ``servicenow/incident`` under ``data_root``."""
    root = data_root / "raw" / "servicenow" / "incident"
    files = sorted(p for p in root.rglob("*.parquet") if not p.name.startswith("."))
    return [row for path in files for row in pq.read_table(path).to_pylist()]


def slices(db_path: Path) -> list[tuple[str, str]]:
    """``(slice_start, status)`` of the ``servicenow/incident`` plan in the store at ``db_path``."""
    reset_connections(path=db_path)
    rows = read_all(
        "SELECT slice_start, status FROM sync_slice"
        " WHERE source = 'servicenow' AND entity = 'incident' ORDER BY slice_start"
    )
    return [(str(r["slice_start"]), str(r["status"])) for r in rows]


def core_state(root: Path) -> dict[str, list[tuple[Any, ...]]]:
    """Build the staging and ``core.*`` over the lake under ``root``; every table's rows."""
    harness = BuildHarness(root / "data")
    try:
        build(harness, enums=INC_ENUM, hi=299)
        tables = harness.query(
            "SELECT table_schema || '.' || table_name FROM information_schema.tables"
            " WHERE table_schema = 'core' ORDER BY 1"
        )
        state = {str(t): harness.query(f"SELECT * FROM {t} ORDER BY ALL") for (t,) in tables}  # noqa: S608
        state["stg.sn_incident"] = harness.query(KEY_QUERY)
        return state
    finally:
        harness.close()
