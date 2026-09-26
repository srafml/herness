"""Retry execution: `retry_call`, `aretry_call`, `retrying`, `retry_page`, `call_with_timeout`
(impl 08 U08-28..U08-32; design 08 §3.1, §5.2 tenacity wiring; TH08-04, TH08-05).

Each attempt runs under the breaker and classification (`_invoke`). Attempt 1 runs before
any tenacity object is built (the no-retry hot path, BT08-01); its failure is handed to
tenacity as attempt 1, so stop, wait and `before_sleep` see the same attempts. Elapsed time
is `clock.monotonic`, sleeps are `process_state().sleep` / `.asleep` (fake-clock driven).
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import threading
from collections.abc import Awaitable, Callable
from typing import Any, Final, cast

import tenacity
from tenacity import RetryCallState, retry_if_exception, stop_after_attempt
from tenacity.stop import stop_base

from herness.core import time as clock
from herness.core.errors import (
    CircuitOpen,
    ConfigError,
    HernessError,
    ModelUnavailable,
    RateLimited,
    RetryableError,
)
from herness.core.logging import get_logger
from herness.core.resilience._state import process_state
from herness.core.resilience.breaker import breaker, guard
from herness.core.resilience.classify import ErrorFamily, classify
from herness.core.resilience.events import record_event
from herness.core.resilience.metrics import record_counter
from herness.core.resilience.policies import (
    POLICY_FAMILY,
    FullJitterRetryAfter,
    RetryPolicy,
    policy,
)
from herness.core.resilience.ports import TracerLike
from herness.core.types import PolicyName

__all__ = ("aretry_call", "call_with_timeout", "retry_call", "retry_page", "retrying")

RETRIES_METRIC: Final = "herness_resilience_retries_total"
_log: Final = get_logger("resilience")


_emitting: Final = threading.local()  # `.active`: a `retry` event is being written here


class _StopAfterDelay(stop_base):
    """`stop_after_delay` on `clock.monotonic`, from the start of the first attempt."""

    def __init__(self, max_s: float, start: float) -> None:
        self.max_s, self.start = max_s, start

    def __call__(self, retry_state: RetryCallState) -> bool:
        return clock.monotonic() - self.start >= self.max_s


def _should_retry(p: RetryPolicy) -> Callable[[BaseException], bool]:
    """U08-28 step 3: retryable, not `CircuitOpen`, and any `retry_after` within the cap."""

    def check(e: BaseException) -> bool:
        if not isinstance(e, RetryableError) or isinstance(e, CircuitOpen):
            return False
        if isinstance(e, RateLimited) and e.retry_after is not None:
            return e.retry_after <= p.retry_after_cap_s  # over the cap: the job reschedules
        return True

    return check


class _Retry:
    """The tenacity wiring of one retried call (U08-28 steps 2 and 5)."""

    def __init__(
        self,
        p: RetryPolicy,
        key: str | None,
        family: ErrorFamily,
        tracer: TracerLike | None,
        target: str | None,
        start: float,
    ) -> None:
        self.p, self.key, self.tracer, self.start = p, key, tracer, start
        # The §4.4 `target`: `llm` for model policies, `tool` for `tool_store`, else the name.
        default = "llm" if family == "model" else "tool" if p.name == "tool_store" else p.name
        self.target = target or default

    def kwargs(self) -> dict[str, Any]:
        """The arguments shared by `Retrying` and `AsyncRetrying` (all but the sleeps)."""
        after = _StopAfterDelay(self.p.max_elapsed_s, self.start)
        return {
            "retry": retry_if_exception(_should_retry(self.p)),
            "stop": stop_after_attempt(self.p.attempts) | after,
            "wait": FullJitterRetryAfter(self.p, process_state().rng),
            "reraise": True,
        }

    def emit(self, retry_state: RetryCallState) -> None:
        """U08-28 step 5: the `retry` event and metric, before the sleep.

        Runs after the failed attempt returned, so after `run_write`'s rollback and never in
        an open transaction. The row is skipped (log line and metric kept) while the ops port
        is unbound or another `retry` event is being written on this thread.
        """
        outcome = retry_state.outcome
        exc = outcome.exception() if outcome is not None else None
        error_type = type(exc).__name__
        detail: dict[str, object] = {
            "target": self.target,
            "attempt": retry_state.attempt_number,
            "error_type": error_type,
            "wait_s": retry_state.upcoming_sleep,
            "policy": self.p.name,
            "breaker_key": self.key,
            "retry_after_s": exc.retry_after if isinstance(exc, RateLimited) else None,
        }
        labels = {"policy": self.p.name, "error_class": error_type}
        record_counter(RETRIES_METRIC, component="resilience", labels=labels)
        target = self.key or self.p.name
        if process_state().ops is None or getattr(_emitting, "active", False):
            _log.warning(
                "resilience.call.retry_scheduled", kind="retry", target=target, detail=detail
            )
            return
        _emitting.active = True
        try:
            record_event(
                "retry", component="resilience", target=target, detail=detail, tracer=self.tracer
            )
        finally:
            _emitting.active = False

    async def aemit(self, retry_state: RetryCallState) -> None:
        """`emit` off the event loop: its writes are synchronous SQLite (ENG §2.5)."""
        await asyncio.to_thread(self.emit, retry_state)


def _fail(key: str | None, err: HernessError) -> None:
    if key is not None:
        breaker(key).record_failure(err)


def _invoke[T](fn: Callable[[], T], key: str | None, family: ErrorFamily) -> T:
    """One attempt (U08-28 step 4): guard, call, record, classify a foreign exception."""
    if key is not None:
        guard(key)
    try:
        value = fn()
    except HernessError as err:
        _fail(key, err)
        raise
    except Exception as exc:
        mapped = classify(exc, family=family)
        _fail(key, mapped)
        raise mapped from exc
    if key is not None:
        breaker(key).record_success()
    return value


async def _ainvoke[T](fn: Callable[[], Awaitable[T]], key: str | None, family: ErrorFamily) -> T:
    """`_invoke` for a coroutine; breaker I/O leaves the loop unless the cache is hot."""
    if key is not None:
        await _breaker_io(key, functools.partial(guard, key))
    try:
        value = await fn()
    except HernessError as err:
        await asyncio.to_thread(_fail, key, err)
        raise
    except Exception as exc:
        mapped = classify(exc, family=family)
        await asyncio.to_thread(_fail, key, mapped)
        raise mapped from exc
    if key is not None:
        await _breaker_io(key, breaker(key).record_success)
    return value


async def _breaker_io(key: str, call: Callable[[], None]) -> None:
    """Run ``call`` on the loop when `breaker(key).hot()`, else through `asyncio.to_thread`."""
    if breaker(key).hot():
        call()
    else:
        await asyncio.to_thread(call)


def _replay[T](first: Exception, call: Callable[[], T]) -> Callable[[], T]:
    """Attempt 1 re-raises the hot-path failure; later attempts run ``call``."""
    pending = [first]

    def attempt() -> T:
        if pending:
            raise pending.pop()
        return call()

    return attempt


def _areplay[T](first: Exception, call: Callable[[], Awaitable[T]]) -> Callable[[], Awaitable[T]]:
    pending = [first]

    async def attempt() -> T:
        if pending:
            raise pending.pop()
        return await call()

    return attempt


def _run[T](
    p: RetryPolicy,
    fn: Callable[[], T],
    key: str | None,
    family: ErrorFamily,
    tracer: TracerLike | None,
    target: str | None,
) -> T:
    """Run ``fn`` under policy ``p`` (U08-28 algorithm)."""
    start = clock.monotonic()
    try:
        return _invoke(fn, key, family)
    except Exception as exc:  # noqa: BLE001 - handed to tenacity as attempt 1
        first = exc
    wiring = _Retry(p, key, family, tracer, target, start)
    retrier = tenacity.Retrying(
        sleep=process_state().sleep, before_sleep=wiring.emit, **wiring.kwargs()
    )
    return retrier(_replay(first, lambda: _invoke(fn, key, family)))


def _bind[T](fn: Callable[..., T], a: tuple[object, ...], kw: dict[str, object]) -> Callable[[], T]:
    return functools.partial(fn, *a, **kw) if a or kw else fn


def retry_call[T](
    name: PolicyName,
    fn: Callable[..., T],
    /,
    *a: object,
    breaker_key: str | None = None,
    **kw: object,
) -> T:
    """Run ``fn(*a, **kw)`` under the named policy, breaker and classification (U08-28).

    Returns ``fn``'s value or raises the last `HernessError` once the policy stops; foreign
    exceptions are classified, other `BaseException`s pass through unchanged.
    """
    return _run(policy(name), _bind(fn, a, kw), breaker_key, POLICY_FAMILY[name], None, None)


async def aretry_call[T](
    name: PolicyName,
    fn: Callable[..., Awaitable[T]],
    /,
    *a: object,
    breaker_key: str | None = None,
    **kw: object,
) -> T:
    """`retry_call` for a coroutine function (U08-29): `AsyncRetrying`, `process_state().asleep`."""
    p, family, call = policy(name), POLICY_FAMILY[name], _bind(fn, a, kw)
    start = clock.monotonic()
    try:
        return await _ainvoke(call, breaker_key, family)
    except Exception as exc:  # noqa: BLE001 - handed to tenacity as attempt 1
        first = exc
    wiring = _Retry(p, breaker_key, family, None, None, start)
    retrier = tenacity.AsyncRetrying(
        sleep=process_state().asleep, before_sleep=wiring.aemit, **wiring.kwargs()
    )
    return await retrier(_areplay(first, lambda: _ainvoke(call, breaker_key, family)))


def retrying[F: Callable[..., Any]](
    name: PolicyName, *, breaker_key: str | None = None
) -> Callable[[F], F]:
    """Decorator form of `retry_call` / `aretry_call` (U08-30), chosen per function kind."""

    def decorate(f: F) -> F:
        if inspect.iscoroutinefunction(f):

            @functools.wraps(f)
            async def async_wrapper(*a: object, **kw: object) -> object:
                return await aretry_call(name, f, *a, breaker_key=breaker_key, **kw)

            return cast("F", async_wrapper)

        @functools.wraps(f)
        def sync_wrapper(*a: object, **kw: object) -> object:
            return retry_call(name, f, *a, breaker_key=breaker_key, **kw)

        return cast("F", sync_wrapper)

    return decorate


def retry_page[T](fn: Callable[[], T], *, source: str) -> T:
    """One connector page fetch (U08-31): policy `source_http_page`, breaker ``source``."""
    return retry_call("source_http_page", fn, breaker_key=source)


def call_with_timeout[T](
    fn: Callable[[], T], timeout_s: float, *, on_timeout: Callable[[], None] | None = None
) -> T:
    """Run ``fn`` on a daemon thread; still running after ``timeout_s`` → `ModelUnavailable`
    (U08-32). The stuck thread is left to die with the job's child process (design 08 §5.2).
    """
    if not timeout_s > 0:
        msg = "call_with_timeout timeout_s must be > 0"
        raise ConfigError(msg)
    box: dict[str, object] = {}

    def runner() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller's thread
            box["error"] = exc

    thread = threading.Thread(target=runner, name="herness-timeout", daemon=True)
    thread.start()
    thread.join(timeout_s)
    if thread.is_alive():
        if on_timeout is not None:
            try:
                on_timeout()
            except Exception as exc:  # noqa: BLE001 - a hook failure never masks the timeout
                _log.warning("resilience.timeout.hook_failed", error_type=type(exc).__name__)
        msg = f"call timed out after {timeout_s}s"
        raise ModelUnavailable(msg)
    error = box.get("error")
    if isinstance(error, BaseException):
        raise error
    return cast("T", box["value"])
