"""Unit tests for herness.store.ops._shims (U02-38 retry, UT02-28).

Both names are re-exports of the real impl 08 hooks (T08-07 `retry_call`, T08-08
`fault_point`); `core` calls them through the module, so the module stays.
"""

from __future__ import annotations

import pytest

from herness.core import resilience
from herness.core.errors import FatalError, SchemaViolation, StoreBusy
from herness.core.resilience import ProcessState
from herness.core.resilience.policies import SQLITE_WRITE_DEFAULT, policy
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


def test_ut02_28_retry_call_is_resilience_retry_call() -> None:
    """UT02-28 `_shims.retry_call` is the real herness.core.resilience.retry_call (T08-07)."""
    assert _shims.retry_call is resilience.retry_call


def test_ut02_28_fault_point_is_resilience_hook() -> None:
    """UT02-28 `_shims.fault_point` is the real herness.core.resilience.fault_point (T08-08)."""
    assert _shims.fault_point is resilience.fault_point


def test_ut02_28_sqlite_write_without_config(reset_process_state: ProcessState) -> None:
    """UT02-28 with no config cached, `sqlite_write` is the design 08 §7 default (no load):
    StoreBusy is retried with full-jitter sleeps, at most 6 attempts."""
    assert policy("sqlite_write") is SQLITE_WRITE_DEFAULT
    sleeps: list[float] = []
    reset_process_state.sleep = sleeps.append
    fn = _Flaky(3)
    assert _shims.retry_call("sqlite_write", fn) == "ok"
    assert fn.calls == 4
    for attempt, delay in enumerate(sleeps, start=1):
        assert 0 <= delay <= min(5.0, 0.2 * 2 ** (attempt - 1))
    always = _Flaky(100)
    with pytest.raises(StoreBusy):
        _shims.retry_call("sqlite_write", always)
    assert always.calls == 6


@pytest.mark.parametrize(
    ("exc", "raised"),
    [
        (SchemaViolation("bad"), SchemaViolation),
        (ValueError("x"), FatalError),
        (KeyboardInterrupt(), KeyboardInterrupt),
    ],
)
def test_ut02_28_non_retryable_is_not_retried(
    reset_process_state: ProcessState, exc: BaseException, raised: type[BaseException]
) -> None:
    """UT02-28 non-retryable errors are raised after one call: a foreign exception is
    classified (U08-28), a BaseException subclass passes through unchanged."""
    fn = _Flaky(1, exc)
    with pytest.raises(raised):
        _shims.retry_call("sqlite_write", fn)
    assert fn.calls == 1
