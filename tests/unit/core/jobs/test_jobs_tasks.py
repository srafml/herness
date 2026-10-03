"""Unit tests for herness.core.jobs.tasks (impl 08 U08-57 to U08-63; T08-16).

The helpers run against the real migrated ops store (impl 02 migration 003 `task`) through the
bound `SqliteJobsBackend` (U08-97); the store-level checks of the mixin live in
tests/unit/store/ops/test_store_ops_tasks.py.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Mapping
from datetime import UTC, datetime

import pytest
import structlog
from tests.support.ops_store import OpsStoreHandle

from herness.core import redact as r
from herness.core import secrets
from herness.core import time as clock
from herness.core.errors import (
    BudgetExceeded,
    CircuitOpen,
    ConfigError,
    HernessError,
    JobStateError,
    ModelUnavailable,
    OutputValidationError,
    QueryError,
    SchemaViolation,
)
from herness.core.ids import IdKind, new_id
from herness.core.jobs import tasks as tasks_mod
from herness.core.jobs.ports import SqlWrites, bind_jobs_backend
from herness.core.jobs.tasks import (
    CHECKPOINT_MAX_BYTES,
    CHECKPOINT_SCHEMA_VERSION,
    RecoverySummary,
    build_checkpoint_envelope,
    claim_task,
    complete_task,
    fail_task,
    recover_run_tasks,
    release_task,
    save_checkpoint,
)
from herness.core.redact_directory import NameDirectory
from herness.core.settings import RedactionConfig
from herness.store.ops.core import read_one, run_write
from herness.store.ops.jobs import SqliteJobsBackend

pytestmark = pytest.mark.unit

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
BIG = "x" * (5 * 1024 * 1024)
KNOWN_VALUE = "plainvaluewithoutshape"  # a resolved secret value with no credential shape
KNOWN_WITH_EMAIL = "hunter2-jane.doe@example.com-zz"  # a known value holding an e-mail shape


@pytest.fixture
def test_redactor(monkeypatch: pytest.MonkeyPatch) -> r.Redactor:
    """A process redactor with a fixed key and no directory names (no config needed)."""
    directory = NameDirectory.from_files(None, (), None)
    redactor = r.Redactor(RedactionConfig(directory_file=None), bytes(range(32)), directory)
    monkeypatch.setattr(r._State, "redactor", redactor)
    return redactor


@pytest.fixture
def run_id(ops_store: OpsStoreHandle, test_redactor: r.Redactor) -> str:
    del ops_store, test_redactor
    bind_jobs_backend(SqliteJobsBackend())
    rid = new_id(IdKind.RUN)

    def insert(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO run (run_id, kind, depth, profile, config_hash, status, started_at)"
            " VALUES (?, 'org_review', 'standard', 'default', 'h', 'running', ?)",
            (rid, clock.format_utc(T0)),
        )

    run_write(insert, op="test_setup")
    return rid


def _task(
    run_id: str,
    status: str = "running",
    attempts: int = 1,
    checkpoint: Mapping[str, object] | None = None,
) -> str:
    task_id = new_id(IdKind.TASK)
    params = (
        task_id,
        run_id,
        json.dumps({"dedup_key": task_id}),
        status,
        attempts,
        None if checkpoint is None else json.dumps(checkpoint),
        clock.format_utc(T0),
        clock.format_utc(T0),
    )

    def insert(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO task (task_id, run_id, role, spec, status, attempts, checkpoint,"
            " created_at, updated_at) VALUES (?, ?, 'analyst', ?, ?, ?, ?, ?, ?)",
            params,
        )

    run_write(insert, op="test_setup")
    return task_id


def _row(task_id: str) -> dict[str, object]:
    row = read_one("SELECT * FROM task WHERE task_id = ?", (task_id,))
    assert row is not None
    return dict(row)


def _envelope(task_id: str) -> object:
    text = _row(task_id)["checkpoint"]
    return None if text is None else json.loads(str(text))


def _finding_count(task_id: str) -> int:
    row = read_one("SELECT COUNT(*) AS n FROM finding WHERE task_id = ?", (task_id,))
    assert row is not None
    return int(row["n"])


def _insert_finding(run_id: str, task_id: str) -> SqlWrites:
    def writes(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO finding (finding_id, run_id, task_id, author_role, claim, created_at)"
            " VALUES (?, ?, ?, 'analyst', 'claim', ?)",
            (new_id(IdKind.FINDING), run_id, task_id, clock.format_utc(T0)),
        )

    return writes


def _raising_writes(run_id: str, task_id: str, exc_type: type[Exception] = QueryError) -> SqlWrites:
    insert = _insert_finding(run_id, task_id)

    def writes(conn: sqlite3.Connection) -> None:
        insert(conn)
        msg = "boom"
        raise exc_type(msg)

    return writes


# --- UT08-66 recover_run_tasks ---------------------------------------------------------------


def test_ut08_66_recover_resets_running_and_retryable_failed(run_id: str) -> None:
    """UT08-66 running → pending with attempts kept; done untouched; one failed reset; dead
    reset only with retry_dead (attempts 0)."""
    running = [_task(run_id, "running", 2), _task(run_id, "running", 1)]
    done = _task(run_id, "done", 1)
    failed_low = _task(run_id, "failed", 1)
    failed_cap = _task(run_id, "failed", 3)
    dead = _task(run_id, "dead", 3)
    other_run = _task(_other_run(), "running", 1)

    with structlog.testing.capture_logs() as logs:
        first = recover_run_tasks(run_id, max_task_attempts=3)
    assert first == RecoverySummary(running_reset=2, failed_reset=1, dead_reset=0)
    assert [(_row(t)["status"], _row(t)["attempts"]) for t in running] == [
        ("pending", 2),
        ("pending", 1),
    ]
    assert _row(done)["status"] == "done"
    assert _row(done)["updated_at"] == clock.format_utc(T0)
    assert (_row(failed_low)["status"], _row(failed_low)["attempts"]) == ("pending", 1)
    assert _row(failed_cap)["status"] == "failed"
    assert _row(dead)["status"] == "dead"
    assert _row(other_run)["status"] == "running"
    event = next(e for e in logs if e["event"] == "jobs.tasks.recovered")
    assert event["log_level"] == "info"
    assert (event["running_reset"], event["failed_reset"], event["dead_reset"]) == (2, 1, 0)

    second = recover_run_tasks(run_id, max_task_attempts=3, retry_dead=True)
    assert second == RecoverySummary(running_reset=0, failed_reset=0, dead_reset=1)
    assert (_row(dead)["status"], _row(dead)["attempts"]) == ("pending", 0)
    assert _row(dead)["updated_at"] != clock.format_utc(T0)
    assert _row(failed_cap)["status"] == "failed"
    assert _row(done)["status"] == "done"


def _other_run() -> str:
    rid = new_id(IdKind.RUN)

    def insert(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO run (run_id, kind, depth, profile, config_hash, status, started_at)"
            " VALUES (?, 'org_review', 'standard', 'default', 'h', 'running', ?)",
            (rid, clock.format_utc(T0)),
        )

    run_write(insert, op="test_setup")
    return rid


@pytest.mark.parametrize(
    ("rid", "attempts"),
    [("run_bad", 3), ("RUN_01J8ZZZZZZZZZZZZZZZZZZZZZZ", 3), (None, 3), ("VALID", 0)],
)
def test_ut08_66_recover_rejects_invalid_arguments(
    run_id: str, rid: object, attempts: object
) -> None:
    """UT08-66 invalid run_id or max_task_attempts → ConfigError."""
    target = run_id if rid == "VALID" else rid
    with pytest.raises(ConfigError):
        recover_run_tasks(target, max_task_attempts=attempts)  # type: ignore[arg-type]


def test_ut08_66_recover_rejects_bool_attempts(run_id: str) -> None:
    """UT08-66 a bool max_task_attempts is not an int argument → ConfigError."""
    with pytest.raises(ConfigError):
        recover_run_tasks(run_id, max_task_attempts=True)


# --- UT08-67 claim_task -----------------------------------------------------------------------


def test_ut08_67_claim_pending_only(run_id: str) -> None:
    """UT08-67 claim: pending → true and attempts +1; running → false and unchanged."""
    pending = _task(run_id, "pending", 0)
    running = _task(run_id, "running", 1)
    assert claim_task(pending) is True
    assert (_row(pending)["status"], _row(pending)["attempts"]) == ("running", 1)
    assert claim_task(running) is False
    assert (_row(running)["status"], _row(running)["attempts"]) == ("running", 1)
    assert claim_task(new_id(IdKind.TASK)) is False


def test_ut08_67_claim_rejects_bad_task_id(run_id: str) -> None:
    """UT08-67 a task_id outside the spec 00 §5 format → ConfigError."""
    del run_id
    with pytest.raises(ConfigError):
        claim_task("task_nope")


# --- UT08-68 checkpoint envelope --------------------------------------------------------------


def test_ut08_68_envelope_new_and_merge() -> None:
    """UT08-68 the envelope starts at schema_version 1 and keeps the other owners' keys."""
    assert CHECKPOINT_SCHEMA_VERSION == 1
    assert CHECKPOINT_MAX_BYTES == 4_194_304
    first, dropped = build_checkpoint_envelope("state", {"phase": "plan"}, None)
    assert dropped is False
    assert json.loads(first) == {"schema_version": 1, "state": {"phase": "plan"}}
    second, _ = build_checkpoint_envelope("loop", {"turn": 2}, json.loads(first))
    assert json.loads(second) == {
        "schema_version": 1,
        "loop": {"turn": 2},
        "state": {"phase": "plan"},
    }
    assert second == b'{"loop":{"turn":2},"schema_version":1,"state":{"phase":"plan"}}'


def test_ut08_68_big_loop_dropped_scratchpad_kept(run_id: str) -> None:
    """UT08-68 envelope with scratchpad; save loop 5 MB → scratchpad kept, loop dropped, WARNING."""
    task = _task(run_id, checkpoint={"schema_version": 1, "scratchpad": {"m": 1}})
    with structlog.testing.capture_logs() as logs:
        save_checkpoint(task, "loop", {"blob": BIG})
    assert _envelope(task) == {"schema_version": 1, "scratchpad": {"m": 1}}
    event = next(e for e in logs if e["event"] == "jobs.checkpoint.loop_dropped")
    assert event["log_level"] == "warning"
    assert event["task_id"] == task


def test_ut08_68_big_state_violates(run_id: str) -> None:
    """UT08-68 save state 5 MB → SchemaViolation, stored envelope unchanged."""
    task = _task(run_id, checkpoint={"schema_version": 1, "loop": {"t": 1}})
    with pytest.raises(SchemaViolation, match="exceeds 4 MiB without loop"):
        save_checkpoint(task, "state", {"blob": BIG})
    assert _envelope(task) == {"schema_version": 1, "loop": {"t": 1}}
    with pytest.raises(SchemaViolation, match="exceeds 4 MiB without loop"):
        build_checkpoint_envelope("state", {"blob": BIG}, None)


@pytest.mark.parametrize(
    "stored",
    [
        {"schema_version": 2},
        {"schema_version": 1, "extra": {}},
        {"loop": {}},
    ],
)
def test_ut08_68_invalid_envelope_violates(run_id: str, stored: Mapping[str, object]) -> None:
    """UT08-68 schema_version 2, an extra top-level key or no version → SchemaViolation."""
    task = _task(run_id, checkpoint=stored)
    with pytest.raises(SchemaViolation, match="checkpoint envelope invalid"):
        save_checkpoint(task, "state", {"a": 1})
    assert _envelope(task) == stored


def test_ut08_68_invalid_key_and_value() -> None:
    """UT08-68 an unknown key is a ConfigError; a non-object value a SchemaViolation."""
    with pytest.raises(ConfigError):
        build_checkpoint_envelope("other", {}, None)  # type: ignore[arg-type]
    with pytest.raises(SchemaViolation):
        build_checkpoint_envelope("loop", [1, 2], None)  # type: ignore[arg-type]
    with pytest.raises(SchemaViolation):
        build_checkpoint_envelope("loop", {"t": object()}, None)


def test_ut08_68_save_on_null_checkpoint(run_id: str) -> None:
    """UT08-68 a NULL checkpoint column starts a new envelope."""
    task = _task(run_id)
    save_checkpoint(task, "scratchpad", {"n": [1, 2]})
    assert _envelope(task) == {"schema_version": 1, "scratchpad": {"n": [1, 2]}}
    assert _row(task)["updated_at"] != clock.format_utc(T0)


# --- UT08-69 writes rollback and state guard ---------------------------------------------------


def test_ut08_69_save_writes_rollback(run_id: str) -> None:
    """UT08-69 writes inserting a finding then raising → finding row absent, checkpoint kept."""
    task = _task(run_id, checkpoint={"schema_version": 1, "state": {"a": 1}})
    with pytest.raises(QueryError, match="boom"):
        save_checkpoint(task, "state", {"a": 2}, writes=_raising_writes(run_id, task))
    assert _finding_count(task) == 0
    assert _envelope(task) == {"schema_version": 1, "state": {"a": 1}}


def test_ut08_69_foreign_error_rolls_back(run_id: str) -> None:
    """UT08-69 a non-Herness error in writes also rolls back; run_write's retry layer
    (T08-07) re-raises it classified, with the original as the cause."""
    task = _task(run_id, checkpoint={"schema_version": 1, "state": {"a": 1}})
    with pytest.raises(HernessError) as info:
        save_checkpoint(task, "state", {"a": 2}, writes=_raising_writes(run_id, task, RuntimeError))
    assert isinstance(info.value.__cause__, RuntimeError)
    assert _finding_count(task) == 0
    assert _envelope(task) == {"schema_version": 1, "state": {"a": 1}}


def test_ut08_69_save_writes_commit(run_id: str) -> None:
    """UT08-69 writes that succeed commit with the checkpoint."""
    task = _task(run_id)
    save_checkpoint(task, "state", {"a": 2}, writes=_insert_finding(run_id, task))
    assert _finding_count(task) == 1
    assert _envelope(task) == {"schema_version": 1, "state": {"a": 2}}


def test_ut08_69_complete_writes_rollback(run_id: str) -> None:
    """UT08-69 complete_task with raising writes → finding absent, task still running."""
    task = _task(run_id)
    with pytest.raises(QueryError, match="boom"):
        complete_task(task, {"ok": True}, writes=_raising_writes(run_id, task))
    assert _finding_count(task) == 0
    assert (_row(task)["status"], _row(task)["result"]) == ("running", None)


def test_ut08_69_complete_commits(run_id: str) -> None:
    """UT08-69 complete_task sets done with the canonical result and runs writes."""
    task = _task(run_id)
    complete_task(task, {"b": 1, "a": [1]}, writes=_insert_finding(run_id, task))
    row = _row(task)
    assert (row["status"], row["result"]) == ("done", '{"a":[1],"b":1}')
    assert _finding_count(task) == 1
    complete_task(_task(run_id), {})


@pytest.mark.parametrize("status", ["pending", "done", "dead"])
def test_ut08_69_not_running_raises(run_id: str, status: str) -> None:
    """UT08-69 save / complete on a task that is not running → JobStateError, nothing written."""
    task = _task(run_id, status)
    with pytest.raises(JobStateError) as info:
        save_checkpoint(task, "loop", {"t": 1}, writes=_insert_finding(run_id, task))
    assert info.value.task_id == task
    with pytest.raises(JobStateError):
        complete_task(task, {"ok": True}, writes=_insert_finding(run_id, task))
    assert _finding_count(task) == 0
    assert _row(task)["status"] == status
    with pytest.raises(JobStateError):
        save_checkpoint(new_id(IdKind.TASK), "loop", {"t": 1})


def test_ut08_69_complete_result_cap(run_id: str) -> None:
    """UT08-69 a result over 4 MiB or not JSON → SchemaViolation before any write."""
    task = _task(run_id)
    with pytest.raises(SchemaViolation):
        complete_task(task, {"blob": BIG})
    with pytest.raises(SchemaViolation):
        complete_task(task, {"x": float("nan")})
    assert _row(task)["status"] == "running"


# --- UT08-70 fail_task --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("err", "attempts", "expected"),
    [
        (ModelUnavailable("model down"), 1, "pending"),
        (ModelUnavailable("model down"), 3, "dead"),
        (OutputValidationError("bad output"), 1, "dead"),
        (BudgetExceeded("over budget"), 1, "dead"),
        (CircuitOpen("open", key="m", retry_at=T0), 1, "pending"),
        (CircuitOpen("open", key="m", retry_at=T0), 3, "dead"),
    ],
)
def test_ut08_70_fail_pending_or_dead(
    run_id: str, err: HernessError, attempts: int, expected: str
) -> None:
    """UT08-70 retryable below the cap → pending; at the cap or not retryable → dead."""
    task = _task(run_id, "running", attempts)
    assert fail_task(task, err, max_task_attempts=3) == expected
    row = _row(task)
    assert (row["status"], row["attempts"]) == (expected, attempts)
    last_error = json.loads(str(row["last_error"]))
    assert last_error["class"] == type(err).__name__
    assert last_error["message"] == str(err)
    assert last_error["attempt"] == attempts
    assert last_error["at"] == row["updated_at"]


def test_ut08_70_fail_guards(run_id: str) -> None:
    """UT08-70 not running → JobStateError; max_task_attempts < 1 → ConfigError; message cut."""
    done = _task(run_id, "done")
    with pytest.raises(JobStateError):
        fail_task(done, ModelUnavailable("x"), max_task_attempts=3)
    assert _row(done)["last_error"] is None
    with pytest.raises(ConfigError):
        fail_task(_task(run_id), ModelUnavailable("x"), max_task_attempts=0)
    task = _task(run_id)
    fail_task(task, ModelUnavailable("y" * 3000), max_task_attempts=3)
    assert len(json.loads(str(_row(task)["last_error"]))["message"]) <= 2048


def test_ut08_70_last_error_redacted(run_id: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT08-70 last_error.message is redacted; a redaction failure stores no text."""
    task = _task(run_id)
    fail_task(task, QueryError("mail jane.doe@example.com failed"), max_task_attempts=3)
    assert "jane.doe@example.com" not in str(_row(task)["last_error"])
    monkeypatch.setattr(tasks_mod, "redact_text", lambda text: None)
    other = _task(run_id)
    assert fail_task(other, QueryError("secret text"), max_task_attempts=3) == "dead"
    assert json.loads(str(_row(other)["last_error"]))["message"] == ""


def _plant_known(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """Make `value` a known secret value for this test only (restored by monkeypatch)."""
    monkeypatch.setattr(secrets, "_KNOWN", {value})
    monkeypatch.setattr(secrets, "_KNOWN_VERSION", secrets._KNOWN_VERSION + 1)
    # force a scrub-cache rebuild now and restore the previous cache afterwards
    monkeypatch.setattr(secrets, "_scrub_version", -1)
    monkeypatch.setattr(secrets, "_scrub_pattern", None)


def test_ut08_70_last_error_known_secret_masked(
    run_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT08-70 a known secret value (no credential shape) in the error is scrubbed before
    redaction (T08-15b, U10-36); a scrub failure stores no text (fail closed)."""
    _plant_known(monkeypatch, KNOWN_VALUE)
    task = _task(run_id)
    fail_task(task, QueryError(f"upstream rejected credential {KNOWN_VALUE}"), max_task_attempts=3)
    message = json.loads(str(_row(task)["last_error"]))["message"]
    assert KNOWN_VALUE not in message
    assert message.startswith("upstream rejected credential ")
    monkeypatch.setattr(tasks_mod, "scrub_secrets", lambda *_a: {"event": "log.scrub.failed"})
    other = _task(run_id)
    fail_task(other, QueryError(f"again {KNOWN_VALUE}"), max_task_attempts=3)
    assert json.loads(str(_row(other)["last_error"]))["message"] == ""


def test_ut08_70_last_error_scrubs_before_redacting(
    run_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT08-70 scrub runs before redaction (T08-15b): a known value holding an e-mail shape is
    masked whole, so no part of it survives as a redaction placeholder."""
    _plant_known(monkeypatch, KNOWN_WITH_EMAIL)
    task = _task(run_id)
    fail_task(task, QueryError(f"rejected {KNOWN_WITH_EMAIL} today"), max_task_attempts=3)
    message = json.loads(str(_row(task)["last_error"]))["message"]
    assert "***" in message
    assert "-zz" not in message
    assert "[EMAIL_" not in message


# --- UT08-71 release_task -----------------------------------------------------------------------


def test_ut08_71_release(run_id: str) -> None:
    """UT08-71 running attempts 2 → pending attempts 1; done unchanged with a DEBUG log."""
    running = _task(run_id, "running", 2)
    done = _task(run_id, "done", 2)
    zero = _task(run_id, "running", 0)
    with structlog.testing.capture_logs() as logs:
        release_task(running)
        release_task(done)
        release_task(zero)
    assert (_row(running)["status"], _row(running)["attempts"]) == ("pending", 1)
    assert (_row(done)["status"], _row(done)["attempts"]) == ("done", 2)
    assert (_row(zero)["status"], _row(zero)["attempts"]) == ("pending", 0)
    skipped = [e for e in logs if e["event"] == "jobs.task.release_skipped"]
    assert [(e["task_id"], e["log_level"]) for e in skipped] == [(done, "debug")]


# --- UT08-109 key-scoped saves ------------------------------------------------------------------


def test_ut08_109_sequential_saves_keep_other_keys(run_id: str) -> None:
    """UT08-109 save state {a}, loop {b}, scratchpad {c}, state {d} → every key kept."""
    task = _task(run_id)
    save_checkpoint(task, "state", {"a": 1})
    save_checkpoint(task, "loop", {"b": 1})
    save_checkpoint(task, "scratchpad", {"c": 1})
    save_checkpoint(task, "state", {"d": 1})
    assert _envelope(task) == {
        "schema_version": 1,
        "loop": {"b": 1},
        "state": {"d": 1},
        "scratchpad": {"c": 1},
    }


def test_ut08_109_concurrent_saves_keep_both_keys(run_id: str) -> None:
    """UT08-109 two threads saving loop and scratchpad at once both keep their key (R-21)."""
    task = _task(run_id)
    in_txn = threading.Event()
    errors: list[BaseException] = []

    def slow_writes(conn: sqlite3.Connection) -> None:
        del conn
        in_txn.set()
        threading.Event().wait(0.3)  # hold the write lock while the other thread starts

    def save_loop() -> None:
        try:
            save_checkpoint(task, "loop", {"b": 1}, writes=slow_writes)
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assert below
            errors.append(exc)

    def save_scratchpad() -> None:
        try:
            assert in_txn.wait(5)
            save_checkpoint(task, "scratchpad", {"c": 1})
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assert below
            errors.append(exc)

    threads = [threading.Thread(target=save_loop), threading.Thread(target=save_scratchpad)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert errors == []
    assert _envelope(task) == {"schema_version": 1, "loop": {"b": 1}, "scratchpad": {"c": 1}}


def test_ut08_109_many_concurrent_rounds(run_id: str) -> None:
    """UT08-109 repeated racing saves of two keys never lose either key."""
    task = _task(run_id)
    barrier = threading.Barrier(2)

    def worker(key: str) -> None:
        for n in range(15):
            barrier.wait(5)
            save_checkpoint(task, key, {"n": n})  # type: ignore[arg-type]

    threads = [threading.Thread(target=worker, args=(k,)) for k in ("loop", "scratchpad")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert _envelope(task) == {"schema_version": 1, "loop": {"n": 14}, "scratchpad": {"n": 14}}
