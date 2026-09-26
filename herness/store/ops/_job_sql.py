"""Constant SQL of the jobs area (impl 08 U08-95; T08-11 spec note): the design 08 §5.7
statements plus the U08-95 extensions, split off `herness.store.ops.jobs` for its 400-line
budget. Only `jobs` imports it (`ops-areas-acyclic` ignore entry). Every value is bound as a
named parameter; list parameters are JSON arrays read with `json_each`.
"""

from __future__ import annotations

from typing import Final

INSERT: Final = (
    "INSERT INTO job (job_id, kind, gpu_class, status, priority, payload, idem_key, attempts,"
    " max_attempts, scheduled_for, created_at)"
    " VALUES (:job_id, :kind, :gpu_class, 'queued', :priority, :payload, :idem_key, 0,"
    " :max_attempts, :scheduled_for, :now)"
    " ON CONFLICT (idem_key) WHERE status IN ('queued', 'running') DO NOTHING RETURNING job_id"
)
SCHED_SEEN: Final = (
    "SELECT job_id FROM job WHERE (idem_key = :sched_key) OR"
    " (json_extract(payload, '$.schedule') = :schedule"
    " AND json_extract(payload, '$.fire_at') = :fire_at) LIMIT 1"
)
ACTIVE_IDEM: Final = (
    "SELECT job_id FROM job WHERE idem_key = :idem_key AND status IN ('queued', 'running')"
)
# The claim subquery filter of design 08 §5.7 plus the U08-95 priority and slot rules (R-43).
ELIGIBLE: Final = (
    "j.status = 'queued' AND j.scheduled_for <= :now"
    " AND j.gpu_class IN (SELECT value FROM json_each(:allowed_classes))"
    " AND NOT (j.kind IN (SELECT value FROM json_each(:exclusive_kinds))"
    " AND EXISTS (SELECT 1 FROM job r WHERE r.status = 'running'"
    " AND r.kind IN (SELECT value FROM json_each(:exclusive_kinds))))"
    " AND (:min_priority IS NULL OR j.priority >= :min_priority"
    " OR j.kind IN (SELECT value FROM json_each(:exempt_kinds)))"
    " AND (j.gpu_class != 'none' OR :slot = 'cli'"
    " OR ((j.kind IN (SELECT value FROM json_each(:gpu_slot_kinds))) = (:slot = 'gpu')))"
)
_CLAIM_HEAD: Final = (
    "UPDATE job SET status = 'running', lease_owner = :owner, lease_expires_at = :lease_until,"
    " attempts = attempts + 1, started_at = COALESCE(started_at, :now)"
    " WHERE job_id = (SELECT j.job_id FROM job j WHERE "
)
_CLAIM_TAIL: Final = (
    " AND (:job_id IS NULL OR j.job_id = :job_id)"
    " ORDER BY j.priority DESC, j.scheduled_for ASC, j.created_at ASC LIMIT 1)"
    " AND status = 'queued' RETURNING *"
)
CLAIM: Final = _CLAIM_HEAD + ELIGIBLE + _CLAIM_TAIL
COUNTS: Final = (
    "SELECT j.gpu_class, COUNT(*) FROM job j WHERE "  # noqa: S608 - constant fragments only
    + ELIGIBLE
    + " GROUP BY j.gpu_class"
)
HEARTBEAT: Final = (
    "UPDATE job SET lease_expires_at = :lease_until"
    " WHERE job_id = :id AND lease_owner = :owner AND status = 'running'"
)
DONE: Final = (
    "UPDATE job SET status = 'done', result = :result, finished_at = :now, lease_owner = NULL,"
    " lease_expires_at = NULL WHERE job_id = :id AND lease_owner = :owner AND status = 'running'"
)
YIELD: Final = (
    "UPDATE job SET status = 'queued', attempts = attempts - 1, scheduled_for = :resume_at,"
    " lease_owner = NULL, lease_expires_at = NULL"
    " WHERE job_id = :id AND lease_owner = :owner AND status = 'running'"
)
REQUEUE: Final = (
    "UPDATE job SET status = 'queued', scheduled_for = :at, last_error = :le,"
    " lease_owner = NULL, lease_expires_at = NULL"
    " WHERE job_id = :id AND lease_owner = :owner AND status = 'running'"
)
FAILED: Final = (
    "UPDATE job SET status = 'failed', finished_at = :now, last_error = :le,"
    " lease_owner = NULL, lease_expires_at = NULL"
    " WHERE job_id = :id AND lease_owner = :owner AND status = 'running'"
)
FINALIZE_CANCELED: Final = (
    "UPDATE job SET finished_at = :now, lease_owner = NULL, lease_expires_at = NULL"
    " WHERE job_id = :id AND lease_owner = :owner AND status = 'canceled'"
)
SAVE_STATE: Final = (
    "UPDATE job SET result = json_object('state', json(:state))"
    " WHERE job_id = :id AND lease_owner = :owner AND status = 'running'"
)
CANCEL_QUEUED: Final = (
    "UPDATE job SET status = 'canceled', finished_at = :now"
    " WHERE job_id = :id AND status = 'queued'"
)
CANCEL_RUNNING: Final = (
    "UPDATE job SET status = 'canceled' WHERE job_id = :id AND status = 'running'"
)
RETRY: Final = (
    "UPDATE job SET status = 'queued', attempts = 0, scheduled_for = :now, lease_owner = NULL,"
    " lease_expires_at = NULL, finished_at = NULL WHERE job_id = :id AND status = 'failed'"
)
# Reaper statements of design 08 §5.7; `{rows}` is one of the two constant row tests below.
_REAP_SELECT: Final = (
    "SELECT job_id, kind, attempts, max_attempts FROM job WHERE status = 'running' AND {rows}"
    " ORDER BY job_id"
)
_REAP_QUEUE: Final = (
    "UPDATE job SET status = 'queued', lease_owner = NULL, lease_expires_at = NULL,"
    " scheduled_for = :now,"
    " last_error = json_object('class', 'LeaseExpired', 'at', :now, 'attempt', attempts)"
    " WHERE status = 'running' AND {rows} AND attempts < max_attempts"
)
_REAP_FAIL: Final = (
    "UPDATE job SET status = 'failed', finished_at = :now, lease_owner = NULL,"
    " last_error = json_object('class', 'LeaseExpired', 'at', :now, 'attempt', attempts)"
    " WHERE status = 'running' AND {rows} AND attempts >= max_attempts"
)
EXPIRED: Final = "lease_expires_at < :now"
OWNED: Final = "lease_owner IN (SELECT value FROM json_each(:owners))"
# (select, requeue, fail) per row test.
REAPERS: Final = {
    rows: tuple(sql.format(rows=rows) for sql in (_REAP_SELECT, _REAP_QUEUE, _REAP_FAIL))
    for rows in (EXPIRED, OWNED)
}
RUNNING_ON_HOST: Final = (
    "SELECT * FROM job WHERE status = 'running' AND lease_owner LIKE :prefix ESCAPE '\\'"
)
POSTPONE: Final = (
    "UPDATE job SET scheduled_for = :until"
    " WHERE status = 'queued' AND gpu_class = :cls AND scheduled_for <= :now"
)
SCHED_FIRED: Final = (
    "SELECT 1 FROM job WHERE json_extract(payload, '$.schedule') = :schedule"
    " AND json_extract(payload, '$.fire_at') = :fire_at LIMIT 1"
)
RECENT_SCHEDULED: Final = (
    "SELECT * FROM job WHERE status IN ('done', 'failed')"
    " AND json_extract(payload, '$.schedule') IS NOT NULL AND finished_at >= :since"
    " ORDER BY finished_at, job_id"
)
REKEY_ON: Final = (
    "SELECT * FROM job WHERE kind = 'maintenance' AND json_extract(payload, '$.action') = 'rekey'"
    " AND status IN ('queued', 'running', 'done')"
    " AND scheduled_for >= :start AND scheduled_for < :end ORDER BY scheduled_for LIMIT 1"
)
LIST: Final = (
    "SELECT * FROM job WHERE (:status IS NULL OR status = :status)"
    " AND (:kind IS NULL OR kind = :kind) ORDER BY created_at DESC, job_id DESC LIMIT :limit"
)
# Highest-priority due queued job (priority DESC, scheduled_for, created_at), else the earliest
# job not yet due. Class, slot and exclusive-kind eligibility are not applied.
NEXT_JOB: Final = (
    "SELECT job_id, kind, priority, scheduled_for FROM job WHERE status = 'queued'"
    " ORDER BY scheduled_for > :now, CASE WHEN scheduled_for <= :now THEN -priority END,"
    " scheduled_for, created_at LIMIT 1"
)
QUEUE_COUNTS: Final = (
    "SELECT COUNT(*) FILTER (WHERE status = 'queued'),"
    " COUNT(*) FILTER (WHERE status = 'failed' AND finished_at >= :since),"
    " COUNT(*) FILTER (WHERE status = 'failed') FROM job"
)
