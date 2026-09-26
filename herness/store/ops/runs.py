"""Ops area ``runs`` (impl 06 U06-32 … U06-40, R-08): ``run`` rows, ``task`` inserts and reads.

Write functions take ``conn`` first and run inside a write transaction (the ``run_write``
callback, or a spec 08 ``writes`` callback); read functions use ``read_one`` / ``read_all`` on
the thread's connection (R-10). JSON columns are written with ``dump_json``, timestamps with
``clock.format_utc``. The only dynamic SQL is the ``IN (...)`` placeholder list, built from the
count of values (ENG §3.5). Later task status changes belong to the spec 08 helpers.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final

from pydantic import ValidationError

from herness.core import time as clock
from herness.core.errors import NotFound, SchemaViolation
from herness.core.types import TaskSpec

from . import core

_TERMINAL: Final = frozenset({"done", "partial", "failed", "canceled"})
_RUN_COLS: Final = (
    "run_id, kind, depth, profile, build_id, status, started_at, finished_at, token_usage,"
    " cost_usd, config_hash, meta"
)
_TASK_COLS: Final = (
    "task_id, run_id, parent_task_id, role, spec, status, attempts, last_error, checkpoint,"
    " result, created_at, updated_at"
)
_SELECT_RUN: Final = f"SELECT {_RUN_COLS} FROM run"  # noqa: S608 - constant column list
_SELECT_TASK: Final = f"SELECT {_TASK_COLS} FROM task"  # noqa: S608 - constant column list


@dataclass(frozen=True, slots=True)
class RunRow:
    """One ``run`` row (U06-32); ``cost_usd`` NULL reads as ``Decimal("0")``."""

    run_id: str
    kind: str
    depth: str
    profile: str
    build_id: str | None
    status: str
    started_at: datetime
    finished_at: datetime | None
    token_usage: dict[str, object]
    cost_usd: Decimal
    config_hash: str
    meta: dict[str, object]


@dataclass(frozen=True, slots=True)
class TaskRow:
    """One ``task`` row (U06-32) with ``spec`` parsed as ``TaskSpec``."""

    task_id: str
    run_id: str
    parent_task_id: str | None
    role: str
    spec: TaskSpec
    status: str
    attempts: int
    last_error: dict[str, object] | None
    checkpoint: dict[str, object] | None
    result: dict[str, object] | None
    created_at: datetime
    updated_at: datetime


def _in(values: Collection[str]) -> tuple[str, list[str]]:
    """``(?, ?, ...)`` for ``values`` (empty ``()`` matches nothing in SQLite) and its params."""
    items = list(values)
    return "(" + ", ".join("?" * len(items)) + ")", items


def _obj(text: str | None, field: str, ref: str) -> dict[str, object] | None:
    """A JSON object column or None; ``ref`` (``<key>=<id>``) names the row, never content."""
    value = core.load_json(text, field=field)
    if value is None or isinstance(value, dict):
        return value
    msg = f"JSON in {field} is not an object: {ref}"
    raise SchemaViolation(msg)


def _run_from_row(row: sqlite3.Row) -> RunRow:
    cost, finished, ref = row["cost_usd"], row["finished_at"], f"run_id={row['run_id']}"
    return RunRow(
        run_id=row["run_id"],
        kind=row["kind"],
        depth=row["depth"],
        profile=row["profile"],
        build_id=row["build_id"],
        status=row["status"],
        started_at=clock.parse_utc(row["started_at"]),
        finished_at=None if finished is None else clock.parse_utc(finished),
        token_usage=_obj(row["token_usage"], "run.token_usage", ref) or {},
        cost_usd=Decimal("0") if cost is None else Decimal(cost),
        config_hash=row["config_hash"],
        meta=_obj(row["meta"], "run.meta", ref) or {},
    )


def _last_error(text: str | None) -> dict[str, object] | None:
    """A JSON object stays as is; any other text (plain or a JSON string) becomes a message."""
    if text is None:
        return None
    try:
        value = core.load_json(text, field="task.last_error")
    except SchemaViolation:
        return {"message": text}
    if isinstance(value, dict):
        return value
    return {"message": value if isinstance(value, str) else text}


def _task_from_row(row: sqlite3.Row) -> TaskRow:
    task_id: str = row["task_id"]
    try:
        spec = TaskSpec.model_validate_json(row["spec"])
    except ValidationError as exc:
        msg = f"task spec invalid: task_id={task_id}"
        raise SchemaViolation(msg) from exc
    return TaskRow(
        task_id=task_id,
        run_id=row["run_id"],
        parent_task_id=row["parent_task_id"],
        role=row["role"],
        spec=spec,
        status=row["status"],
        attempts=int(row["attempts"]),
        last_error=_last_error(row["last_error"]),
        checkpoint=_obj(row["checkpoint"], "task.checkpoint", f"task_id={task_id}"),
        result=_obj(row["result"], "task.result", f"task_id={task_id}"),
        created_at=clock.parse_utc(row["created_at"]),
        updated_at=clock.parse_utc(row["updated_at"]),
    )


def _one_run(where: str, params: Sequence[object]) -> RunRow | None:
    row = core.read_one(f"{_SELECT_RUN} WHERE {where}", params)
    return None if row is None else _run_from_row(row)


def _one_task(where: str, params: Sequence[object]) -> TaskRow | None:
    row = core.read_one(f"{_SELECT_TASK} WHERE {where}", params)
    return None if row is None else _task_from_row(row)


# --- run (U06-33 … U06-36) ------------------------------------------------------------------


def insert_run(conn: sqlite3.Connection, row: RunRow) -> bool:
    """Insert a ``run`` row; False when ``run_id`` exists (U06-33); other violations raise."""
    finished = None if row.finished_at is None else clock.format_utc(row.finished_at)
    cursor = conn.execute(
        f"INSERT INTO run ({_RUN_COLS}) VALUES ({', '.join('?' * 12)})"  # noqa: S608 - constants
        " ON CONFLICT(run_id) DO NOTHING",
        (
            row.run_id,
            row.kind,
            row.depth,
            row.profile,
            row.build_id,
            row.status,
            clock.format_utc(row.started_at),
            finished,
            core.dump_json(row.token_usage, field="run.token_usage"),
            str(row.cost_usd),
            row.config_hash,
            core.dump_json(row.meta, field="run.meta"),
        ),
    )
    return cursor.rowcount == 1


def get_run(run_id: str) -> RunRow | None:
    """The run with ``run_id``, or None (U06-34)."""
    return _one_run("run_id = ?", (run_id,))


def find_run_by_job(job_id: str) -> RunRow | None:
    """The newest run whose ``meta.job_id`` is ``job_id``, or None (U06-34)."""
    where = "json_extract(meta, '$.job_id') = ? ORDER BY started_at DESC, run_id DESC LIMIT 1"
    return _one_run(where, (job_id,))


def find_run_by_escalation(session_id: str, message_id: str) -> RunRow | None:
    """The newest run escalated from chat message ``(session_id, message_id)`` (U06-34)."""
    where = (
        "json_extract(meta, '$.escalated_from.session_id') = ?"
        " AND json_extract(meta, '$.escalated_from.message_id') = ?"
        " ORDER BY started_at DESC, run_id DESC LIMIT 1"
    )
    return _one_run(where, (session_id, message_id))


def select_runs(*, statuses: Collection[str], kinds: Collection[str] | None = None) -> list[RunRow]:
    """Runs in ``statuses`` (and ``kinds`` when given), ordered by ``started_at`` (U06-34)."""
    marks, params = _in(statuses)
    sql = f"{_SELECT_RUN} WHERE status IN {marks}"
    if kinds is not None:
        kind_marks, kind_params = _in(kinds)
        sql += f" AND kind IN {kind_marks}"
        params += kind_params
    rows = core.read_all(sql + " ORDER BY started_at, run_id", params)
    return [_run_from_row(r) for r in rows]


def set_run_status(
    conn: sqlite3.Connection,
    run_id: str,
    to: str,
    allowed_from: Collection[str],
    *,
    now: datetime,
) -> bool:
    """Compare-and-set ``status``; terminal targets set ``finished_at = now`` (U06-35)."""
    if not allowed_from:
        msg = "allowed_from must not be empty"
        raise ValueError(msg)
    marks, params = _in(allowed_from)
    cursor = conn.execute(
        "UPDATE run SET status = ?, finished_at = CASE WHEN ? THEN ? ELSE finished_at END"  # noqa: S608
        f" WHERE run_id = ? AND status IN {marks}",
        (to, to in _TERMINAL, clock.format_utc(now), run_id, *params),
    )
    return cursor.rowcount == 1


def update_run_fields(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    token_usage: Mapping[str, object] | None = None,
    cost_usd: Decimal | None = None,
    meta_patch: Mapping[str, object] | None = None,
) -> None:
    """Replace usage and cost, shallow-merge ``meta_patch`` into ``meta`` (U06-36).

    A patch value ``None`` is stored as JSON null. Raises NotFound for an unknown run."""
    row = conn.execute("SELECT meta FROM run WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        msg = f"run not found: run_id={run_id}"
        raise NotFound(msg)
    sets: list[str] = []
    params: list[object] = []
    if token_usage is not None:
        sets.append("token_usage = ?")
        params.append(core.dump_json(token_usage, field="run.token_usage"))
    if cost_usd is not None:
        sets.append("cost_usd = ?")
        params.append(str(cost_usd))
    if meta_patch is not None:
        meta = (_obj(row["meta"], "run.meta", f"run_id={run_id}") or {}) | dict(meta_patch)
        sets.append("meta = ?")
        params.append(core.dump_json(meta, field="run.meta"))
    if sets:
        conn.execute(f"UPDATE run SET {', '.join(sets)} WHERE run_id = ?", (*params, run_id))  # noqa: S608 - fixed column names


# --- task (U06-37 … U06-40) -----------------------------------------------------------------


def insert_tasks(
    conn: sqlite3.Connection, specs: Sequence[TaskSpec], *, now: datetime
) -> list[str]:
    """Insert ``pending`` task rows; return the ids actually inserted, in input order (U06-37).

    A spec whose ``task_id`` or ``(run_id, dedup_key)`` exists is skipped (``task_dedup``)."""
    stamp = clock.format_utc(now)
    inserted: list[str] = []
    for spec in specs:
        cursor = conn.execute(
            "INSERT INTO task (task_id, run_id, parent_task_id, role, spec, status, attempts,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?)"
            " ON CONFLICT DO NOTHING",
            (
                spec.task_id,
                spec.run_id,
                spec.parent_task_id,
                spec.role,
                core.dump_json(spec.model_dump(mode="json"), field="task.spec"),
                stamp,
                stamp,
            ),
        )
        if cursor.rowcount == 1:
            inserted.append(spec.task_id)
    return inserted


def get_task(task_id: str) -> TaskRow | None:
    """The task with ``task_id``, or None (U06-38)."""
    return _one_task("task_id = ?", (task_id,))


def get_task_by_dedup(run_id: str, dedup_key: str) -> TaskRow | None:
    """The task of ``run_id`` with ``spec.dedup_key``, or None (U06-38)."""
    return _one_task("run_id = ? AND json_extract(spec, '$.dedup_key') = ?", (run_id, dedup_key))


def _task_filter(
    run_id: str,
    roles: Collection[str] | None,
    statuses: Collection[str] | None,
    parent_task_id: str | None = None,
) -> tuple[str, list[object]]:
    """``WHERE`` clause and params for the given task filters (None means no filter)."""
    clauses = ["run_id = ?"]
    params: list[object] = [run_id]
    for column, values in (("role", roles), ("status", statuses)):
        if values is not None:
            marks, items = _in(values)
            clauses.append(f"{column} IN {marks}")
            params += items
    if parent_task_id is not None:
        clauses.append("parent_task_id = ?")
        params.append(parent_task_id)
    return " WHERE " + " AND ".join(clauses), params


def select_tasks(
    run_id: str,
    *,
    roles: Collection[str] | None = None,
    statuses: Collection[str] | None = None,
) -> list[TaskRow]:
    """Tasks of ``run_id`` matching the filters, ordered by ``created_at, task_id`` (U06-38)."""
    where, params = _task_filter(run_id, roles, statuses)
    rows = core.read_all(f"{_SELECT_TASK}{where} ORDER BY created_at, task_id", params)
    return [_task_from_row(r) for r in rows]


def ready_tasks(
    run_id: str, roles: Collection[str], *, now: datetime, aging_per_min: float
) -> list[TaskRow]:
    """Pending ``roles`` tasks in design 06 §5.4 order (U06-39): round, depth, aged priority
    descending, task_id. ``aging_per_min`` must be finite (ValueError)."""
    if not math.isfinite(aging_per_min):
        msg = "aging_per_min must be finite"
        raise ValueError(msg)
    tasks = select_tasks(run_id, roles=roles, statuses=("pending",))

    def key(task: TaskRow) -> tuple[int, int, float, str]:
        waited = (now - task.created_at).total_seconds() / 60
        aged = task.spec.priority + aging_per_min * waited
        return (task.spec.round, task.spec.depth, -aged, task.task_id)

    return sorted(tasks, key=key)


def _count(where: str, params: Sequence[object]) -> int:
    row = core.read_one(f"SELECT count(*) AS n FROM task{where}", params)  # noqa: S608 - built from placeholders
    return 0 if row is None else int(row["n"])


def count_open(run_id: str, roles: Collection[str], *, exclude: Collection[str] = ()) -> int:
    """``pending`` and ``running`` tasks of ``roles`` whose id is not in ``exclude`` (U06-40)."""
    where, params = _task_filter(run_id, roles, ("pending", "running"))
    marks, excluded = _in(exclude)
    return _count(f"{where} AND task_id NOT IN {marks}", [*params, *excluded])


def count_tasks(
    run_id: str,
    *,
    roles: Collection[str] | None = None,
    parent_task_id: str | None = None,
    statuses: Collection[str] | None = None,
) -> int:
    """Tasks of ``run_id`` matching every filter given (U06-40)."""
    where, params = _task_filter(run_id, roles, statuses, parent_task_id)
    return _count(where, params)
