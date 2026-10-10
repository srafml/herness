"""Audit-outcome helpers for `herness.reports.actions` (private; T09-12 review I1).

An impl 09 ``admin_action`` line is written before the change and records a request
(``detail="requested"``). When the change then raises, one best-effort follow-up line with
``detail="failed:<ErrorType>"`` is written and ``app.action.failed`` is logged, so the trail
never reads a failed change as done. Outcomes that are not failures (``not_active``) go to
``app.action.completed`` only. ``memory_decide`` maps impl 07's "not pending" refusal
of a memory decision to a ``UserInputError``. Logs carry ids and error class names only.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Final, Literal

from herness.core.audit import audit
from herness.core.errors import PolicyViolation
from herness.core.logging import get_logger
from herness.reports.rules import Actor, UserInputError

if TYPE_CHECKING:
    from herness.harness.memory import MemoryStore

__all__ = ["Decided", "admin_change", "already_finished", "completed", "failed", "memory_decide"]

type Decided = Literal["approved", "rejected"]

_log = get_logger("app.action")
_NOT_PENDING: Final = frozenset({"approve.not_pending", "reject.not_pending"})


def completed(actor: Actor, action: str, **fields: object) -> None:
    """Log ``app.action.completed`` with the action's ids and the channel."""
    _log.info("app.action.completed", action=action, **fields, channel=actor.channel)


def failed(actor: Actor, action: str, target: str, exc: Exception, *, line: bool) -> None:
    """Log ``app.action.failed``; with ``line`` also a best-effort ``failed:<Type>`` audit line
    (its own write failure is logged, never raised: the change error is what propagates)."""
    error = type(exc).__name__
    _log.error("app.action.failed", action=action, target=target, error_type=error,
               channel=actor.channel)  # fmt: skip
    if not line:
        return
    try:
        audit("admin_action", actor.user_ref, action=action, target=target,
              detail=f"failed:{error}")  # fmt: skip
    except Exception as audit_exc:  # noqa: BLE001 - logged; the change error propagates
        _log.error("app.action.audit_failed", action=action, target=target,
                   error_type=type(audit_exc).__name__)  # fmt: skip


def admin_change[T](actor: Actor, action: str, target: str, change: Callable[[], T]) -> T:
    """``admin_action`` line marked ``requested`` (its failure blocks the change), then the
    change; a raising change adds the ``failed:<Type>`` line and re-raises."""
    audit("admin_action", actor.user_ref, action=action, target=target, detail="requested")
    try:
        return change()
    except Exception as exc:
        failed(actor, action, target, exc, line=True)
        raise


def already_finished(run_id: str, status: str) -> UserInputError:
    """The U09-40 refusal for a finished run resumed without ``--force``."""
    return UserInputError(f"Run {run_id} is already {status}; use --force to resume it.")


def memory_decide(
    memory: MemoryStore, memory_id: str, actor: Actor, note: str | None, *, approve: bool,
    refusal: str,
) -> Decided:  # fmt: skip
    """``MemoryStore.approve`` or ``reject``; impl 07 "not pending" → UserInputError(refusal)."""
    try:
        if approve:
            memory.approve(memory_id, actor.user_ref, note)
        else:
            memory.reject(memory_id, actor.user_ref, note or "")  # required: validated non-empty
    except PolicyViolation as exc:
        if exc.details.get("rule") in _NOT_PENDING:
            raise UserInputError(refusal) from None
        raise
    return "approved" if approve else "rejected"
