"""Unit and property tests for the ops area `runs` (impl 06 U06-32 … U06-40, T06-05).

UT06-21 … UT06-26 and PT06-08 run on a tmp ops store migrated on top of `ops_store`.
"""

from __future__ import annotations

import random
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from herness.core import time as clock
from herness.core.errors import NotFound, SchemaViolation
from herness.core.types import TaskSpec
from herness.store import ops
from herness.store.ops import core, runs
from herness.store.ops.migrate import migrate
from herness.store.ops.runs import RunRow, TaskRow

pytestmark = pytest.mark.unit

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # pragma: allowlist secret - ULID alphabet
_T0 = datetime(2026, 9, 26, 10, 0, tzinfo=UTC)


def _ulid(n: int) -> str:
    """A 26-char Crockford id whose sort order follows ``n``."""
    digits = []
    for _ in range(26):
        n, r = divmod(n, 32)
        digits.append(_CROCKFORD[r])
    return "".join(reversed(digits))


def _run_id(n: int) -> str:
    return f"run_{_ulid(n)}"


def _task_id(n: int) -> str:
    return f"task_{_ulid(n)}"


def _run(n: int = 1, **overrides: object) -> RunRow:
    values: dict[str, object] = {
        "run_id": _run_id(n),
        "kind": "funding_review",
        "depth": "standard",
        "profile": "default",
        "build_id": "b_1",
        "status": "created",
        "started_at": _T0,
        "finished_at": None,
        "token_usage": {"input": 10},
        "cost_usd": Decimal("1.25"),
        "config_hash": "c" * 16,
        "meta": {},
    }
    values.update(overrides)
    return RunRow(**values)  # type: ignore[arg-type]


def _spec(n: int, run: int = 1, **overrides: object) -> TaskSpec:
    values: dict[str, object] = {
        "task_id": _task_id(n),
        "run_id": _run_id(run),
        "role": "analyst",
        "objective": "Assess delivery",
        "scope": {"entity_type": "team", "entity_ids": ["t1"]},
        "tools": ["run_sql"],
        "budget": {"max_steps": 5, "max_tokens": 1_000, "wall_clock_s": 60},
        "model_role": "analyst",
        "dedup_key": f"{n:016x}",
    }
    values.update(overrides)
    return TaskSpec.model_validate(values)


def _write[T](fn: Callable[[sqlite3.Connection], T]) -> T:
    return core.run_write(fn, op="test_runs")


def _sql(sql: str, *params: object) -> None:
    _write(lambda c: c.execute(sql, params))


@pytest.fixture
def store(ops_store: Path) -> Path:
    """A migrated tmp ops store."""
    migrate()
    return ops_store


def _insert_runs(*rows: RunRow) -> list[bool]:
    return _write(lambda conn: [runs.insert_run(conn, r) for r in rows])


def _insert_tasks(specs: list[TaskSpec], now: datetime = _T0) -> list[str]:
    return _write(lambda conn: runs.insert_tasks(conn, specs, now=now))


# UT06-21 ---------------------------------------------------------------------------------


def test_ut06_21_insert_idempotent_and_get(store: Path) -> None:
    """UT06-21 second insert returns False; get_run returns the typed row."""
    row = _run(meta={"job_id": "job_1"})
    assert _insert_runs(row) == [True]
    assert _insert_runs(row) == [False]
    assert runs.get_run(row.run_id) == row
    assert runs.get_run(_run_id(99)) is None


def test_ut06_21_insert_run_constraint_violation_raises(store: Path) -> None:
    """UT06-21 a CHECK violation raises instead of reading as an idempotent replay."""
    with pytest.raises(SchemaViolation, match="ops constraint failed"):
        _insert_runs(_run(kind="bogus"))
    assert runs.get_run(_run_id(1)) is None
    _insert_runs(_run())
    _sql("UPDATE run SET meta = '[1]' WHERE run_id = ?", _run_id(1))
    with pytest.raises(SchemaViolation, match=f"run.meta is not an object: run_id={_run_id(1)}$"):
        runs.get_run(_run_id(1))


def test_ut06_21_find_by_job_and_escalation(store: Path) -> None:
    """UT06-21 find_run_by_job picks the newest run of the job; escalation lookup matches."""
    esc = {"escalated_from": {"session_id": "s1", "message_id": "m1"}}
    old = _run(1, meta={"job_id": "job_1"})
    new = _run(2, meta={"job_id": "job_1"}, started_at=_T0 + timedelta(minutes=1))
    chat = _run(3, kind="chat", meta=esc)
    _insert_runs(old, new, chat)
    assert runs.find_run_by_job("job_1") == new
    assert runs.find_run_by_job("job_x") is None
    assert runs.find_run_by_escalation("s1", "m1") == chat
    assert runs.find_run_by_escalation("s1", "m2") is None


def test_ut06_21_select_runs_filters_and_order(store: Path) -> None:
    """UT06-21 select_runs filters by status and kind, ordered by started_at."""
    a = _run(1, status="running", started_at=_T0 + timedelta(minutes=2))
    b = _run(2, status="running", kind="org_review")
    c = _run(3, status="done", finished_at=_T0)
    _insert_runs(a, b, c)
    assert runs.select_runs(statuses={"running"}) == [b, a]
    assert runs.select_runs(statuses={"running", "done"}, kinds=["org_review"]) == [b]
    assert runs.select_runs(statuses=[]) == []
    assert runs.select_runs(statuses={"running"}, kinds=()) == []


def test_ut06_21_null_columns_read_back(store: Path) -> None:
    """UT06-21 NULL build_id stays None and NULL cost_usd reads as Decimal("0")."""
    row = _run(build_id=None)
    _insert_runs(row)
    _sql("UPDATE run SET cost_usd = NULL WHERE run_id = ?", row.run_id)
    got = runs.get_run(row.run_id)
    assert got is not None
    assert got.build_id is None
    assert got.cost_usd == Decimal("0")


def test_ut06_21_task_row_spec_and_last_error(store: Path) -> None:
    """UT06-21 TaskRow parses spec; a plain-string last_error is wrapped; bad spec raises."""
    _insert_runs(_run())
    _insert_tasks([_spec(1), _spec(2), _spec(3)])
    sql = "UPDATE task SET last_error = ?, checkpoint = ?, result = ? WHERE task_id = ?"
    _sql(sql, "boom", '{"k":1}', '{"r":2}', _task_id(1))
    _sql(sql, '{"class":"X"}', None, None, _task_id(2))
    t1, t2 = runs.get_task(_task_id(1)), runs.get_task(_task_id(2))
    assert t1 is not None
    assert t2 is not None
    assert isinstance(t1, TaskRow)
    assert t1.last_error == {"message": "boom"}
    assert (t1.checkpoint, t1.result) == ({"k": 1}, {"r": 2})
    assert t2.last_error == {"class": "X"}
    assert t2.checkpoint is None
    _sql(sql, '"quoted"', None, None, _task_id(2))
    t2b = runs.get_task(_task_id(2))
    assert t2b is not None
    assert t2b.last_error == {"message": "quoted"}
    _sql(sql, "7", "[1]", None, _task_id(2))
    with pytest.raises(
        SchemaViolation, match=rf"task\.checkpoint is not an object: task_id={_task_id(2)}$"
    ):
        runs.get_task(_task_id(2))
    _sql(sql, "7", None, None, _task_id(2))
    t2c = runs.get_task(_task_id(2))
    assert t2c is not None
    assert t2c.last_error == {"message": "7"}
    _sql("UPDATE task SET spec = json_set(spec, '$.role', 'nobody') WHERE task_id = ?", _task_id(3))
    with pytest.raises(SchemaViolation, match=f"task spec invalid: task_id={_task_id(3)}"):
        runs.get_task(_task_id(3))


# UT06-22 ---------------------------------------------------------------------------------


def test_ut06_22_set_run_status_cas(store: Path) -> None:
    """UT06-22 CAS from a wrong state is False; running -> done is True with finished_at."""
    row = _run(status="running")
    _insert_runs(row)
    now = _T0 + timedelta(hours=1)

    def cas(to: str, allowed: set[str]) -> bool:
        return _write(lambda c: runs.set_run_status(c, row.run_id, to, allowed, now=now))

    assert cas("challenging", {"planning"}) is False
    assert runs.get_run(row.run_id) == row
    assert cas("done", {"running"}) is True
    got = runs.get_run(row.run_id)
    assert got is not None
    assert (got.status, got.finished_at) == ("done", now)


def test_ut06_22_non_terminal_keeps_finished_at(store: Path) -> None:
    """UT06-22 a non-terminal target leaves finished_at; empty allowed_from is rejected."""
    row = _run(status="planning")
    _insert_runs(row)
    ok = _write(lambda c: runs.set_run_status(c, row.run_id, "running", ["planning"], now=_T0))
    assert ok is True
    got = runs.get_run(row.run_id)
    assert got is not None
    assert (got.status, got.finished_at) == ("running", None)
    with pytest.raises(ValueError, match="allowed_from"):
        _write(lambda c: runs.set_run_status(c, row.run_id, "done", (), now=_T0))


# UT06-23 ---------------------------------------------------------------------------------


def test_ut06_23_update_run_fields_merges_meta(store: Path) -> None:
    """UT06-23 meta_patch is merged shallowly and a None value is stored as JSON null."""
    row = _run(meta={"job_id": "job_1", "stage": "small", "render_error": "old"})
    _insert_runs(row)
    patch = {"render_error": {"class": "RenderError"}, "stage": None}
    _write(lambda c: runs.update_run_fields(c, row.run_id, meta_patch=patch))
    got = runs.get_run(row.run_id)
    assert got is not None
    expected = {"job_id": "job_1", "stage": None, "render_error": {"class": "RenderError"}}
    assert got.meta == expected
    assert (got.token_usage, got.cost_usd) == (row.token_usage, row.cost_usd)
    raw = core.read_one("SELECT meta FROM run WHERE run_id = ?", (row.run_id,))
    assert raw is not None
    assert '"stage":null' in raw["meta"]


def test_ut06_23_update_usage_and_cost(store: Path) -> None:
    """UT06-23 token_usage and cost_usd are replaced; meta is untouched; no-op is allowed."""
    row = _run(meta={"a": 1})
    _insert_runs(row)
    usage = {"input": 99, "output": 7}
    cost = Decimal("3.5")
    _write(lambda c: runs.update_run_fields(c, row.run_id, token_usage=usage, cost_usd=cost))
    _write(lambda c: runs.update_run_fields(c, row.run_id))
    got = runs.get_run(row.run_id)
    assert got is not None
    assert (got.token_usage, got.cost_usd, got.meta) == (usage, cost, {"a": 1})


def test_ut06_23_update_missing_run_raises(store: Path) -> None:
    """UT06-23 an unknown run raises NotFound naming the run id."""
    with pytest.raises(NotFound, match=f"run not found: run_id={_run_id(7)}"):
        _write(lambda c: runs.update_run_fields(c, _run_id(7), meta_patch={"a": 1}))


# UT06-24 ---------------------------------------------------------------------------------


def test_ut06_24_insert_tasks_dedup(store: Path) -> None:
    """UT06-24 two specs with one dedup key insert one row; reads return that row."""
    _insert_runs(_run())
    first = _spec(1, dedup_key="ab" * 8)
    dup = _spec(2, dedup_key="ab" * 8)
    assert _insert_tasks([first, dup]) == [first.task_id]
    assert _insert_tasks([first]) == []
    got = runs.get_task(first.task_id)
    assert got is not None
    assert got.spec == first
    assert (got.status, got.attempts, got.role, got.run_id) == ("pending", 0, "analyst", _run_id(1))
    assert (got.created_at, got.updated_at, got.parent_task_id) == (_T0, _T0, None)
    assert got.last_error is None
    assert runs.get_task_by_dedup(first.run_id, "ab" * 8) == got
    assert runs.get_task_by_dedup(first.run_id, "cd" * 8) is None
    assert runs.select_tasks(first.run_id) == [got]
    assert runs.get_task(dup.task_id) is None
    assert runs.get_task(_task_id(999)) is None


def test_ut06_24_select_tasks_filters_and_order(store: Path) -> None:
    """UT06-24 select_tasks filters by role and status, ordered by created_at, task_id."""
    _insert_runs(_run(1), _run(2))
    parent = _task_id(1)
    assert _insert_tasks([_spec(3, role="writer"), _spec(2, parent_task_id=parent)]) == [
        _task_id(3),
        _task_id(2),
    ]
    assert _insert_tasks([_spec(1)], now=_T0 - timedelta(minutes=1)) == [_task_id(1)]
    assert _insert_tasks([_spec(4, run=2)]) == [_task_id(4)]
    ids = [t.task_id for t in runs.select_tasks(_run_id(1))]
    assert ids == [_task_id(1), _task_id(2), _task_id(3)]
    writers = runs.select_tasks(_run_id(1), roles={"writer"})
    assert [t.task_id for t in writers] == [_task_id(3)]
    assert runs.select_tasks(_run_id(1), statuses={"running"}) == []
    assert runs.select_tasks(_run_id(1), roles=()) == []
    child = runs.get_task(_task_id(2))
    assert child is not None
    assert child.parent_task_id == parent


def test_ut06_24_select_tasks_distinct_from_ui_list_tasks() -> None:
    """UT06-24 acceptance: ops.select_tasks is the runs reader, not an impl 09 UI projection."""
    assert ops.select_tasks is runs.select_tasks
    assert ops.select_tasks is not getattr(ops, "ui_list_tasks", None)
    assert ops.get_run is runs.get_run


# UT06-25 ---------------------------------------------------------------------------------


def test_ut06_25_ready_tasks_order(store: Path) -> None:
    """UT06-25 ready_tasks sorts by round, depth, aged priority desc, task_id (design 06 §5.4)."""
    _insert_runs(_run())
    _insert_tasks([_spec(1, priority=10.0)])  # effective 10
    # effective 9.5 + 0.01 * 100 = 10.5: aging lifts it above task 1
    _insert_tasks([_spec(2, priority=9.5)], now=_T0 - timedelta(minutes=100))
    _insert_tasks([_spec(3, priority=10.0)])  # ties with 1 -> task_id breaks it
    _insert_tasks([_spec(4, priority=99.0, depth=1)])
    inputs = {"finding_ids": [f"fnd_{_ulid(1)}"]}
    _insert_tasks([_spec(5, role="skeptic", round=1, inputs=inputs, priority=500.0)])
    _insert_tasks([_spec(6, role="writer", priority=1_000.0)])
    _insert_tasks([_spec(7)])
    _sql("UPDATE task SET status = 'running' WHERE task_id = ?", _task_id(7))
    got = runs.ready_tasks(_run_id(1), {"analyst", "skeptic"}, now=_T0, aging_per_min=0.01)
    assert [t.task_id for t in got] == [_task_id(n) for n in (2, 1, 3, 4, 5)]
    assert runs.ready_tasks(_run_id(1), (), now=_T0, aging_per_min=0.01) == []
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="aging_per_min must be finite"):
            runs.ready_tasks(_run_id(1), {"analyst"}, now=_T0, aging_per_min=bad)


# PT06-08 ---------------------------------------------------------------------------------

_task_values = st.lists(
    st.tuples(
        st.integers(0, 2),  # round
        st.integers(0, 2),  # depth
        st.sampled_from([0.0, 1.0, 5.0, 10.0, 50.5]),  # priority
        st.integers(0, 120),  # minutes waiting
    ),
    max_size=12,
)


@settings(
    max_examples=30, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(values=_task_values, rng=st.randoms(use_true_random=False))
def test_pt06_08_ready_order_total_and_stable(
    store: Path, values: list[tuple[int, int, float, int]], rng: random.Random
) -> None:
    """PT06-08 the ready order is total (sorted by the key) and independent of insert order."""
    _sql("DELETE FROM task")
    if runs.get_run(_run_id(1)) is None:
        _insert_runs(_run())
    specs = [_spec(i + 1, round=r, depth=d, priority=p) for i, (r, d, p, _) in enumerate(values)]
    waits = {s.task_id: v[3] for s, v in zip(specs, values, strict=True)}
    order = list(range(len(specs)))
    rng.shuffle(order)
    for i in order:
        _insert_tasks([specs[i]], now=_T0 - timedelta(minutes=waits[specs[i].task_id]))
    first = runs.ready_tasks(_run_id(1), {"analyst"}, now=_T0, aging_per_min=0.01)
    assert runs.ready_tasks(_run_id(1), {"analyst"}, now=_T0, aging_per_min=0.01) == first
    assert sorted(t.task_id for t in first) == sorted(waits)

    def key(t: TaskRow) -> tuple[int, int, float, str]:
        waited = (_T0 - t.created_at).total_seconds() / 60
        return (t.spec.round, t.spec.depth, -(t.spec.priority + 0.01 * waited), t.task_id)

    keys = [key(t) for t in first]
    assert keys == sorted(keys)
    assert len({k[3] for k in keys}) == len(keys)  # task_id makes every key unique: total


# UT06-26 ---------------------------------------------------------------------------------


def test_ut06_26_count_open_and_count_tasks(store: Path) -> None:
    """UT06-26 count_open counts pending and running minus excluded ids; count_tasks filters."""
    _insert_runs(_run(1), _run(2))
    parent = _task_id(1)
    _insert_tasks(
        [
            _spec(1),
            _spec(2, parent_task_id=parent),
            _spec(3, parent_task_id=parent),
            _spec(4, role="writer"),
            _spec(5),
        ]
    )
    _insert_tasks([_spec(6, run=2)])
    _sql("UPDATE task SET status = 'running' WHERE task_id = ?", _task_id(2))
    _sql("UPDATE task SET status = 'done' WHERE task_id = ?", _task_id(5))
    run = _run_id(1)
    assert runs.count_open(run, {"analyst"}) == 3
    assert runs.count_open(run, {"analyst"}, exclude={_task_id(2), _task_id(3)}) == 1
    assert runs.count_open(run, {"analyst", "writer"}, exclude=[_task_id(1)]) == 3
    assert runs.count_open(run, ()) == 0
    assert runs.count_tasks(run) == 5
    assert runs.count_tasks(run, roles={"analyst"}) == 4
    assert runs.count_tasks(run, parent_task_id=parent) == 2
    assert runs.count_tasks(run, parent_task_id=parent, statuses={"running"}) == 1
    assert runs.count_tasks(run, roles={"analyst"}, statuses={"done"}) == 1
    assert runs.count_tasks(run, statuses=()) == 0


def test_ut06_24_timestamps_are_fixed_width(store: Path) -> None:
    """UT06-24 insert_tasks stores created_at in the spec 00 fixed-width UTC text."""
    _insert_runs(_run())
    _insert_tasks([_spec(1)])
    raw = core.read_one("SELECT created_at FROM task")
    assert raw is not None
    assert raw["created_at"] == clock.format_utc(_T0)
