"""Job child process entry (impl 08 U08-88; design 08 §5.9; TH08-13).

The supervisor starts `child_main` in a `multiprocessing` spawn process with plain-string
arguments and one pipe end, so the spawn payload carries no application objects. The child
ignores console interrupts (the supervisor coordinates stops through the pipe), bootstraps
config and backends, checks that the lease is still its own, runs the handler with a
`ChildJobContext` and sends one `outcome` message. Error messages are redacted and cut to
2 KB before the message is built, so a validation error can never echo raw text.
"""

from __future__ import annotations

import importlib
import signal
import sys
from contextlib import suppress
from typing import TYPE_CHECKING, Final, Literal

from pydantic import JsonValue, ValidationError

from herness.core import time as clock
from herness.core.errors import CircuitOpen, ConfigError, HernessError, RateLimited, SchemaViolation
from herness.core.jobs.context import ChildJobContext
from herness.core.jobs.handlers import resolve_handler, run_handler
from herness.core.jobs.pipe import (
    ERROR_CLASS_CHARS,
    ERROR_MESSAGE_CHARS,
    OutcomeMsg,
    encode_message,
)
from herness.core.jobs.queue import get
from herness.core.logging import bind_ids, get_logger
from herness.core.redact import redact_text
from herness.core.resilience.metrics import flush_metrics

if TYPE_CHECKING:
    from multiprocessing.connection import Connection

    from herness.core.types import JobOutcome

__all__ = ["call_bootstrap", "child_main", "outcome_message"]

MESSAGE_WITHHELD: Final = "[message withheld: redaction failed]"
KEY_CHARS: Final = 200  # a breaker key carried back with a `CircuitOpen` outcome
_BAD_BOOTSTRAP: Final = "bootstrap must name a callable as 'module:function'"

_log = get_logger("jobs")


def call_bootstrap(spec: str) -> None:
    """Import `module` and call `function` of a `"module:function"` bootstrap (U08-87 step 1)."""
    module, sep, name = spec.partition(":")
    if not sep or not module or not name:
        raise ConfigError(_BAD_BOOTSTRAP)
    try:
        target: object = getattr(importlib.import_module(module), name, None)
    except ImportError:
        raise ConfigError(_BAD_BOOTSTRAP) from None
    if not callable(target):
        raise ConfigError(_BAD_BOOTSTRAP)
    target()


def _error_extra(err: HernessError) -> dict[str, JsonValue]:
    """What `finish_job` needs to rebuild a `CircuitOpen` or `RateLimited` (U08-49)."""
    if isinstance(err, CircuitOpen):
        return {"key": err.key[:KEY_CHARS], "retry_at": clock.format_utc(err.retry_at)}
    if isinstance(err, RateLimited) and err.retry_after is not None:
        return {"retry_after": err.retry_after}
    return {}


def outcome_message(res: JobOutcome | HernessError) -> OutcomeMsg:
    """The `outcome` message of a handler result; the error text is redacted, then cut."""
    if not isinstance(res, HernessError):
        return OutcomeMsg(status=res.status, result=dict(res.result))
    clean = redact_text(str(res))
    message = MESSAGE_WITHHELD if clean is None else clean[:ERROR_MESSAGE_CHARS]
    return OutcomeMsg(
        status="error",
        result=_error_extra(res),
        error_class=type(res).__name__[:ERROR_CLASS_CHARS],
        error_message=message,
    )


def _encoded(res: JobOutcome | HernessError) -> bytes:
    """Encoded outcome; one that cannot be encoded becomes a `SchemaViolation` outcome."""
    try:
        return encode_message(outcome_message(res))
    except (ValidationError, SchemaViolation):
        return encode_message(outcome_message(SchemaViolation("job outcome not encodable")))


def _ignore_interrupts() -> None:
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    if sys.platform == "win32":
        signal.signal(signal.SIGBREAK, signal.SIG_IGN)


def _run(job_id: str, owner: str, slot: Literal["gpu", "cpu"], conn: Connection) -> None:
    row = get(job_id)
    if row.status != "running" or row.lease_owner != owner:
        _log.warning("jobs.job.lease_lost", job_id=job_id, owner=owner[:64])
        raise SystemExit(1)
    ctx = ChildJobContext(conn, row, slot=slot)
    try:
        res: JobOutcome | HernessError = run_handler(ctx, resolve_handler(row.kind))
    except ConfigError as exc:  # no handler for the kind
        res = exc
    finally:
        ctx.close()
    with suppress(OSError):  # the supervisor is gone: it recovers the job at its next start
        conn.send_bytes(_encoded(res))
    flush_metrics()


def child_main(
    job_id: str, owner: str, slot: Literal["gpu", "cpu"], bootstrap: str, conn: Connection
) -> None:
    """Run one claimed job in this process (U08-88); exit code 1 when the lease is not ours."""
    _ignore_interrupts()
    try:
        call_bootstrap(bootstrap)
        with bind_ids(job_id=job_id):
            _run(job_id, owner, slot, conn)
    finally:
        conn.close()
