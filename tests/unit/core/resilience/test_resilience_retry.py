"""Tests for herness.core.resilience.retry: retry_call, aretry_call, retrying, retry_page and
call_with_timeout (impl 08 U08-28..U08-32; T08-07)."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from collections.abc import Callable
from typing import Any

import httpx
import pytest
import structlog
from tests.support.fake_clock import FakeClock
from tests.support.ops_store import OpsStoreHandle

import herness.core.resilience.retry as rmod
from herness.core import config as c
from herness.core import redact as r
from herness.core import resilience
from herness.core import time as clock
from herness.core.errors import (
    CircuitOpen,
    ConfigError,
    HernessError,
    ModelUnavailable,
    RateLimited,
    SourceUnavailable,
    StoreBusy,
)
from herness.core.resilience import ProcessState, bind_ops_backend, policy, process_state
from herness.core.resilience.ports import EventRow
from herness.store.ops import core
from herness.store.ops.core import read_all
from herness.store.ops.resilience import SqliteResilienceBackend

pytestmark = pytest.mark.unit

RETRIES = "herness_resilience_retries_total"
FIELDS = {"target", "attempt", "error_type", "wait_s", "policy", "breaker_key", "retry_after_s"}


@pytest.fixture
def cfg(herness_cfg: c.HernessConfig, reset_process_state: ProcessState) -> ProcessState:
    """The full test config and a fresh process state (no ops backend bound)."""
    del herness_cfg
    return reset_process_state


@pytest.fixture
def env(
    ops_db: OpsStoreHandle, herness_cfg: c.HernessConfig, test_redactor: r.Redactor
) -> ProcessState:
    """A migrated ops store bound as the backend, the full test config, a test redactor."""
    del ops_db, herness_cfg, test_redactor
    return process_state()


class _Flaky:
    """Raises the given errors in order, then returns "ok"."""

    def __init__(self, *errors: BaseException, tick: Callable[[], object] | None = None) -> None:
        self.errors = list(errors)
        self.calls = 0
        self.tick = tick

    def __call__(self, *a: object, **kw: object) -> str:
        self.calls += 1
        if self.tick is not None:
            self.tick()
        if self.errors:
            raise self.errors.pop(0)
        return "ok"


class _Tracer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    @property
    def run_id(self) -> str | None:
        return None

    @property
    def task_id(self) -> str | None:
        return None

    def emit(self, type: str, **fields: object) -> None:  # noqa: A002 - port name
        self.calls.append((type, fields))


def _retry_rows() -> list[dict[str, Any]]:
    rows = read_all("SELECT * FROM resilience_event WHERE kind = 'retry' ORDER BY ts")
    return [dict(row) | {"detail": json.loads(row["detail"])} for row in rows]


def _counted(policy_name: str, error_class: str) -> float:
    labels = (("error_class", error_class), ("policy", policy_name))
    return process_state().metric_buffer.counters.get((RETRIES, labels, "resilience"), 0.0)


def _unavailable(n: int) -> list[BaseException]:
    return [ModelUnavailable(f"down {i}") for i in range(n)]


# --- UT08-08 / UT08-09: Retry-After -----------------------------------------------------


def test_ut08_08_retry_after_honoured_within_cap(cfg: ProcessState, fake_clock: FakeClock) -> None:
    """UT08-08 llm_cloud, RateLimited(retry_after=7) then success: slept >= 7 s and <= 120 s."""
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        fake_clock.sleep(seconds)

    cfg.sleep = sleep
    start = fake_clock.now()
    fn = _Flaky(RateLimited("slow down", retry_after=7))
    with structlog.testing.capture_logs() as logs:
        assert resilience.retry_call("llm_cloud", fn) == "ok"
    assert fn.calls == 2
    assert len(slept) == 1
    assert 7 <= slept[0] <= 120
    assert (fake_clock.now() - start).total_seconds() == pytest.approx(slept[0])
    detail = next(e["detail"] for e in logs if e["event"] == "resilience.call.retry_scheduled")
    assert (detail["retry_after_s"], detail["wait_s"]) == (7, slept[0])


def test_ut08_09_retry_after_over_cap_raises_at_once(cfg: ProcessState) -> None:
    """UT08-09 source_http_page (cap 300): RateLimited(retry_after=301) raised after one call,
    no sleep."""
    slept: list[float] = []
    cfg.sleep = slept.append
    fn = _Flaky(RateLimited("later", retry_after=301))
    with pytest.raises(RateLimited) as info:
        resilience.retry_call("source_http_page", fn)
    assert (fn.calls, slept, info.value.retry_after) == (1, [], 301)


# --- UT08-20 .. UT08-23: retry_call ------------------------------------------------------


def test_ut08_20_retries_until_success_or_attempts(cfg: ProcessState) -> None:
    """UT08-20 llm_local: 3 ModelUnavailable then success → 4 calls and the value; 4 failures
    → the 4th error is raised."""
    fn = _Flaky(*_unavailable(3))
    assert resilience.retry_call("llm_local", fn, 1, key="v") == "ok"
    assert fn.calls == 4
    errors = _unavailable(4)
    always = _Flaky(*errors, ModelUnavailable("never reached"))
    with pytest.raises(ModelUnavailable) as info:
        resilience.retry_call("llm_local", always)
    assert always.calls == 4
    assert info.value is errors[3]


def test_ut08_21_stops_after_the_attempt_crossing_max_elapsed(
    cfg: ProcessState, fake_clock: FakeClock
) -> None:
    """UT08-21 fake clock, each attempt advances 400 s, llm_local (900 s): the third attempt
    (1 200 s) is the last."""
    fn = _Flaky(*_unavailable(10), tick=lambda: fake_clock.advance(400))
    with pytest.raises(ModelUnavailable):
        resilience.retry_call("llm_local", fn)
    assert fn.calls == 3


def test_ut08_21_elapsed_follows_the_herness_clock(
    cfg: ProcessState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT08-21 the elapsed budget reads `clock.monotonic` (not tenacity's time.monotonic)."""
    now = [0.0]
    monkeypatch.setattr(clock, "monotonic", lambda: now[0])

    def tick() -> None:
        now[0] += 400

    fn = _Flaky(*_unavailable(10), tick=tick)
    with pytest.raises(ModelUnavailable):
        resilience.retry_call("llm_local", fn)
    assert fn.calls == 3


def test_ut08_22_circuit_open_is_not_retried(cfg: ProcessState) -> None:
    """UT08-22 fn raises CircuitOpen: one call, raised."""
    fn = _Flaky(CircuitOpen("open", key="jira", retry_at=clock.now()))
    with pytest.raises(CircuitOpen):
        resilience.retry_call("source_http_page", fn)
    assert fn.calls == 1


def test_ut08_23_foreign_error_is_classified_and_retried(cfg: ProcessState) -> None:
    """UT08-23 httpx.ConnectError under source_http_page is retried as SourceUnavailable; the
    final error keeps the original as __cause__."""
    fn = _Flaky(*(httpx.ConnectError("refused") for _ in range(10)))
    with pytest.raises(SourceUnavailable) as info:
        resilience.retry_call("source_http_page", fn)
    assert fn.calls == 6
    assert isinstance(info.value.__cause__, httpx.ConnectError)


@pytest.mark.parametrize("exc", [KeyboardInterrupt(), SystemExit(3)])
def test_ut08_23_base_exceptions_pass_through(cfg: ProcessState, exc: BaseException) -> None:
    """UT08-23 BaseException subclasses that are not Exception pass through unchanged."""
    fn = _Flaky(exc)
    with pytest.raises(type(exc)) as info:
        resilience.retry_call("tool_store", fn)
    assert (fn.calls, info.value) == (1, exc)


def test_ut08_23_unknown_policy(cfg: ProcessState) -> None:
    """UT08-23 an unknown policy name raises ConfigError before any call."""
    fn = _Flaky()
    with pytest.raises(ConfigError, match="unknown retry policy"):
        resilience.retry_call("nope", fn)  # type: ignore[arg-type]
    assert fn.calls == 0


# --- UT08-24: retrying and aretry_call --------------------------------------------------


def test_ut08_24_decorated_sync_and_async(cfg: ProcessState) -> None:
    """UT08-24 decorated sync and async functions are both retried; functools.wraps is kept;
    the async one sleeps through `asleep`, the sync one through `sleep`."""
    sleeps: list[float] = []
    asleeps: list[float] = []

    async def asleep(seconds: float) -> None:
        asleeps.append(seconds)

    cfg.sleep, cfg.asleep = sleeps.append, asleep
    flaky = _Flaky(*_unavailable(2))

    @resilience.retrying("llm_local")
    def sync_fn(x: int) -> str:
        """Sync doc."""
        return flaky(x)

    aflaky = _Flaky(*_unavailable(2))

    @resilience.retrying("llm_local")
    async def async_fn(x: int) -> str:
        """Async doc."""
        return aflaky(x)

    assert sync_fn(1) == "ok"
    assert (flaky.calls, len(sleeps), asleeps) == (3, 2, [])
    sleeps.clear()
    assert asyncio.run(async_fn(1)) == "ok"
    assert (aflaky.calls, len(asleeps), sleeps) == (3, 2, [])
    assert (sync_fn.__name__, sync_fn.__doc__) == ("sync_fn", "Sync doc.")
    assert (async_fn.__name__, async_fn.__doc__) == ("async_fn", "Async doc.")
    assert asyncio.iscoroutinefunction(async_fn)


def test_ut08_24_async_classifies_and_stops(cfg: ProcessState) -> None:
    """UT08-24 aretry_call classifies a foreign error, keeps __cause__ and re-raises the last
    error after the policy's attempts; a BaseException passes through."""

    async def boom() -> None:
        msg = "refused"
        raise httpx.ConnectError(msg)

    with pytest.raises(SourceUnavailable) as info:
        asyncio.run(resilience.aretry_call("source_http_page", boom))
    assert isinstance(info.value.__cause__, httpx.ConnectError)

    async def interrupted() -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        asyncio.run(resilience.aretry_call("tool_store", interrupted))


def test_ut08_24_async_breaker_hot_path_and_writes(env: ProcessState) -> None:
    """UT08-24 with a breaker key: failures are recorded (off the loop), success closes the
    counter again and a clean cached breaker is `hot` (no I/O on the loop)."""
    aflaky = _Flaky(*[SourceUnavailable(f"down {i}") for i in range(2)])

    async def fetch() -> str:
        return aflaky()

    assert not resilience.breaker("jira").hot()  # nothing cached yet
    assert asyncio.run(resilience.aretry_call("source_http_page", fetch, breaker_key="jira")) == (
        "ok"
    )
    assert aflaky.calls == 3
    assert resilience.breaker("jira").hot()
    assert asyncio.run(resilience.aretry_call("source_http_page", fetch, breaker_key="jira")) == (
        "ok"
    )
    rows = _retry_rows()
    assert [(row["target"], row["detail"]["attempt"]) for row in rows] == [("jira", 1), ("jira", 2)]


# --- UT08-25: retry events and metric ---------------------------------------------------


def test_ut08_25_retry_rows_metric_and_trace(env: ProcessState) -> None:
    """UT08-25 one retry on the tracer path (`_run` with a tracer and target, as ModelChain
    will call it) and one on the plain `retry_call` path: one `retry` row each with the §4.4
    fields, the metric counted twice, a trace event only on the tracer path."""
    tracer = _Tracer()
    fn = _Flaky(ModelUnavailable("down"))
    value = rmod._run(policy("llm_local"), fn, "model:local-30b", "model", tracer, "llm")
    assert value == "ok"
    plain = _Flaky(StoreBusy("busy"))
    assert resilience.retry_call("tool_store", plain) == "ok"
    rows = _retry_rows()
    assert [row["target"] for row in rows] == ["model:local-30b", "tool_store"]
    assert all(set(row["detail"]) == FIELDS for row in rows)
    first, second = (row["detail"] for row in rows)
    assert first | {"wait_s": None} == {
        "target": "llm",
        "attempt": 1,
        "error_type": "ModelUnavailable",
        "wait_s": None,
        "policy": "llm_local",
        "breaker_key": "model:local-30b",
        "retry_after_s": None,
    }
    assert 0 <= first["wait_s"] <= 2
    assert (second["target"], second["breaker_key"], second["retry_after_s"]) == (
        "tool",
        None,
        None,
    )
    assert _counted("llm_local", "ModelUnavailable") == 1
    assert _counted("tool_store", "StoreBusy") == 1
    assert [(kind, fields["policy"]) for kind, fields in tracer.calls] == [("retry", "llm_local")]


def test_ut08_25_unbound_ops_skips_row_keeps_log_and_metric(cfg: ProcessState) -> None:
    """UT08-25 (ruling) with no ops backend bound the row is skipped, never raised: the
    `resilience.call.retry_scheduled` WARNING and the metric are still emitted."""
    fn = _Flaky(StoreBusy("busy"))
    with structlog.testing.capture_logs() as logs:
        assert resilience.retry_call("sqlite_write", fn) == "ok"
    scheduled = [e for e in logs if e["event"] == "resilience.call.retry_scheduled"]
    assert [(e["log_level"], e["target"]) for e in scheduled] == [("warning", "sqlite_write")]
    assert set(scheduled[0]["detail"]) == FIELDS
    assert _counted("sqlite_write", "StoreBusy") == 1


class _RetryingBackend(SqliteResilienceBackend):
    """Its event insert itself hits one sqlite_write retry (event → write → retry → event)."""

    def insert_event(self, row: EventRow) -> None:
        resilience.retry_call("sqlite_write", _Flaky(StoreBusy("nested busy")))
        super().insert_event(row)


def test_ut08_25_nested_retry_while_writing_an_event_skips_its_row(env: ProcessState) -> None:
    """UT08-25 (ruling) a retry raised while a `retry` event is being written logs and counts
    but writes no row of its own: no unbounded recursion."""
    bind_ops_backend(_RetryingBackend())
    with structlog.testing.capture_logs() as logs:
        assert resilience.retry_call("tool_store", _Flaky(StoreBusy("busy"))) == "ok"
    assert [row["detail"]["policy"] for row in _retry_rows()] == ["tool_store"]
    nested = [e for e in logs if e["event"] == "resilience.call.retry_scheduled"]
    assert [e["target"] for e in nested] == ["sqlite_write", "tool_store"]
    assert _counted("sqlite_write", "StoreBusy") == 1


class _BusyFlushBackend(SqliteResilienceBackend):
    """Its metric flush hits one sqlite_write retry (counter → flush → write → retry)."""

    def insert_metric_samples(self, rows: Any) -> int:
        resilience.retry_call("sqlite_write", _Flaky(StoreBusy("flush busy")))
        return super().insert_metric_samples(rows)


def test_ut08_25_retry_inside_a_triggered_metric_flush_writes_no_row(env: ProcessState) -> None:
    """UT08-25 (review m1) a sqlite_write retry inside the metric flush that the retry counter
    triggers is guarded too: it logs and counts but writes no `retry` row of its own."""
    bind_ops_backend(_BusyFlushBackend())
    env.metric_buffer.last_flush = clock.monotonic() - 60  # the counter flushes at once
    with structlog.testing.capture_logs() as logs:
        assert resilience.retry_call("tool_store", _Flaky(StoreBusy("busy"))) == "ok"
    assert [row["target"] for row in _retry_rows()] == ["tool_store"]
    nested = [e for e in logs if e["event"] == "resilience.call.retry_scheduled"]
    assert [e["target"] for e in nested] == ["sqlite_write", "tool_store"]


def test_ut08_24_async_failure_without_breaker_key_stays_on_the_loop(
    cfg: ProcessState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT08-24 (review m2) with no breaker key a failed async attempt needs no breaker I/O:
    no `asyncio.to_thread` hop (the retry event still leaves the loop)."""
    hops: list[str] = []
    real = asyncio.to_thread

    async def spy(fn: Callable[..., Any], /, *a: Any, **kw: Any) -> Any:
        hops.append(getattr(fn, "__name__", type(fn).__name__))
        return await real(fn, *a, **kw)

    monkeypatch.setattr(rmod.asyncio, "to_thread", spy)
    aflaky = _Flaky(ModelUnavailable("down"), httpx.ConnectError("refused"))

    async def call() -> str:
        return aflaky()

    assert asyncio.run(resilience.aretry_call("llm_local", call)) == "ok"
    assert aflaky.calls == 3
    assert "_fail" not in hops
    assert hops == ["emit", "emit"]


def test_ut08_25_sqlite_write_retry_through_run_write(env: ProcessState) -> None:
    """UT08-25 (ruling) a sqlite_write retry inside real run_write records its retry row and
    flushes its metric after the rollback: no nested-write ConfigError."""
    env.metric_buffer.last_flush = clock.monotonic() - 60  # the metric call flushes at once
    busy = [StoreBusy("ops store busy in test_write")]

    def write(conn: sqlite3.Connection) -> str:
        assert conn.in_transaction
        if busy:
            raise busy.pop()
        return "written"

    assert core.run_write(write, op="test_write") == "written"
    assert [(r["target"], r["detail"]["policy"]) for r in _retry_rows()] == [
        ("sqlite_write", "sqlite_write")
    ]
    samples = read_all("SELECT labels FROM metric_sample WHERE name = ?", (RETRIES,))
    assert [json.loads(s["labels"]) for s in samples] == [
        {"error_class": "StoreBusy", "policy": "sqlite_write"}
    ]


# --- UT08-26 / UT08-27: breaker wiring, retry_page --------------------------------------


class _SpyBreaker:
    def __init__(self) -> None:
        self.failures: list[HernessError] = []
        self.successes = 0

    def record_failure(self, err: HernessError) -> None:
        self.failures.append(err)

    def record_success(self) -> None:
        self.successes += 1


def test_ut08_26_breaker_guard_and_records(
    cfg: ProcessState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT08-26 breaker key, spy breaker, 2 failures + success: guard 3 times, record_failure twice,
    record_success once; a foreign error is recorded as its classified form."""
    spy = _SpyBreaker()
    guards: list[str] = []
    monkeypatch.setattr(rmod, "guard", guards.append)
    monkeypatch.setattr(rmod, "breaker", lambda _key: spy)
    fn = _Flaky(SourceUnavailable("down"), httpx.ReadTimeout("slow"))
    assert resilience.retry_call("source_http_page", fn, breaker_key="jira") == "ok"
    assert guards == ["jira"] * 3
    assert [type(e) for e in spy.failures] == [SourceUnavailable, SourceUnavailable]
    assert spy.successes == 1


def test_ut08_27_retry_page_uses_source_policy_and_breaker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT08-27 retry_page(fn, source="jira") → retry_call("source_http_page", fn,
    breaker_key="jira")."""
    seen: list[tuple[object, ...]] = []

    def spy(name: str, fn: Callable[[], str], *, breaker_key: str | None = None) -> str:
        seen.append((name, fn, breaker_key))
        return fn()

    monkeypatch.setattr(rmod, "retry_call", spy)

    def page() -> str:
        return "page"

    assert resilience.retry_page(page, source="jira") == "page"
    assert seen == [("source_http_page", page, "jira")]


# --- UT08-28: call_with_timeout ---------------------------------------------------------


def test_ut08_28_call_with_timeout() -> None:
    """UT08-28 value returned; a 2 s call with timeout 0.1 s → ModelUnavailable and on_timeout
    called once; KeyError re-raised unchanged."""
    assert resilience.call_with_timeout(lambda: 42, 5) == 42
    release = threading.Event()
    hooks: list[int] = []
    try:
        with pytest.raises(ModelUnavailable, match=r"call timed out after 0\.1s"):
            resilience.call_with_timeout(
                lambda: release.wait(2), 0.1, on_timeout=lambda: hooks.append(1)
            )
    finally:
        release.set()
    assert hooks == [1]
    missing = KeyError("k")

    def lookup() -> None:
        raise missing

    with pytest.raises(KeyError) as info:
        resilience.call_with_timeout(lookup, 5)
    assert info.value is missing


def test_ut08_28_hook_failure_is_logged_and_bad_timeout_rejected() -> None:
    """UT08-28 an on_timeout hook that raises is logged (WARNING resilience.timeout.hook_failed)
    and the timeout still raised; timeout_s <= 0 → ConfigError."""
    release = threading.Event()

    def hook() -> None:
        msg = "hook broke"
        raise RuntimeError(msg)

    try:
        with structlog.testing.capture_logs() as logs, pytest.raises(ModelUnavailable):
            resilience.call_with_timeout(lambda: release.wait(2), 0.05, on_timeout=hook)
    finally:
        release.set()
    failed = [e for e in logs if e["event"] == "resilience.timeout.hook_failed"]
    assert [(e["log_level"], e["error_type"]) for e in failed] == [("warning", "RuntimeError")]
    for bad in (0, -1.0):
        with pytest.raises(ConfigError):
            resilience.call_with_timeout(lambda: None, bad)
