"""Job handler registry and wrapper (impl 08 U08-55, U08-56; R-42).

Handlers are looked up only by `JobKind` in this static, per-process registry
(`process_state().handlers`); nothing in a payload selects the callable.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final, cast, get_args

from pydantic import ValidationError

from herness.core.errors import ConfigError, FatalError, HernessError, SchemaViolation
from herness.core.jobs.ports import JobContext
from herness.core.logging import get_logger
from herness.core.resilience._state import process_state
from herness.core.types import JobKind, JobOutcome

type Handler = Callable[[JobContext], JobOutcome]

_KINDS: Final[frozenset[str]] = frozenset(get_args(JobKind.__value__))
_KIND_CHARS: Final = 40  # an unknown kind is cut to this length in messages

_log: Final = get_logger("jobs")


def register_handler(kind: JobKind, handler: Handler) -> None:
    """Register ``handler`` for ``kind`` (U08-55); the same object again is a no-op.

    Raises ConfigError for an unknown kind, a non-callable, or a different handler for a kind
    that already has one.
    """
    if kind not in _KINDS:
        msg = f"unknown job kind {str(kind)[:_KIND_CHARS]}"
        raise ConfigError(msg)
    if not callable(cast("object", handler)):  # checked at run time, not trusted
        msg = f"handler for {kind} is not callable"
        raise ConfigError(msg)
    state = process_state()
    with state.lock:
        existing = state.handlers.setdefault(kind, handler)
    if existing is not handler:
        msg = f"handler already registered for {kind}"
        raise ConfigError(msg)


def resolve_handler(kind: JobKind) -> Handler:
    """The handler registered for ``kind`` (U08-55); none → ConfigError."""
    state = process_state()
    with state.lock:
        handler = state.handlers.get(kind)
    if handler is None:
        msg = f"no handler for {str(kind)[:_KIND_CHARS]}"
        raise ConfigError(msg)
    return handler


def run_handler(ctx: JobContext, handler: Handler) -> JobOutcome | HernessError:
    """Call ``handler(ctx)``; errors are returned, not raised, for U08-50 (U08-56).

    A non-`JobOutcome` result → FatalError; HernessError → itself; pydantic ValidationError →
    SchemaViolation; any other Exception → logged `jobs.handler.crashed` and FatalError.
    BaseException that is not an Exception propagates.
    """
    try:
        result: object = handler(ctx)
    except HernessError as exc:
        return exc
    except ValidationError:
        return SchemaViolation("handler output invalid")
    except Exception as exc:  # noqa: BLE001 - worker top level (ENG §3.4): wrap, never raise
        error_type = type(exc).__name__
        _log.error("jobs.handler.crashed", job_id=ctx.job_id, kind=ctx.kind, error_type=error_type)
        return FatalError(f"handler raised {error_type}")
    if not isinstance(result, JobOutcome):
        return FatalError(f"handler returned {type(result).__name__}")
    return result
