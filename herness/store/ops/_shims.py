"""Interim stand-in for one impl 08 function that ``run_write`` needs (impl 02 U02-38).

``retry_call`` replaces ``T08-07 (herness.core.resilience.retry_call)`` for the ``sqlite_write``
policy only. ``fault_point`` is now a re-export of ``herness.core.resilience.fault_point``
(T08-08). ``core`` calls both through this module's attributes, so tests monkeypatch them here;
T08-07 swaps the call sites to ``herness.core.resilience`` and deletes this module.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from herness.core import time as clock
from herness.core.errors import ConfigError, RetryableError
from herness.core.resilience import fault_point, process_state

__all__ = ("fault_point", "retry_call")  # fault_point: re-export of the real hook (T08-08)


@dataclass(frozen=True, slots=True)
class _Policy:
    attempts: int
    base_s: float
    cap_s: float
    max_elapsed_s: float


# Design 08 §5.2 / §7 defaults of `sqlite_write`. Design 08 lists it for StoreBusy; like U08-28,
# the shim retries every RetryableError (run_write raises no other RetryableError).
_POLICIES: Final[dict[str, _Policy]] = {
    "sqlite_write": _Policy(attempts=6, base_s=0.2, cap_s=5.0, max_elapsed_s=30.0),
}


# T08-07: replace with herness.core.resilience.retry_call (U08-28) and delete this function.
def retry_call[T](name: str, fn: Callable[[], T]) -> T:
    """Run ``fn`` under policy ``name``, retrying ``RetryableError`` with full jitter.

    Stops after ``attempts`` calls or once ``max_elapsed_s`` has passed after a failure
    (tenacity ``stop_after_attempt | stop_after_delay``); the last error is re-raised. Other
    exceptions, and ``BaseException`` subclasses, pass through unchanged. Sleeps go through
    ``process_state().sleep`` with ``process_state().rng``, as U08-28 does.
    """
    policy = _POLICIES.get(name)
    if policy is None:
        msg = f"unknown retry policy {name}"
        raise ConfigError(msg)
    state = process_state()
    start = clock.monotonic()
    attempt = 1
    while True:
        try:
            return fn()
        except RetryableError:
            elapsed = clock.monotonic() - start
            if attempt >= policy.attempts or elapsed >= policy.max_elapsed_s:
                raise
            ceiling = min(policy.cap_s, policy.base_s * 2 ** (attempt - 1))
            state.sleep(state.rng.uniform(0, ceiling))
            attempt += 1
