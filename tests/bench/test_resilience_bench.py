"""Resilience benchmarks (impl 08 BT08-01; T08-07).

Run: pytest -m "integration and slow" tests/bench.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from tests.support.config_tree import write_full_config
from tests.support.fake_keyring import MemoryKeyring
from tests.support.ops_store import OpsStoreHandle

from herness.core import config as c
from herness.core.resilience import ProcessState, bind_ops_backend, breaker, retry_call
from herness.store.ops.resilience import SqliteResilienceBackend

pytestmark = [pytest.mark.integration, pytest.mark.slow]

CALLS = 100_000


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
