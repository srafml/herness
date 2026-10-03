"""Task lease, resume and checkpoint helpers of impl 08 (U08-57 to U08-63; design 08 §3.7, §5.12).

Spec 06 drives `task` rows with these helpers; the SQL is `herness.store.ops.tasks.TaskSqlMixin`
(U08-97), reached through the bound `JobsBackend` port (R-04). The run's job lease is the task
lease, so no helper takes an owner. The checkpoint envelope `{schema_version, loop, state,
scratchpad}` is owned here (R-21): each of impl 05, 06 and 07 replaces only its own key, and 08
never interprets their content. Durable side effects of a task happen only in the `writes`
callback of `save_checkpoint` / `complete_task`, in the same transaction (idempotency rule).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

from herness.core import time as clock
from herness.core.errors import ConfigError, HernessError, RetryableError, SchemaViolation
from herness.core.ids import IdKind, canonical_json, is_valid_id
from herness.core.jobs.ports import CheckpointKey, SqlWrites, require_jobs_backend
from herness.core.logging import get_logger
from herness.core.redact import redact_text
from herness.core.secrets import scrub_secrets

CHECKPOINT_SCHEMA_VERSION: Final = 1
CHECKPOINT_MAX_BYTES: Final = 4_194_304  # 4 MiB (TH08-10)
RESULT_MAX_BYTES: Final = 4_194_304  # U08-61: result canonical JSON ≤ 4 MiB
LAST_ERROR_MESSAGE_CHARS: Final = 2048  # U08-50 `last_error.message` cut

_KEYS: Final[frozenset[str]] = frozenset(("loop", "state", "scratchpad"))
_ENVELOPE_KEYS: Final[frozenset[str]] = _KEYS | {"schema_version"}
_RUN_ID_RE: Final = re.compile(r"run_[0-9A-HJKMNP-TV-Z]{26}")

_log = get_logger("jobs")


@dataclass(frozen=True, slots=True)
class RecoverySummary:
    """Row counts of one `recover_run_tasks` call (U08-57)."""

    running_reset: int
    failed_reset: int
    dead_reset: int


def _check_task_id(task_id: object) -> str:
    if not is_valid_id(IdKind.TASK, task_id):
        msg = "task_id must be a task ID (task_<ULID>)"
        raise ConfigError(msg)
    return str(task_id)


def _check_max_attempts(max_task_attempts: object) -> int:
    if (
        not isinstance(max_task_attempts, int)
        or isinstance(max_task_attempts, bool)
        or max_task_attempts < 1
    ):
        msg = "max_task_attempts must be an int >= 1"
        raise ConfigError(msg)
    return max_task_attempts


def _encode(value: object, *, limit: int, what: str) -> bytes:
    """Canonical JSON bytes (R-14) of at most `limit` bytes, else SchemaViolation."""
    data = canonical_json(value).encode("utf-8")
    if len(data) > limit:
        msg = f"{what} exceeds 4 MiB"
        raise SchemaViolation(msg)
    return data


def recover_run_tasks(
    run_id: str, *, max_task_attempts: int, retry_dead: bool = False
) -> RecoverySummary:
    """Reset a run's interrupted and retryable tasks before spec 06 drives it (U08-57).

    `running` → `pending`; `failed` below the cap → `pending`; with `retry_dead`, `dead` →
    `pending` with `attempts = 0`. `done` tasks are never touched. Invalid arguments →
    ConfigError.
    """
    given: object = run_id  # runtime guard for untyped callers
    if not isinstance(given, str) or _RUN_ID_RE.fullmatch(given) is None:
        msg = "run_id must match ^run_[0-9A-HJKMNP-TV-Z]{26}$"
        raise ConfigError(msg)
    cap = _check_max_attempts(max_task_attempts)
    running, failed, dead = require_jobs_backend().recover_tasks(
        run_id, max_task_attempts=cap, retry_dead=bool(retry_dead), now=clock.now()
    )
    summary = RecoverySummary(running_reset=running, failed_reset=failed, dead_reset=dead)
    _log.info(
        "jobs.tasks.recovered",
        run_id=run_id,
        running_reset=running,
        failed_reset=failed,
        dead_reset=dead,
    )
    return summary


def claim_task(task_id: str) -> bool:
    """Lease a `pending` task: `running`, `attempts + 1`; False when it was not pending (U08-58)."""
    return require_jobs_backend().claim_task(_check_task_id(task_id), clock.now())


def build_checkpoint_envelope(
    key: CheckpointKey, value: Mapping[str, object], existing: Mapping[str, object] | None
) -> tuple[bytes, bool]:
    """Replace `key` of the checkpoint envelope; return (canonical JSON, loop_dropped) (U08-59).

    The other two keys keep their stored values (R-21). An invalid stored envelope, a
    non-object `value`, or an envelope over 4 MiB even without `loop` → SchemaViolation.
    """
    if key not in _KEYS:
        msg = "checkpoint key must be loop, state or scratchpad"
        raise ConfigError(msg)
    given: object = value  # runtime guard for untyped callers
    if not isinstance(given, Mapping):
        msg = "checkpoint value must be a JSON object"
        raise SchemaViolation(msg)
    env: dict[str, object] = (
        {"schema_version": CHECKPOINT_SCHEMA_VERSION} if existing is None else dict(existing)
    )
    if env.get("schema_version") != CHECKPOINT_SCHEMA_VERSION or not set(env) <= _ENVELOPE_KEYS:
        msg = "checkpoint envelope invalid"
        raise SchemaViolation(msg)
    env[key] = dict(value)
    data = canonical_json(env).encode("utf-8")
    if len(data) <= CHECKPOINT_MAX_BYTES:
        return data, False
    if "loop" in env:
        del env["loop"]  # the loop then resumes from its task spec
        data = canonical_json(env).encode("utf-8")
        if len(data) <= CHECKPOINT_MAX_BYTES:
            return data, True
    msg = "checkpoint exceeds 4 MiB without loop"
    raise SchemaViolation(msg)


def save_checkpoint(
    task_id: str,
    key: CheckpointKey,
    value: Mapping[str, object],
    *,
    writes: SqlWrites | None = None,
) -> None:
    """Replace one envelope key of a running task and commit `writes` with it (U08-60).

    `writes` runs on the transaction's connection, performs only SQL, never calls `run_write`
    and must be safe to re-run (the transaction is retried on lock contention). Not running →
    JobStateError; any exception rolls everything back and propagates.
    """
    tid = _check_task_id(task_id)
    if require_jobs_backend().save_checkpoint(tid, key, value, writes):
        _log.warning("jobs.checkpoint.loop_dropped", task_id=tid, key=key)


def complete_task(
    task_id: str, result: Mapping[str, object], *, writes: SqlWrites | None = None
) -> None:
    """Mark a running task `done` with `result`, committing `writes` with it (U08-61).

    `result` canonical JSON over 4 MiB → SchemaViolation; not running → JobStateError.
    """
    tid = _check_task_id(task_id)
    _encode(result, limit=RESULT_MAX_BYTES, what="task result")
    require_jobs_backend().complete_task(tid, result, writes=writes, now=clock.now())


def _last_error(err: HernessError, now_ts: str) -> dict[str, str]:
    """The U08-50 `last_error` shape without `attempt` (the backend adds the row's attempts):
    known secret values scrubbed (U10-36), then redacted, then cut; a failure stores no text."""
    scrubbed = scrub_secrets(None, "last_error", {"m": str(err)}).get("m")
    message = (redact_text(scrubbed) if isinstance(scrubbed, str) else None) or ""
    return {
        "class": type(err).__name__,
        "message": message[:LAST_ERROR_MESSAGE_CHARS],
        "at": now_ts,
    }


def fail_task(
    task_id: str, err: HernessError, *, max_task_attempts: int
) -> Literal["pending", "dead"]:
    """Record a task failure: `pending` for a retryable error below the cap, else `dead` (U08-62).

    Retryable means a `RetryableError` (including `RateLimited` and `CircuitOpen`); every
    `RecoverableError` and `FatalError` is dead (design 08 §5.2 "Task"). Not running →
    JobStateError. The design's `failed` task status is never written (O08-07).
    """
    tid = _check_task_id(task_id)
    cap = _check_max_attempts(max_task_attempts)
    now = clock.now()
    return require_jobs_backend().fail_task(
        tid,
        retryable=isinstance(err, RetryableError),
        max_task_attempts=cap,
        last_error=_last_error(err, clock.format_utc(now)),
        now=now,
    )


def release_task(task_id: str) -> None:
    """Return a cancelled or preempted running task to `pending`, `attempts - 1` (U08-63, R-36).

    A task that is no longer running is left as it is (DEBUG log `jobs.task.release_skipped`).
    """
    tid = _check_task_id(task_id)
    if not require_jobs_backend().release_task(tid, clock.now()):
        _log.debug("jobs.task.release_skipped", task_id=tid)
