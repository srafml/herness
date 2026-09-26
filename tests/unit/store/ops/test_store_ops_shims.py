"""Unit tests for the interim resilience shims of herness.store.ops (U02-38 retry, UT02-28).

`retry_call` and `fault_point` stand in for T08-07 and T08-08; these tests go with them.
"""

from __future__ import annotations

import pytest

from herness.core.errors import ConfigError, SchemaViolation, StoreBusy
from herness.core.resilience import ProcessState
from herness.store.ops import _shims

pytestmark = pytest.mark.unit


class _Flaky:
    def __init__(self, failures: int, exc: BaseException | None = None) -> None:
        self.failures = failures
        self.calls = 0
        self.exc = exc

    def __call__(self) -> str:
        self.calls += 1
        if self.calls <= self.failures:
            raise self.exc if self.exc is not None else StoreBusy("busy")
        return "ok"


def test_ut02_28_retry_until_success(reset_process_state: ProcessState) -> None:
    """UT02-28 StoreBusy is retried with full-jitter sleeps bounded by the policy."""
    sleeps: list[float] = []
    reset_process_state.sleep = sleeps.append
    fn = _Flaky(3)
    assert _shims.retry_call("sqlite_write", fn) == "ok"
    assert fn.calls == 4
    assert len(sleeps) == 3
    for attempt, delay in enumerate(sleeps, start=1):
        assert 0 <= delay <= min(5.0, 0.2 * 2 ** (attempt - 1))


def test_ut02_28_retry_stops_after_six_attempts(reset_process_state: ProcessState) -> None:
    """UT02-28 the sqlite_write policy makes at most 6 attempts, then re-raises StoreBusy."""
    fn = _Flaky(100)
    with pytest.raises(StoreBusy):
        _shims.retry_call("sqlite_write", fn)
    assert fn.calls == 6


def test_ut02_28_retry_stops_after_max_elapsed(
    reset_process_state: ProcessState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT02-28 no retry is scheduled once 30 s have elapsed."""
    ticks = iter([0.0, 31.0])
    monkeypatch.setattr(_shims.clock, "monotonic", lambda: next(ticks))
    fn = _Flaky(100)
    with pytest.raises(StoreBusy):
        _shims.retry_call("sqlite_write", fn)
    assert fn.calls == 1


@pytest.mark.parametrize("exc", [SchemaViolation("bad"), ValueError("x"), KeyboardInterrupt()])
def test_ut02_28_non_retryable_passes_through(
    reset_process_state: ProcessState, exc: BaseException
) -> None:
    """UT02-28 non-retryable errors and BaseException subclasses are not retried."""
    fn = _Flaky(1, exc)
    with pytest.raises(type(exc)):
        _shims.retry_call("sqlite_write", fn)
    assert fn.calls == 1


def test_ut02_28_unknown_policy(reset_process_state: ProcessState) -> None:
    """UT02-28 the shim knows only the sqlite_write policy."""
    with pytest.raises(ConfigError, match="unknown retry policy"):
        _shims.retry_call("llm_default", lambda: None)


def test_ut02_28_fault_point_is_noop() -> None:
    """UT02-28 the fault_point shim loads no plan and returns None."""
    assert _shims.fault_point("sqlite.write", kind="insert") is None
