"""Chat policy benchmark (impl 08 BT08-09; T08-19): `chat_policy(now)` p95 < 5 ms warm.

Warm caches over the real stack: the full test config, a migrated ops store bound as the
resilience and jobs backends, the process-wide `WorkerGpuState` against the `fake_gpu` stub
health servers, and real `model:` breakers. Run:
pytest -m "integration and slow" tests/bench/test_jobs_chat_policy_bench.py.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.support.config_tree import write_full_config
from tests.support.fake_gpu import FakeGpu
from tests.support.fake_keyring import MemoryKeyring
from tests.support.ops_store import OpsStoreHandle

from herness.core import config as c
from herness.core import redact as r
from herness.core import time as clock
from herness.core.jobs import gpu
from herness.core.jobs.chat_policy import chat_policy
from herness.core.jobs.ports import WorkerRow, bind_jobs_backend
from herness.core.redact_directory import NameDirectory
from herness.core.resilience import ProcessState, bind_chain_registry, bind_ops_backend
from herness.core.settings import RedactionConfig
from herness.store.ops.jobs import SqliteJobsBackend
from herness.store.ops.resilience import SqliteResilienceBackend

pytestmark = [pytest.mark.integration, pytest.mark.slow]

CALLS = 2_000
VLLM_KEY = "vllm-" + "stub-bearer-" + "value-2"  # built at runtime (detect-secrets)
TUESDAY_10 = datetime(2026, 1, 6, 10, tzinfo=UTC)
TUESDAY_22 = datetime(2026, 1, 6, 22, tzinfo=UTC)
CHAINS: dict[str, tuple[str, ...]] = {
    "chat": ("local-30b", "claude-opus"),
    "chat_off_hours": ("local-small-cpu",),
}


@dataclass(frozen=True)
class Client:
    off_network: bool


class Chains:
    """The shipped `models.yaml` chat chains (spec 05 default clients)."""

    def chain_for(self, model_role: str, depth: str) -> list[str]:
        del depth
        return list(CHAINS.get(model_role, ()))

    def config(self, name: str) -> Client:
        return Client(off_network=name.startswith("claude"))


@pytest.fixture
def jobs(
    fake_gpu: FakeGpu,
    ops_store: OpsStoreHandle,
    fake_keyring: MemoryKeyring,
    reset_process_state: ProcessState,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[SqliteJobsBackend]:
    """Config, stub-pointed GPU settings, bound backends and chains, a fresh `gpu_state`."""
    del reset_process_state
    config_dir = write_full_config(tmp_path / "cfgroot")
    c.init_config(
        "local", config_dir=config_dir, env={"HERNESS_PATHS__DATA": str(ops_store.data_root)}
    )
    fake_keyring.store[("herness", "vllm.api_key")] = VLLM_KEY
    directory = NameDirectory.from_files(None, (), None)
    redactor = r.Redactor(RedactionConfig(directory_file=None), bytes(range(32)), directory)
    monkeypatch.setattr(r._State, "redactor", redactor)
    monkeypatch.setattr(gpu, "_gpu", lambda: fake_gpu.gpu)
    monkeypatch.setattr(gpu._Holder, "reader", None)
    backend = SqliteJobsBackend()
    bind_ops_backend(SqliteResilienceBackend())
    bind_jobs_backend(backend)
    bind_chain_registry(Chains())
    yield backend
    c.reset_config()


def _worker(loaded: str) -> WorkerRow:
    now = clock.now()
    return WorkerRow.model_validate(
        {
            "worker_id": "w1", "host": "h", "pid": 1, "gpu_slot": 1, "cpu_slots": 2,
            "gpu_class_loaded": loaded, "status": "running", "started_at": now,
            "heartbeat_at": now, "version": "t",
        }
    )  # fmt: skip


def _p95(now: datetime) -> float:
    chat_policy(now)  # warm: worker row, vllm health and breaker rows now cached
    durations: list[float] = []
    for _ in range(CALLS):
        start = time.perf_counter()
        chat_policy(now)
        durations.append(time.perf_counter() - start)
    durations.sort()
    return durations[int(0.95 * (len(durations) - 1))]


def test_bt08_09_chat_policy_live_p95_under_5_ms(jobs: SqliteJobsBackend) -> None:
    """BT08-09 warm caches, stub health server: `live` path p95 < 5 ms."""
    jobs.upsert_worker(_worker("reasoning"))
    assert chat_policy(TUESDAY_10) == "live"
    p95 = _p95(TUESDAY_10)
    sys.stderr.write(f"BT08-09 chat_policy live p95={p95 * 1000:.4f} ms\n")
    assert p95 < 0.005


def test_bt08_09_chat_policy_fallback_p95_under_5_ms(jobs: SqliteJobsBackend) -> None:
    """BT08-09 warm caches: the window fallback path (decider loaded) p95 < 5 ms."""
    jobs.upsert_worker(_worker("decider"))
    assert chat_policy(TUESDAY_22) == "small_model"
    p95 = _p95(TUESDAY_22)
    sys.stderr.write(f"BT08-09 chat_policy fallback p95={p95 * 1000:.4f} ms\n")
    assert p95 < 0.005
