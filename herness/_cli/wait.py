"""Enqueue, follow and inline-run jobs for the long-running CLI commands (impl 09 U09-90,
U09-91, U09-103).

Every line written here goes to stderr (progress, the worker warning, detach and failure
notes), so stdout stays the command's own output (TH09-22). The impl 08 job functions are
reached through the ``jobs`` module reference at call time. ``--inline`` is admin only
(TH09-27, R-45).
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Final, cast

# T09-25: replace _clean with herness._cli.term.safe_terminal_text (U09-100) once term.py exists.
from herness._cli.output import _clean, exit_code_for_class_name, json_default
from herness.core import jobs
from herness.core import time as clock
from herness.core.errors import SchemaViolation, StoreBusy
from herness.core.logging import get_logger
from herness.reports.rules import UserInputError, require_role

if TYPE_CHECKING:
    from pydantic import JsonValue

    from herness.cli import GlobalOptions
    from herness.core.jobs import JobRow
    from herness.core.types import GpuClass, JobKind
    from herness.reports.rules import Actor

__all__ = ["MAX_FOLLOW_S", "FollowOutcome", "follow_job", "run_job_inline", "submit_job"]

MAX_FOLLOW_S: Final = 172_800  # 48 h bounded wait (ENG §2.5)
MAX_PAYLOAD_BYTES: Final = 65_536  # spec 08 §4.1
MAX_BUSY_POLLS: Final = 10
_TERMINAL: Final = frozenset({"done", "failed", "canceled"})
_NO_WORKER: Final = (
    "Warning: No worker is running; the job is queued.\n"
    "Fix: Start `herness worker` or the `herness-worker` task, or rerun with `--inline` (admin).\n"
)
_PARTIAL: Final = "Run `{run_id}` finished partial: some tasks did not finish."
_CIRCUIT: Final = (
    "The source circuit is open; this sync was skipped. The next scheduled run retries."
)
_MAX_FOLLOW_NOTE: Final = "Still running; detached. Follow with `herness jobs list`."

_log = get_logger("cli")


@dataclass(frozen=True)
class FollowOutcome:
    """How a followed or inline-run job ended (U09-91)."""

    job: JobRow
    exit_code: int
    run_id: str | None
    detached: bool
    partial: bool
    warnings: tuple[str, ...]


def _say(text: str) -> None:
    sys.stderr.write(_clean(text) + "\n")


def _warn_no_worker(opts: GlobalOptions, job_id: str) -> None:
    sys.stderr.write(_NO_WORKER)
    opts.worker_warned = True
    _log.warning("cli.worker.absent", job_id=job_id)


def submit_job(  # noqa: PLR0913 - U09-90 keywords plus the options and the inline flag
    opts: GlobalOptions,
    *,
    kind: JobKind,
    payload: dict[str, object],
    gpu_class: GpuClass,
    priority: int | None = jobs.MANUAL_PRIORITY,
    scheduled_for: datetime | None = None,
    idem_key: str | None = None,
    inline: bool = False,
) -> str:
    """Enqueue one job (U09-90); warn once on stderr when no worker runs, unless ``inline``.

    A payload over 64 KB of JSON raises UserInputError before anything is enqueued.
    """
    try:
        text = json.dumps(payload, default=json_default, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        msg = "job payload is not JSON serialisable"
        raise SchemaViolation(msg) from None
    if len(text.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        msg = "job payload too large"
        raise UserInputError(msg, hint="Pass fewer or shorter options.")
    body = cast("dict[str, JsonValue]", json.loads(text))
    job_id = jobs.enqueue(kind, body, gpu_class, priority, scheduled_for, idem_key=idem_key)
    _log.info("cli.job.enqueued", job_id=job_id, kind=kind)
    if not inline and not jobs.worker_alive():
        _warn_no_worker(opts, job_id)
    return job_id


def _run_id(job: JobRow) -> str | None:
    """``result.run_id``, else ``payload.request.run_id`` or ``payload.run_id`` (U09-91)."""
    request = job.payload.get("request")
    found = [
        (job.result or {}).get("run_id"),
        request.get("run_id") if isinstance(request, Mapping) else None,
        job.payload.get("run_id"),
    ]
    return next((value for value in found if isinstance(value, str)), None)


def _run_partial(run_id: str | None) -> bool:
    if run_id is None:
        return False
    from herness.store import ops  # noqa: PLC0415 - lazy: the store loads on first follow

    run = ops.get_run(run_id)
    return run is not None and run.status == "partial"


def _failed(opts: GlobalOptions, job: JobRow) -> int:
    error = job.last_error or {}
    name = error.get("class")
    name = name if isinstance(name, str) else "UnknownError"
    key = error.get("key")
    code = exit_code_for_class_name(name, key=key if isinstance(key, str) else None)
    message = error.get("message")
    if not opts.json:
        text = message if isinstance(message, str) else ""
        _say(f"Error: Job `{job.job_id}` failed ({name}): {text}")
    return code


def _terminal(opts: GlobalOptions, job: JobRow, run_id: str | None) -> FollowOutcome:
    """U09-91 step 5: the R-46 exit code and warnings of a job in a final status."""
    code, partial = 1, False
    warnings: tuple[str, ...] = ()
    result = job.result or {}
    if job.status == "done":
        code = 0
        if result.get("partial") is True or _run_partial(run_id):
            code, partial, warnings = 6, True, (_PARTIAL.format(run_id=run_id),)
        elif result.get("outcome") == "skipped_open_circuit":
            code, warnings = 4, (_CIRCUIT,)
    elif job.status == "failed":
        code = _failed(opts, job)
    elif job.status == "canceled" and not opts.json:
        _say(f"Error: Job `{job.job_id}` was canceled.")
    return FollowOutcome(job, code, run_id, False, partial, warnings)


@dataclass
class _Follow:
    """Per-follow state: the last job row, progress key and consecutive busy polls."""

    opts: GlobalOptions
    job_id: str
    job: JobRow | None = None
    run_id: str | None = None
    shown: object = None
    busy: int = 0

    def poll(self) -> JobRow | None:
        """One ``jobs.get`` (plus progress and worker check); None after a StoreBusy."""
        try:
            job = jobs.get(self.job_id)
            self.run_id = _run_id(job)
            self._progress(job)
            if not self.opts.worker_warned and not jobs.worker_alive():
                _warn_no_worker(self.opts, self.job_id)
        except StoreBusy:
            self.busy += 1
            _log.warning("cli.job.poll_busy", job_id=self.job_id, failures=self.busy)
            if self.busy >= MAX_BUSY_POLLS:
                raise
            return None
        self.busy, self.job = 0, job
        return job

    def _progress(self, job: JobRow) -> None:
        counts: list[tuple[str, str, int]] = []
        if job.kind == "review" and self.run_id is not None:
            from herness.store import ops  # noqa: PLC0415 - lazy: the store loads on first follow

            counts = ops.ui_task_status_counts(self.run_id)
        key = (job.status, job.attempts, tuple(counts))
        if key == self.shown:
            return
        self.shown = key
        if not self.opts.quiet:
            line = f"Job `{job.job_id}`: {job.status} (attempt {job.attempts}/{job.max_attempts})"
            tasks = ", ".join(f"{role} {status} {n}" for role, status, n in counts)
            _say(line + (f"; tasks: {tasks}" if tasks else ""))


def _detached(state: _Follow, job: JobRow, code: int, reason: str, note: str) -> FollowOutcome:
    _say(note)
    _log.info("cli.job.detached", job_id=state.job_id, reason=reason)
    return FollowOutcome(job, code, state.run_id, True, False, ())


def _loop(state: _Follow, poll_s: float) -> FollowOutcome:
    start = clock.now()
    while True:
        job = state.poll()
        if job is not None and job.status in _TERMINAL:
            return _terminal(state.opts, job, state.run_id)
        if state.job is not None and (clock.now() - start).total_seconds() >= MAX_FOLLOW_S:
            return _detached(state, state.job, 0, "max_follow", _MAX_FOLLOW_NOTE)
        clock.sleep(poll_s)


def follow_job(opts: GlobalOptions, job_id: str, *, poll_s: float) -> FollowOutcome:
    """``--wait``: poll the job until it ends, Ctrl+C (exit 130) or 48 h (exit 0), detached
    in both latter cases; the job is never canceled (U09-91).

    A failed or canceled job's message is printed in human mode; JSON callers build the error
    envelope from ``outcome.job.last_error``. An interrupt before the first row is read
    propagates to ``main`` (exit 130, U09-84).
    """
    state = _Follow(opts, job_id)
    try:
        return _loop(state, poll_s)
    except KeyboardInterrupt:
        if state.job is None:
            raise
        note = f"Detached; job `{job_id}` keeps running."
        return _detached(state, state.job, 130, "interrupt", note)


def run_job_inline(opts: GlobalOptions, actor: Actor, job_id: str) -> FollowOutcome:
    """``--inline`` (admin only, R-45): run the queued job in this process (U09-103)."""
    require_role(actor, "job_inline")
    _log.info("cli.job.inline_started", job_id=job_id)
    outcome = jobs.run_inline(job_id)
    job = jobs.get(job_id)
    if outcome.status == "yield":
        _say(f"Interrupted; job `{job_id}` was released and stays queued.")
        return FollowOutcome(job, 130, _run_id(job), True, False, ())
    result = _terminal(opts, job, _run_id(job))
    _log.info(
        "cli.job.inline_completed",
        job_id=job_id,
        kind=job.kind,
        status=job.status,
        exit_code=result.exit_code,
    )
    return result
