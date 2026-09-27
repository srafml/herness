"""Scheduler tick, sequential chains and the planned rekey (impl 08 U08-71 to U08-74;
design 08 §5.11).

Schedules come only from validated settings: per-source `schedule` and `reconcile.schedule`
(read through `cfg.sources.enabled_sources()`, no connectors import), `maintenance` at
`backup.nightly_at`, and `schedule.jobs`. Firing is exactly-once across workers and restarts:
`submit` runs the idem-key insert and the sched check in one `BEGIN IMMEDIATE` transaction.
`schedule_rekey` resolves the next redaction key at call time; only its 8-hex id leaves it
(TH08-02).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Final, Literal
from zoneinfo import ZoneInfo

from pydantic import JsonValue

from herness.core import time as clock
from herness.core.config import get_config
from herness.core.errors import ConfigError, HernessError
from herness.core.jobs.cron import CronExpr, resolve_local
from herness.core.jobs.ports import JobRow, SchedCheck, require_jobs_backend
from herness.core.jobs.queue import enqueue, submit
from herness.core.logging import get_logger
from herness.core.resilience._state import require_ops_backend
from herness.core.resilience.events import record_event
from herness.core.resilience.metrics import record_counter
from herness.core.resilience.settings import ChainStep
from herness.core.secrets import resolve
from herness.core.types import JobSpec

__all__ = [
    "SOURCE_RECONCILE_CATCH_UP",
    "SOURCE_SYNC_CATCH_UP",
    "ScheduleEntry",
    "SchedulerReport",
    "advance_chain",
    "collect_schedules",
    "run_scheduler",
    "schedule_rekey",
]

type IdemMode = Literal["sched", "sync"]

SOURCE_SYNC_CATCH_UP: Final = dt.timedelta(hours=1)  # O08-03
SOURCE_RECONCILE_CATCH_UP: Final = dt.timedelta(hours=12)  # O08-03
REPAIR_WINDOW: Final = dt.timedelta(hours=24)
NEXT_KEY_REF: Final = "redact.hmac_key.next"
REKEY_PRIORITY: Final = 70
_MISSED_MAX: Final = 1000
_KEY_HEX: Final = re.compile(r"[0-9a-fA-F]{64}")
_DURATION: Final = re.compile(r"([0-9]{1,9})([mhd])")
_UNIT_S: Final = {"m": 60, "h": 3600, "d": 86400}
_WEEKDAYS: Final = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
_TICK: Final = dt.timedelta(microseconds=1)
_DAY: Final = dt.timedelta(days=1)

_log: Final = get_logger("jobs")
# Process-local `(schedule, fire_at)` pairs already reported as missed (U08-72 step 1b).
_missed_reported: OrderedDict[tuple[str, str], None] = OrderedDict()
_missed_lock: Final = threading.Lock()


@dataclass(frozen=True, slots=True)
class ScheduleEntry:
    """One schedule (U08-71): cron, catch-up limit, first job and chained steps."""

    name: str
    cron: CronExpr
    catch_up_max: dt.timedelta
    job: ChainStep
    then: tuple[ChainStep, ...]
    idem_mode: IdemMode


@dataclass(frozen=True, slots=True)
class SchedulerReport:
    """Counts of one scheduler tick (U08-72)."""

    fired: int
    missed: int
    errors: int
    chains_advanced: int


@dataclass(frozen=True, slots=True)
class _Source:
    """An entry before its cron is parsed (collection order of U08-71)."""

    name: str
    cron: str
    catch_up: dt.timedelta | str
    job: ChainStep
    then: tuple[ChainStep, ...] = ()
    idem_mode: IdemMode = "sched"


def _duration(text: str) -> dt.timedelta:
    """`<n>m`, `<n>h` or `<n>d` (the settings `catch_up_max` form) as a timedelta."""
    match = _DURATION.fullmatch(text)
    if match is None:
        msg = "catch_up_max must look like <n>m, <n>h or <n>d"
        raise ConfigError(msg)
    return dt.timedelta(seconds=int(match[1]) * _UNIT_S[match[2]])


def _tz() -> ZoneInfo:
    return clock.zone(get_config().weights.business_timezone)


def _sources() -> list[_Source]:
    """Every schedule source in U08-71 order: sync, reconcile, maintenance, `schedule.jobs`."""
    cfg = get_config()
    schedule = cfg.resilience.schedule
    found: list[_Source] = []
    for name, src in cfg.sources.enabled_sources():
        payload: dict[str, JsonValue] = {"source": name}
        if src.schedule is not None:
            step = ChainStep(kind="sync", gpu_class="none", payload=payload)
            found.append(
                _Source(f"sync.{name}", src.schedule, SOURCE_SYNC_CATCH_UP, step, (), "sync")
            )
        step = ChainStep(kind="reconcile", gpu_class="none", payload=payload)
        found.append(
            _Source(f"reconcile.{name}", src.reconcile.schedule, SOURCE_RECONCILE_CATCH_UP, step)
        )
    hour, _, minute = cfg.backup.nightly_at.partition(":")
    backup = ChainStep(kind="maintenance", gpu_class="none", payload={"action": "backup"})
    purge = ChainStep(kind="maintenance", gpu_class="none", payload={"action": "purge"})
    cron = f"{int(minute)} {int(hour)} * * *"
    found.append(_Source("maintenance", cron, schedule.maintenance.catch_up_max, backup, (purge,)))
    found.extend(
        _Source(job.name, job.cron, job.catch_up_max, job.job, tuple(job.then))
        for job in schedule.jobs
    )
    return found


def collect_schedules() -> list[ScheduleEntry]:
    """Every schedule (U08-71); an entry whose cron does not parse is skipped with ERROR
    `jobs.schedule.error` (config validation normally prevents it)."""
    entries: list[ScheduleEntry] = []
    for src in _sources():
        try:
            cron = CronExpr.parse(src.cron)
            catch_up = (
                src.catch_up if isinstance(src.catch_up, dt.timedelta) else _duration(src.catch_up)
            )
        except ConfigError as exc:
            _log.error("jobs.schedule.error", schedule=src.name, error_type=type(exc).__name__)
            continue
        entries.append(ScheduleEntry(src.name, cron, catch_up, src.job, src.then, src.idem_mode))
    return entries


def _local_day(instant: dt.datetime, tz: ZoneInfo) -> tuple[dt.datetime, dt.datetime]:
    """`[start, end)` in UTC of the business-timezone day holding ``instant``."""
    day = instant.astimezone(tz).date()
    midnight = dt.time()
    start = resolve_local(dt.datetime.combine(day, midnight), tz)
    return start, resolve_local(dt.datetime.combine(day + _DAY, midnight), tz)


def _rekey_night(fire: dt.datetime, tz: ZoneInfo) -> bool:
    """Whether the local day of ``fire`` has a queued, running or done rekey job."""
    start, end = _local_day(fire, tz)
    return require_jobs_backend().rekey_on(start, end) is not None


def _note_missed(key: tuple[str, str]) -> bool:
    """Add ``key`` to the bounded missed set; False when it was already there."""
    with _missed_lock:
        if key in _missed_reported:
            return False
        _missed_reported[key] = None
        while len(_missed_reported) > _MISSED_MAX:
            _missed_reported.popitem(last=False)
        return True


def _report_missed(name: str, fire_at: str) -> bool:
    """`schedule_missed` once per process for a fire that never got a job (U08-72 step 1b)."""
    with _missed_lock:
        if (name, fire_at) in _missed_reported:
            return False
    if require_jobs_backend().sched_fired(name, fire_at) or not _note_missed((name, fire_at)):
        return False
    detail = {"schedule": name, "fire_at": fire_at}
    record_event("schedule_missed", component="jobs", target=name, detail=detail)
    record_counter(
        "herness_jobs_schedule_missed_total", component="jobs", labels={"schedule": name}
    )
    return True


def _fire(
    entry: ScheduleEntry, now: dt.datetime, tz: ZoneInfo
) -> Literal["fired", "missed"] | None:
    """Fire the latest due time of ``entry``, or report it missed (U08-72 step 1a-1e)."""
    fire = entry.cron.latest_at_or_before(now, tz)
    if fire is None:
        return None
    name, fire_at = entry.name, clock.format_utc(fire)
    if now - fire > entry.catch_up_max:
        return "missed" if _report_missed(name, fire_at) else None
    payload: dict[str, JsonValue] = {**entry.job.payload, "schedule": name, "fire_at": fire_at}
    if name == "nightly" and _rekey_night(fire, tz):
        payload["rekey_night"] = True
    if entry.idem_mode == "sync":
        idem = f"sync:{entry.job.payload['source']}"
    else:
        idem = f"sched:{name}:{fire_at}"
    spec = _spec(entry.job, payload, idem)
    job_id, created = submit(spec, sched_check=SchedCheck(name, fire_at))
    if not created:
        return None
    detail = {"schedule": name, "fire_at": fire_at}
    record_event("schedule_fired", component="jobs", target=name, job_id=job_id, detail=detail)
    record_counter("herness_jobs_schedule_fired_total", component="jobs", labels={"schedule": name})
    return "fired"


def _spec(step: ChainStep, payload: dict[str, JsonValue], idem: str) -> JobSpec:
    """The job of ``step``; `priority` None becomes `DEFAULT_PRIORITY[kind]` in `submit`."""
    return JobSpec(
        kind=step.kind,
        payload=payload,
        gpu_class=step.gpu_class,
        priority=step.priority,
        idem_key=idem,
    )


def _error(schedule: object, exc: HernessError) -> None:
    name = schedule if isinstance(schedule, str) else None
    _log.error("jobs.schedule.error", schedule=name, error_type=type(exc).__name__)


def run_scheduler(now: dt.datetime) -> SchedulerReport:
    """Fire due schedules with catch-up, then repair chains (U08-72; design 08 §5.11).

    Each entry and each repaired row has its own error boundary: a `HernessError` is logged
    as ERROR `jobs.schedule.error` (`schedule`, `error_type`), counted, and the tick goes on.
    """
    now = clock.ensure_utc(now)
    tz = _tz()
    entries = collect_schedules()
    counts = {"fired": 0, "missed": 0, "errors": 0, "advanced": 0}
    for entry in entries:
        try:
            outcome = _fire(entry, now, tz)
        except HernessError as exc:
            _error(entry.name, exc)
            counts["errors"] += 1
            continue
        if outcome is not None:
            counts[outcome] += 1
    by_name = {entry.name: entry for entry in entries}
    for row in require_jobs_backend().recent_scheduled(since=now - REPAIR_WINDOW):
        try:
            step = _advance(row, by_name, tz)
        except HernessError as exc:
            _error(row.payload.get("schedule"), exc)
            counts["errors"] += 1
            continue
        counts["advanced"] += step is not None and step[1]
    return SchedulerReport(counts["fired"], counts["missed"], counts["errors"], counts["advanced"])


def advance_chain(row: JobRow) -> str | None:
    """Enqueue the next enabled, non-skipped step after a `done` scheduled job, or record
    `chain_broken` for a `failed` one (U08-73); returns the next step's job id."""
    entries = {entry.name: entry for entry in collect_schedules()}
    step = _advance(row, entries, _tz())
    return None if step is None else step[0]


def _once(kind: str, target: str, since: dt.datetime, detail: dict[str, object]) -> None:
    """Record ``kind`` for ``target`` unless one exists since ``since`` (chain repair)."""
    if require_ops_backend().count_events(kind, target=target, since=since) == 0:
        record_event(kind, component="jobs", target=target, detail=detail)


def _skip_reason(step: ChainStep, fire: dt.datetime, tz: ZoneInfo) -> str | None:
    """Why ``step`` of the fire at ``fire`` is skipped (U08-73 step 4), else None."""
    if not step.enabled:
        return "disabled"
    if _WEEKDAYS[fire.astimezone(tz).weekday()] in step.skip_on:
        return "skip_on"
    standard_review = step.kind == "review" and step.payload.get("depth") == "standard"
    if standard_review and _rekey_night(fire, tz):
        return "rekey"
    return None


def _advance(
    row: JobRow, entries: dict[str, ScheduleEntry], tz: ZoneInfo
) -> tuple[str, bool] | None:
    """U08-73 on ``row``: `(job_id, created)` of the next step, or None."""
    name, fire_at = row.payload.get("schedule"), row.payload.get("fire_at")
    done_step = row.payload.get("step", 0)
    if not isinstance(name, str) or not isinstance(fire_at, str) or type(done_step) is not int:
        return None
    entry = entries.get(name)
    if entry is None or not entry.then:
        return None
    fire = clock.parse_utc(fire_at)
    target = f"{name}:{fire_at}"
    if row.status == "failed":
        detail = {"schedule": name, "fire_at": fire_at, "step": done_step, "reason": "failed"}
        _once("chain_broken", target, fire, detail)
        return None
    if row.status != "done":
        return None
    for k in range(done_step + 1, len(entry.then) + 1):
        step = entry.then[k - 1]
        reason = _skip_reason(step, fire, tz)
        if reason is None:
            payload: dict[str, JsonValue] = {
                **step.payload,
                "schedule": name,
                "fire_at": fire_at,
                "step": k,
            }
            # The step's own sched key blocks re-creation once it finished (chain repair).
            check = SchedCheck(name, f"{fire_at}:{k}")
            return submit(_spec(step, payload, f"sched:{name}:{fire_at}:{k}"), sched_check=check)
        detail = {"schedule": name, "fire_at": fire_at, "step": k, "reason": reason}
        _once("chain_skipped", f"{target}:{k}", fire, detail)
    return None


def _key_id(value: str) -> str:
    """First 8 hex chars of SHA-256 over the key bytes (spec 10 `Redactor.key_id` rule)."""
    if _KEY_HEX.fullmatch(value) is None:
        msg = f"{NEXT_KEY_REF} must be 64 hex characters"
        raise ConfigError(msg)
    return hashlib.sha256(bytes.fromhex(value)).hexdigest()[:8]


def _first_fire_at_or_after(earliest: dt.datetime) -> dt.datetime:
    """First `schedule.rekey.cron` fire at or after ``earliest`` (business timezone)."""
    cron = CronExpr.parse(get_config().resilience.schedule.rekey.cron)
    return cron.next_after(earliest - _TICK, _tz())


def schedule_rekey(*, now: dt.datetime | None = None) -> str:
    """Plan the redaction-key rotation night (U08-74; design 08 §5.11 "Rekey"): enqueue
    `maintenance` `{"action": "rekey", "key_id"}` on the decider class at priority 70 for the
    first rekey fire at least `min_notice_h` ahead; returns the job id.

    A missing or malformed `redact.hmac_key.next` raises ConfigError. Only the 8-hex key id
    is written to the payload, the event and the log (TH08-02).
    """
    key_id = _key_id(resolve(NEXT_KEY_REF).get_secret_value())
    start = clock.now() if now is None else clock.ensure_utc(now)
    notice = dt.timedelta(hours=get_config().resilience.schedule.rekey.min_notice_h)
    fire = _first_fire_at_or_after(start + notice)
    payload: dict[str, JsonValue] = {"action": "rekey", "key_id": key_id}
    job_id = enqueue(
        "maintenance", payload, "decider", REKEY_PRIORITY, fire, idem_key=f"rekey:{key_id}"
    )
    detail = {"fire_at": clock.format_utc(fire), "key_id": key_id}
    record_event("rekey_planned", component="jobs", job_id=job_id, detail=detail)
    return job_id
