"""Resilience benchmarks (impl 08 BT08-01; T08-07).

Run: pytest -m "integration and slow" tests/bench.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import IO

import psutil
import pytest
from tests.support.config_tree import write_full_config
from tests.support.fake_keyring import MemoryKeyring
from tests.support.ops_store import OpsStoreHandle

from herness.core import config as c
from herness.core.errors import SourceUnavailable
from herness.core.resilience import ProcessState, bind_ops_backend, breaker, retry_call
from herness.store.ops.resilience import SqliteResilienceBackend

pytestmark = [pytest.mark.integration, pytest.mark.slow]

CALLS = 100_000
CACHE_AGE_S = 1.0  # BT08-05: B's cached closed row is this old when A opens (TTL is 5 s)


@pytest.fixture
def bound(
    ops_store: OpsStoreHandle,
    reset_process_state: ProcessState,
    fake_keyring: MemoryKeyring,
    tmp_path: Path,
) -> Iterator[None]:
    """A migrated ops store bound as the backend and the full test config."""
    del ops_store, reset_process_state, fake_keyring
    c.init_config("local", config_dir=write_full_config(tmp_path / "cfg"), env={})
    bind_ops_backend(SqliteResilienceBackend())
    yield
    c.reset_config()


def _noop() -> None:
    return None


def _mean_s(call: Callable[[], object]) -> float:
    start = time.perf_counter()
    for _ in range(CALLS):
        call()
    return (time.perf_counter() - start) / CALLS


def test_bt08_01_retry_wrapper_overhead(bound: None) -> None:
    """BT08-01 retry_call("tool_store", noop, breaker_key="tool"), no retry, breaker cached:
    mean overhead over the bare noop < 50 µs across 100 000 calls after warm-up."""
    del bound

    def wrapped() -> None:
        retry_call("tool_store", _noop, breaker_key="tool")

    for _ in range(1_000):  # warm-up: policy cache, breaker row cached as closed
        wrapped()
    assert breaker("tool").hot()
    overhead = _mean_s(wrapped) - _mean_s(_noop)
    sys.stderr.write(f"BT08-01 mean overhead={overhead * 1e6:.2f} us\n")
    assert overhead < 50e-6


_PEER = """
import sys, time
from pathlib import Path
from herness.core import config as c
from herness.core.resilience import bind_ops_backend, breaker
from herness.store.ops import reset_connections
from herness.store.ops.resilience import SqliteResilienceBackend

reset_connections(path=Path(sys.argv[2]))
c.init_config("local", config_dir=Path(sys.argv[1]), env={})
bind_ops_backend(SqliteResilienceBackend())
b = breaker("tool")
b.state()  # warm the per-process cache as closed
print("ready", flush=True)
start = time.monotonic()
while b.state() != "open":
    if time.monotonic() - start > 30:
        sys.exit(2)
    time.sleep(0.05)
print("seen", flush=True)
"""


def _await_line(stream: IO[str], token: str) -> bool:
    """Read the peer's stdout (it also carries log lines) until a line equals ``token``."""
    return any(line.strip() == token for line in stream)


def _kill_tree(proc: subprocess.Popen[str]) -> None:
    """Kill the peer and its descendants (the venv python.exe launcher spawns the real one)."""
    try:
        family = [*psutil.Process(proc.pid).children(recursive=True), psutil.Process(proc.pid)]
    except psutil.NoSuchProcess:
        family = []
    for member in family:
        with contextlib.suppress(psutil.NoSuchProcess):
            member.kill()
    proc.wait(timeout=30)


def test_bt08_05_breaker_open_seen_by_other_process(
    bound: None, ops_store: OpsStoreHandle, tmp_path: Path
) -> None:
    """BT08-05 two processes share the ops store; open the breaker in A (this process), poll
    `state()` in B (a second Python process) until open: seen within 5 s."""
    del bound
    argv = [sys.executable, "-c", _PEER, str(tmp_path / "cfg" / "config"), str(ops_store.db_path)]
    peer = subprocess.Popen(argv, stdout=subprocess.PIPE, text=True)  # noqa: S603
    try:
        assert peer.stdout is not None
        assert _await_line(peer.stdout, "ready")
        time.sleep(CACHE_AGE_S)  # B is mid-way through a cache lifetime, as a running process is
        opened = time.monotonic()
        breaker("tool").force_open(SourceUnavailable("bench"))
        found = _await_line(peer.stdout, "seen")
        seen = time.monotonic() - opened
        peer.wait(timeout=30)
    finally:
        _kill_tree(peer)
    sys.stderr.write(f"BT08-05 open seen by peer after {seen:.3f} s\n")
    assert found
    assert seen <= 5.0
