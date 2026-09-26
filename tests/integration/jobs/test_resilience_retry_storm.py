"""Retry storm against a down endpoint (impl 08 ST08-05; TH08-05; T08-07): 20 concurrent
callers for 10 simulated minutes share one breaker over a real migrated ops store."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from tests.support.config_tree import write_full_config
from tests.support.fake_clock import FakeClock
from tests.support.fake_keyring import MemoryKeyring
from tests.support.ops_store import OpsStoreHandle

from herness.core import config as c
from herness.core import redact as r
from herness.core.errors import CircuitOpen, SourceUnavailable
from herness.core.redact_directory import NameDirectory
from herness.core.resilience import ProcessState, bind_ops_backend, breaker, retry_call
from herness.core.settings import RedactionConfig
from herness.store.ops.resilience import SqliteResilienceBackend

pytestmark = pytest.mark.integration

CALLERS = 20
TICK_S = 5
DURATION_S = 600
THRESHOLD = 8  # breakers.source.failure_threshold (design 08 §7)
COOLDOWN_S = 300  # breakers.source.cooldown_s; the next cooldown doubles to 600 s


@pytest.fixture
def storm(
    ops_store: OpsStoreHandle,
    reset_process_state: ProcessState,
    fake_keyring: MemoryKeyring,
    fake_clock: FakeClock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[FakeClock]:
    """Bound backend, full config, a test redactor, no-op sleeps and a fake clock."""
    del ops_store, reset_process_state, fake_keyring
    c.init_config("local", config_dir=write_full_config(tmp_path / "cfg"), env={})
    directory = NameDirectory.from_files(None, (), None)
    redactor = r.Redactor(RedactionConfig(directory_file=None), bytes(range(32)), directory)
    monkeypatch.setattr(r._State, "redactor", redactor)
    bind_ops_backend(SqliteResilienceBackend())
    yield fake_clock
    c.reset_config()


class _DownEndpoint:
    """Always answers HTTP 503; records the fake second of every call."""

    def __init__(self, fake_clock: FakeClock) -> None:
        self.clock = fake_clock
        self.start = fake_clock.now()
        self.calls: list[float] = []
        self.lock = threading.Lock()

    def __call__(self) -> None:
        with self.lock:
            self.calls.append((self.clock.now() - self.start).total_seconds())
        request = httpx.Request("GET", "https://jira.example.test/rest/api/2/search")
        response = httpx.Response(503, request=request)
        msg = "503 Service Unavailable"
        raise httpx.HTTPStatusError(msg, request=request, response=response)


def _caller(endpoint: _DownEndpoint) -> str:
    try:
        retry_call("source_http_page", endpoint, breaker_key="jira")
    except CircuitOpen:
        return "open"
    except SourceUnavailable:
        return "failed"
    return "ok"  # pragma: no cover - the endpoint never succeeds


def test_st08_05_retry_storm_is_bounded_by_the_breaker(storm: FakeClock) -> None:
    """ST08-05 endpoint always 503, 20 concurrent callers for 10 min (fake clock): calls stop
    after the failure threshold, no caller reaches the endpoint while the breaker is open,
    and exactly one probe call runs per cooldown."""
    endpoint = _DownEndpoint(storm)
    outcomes: list[str] = []
    with ThreadPoolExecutor(max_workers=CALLERS) as pool:
        for _ in range(DURATION_S // TICK_S):
            outcomes += pool.map(lambda _i: _caller(endpoint), range(CALLERS))
            storm.advance(TICK_S)
    first = [t for t in endpoint.calls if t == 0]
    later = [t for t in endpoint.calls if t > 0]
    assert THRESHOLD <= len(first) <= THRESHOLD + CALLERS  # in-flight callers finish once
    assert later == [COOLDOWN_S]  # one probe at 300 s; the next cooldown (600 s) is past 10 min
    assert outcomes.count("ok") == 0
    assert outcomes.count("open") >= len(outcomes) - len(endpoint.calls)
    b = breaker("jira")
    assert b.state() == "open"
    row = SqliteResilienceBackend().health_get("jira")
    assert row is not None
    assert row.trips == 2
