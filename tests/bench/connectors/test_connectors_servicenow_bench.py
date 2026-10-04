"""ServiceNow replay throughput and connector memory (impl 01 BT01-01, BT01-03; T01-25).

Run: pytest -m "integration and slow" tests/bench/connectors/test_connectors_servicenow_bench.py.
The dataset is generated page by page (``tests.bench.connectors._sn_load``; T11-15's
``api_pages`` writer is not on the tree) and served through the ``httpx2`` mock pool, so the
whole production path runs: egress client, OAuth, ``ServiceNowConnector``, ``SyncRunner`` with
its slices, the deletion filter and the real ``LakeWriter`` on a temp lake. ``HERNESS_BT01_ROWS``
and ``HERNESS_BT01_KEYS`` shrink the dataset for a dry run; the thresholds never change.
"""

from __future__ import annotations

import datetime
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import psutil
import pytest
from tests.bench.connectors._sn_load import (
    SyntheticServiceNow,
    keys_wanted,
    metadata_batch,
    rows_wanted,
)
from tests.support.fake_keyring import MemoryKeyring
from tests.support.ops_store import OpsStoreHandle
from tests.support.sn_cassettes import T0
from tests.unit.connectors import _servicenow_env as sn
from tests.unit.connectors._http_data import bind_resilience

from herness.connectors.runner import SyncRunner
from herness.core import config as c
from herness.core.resilience import ProcessState
from herness.store.lake import LakeWriter

pytestmark = [pytest.mark.integration, pytest.mark.slow]

FLOOR_ROWS_PER_S = 5_000
RSS_LIMIT = int(1.5 * 2**30)
MISSING_KEYS = 5_000  # keys the source no longer lists: the reconcile tombstones them
_CHUNK = 250_000


@dataclass
class Bench:
    """A runner over the synthetic ServiceNow of ``host``."""

    runner: SyncRunner
    host: SyntheticServiceNow
    data_root: Path


@pytest.fixture
def bench_env(
    ops_store: OpsStoreHandle,
    reset_process_state: ProcessState,
    fake_keyring: MemoryKeyring,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Bench]:
    """Config with 10,000-row pages and batches, the ops store bound, the mock pool installed."""
    del fake_keyring
    reset_process_state.sleep = lambda _s: None
    sn.store_secret()
    text = sn.sources_yaml().replace("page_size: 100", "page_size: 10000")
    text = text.replace("batch_rows: 1000", "batch_rows: 10000")
    settings = sn.load(tmp_path, text)
    bind_resilience()
    total = max(keys_wanted(), rows_wanted() + MISSING_KEYS)
    host = SyntheticServiceNow(rows_wanted(), keys=total - MISSING_KEYS)
    sn.install(monkeypatch, sn.SnHost(host))
    clock = sn.Clock(T0 + datetime.timedelta(days=30))  # every generated record is settled
    yield Bench(sn.runner(settings, clock, ops_store.data_root), host, ops_store.data_root)
    c.reset_config()


class PeakRss:
    """Samples this process's resident set every 200 ms in a thread; ``peak`` in bytes."""

    def __init__(self) -> None:
        self._proc = psutil.Process()
        self.peak = self._proc.memory_info().rss
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.wait(0.2):
            self.peak = max(self.peak, self._proc.memory_info().rss)

    def __enter__(self) -> PeakRss:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join()
        self.peak = max(self.peak, self._proc.memory_info().rss)


def _ingest(bench: Bench) -> tuple[int, float]:
    """The first run (backfill to now): committed rows and the seconds it took."""
    start = time.perf_counter()
    result = bench.runner.run_incremental("incident")
    return result.rows, time.perf_counter() - start


def test_bt01_01_servicenow_replay_throughput(bench_env: Bench) -> None:
    """BT01-01 a ServiceNow replay of 1M incidents (default) through the full connector path
    commits at least 5,000 rows/s."""
    rows, seconds = _ingest(bench_env)
    assert rows == rows_wanted()
    assert bench_env.host.served_rows >= rows
    rate = rows / seconds
    sys.stderr.write(
        f"BT01-01 rows_per_s {rate:,.0f} (target >= {FLOOR_ROWS_PER_S:,}) {seconds:.1f}s\n"
    )
    assert rate >= FLOOR_ROWS_PER_S, f"{rate:,.0f} committed rows/s < {FLOOR_ROWS_PER_S:,}"


def _write_remaining_keys(root: Path, lo: int, hi: int) -> None:
    """Commit live lake rows for keys ``lo ..< hi`` (the part of a 5M-key lake the replay did
    not ingest), in chunks."""
    with LakeWriter("servicenow", "incident", root=root / "raw") as writer:
        for start in range(lo, hi, _CHUNK):
            writer.write(metadata_batch(start, min(hi, start + _CHUNK)))
        writer.commit()


def test_bt01_03_connector_rss_stays_under_1_5_gb(bench_env: Bench) -> None:
    """BT01-03 the BT01-01 run plus a reconcile of 5M keys (key listing through the same mock
    pool, lake of 5M live keys, 5,000 of them no longer listed): peak RSS < 1.5 GB."""
    total = bench_env.host.keys + MISSING_KEYS
    with PeakRss() as rss:
        rows, _seconds = _ingest(bench_env)
        assert rows == rows_wanted()
        _write_remaining_keys(bench_env.data_root, rows, total)
        result = bench_env.runner.run_reconcile("incident")
    sys.stderr.write(
        f"BT01-03 peak_rss {rss.peak / 2**20:,.0f} MiB (limit {RSS_LIMIT / 2**20:,.0f})\n"
    )
    assert (result.mode, result.tombstones) == ("reconcile", MISSING_KEYS)
    assert rss.peak < RSS_LIMIT, f"peak RSS {rss.peak / 2**20:,.0f} MiB >= 1.5 GB"
