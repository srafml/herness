"""Ops area ``runs``, scheduler reads (impl 06 U06-38 ``select_task_ids``, U06-39, T06-31).

``ready_tasks`` selects the pending rows of the roles with three ``json_extract`` key columns,
sorts them in Python by the design 06 §5.4 key, and parses a ``TaskRow`` only when a row is
consumed, so a scheduler pass parses what it dispatches. ``select_task_ids`` is the id-only
read of ``select_tasks``. Both read on the thread's connection (R-10); the only dynamic SQL is
the ``IN (...)`` list from ``_task_filter`` (ENG §3.5).
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Collection, Iterator
from datetime import datetime

from herness.core import time as clock

from . import core

# Private helpers of the sibling module ``runs`` (same ops area, owner 06).
from .runs import _SELECT_TASK, TaskRow, _task_filter, _task_from_row

# The COALESCE defaults are the TaskSpec field defaults for round, depth and priority. Priority
# is read as its JSON text (``->``) and parsed by Python's float(): SQLite's own text-to-REAL
# conversion is not correctly rounded for every double (T06-31 found 4.90541873407465e-231).
_KEYS = (
    ", COALESCE(json_extract(spec, '$.round'), 0) AS k_round"
    ", COALESCE(json_extract(spec, '$.depth'), 0) AS k_depth"
    ", COALESCE(spec -> '$.priority', '0.0') AS k_priority"
)
_SELECT_READY = _SELECT_TASK.replace(" FROM task", f"{_KEYS} FROM task")


def ready_tasks(
    run_id: str, roles: Collection[str], *, now: datetime, aging_per_min: float
) -> Iterator[TaskRow]:
    """Pending ``roles`` tasks in design 06 §5.4 order (U06-39): round, depth, aged priority
    descending, task_id. ``aging_per_min`` must be finite (ValueError, raised at call time).

    The select and the sort run in the call; each ``TaskRow`` is parsed when yielded, where an
    invalid stored spec raises ``SchemaViolation``."""
    if not math.isfinite(aging_per_min):
        msg = "aging_per_min must be finite"
        raise ValueError(msg)
    where, params = _task_filter(run_id, roles, ("pending",))
    rows = core.read_all(f"{_SELECT_READY}{where}", params)

    def key(row: sqlite3.Row) -> tuple[int, int, float, str]:
        waited = (now - clock.parse_utc(row["created_at"])).total_seconds() / 60
        aged = float(row["k_priority"]) + aging_per_min * waited
        return (row["k_round"], row["k_depth"], -aged, row["task_id"])

    return map(_task_from_row, sorted(rows, key=key))


def select_task_ids(
    run_id: str,
    *,
    parent_task_id: str | None = None,
    roles: Collection[str] | None = None,
    statuses: Collection[str] | None = None,
) -> list[str]:
    """Ids of the tasks ``select_tasks`` would return, in its order, without parsing specs."""
    where, params = _task_filter(run_id, roles, statuses, parent_task_id)
    rows = core.read_all(f"SELECT task_id FROM task{where} ORDER BY created_at, task_id", params)  # noqa: S608
    return [r["task_id"] for r in rows]
