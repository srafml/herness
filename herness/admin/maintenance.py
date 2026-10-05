"""``maintenance`` job dispatch (impl 10 U10-104; impl 08 §5.1, R-42).

``handle_maintenance`` reads ``ctx.job.payload["action"]`` and calls one unit from a static
table; nothing in the payload selects a callable. The units are owned by later cards: until a
card lands, its table entry is a placeholder that raises ``FatalError`` naming the action and
the owner card (that card replaces the one marked line).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Final

from pydantic import JsonValue

from herness.core.errors import ConfigError, FatalError
from herness.core.jobs.ports import JobContext
from herness.core.types import JobOutcome

__all__ = ["handle_maintenance"]

type _Unit = Callable[[JobContext, Callable[[str], int]], JobOutcome | dict[str, JsonValue]]


def _placeholder(action: str, owner: str) -> _Unit:
    """A table entry for a unit whose card is not built: fail the job, typed, never silently."""

    def unit(_ctx: JobContext, _memory_purge: Callable[[str], int]) -> JobOutcome:
        msg = f"maintenance action {action} is not available: owned by {owner}"
        raise FatalError(msg)

    return unit


_ACTIONS: Final[Mapping[str, _Unit]] = {
    "backup": _placeholder("backup", "T10-20"),  # T10-20: run_backup (U10-96)
    "purge": _placeholder("purge", "T10-20"),  # T10-20: run_purge (U10-98)
    "privacy_delete": _placeholder("privacy_delete", "T10-29"),  # T10-29: run_privacy_delete
    "rekey": _placeholder("rekey", "T10-30"),  # T10-30: run_rekey (U10-101)
}


def handle_maintenance(ctx: JobContext, *, memory_purge: Callable[[str], int]) -> JobOutcome:
    """Dispatch one ``maintenance`` job by ``payload.action`` (U10-104).

    An action outside the table raises ``ConfigError("unknown maintenance action")``. A unit
    returns its own ``JobOutcome`` or the counts for ``JobOutcome(status="done", ...)``.
    """
    action = ctx.job.payload.get("action")
    unit = _ACTIONS.get(action) if isinstance(action, str) else None
    if unit is None:
        msg = "unknown maintenance action"
        raise ConfigError(msg)
    outcome = unit(ctx, memory_purge)
    if isinstance(outcome, JobOutcome):
        return outcome
    return JobOutcome(status="done", result=outcome)
