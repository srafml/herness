"""``herness privacy delete`` and ``herness maintenance backup|purge`` (impl 10 U10-73, U10-74).

Dispatch only: each command enqueues ``maintenance`` jobs (the work is ``handle_maintenance``,
U10-104) and, with ``inline`` (admin-only ``--inline``, R-45), runs them in this process through
``run_inline`` after ``register_handlers`` (U10-75, called by the composition root). Record IDs
are validated before they reach any queue, file or SQL path and are never echoed (TH10-42).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final, Literal, cast

from pydantic import JsonValue

from herness._cli.output import CommandResult
from herness.core import time as clock
from herness.core.errors import ConfigError
from herness.core.jobs import enqueue, run_inline, worker_alive
from herness.core.types import JobOutcome

__all__ = ["cmd_maintenance", "cmd_privacy_delete"]

_RECORD_ID: Final = re.compile(
    r"[a-z][a-z0-9_]{0,31}:[a-z][a-z0-9_]{0,63}:[A-Za-z0-9._:@/+-]{1,200}", re.ASCII
)
_REASON_REF: Final = re.compile(r"[A-Za-z0-9._:/-]{1,64}", re.ASCII)
# Mirrors JobSpec.idem_key (herness/core/types/jobs.py): the derived key must be accepted
# before anything is enqueued, so a late SchemaViolation cannot leave earlier IDs enqueued.
_IDEM_KEY: Final = re.compile(r"[A-Za-z0-9_:.\-/]+", re.ASCII)
_IDEM_MAX: Final = 200
_IDEM_PREFIX: Final = "privacy_delete:"
_MAX_IDS: Final = 100
_PRIORITY: Final = 80
_NO_WORKER: Final = "no worker is running; start it with `herness worker` or rerun with `--inline`"


def _validate(record_ids: Sequence[str], reason_ref: str) -> None:
    """Preconditions of U10-73; positions are 1-based and values are never echoed."""
    raw = cast("object", record_ids)  # checked at run time, not trusted
    if isinstance(raw, str) or not 1 <= len(record_ids) <= _MAX_IDS:
        msg = f"record_ids must hold 1 to {_MAX_IDS} ids"
        raise ConfigError(msg)
    for position, item in enumerate(cast("Sequence[object]", record_ids), start=1):
        ok = isinstance(item, str) and _RECORD_ID.fullmatch(item) and ".." not in item
        key = _IDEM_PREFIX + item if isinstance(item, str) else ""
        if not (ok and len(key) <= _IDEM_MAX and _IDEM_KEY.fullmatch(key)):
            msg = f"invalid record_id at position {position}"
            raise ConfigError(msg)
    if not isinstance(cast("object", reason_ref), str) or _REASON_REF.fullmatch(reason_ref) is None:
        msg = "invalid reason_ref"
        raise ConfigError(msg)


def _outcome(job_id: str) -> dict[str, JsonValue]:
    result: JobOutcome = run_inline(job_id)
    return {"status": result.status, "result": result.result}


def cmd_privacy_delete(
    record_ids: Sequence[str], *, reason_ref: str, actor: str, inline: bool = False
) -> CommandResult:
    """``herness privacy delete`` (U10-73): one ``privacy_delete`` job per record ID."""
    _validate(record_ids, reason_ref)
    jobs: list[dict[str, object]] = []
    for record_id in record_ids:
        payload: dict[str, JsonValue] = {
            "action": "privacy_delete",
            "record_id": record_id,
            "reason_ref": reason_ref,
            "requested_by": actor,
        }
        job_id = enqueue(
            "maintenance",
            payload,
            "none",
            priority=_PRIORITY,
            idem_key=_IDEM_PREFIX + record_id,
        )
        jobs.append({"record_id": record_id, "job_id": job_id})
    warnings = [] if worker_alive() else [_NO_WORKER]
    if inline:
        for job in jobs:
            job["outcome"] = _outcome(str(job["job_id"]))
    return CommandResult(ok=True, data={"jobs": jobs}, warnings=warnings, exit_code=0)


def cmd_maintenance(
    action: Literal["backup", "purge"], *, dry_run: bool, actor: str, inline: bool = False
) -> CommandResult:
    """``herness maintenance backup|purge`` (U10-74): one manual ``maintenance`` job."""
    date = clock.now().strftime("%Y-%m-%d")
    payload: dict[str, JsonValue] = {"action": action, "dry_run": dry_run, "requested_by": actor}
    job_id = enqueue(
        "maintenance",
        payload,
        "none",
        priority=_PRIORITY,
        idem_key=f"maintenance:{action}:manual:{date}",
    )
    data: dict[str, object] = {"job_id": job_id}
    warnings = [] if worker_alive() else [_NO_WORKER]
    if inline:
        data["outcome"] = _outcome(job_id)
    return CommandResult(ok=True, data=data, warnings=warnings, exit_code=0)
