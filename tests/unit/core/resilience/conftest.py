"""Fixtures for the impl 08 resilience unit tests: a loaded config, a test redactor,
`ops_db` (a migrated ops store bound by `bind_core_backends`), `recording_tracer`,
`fake_chain_registry` and `fake_gpu_state` (a `GpuStateReader`, not the §11 `fake_gpu`)."""

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import pytest
from tests.support.config_tree import write_full_config
from tests.support.fake_keyring import MemoryKeyring
from tests.support.ops_store import OpsStoreHandle

from herness.core import config as c
from herness.core import redact as r
from herness.core.redact_directory import NameDirectory
from herness.core.resilience import ProcessState
from herness.core.settings import RedactionConfig
from herness.core.types import GpuClass, ServiceName
from herness.store.ops import bind_core_backends


@pytest.fixture
def herness_cfg(tmp_path: Path, fake_keyring: MemoryKeyring) -> Iterator[c.HernessConfig]:
    """A full config (design 08 §7 resilience defaults) cached for `get_config`."""
    c.reset_config()
    yield c.init_config("local", config_dir=write_full_config(tmp_path), env={})
    c.reset_config()


@pytest.fixture
def test_redactor(monkeypatch: pytest.MonkeyPatch) -> r.Redactor:
    """A process redactor with a fixed key and no directory names (no config needed)."""
    directory = NameDirectory.from_files(None, (), None)
    redactor = r.Redactor(RedactionConfig(directory_file=None), bytes(range(32)), directory)
    monkeypatch.setattr(r._State, "redactor", redactor)
    return redactor


@pytest.fixture
def ops_db(ops_store: OpsStoreHandle, reset_process_state: ProcessState) -> OpsStoreHandle:
    """A fresh migrated ops store bound with `bind_core_backends` (the `ops_db` of §11, U08-98)."""
    del reset_process_state
    bind_core_backends()
    return ops_store


class RecordingTracer:
    """A `TracerLike` that collects every `emit(type, **fields)` call (§11 `recording_tracer`)."""

    def __init__(self, run_id: str | None = None, task_id: str | None = None) -> None:
        self._run_id, self._task_id = run_id, task_id
        self.events: list[tuple[str, dict[str, object]]] = []

    @property
    def run_id(self) -> str | None:
        return self._run_id

    @property
    def task_id(self) -> str | None:
        return self._task_id

    def emit(self, type: str, **fields: object) -> None:  # noqa: A002 - spec 05 name
        self.events.append((type, fields))

    def of(self, kind: str) -> list[dict[str, object]]:
        """The fields of every recorded event of ``kind``, in order."""
        return [fields for t, fields in self.events if t == kind]


@pytest.fixture
def recording_tracer() -> RecordingTracer:
    """A fresh `RecordingTracer`."""
    return RecordingTracer()


@dataclass(frozen=True)
class FakeClientInfo:
    """A `ClientInfo` with spec 05 default-client facts."""

    off_network: bool = False
    gpu_class: str | None = None
    timeout_s: float = 60.0
    model: str = "fake-model"
    base_url: str | None = None
    api_key: str | None = None


@dataclass
class FakeChainRegistry:
    """In-memory `ChainRegistry` with the spec 05 default clients (§11 `fake_chain_registry`)."""

    clients: dict[str, FakeClientInfo] = field(
        default_factory=lambda: {
            "claude-opus": FakeClientInfo(off_network=True, timeout_s=120.0, model="opus"),
            "local-30b": FakeClientInfo(gpu_class="reasoning", timeout_s=90.0),
            "local-large-offload": FakeClientInfo(gpu_class="large", timeout_s=300.0),
            "local-small-cpu": FakeClientInfo(timeout_s=45.0),
        }
    )
    chains: dict[tuple[str, str], list[str]] = field(default_factory=dict)

    def chain_for(self, model_role: str, depth: str) -> list[str]:
        return list(self.chains.get((model_role, depth), []))

    def config(self, name: str) -> FakeClientInfo:
        return self.clients[name]


@pytest.fixture
def fake_chain_registry() -> FakeChainRegistry:
    """A fresh `FakeChainRegistry` (no chains set)."""
    return FakeChainRegistry()


@dataclass
class FakeGpu:
    """A `GpuStateReader` whose loaded class and service health tests set directly."""

    loaded: GpuClass | Literal["swapping"] = "reasoning"
    healthy: set[str] = field(default_factory=set)

    def loaded_class(self) -> GpuClass | Literal["swapping"]:
        return self.loaded

    def service_healthy(self, name: ServiceName) -> bool:
        return name in self.healthy


@pytest.fixture
def fake_gpu_state() -> FakeGpu:
    """A `FakeGpu` with the reasoning class loaded."""
    return FakeGpu()
