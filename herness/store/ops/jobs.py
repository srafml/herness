"""Jobs ops-store area (impl 08 U08-95, R-08): every SQL statement on `job`.

`SqliteJobsBackend` implements the job and worker methods of the `JobsBackend` port (U08-41);
the worker SQL comes from `WorkerSqlMixin` (U08-96), the one area-to-area import, recorded in
the `ops-areas-acyclic` contract. The table is impl 02 migration 002 (§4.3.2). Timestamps are
ts text and JSON is TEXT; every write runs in `run_write` (`BEGIN IMMEDIATE`, `sqlite_write`
policy, R-10). Every completion and state write is guarded by `lease_owner` and `status`
(TH08-08). Metrics and events are the caller's (T08-12, the supervisor): nothing here records
them, so no metric flush can start inside a `run_write` callback.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Final, cast

from pydantic import JsonValue

from herness.core import time as clock
from herness.core.jobs.ports import (
    CancelResult,
    ClaimSlot,
    JobRow,
    JobStatus,
    JsonMap,
    NewJob,
    NextJob,
    QueueStats,
    RequeueResult,
    RetryResult,
    SchedCheck,
)
from herness.core.types import GpuClass, JobKind

from . import _job_sql as sql
from .core import dump_json, load_json, read_all, read_one, run_write
from .worker import WorkerSqlMixin

type _Params = dict[str, object]

_LIST_MAX: Final = 1000


def _ts(value: datetime) -> str:
    return clock.format_utc(value)


def _array(values: Sequence[str]) -> str:
    """JSON array text for a `json_each` bind parameter (values are never spliced into SQL)."""
    return json.dumps(list(values))


def _like_prefix(text: str) -> str:
    """`text` with `\\`, `%` and `_` escaped for `LIKE ... ESCAPE '\\'`."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _job(row: sqlite3.Row | dict[str, object]) -> JobRow:
    return JobRow.model_validate(dict(row))


def _filter_params(
    now: datetime,
    allowed: Sequence[GpuClass],
    exclusive: Sequence[JobKind],
    min_priority: int | None,
    exempt: Sequence[JobKind],
) -> _Params:
    return {
        "now": _ts(now),
        "allowed_classes": _array(allowed),
        "exclusive_kinds": _array(exclusive),
        "min_priority": min_priority,
        "exempt_kinds": _array(exempt),
    }


def _update_one(sql: str, params: Mapping[str, object], op: str) -> bool:
    """Run one guarded UPDATE in `run_write`; True when exactly one row changed."""

    def update(conn: sqlite3.Connection) -> bool:
        return conn.execute(sql, params).rowcount == 1

    return run_write(update, op=op)


def _reap(rows: str, params: _Params, op: str) -> RequeueResult:
    """Report, then requeue or fail, the running jobs matching `rows` (design 08 §5.7)."""
    select, requeue, fail = sql.REAPERS[rows]

    def reap(conn: sqlite3.Connection) -> list[sqlite3.Row]:
        found: list[sqlite3.Row] = conn.execute(select, params).fetchall()
        conn.execute(requeue, params)
        conn.execute(fail, params)
        return found

    return [
        (
            str(row["job_id"]),
            cast("JobKind", row["kind"]),
            "queued" if row["attempts"] < row["max_attempts"] else "failed",
        )
        for row in run_write(reap, op=op)
    ]


class SqliteJobsBackend(WorkerSqlMixin):
    """`JobsBackend` job and worker methods (U08-95, U08-96); stateless, thread-safe."""

    def insert_job(self, job: NewJob, *, sched_check: SchedCheck | None) -> tuple[str, bool]:
        """Idempotent enqueue (design 08 §5.7): `(job_id, created)`; an active job with the
        same idem key, or (with ``sched_check``) any job of that fire, returns its id."""
        params: _Params = {
            "job_id": job.job_id,
            "kind": job.kind,
            "gpu_class": job.gpu_class,
            "priority": job.priority,
            "payload": job.payload.decode("utf-8"),
            "idem_key": job.idem_key,
            "max_attempts": job.max_attempts,
            "scheduled_for": _ts(job.scheduled_for),
            "now": _ts(job.created_at),
        }

        def insert(conn: sqlite3.Connection) -> tuple[str, bool]:
            if sched_check is not None:  # the fire's own key, not the job's (sync:<source>)
                key = f"sched:{sched_check.schedule}:{sched_check.fire_at}"
                fire = {"sched_key": key, **sched_check._asdict()}
                seen = conn.execute(sql.SCHED_SEEN, fire).fetchone()
                if seen is not None:
                    return str(seen[0]), False
            row = conn.execute(sql.INSERT, params).fetchone()
            if row is not None:
                return str(row[0]), True
            active = conn.execute(sql.ACTIVE_IDEM, {"idem_key": job.idem_key}).fetchone()
            return str(active[0]), False

        return run_write(insert, op="job_insert")

    def claim_job(  # noqa: PLR0913 - U08-41 signature plus the R-43 slot rule
        self,
        *,
        owner: str,
        now: datetime,
        lease_until: datetime,
        allowed_classes: Sequence[GpuClass],
        exclusive_kinds: Sequence[JobKind],
        job_id: str | None,
        min_priority: int | None,
        priority_exempt_kinds: Sequence[JobKind],
        slot: ClaimSlot,
        gpu_slot_kinds: Sequence[JobKind],
    ) -> JobRow | None:
        """Atomic claim (design 08 §5.7, U08-95): the best eligible job becomes `running`
        under ``owner``; None when nothing is claimable."""
        params = _filter_params(
            now, allowed_classes, exclusive_kinds, min_priority, priority_exempt_kinds
        )
        params |= {
            "owner": owner,
            "lease_until": _ts(lease_until),
            "job_id": job_id,
            "slot": slot,
            "gpu_slot_kinds": _array(gpu_slot_kinds),
        }

        def claim(conn: sqlite3.Connection) -> dict[str, object] | None:
            row = conn.execute(sql.CLAIM, params).fetchone()
            return None if row is None else dict(row)

        claimed = run_write(claim, op="job_claim")
        return None if claimed is None else _job(claimed)

    def claimable_counts(
        self,
        *,
        now: datetime,
        classes: Sequence[GpuClass],
        exclusive_kinds: Sequence[JobKind],
        min_priority: int | None,
        priority_exempt_kinds: Sequence[JobKind],
        gpu_slot_kinds: Sequence[JobKind],
    ) -> dict[GpuClass, int]:
        """Jobs the GPU slot could claim now, per class of ``classes`` (0 when none)."""
        params = _filter_params(now, classes, exclusive_kinds, min_priority, priority_exempt_kinds)
        params |= {"slot": "gpu", "gpu_slot_kinds": _array(gpu_slot_kinds)}
        counts: dict[GpuClass, int] = dict.fromkeys(classes, 0)
        for row in read_all(sql.COUNTS, params, max_rows=16):
            counts[cast("GpuClass", row[0])] = int(row[1])
        return counts

    def heartbeat_job(self, job_id: str, owner: str, lease_until: datetime) -> bool:
        """Extend the lease; False when the job is no longer ours and running."""
        params = {"id": job_id, "owner": owner, "lease_until": _ts(lease_until)}
        return _update_one(sql.HEARTBEAT, params, "job_heartbeat")

    def finish_done(self, job_id: str, owner: str, result: JsonMap, now: datetime) -> bool:
        """Owner-guarded `done` (U08-95); False when 0 rows changed (lease lost)."""
        text = dump_json(result, field="job.result")
        params = {"id": job_id, "owner": owner, "result": text, "now": _ts(now)}
        return _update_one(sql.DONE, params, "job_finish")

    def finish_yield(self, job_id: str, owner: str, resume_at: datetime, now: datetime) -> bool:
        """Owner-guarded yield: back to `queued` at ``resume_at`` with no attempt charge."""
        del now  # part of the port signature; the yield statement stores no finish time
        params = {"id": job_id, "owner": owner, "resume_at": _ts(resume_at)}
        return _update_one(sql.YIELD, params, "job_finish")

    def finish_requeue(self, job_id: str, owner: str, at: datetime, error: JsonMap) -> bool:
        """Owner-guarded requeue at ``at`` with `last_error` = ``error``."""
        text = dump_json(error, field="job.last_error")
        params = {"id": job_id, "owner": owner, "at": _ts(at), "le": text}
        return _update_one(sql.REQUEUE, params, "job_finish")

    def finish_failed(self, job_id: str, owner: str, error: JsonMap, now: datetime) -> bool:
        """Owner-guarded `failed` with `last_error` = ``error``."""
        text = dump_json(error, field="job.last_error")
        params = {"id": job_id, "owner": owner, "now": _ts(now), "le": text}
        return _update_one(sql.FAILED, params, "job_finish")

    def finalize_canceled(self, job_id: str, owner: str, now: datetime) -> bool:
        """Close a canceled job still leased by ``owner`` (status stays `canceled`)."""
        params = {"id": job_id, "owner": owner, "now": _ts(now)}
        return _update_one(sql.FINALIZE_CANCELED, params, "job_finish")

    def save_job_state(self, job_id: str, owner: str, state_json: bytes) -> bool:
        """Owner-guarded `result = {"state": ...}`; invalid JSON raises SchemaViolation."""
        params = {"id": job_id, "owner": owner, "state": state_json.decode("utf-8")}
        return _update_one(sql.SAVE_STATE, params, "job_save_state")

    def load_job_state(self, job_id: str) -> dict[str, JsonValue]:
        """The saved `state` object of the job, or `{}`."""
        row = read_one("SELECT result FROM job WHERE job_id = ?", (job_id,))
        result = None if row is None else load_json(row[0], field="job.result")
        state = result.get("state") if isinstance(result, dict) else None
        return cast("dict[str, JsonValue]", state) if isinstance(state, dict) else {}

    def cancel_job(self, job_id: str, now: datetime) -> CancelResult:
        """U08-51: queued → `canceled`; running → `canceled` with the lease kept."""
        params = {"id": job_id, "now": _ts(now)}

        def cancel(conn: sqlite3.Connection) -> CancelResult:
            if conn.execute(sql.CANCEL_QUEUED, params).rowcount == 1:
                return "canceled"
            if conn.execute(sql.CANCEL_RUNNING, {"id": job_id}).rowcount == 1:
                return "cancel_requested"
            return "not_active"

        return run_write(cancel, op="job_cancel")

    def retry_job(self, job_id: str, now: datetime) -> RetryResult:
        """U08-52: failed → `queued` with attempts 0 (`last_error` kept)."""
        params = {"id": job_id, "now": _ts(now)}

        def retry(conn: sqlite3.Connection) -> RetryResult:
            row = conn.execute("SELECT status FROM job WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                return "missing"
            if row[0] != "failed":
                return "not_failed"
            try:
                conn.execute(sql.RETRY, params)
            except sqlite3.IntegrityError:  # job_idem_active: another active job holds the key
                return "conflict"
            return "queued"

        return run_write(retry, op="job_retry")

    def get_job(self, job_id: str) -> JobRow | None:
        """The job row, or None."""
        row = read_one("SELECT * FROM job WHERE job_id = ?", (job_id,))
        return None if row is None else _job(row)

    def list_jobs(
        self, *, status: JobStatus | None, kind: JobKind | None, limit: int
    ) -> list[JobRow]:
        """Newest first (`created_at` descending), optionally filtered; at most ``limit``
        (1 to 1000, checked by the caller, U08-53)."""
        params = {"status": status, "kind": kind, "limit": limit}
        return [_job(row) for row in read_all(sql.LIST, params, max_rows=_LIST_MAX)]

    def reap_expired(self, now: datetime) -> RequeueResult:
        """Requeue (or fail at `max_attempts`) every running job whose lease expired."""
        return _reap(sql.EXPIRED, {"now": _ts(now)}, "job_reap")

    def requeue_owned(self, owners: Sequence[str], now: datetime) -> RequeueResult:
        """The reaper statements for jobs leased by ``owners`` (crash recovery)."""
        if not owners:
            return []
        return _reap(sql.OWNED, {"now": _ts(now), "owners": _array(owners)}, "job_requeue_owned")

    def running_on_host(self, host: str) -> list[JobRow]:
        """Running jobs whose lease owner is `<host>:…` (LIKE wildcards in ``host`` escaped)."""
        rows = read_all(sql.RUNNING_ON_HOST, {"prefix": _like_prefix(host) + ":%"})
        return [_job(row) for row in rows]

    def postpone_class(self, gpu_class: GpuClass, now: datetime, until: datetime) -> int:
        """Move queued jobs of ``gpu_class`` due at ``now`` to ``until``; return the count."""
        params = {"cls": gpu_class, "now": _ts(now), "until": _ts(until)}

        def postpone(conn: sqlite3.Connection) -> int:
            return conn.execute(sql.POSTPONE, params).rowcount

        return run_write(postpone, op="job_postpone")

    def sched_fired(self, schedule: str, fire_at_ts: str) -> bool:
        """True when any job, in any status, carries this `schedule` / `fire_at`."""
        params = {"schedule": schedule, "fire_at": fire_at_ts}
        return read_one(sql.SCHED_FIRED, params) is not None

    def recent_scheduled(self, since: datetime) -> list[JobRow]:
        """Done or failed scheduled jobs finished at or after ``since``, oldest first."""
        return [_job(row) for row in read_all(sql.RECENT_SCHEDULED, {"since": _ts(since)})]

    def rekey_on(self, start: datetime, end: datetime) -> JobRow | None:
        """The queued, running or done rekey job scheduled in ``[start, end)``, or None."""
        row = read_one(sql.REKEY_ON, {"start": _ts(start), "end": _ts(end)})
        return None if row is None else _job(row)

    def queue_stats(self, now: datetime, since: datetime) -> QueueStats:
        """Queue counts for `status_snapshot`: queued, next job, failed since ``since``, and
        every failed job (dead letters)."""
        queued, failed_recent, dead = read_all(sql.QUEUE_COUNTS, {"since": _ts(since)})[0]
        head = read_one(sql.NEXT_JOB, {"now": _ts(now)})
        next_job = None
        if head is not None:
            next_job = NextJob(
                job_id=str(head["job_id"]),
                kind=cast("JobKind", head["kind"]),
                priority=int(head["priority"]),
                scheduled_for=clock.parse_utc(head["scheduled_for"]),
            )
        return QueueStats(int(queued), next_job, int(failed_recent), int(dead))
