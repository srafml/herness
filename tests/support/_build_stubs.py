"""Private sibling of `tests.support.builds` (U11-48, T11-34 spec note): the loopback stubs a
build is enriched with, started for one build and stopped after it, always.

`running_stubs(root)` starts `StubDeciderServer("oracle", truth_labels=<root>/truth/
truth_labels.parquet)` (OpenJev) and `FakeLLMServer(tests/fixtures/llm_scripts/build)`
(cluster naming) on loopback and yields their URLs; both are stopped in `finally`.
`stub_overrides` points OpenJev and every `openai_compat` client at them; `LlmFactory`
gives `run_enrichment` the `cluster_namer` client (on the stub LLM, never a real server).
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import subprocess
import sys
import types
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, Final, Literal, cast
from urllib.parse import urlsplit

import numpy as np
import pytest
import yaml
from tests.support._build_cache import BuildFixtureError
from tests.support.fake_gpu import FakeGpu, shipped_gpu_settings
from tests.support.fake_laya import FakeLayaAgent, fake_laya_module, write_laya_version
from tests.support.fake_llm import FakeLLMServer
from tests.support.stub_decider import StubDeciderServer

from herness.core.config import get_config
from herness.core.jobs import gpu_services
from herness.core.resilience.settings import GpuSettings
from herness.core.types import GpuClass, ServiceName
from herness.enrich import _pipeline_stages, decide_stage, embed
from herness.enrich.embed import Device, Encoder
from herness.harness.llm.registry import LLMRegistry
from herness.store.vectors import EMBEDDING_DIM

__all__ = ["BUILD_SCRIPTS", "STUBS", "STUB_SECRETS", "LlmFactory", "StubSet", "StubUrls",
           "install_laya", "running_stubs", "stub_overrides", "wire_enrichment"]  # fmt: skip

BUILD_SCRIPTS: Final = Path(__file__).resolve().parents[1] / "fixtures" / "llm_scripts" / "build"
STUB_SECRETS: Final = ("vllm.api_key", "openjev_api_key")  # the keyring names the clients read
LAYA_VERSION: Final = "laya-20260901-1"
LOOPBACK: Final = frozenset({"127.0.0.1", "localhost", "::1"})
TRUTH_LABELS: Final = "truth/truth_labels.parquet"


@dataclasses.dataclass(frozen=True)
class StubSet:
    """What a build was enriched with (descriptors; a reused build starts no stubs)."""

    decider: str | None = None  # stub decider mode
    truth_labels: str | None = None  # its labels, relative to the slot root
    llm_scripts: str | None = None  # the stub LLM's script book, relative to the repo root


STUBS: Final = StubSet("oracle", TRUTH_LABELS, "tests/fixtures/llm_scripts/build")


@dataclasses.dataclass(frozen=True)
class StubUrls:
    decider: str
    llm: str  # the reasoning-class stub LLM
    llm_large: str  # the large-class stub LLM (C09: deploy ports are pairwise distinct)


def _port(url: str) -> str:
    return url.rsplit(":", 1)[1]


def stub_overrides(config_dir: Path, urls: StubUrls) -> list[str]:
    """`init_config` overrides: OpenJev and every client on a stub (C09: ports equal the
    pairwise distinct deploy ports, so the large class has its own stub)."""
    models = yaml.safe_load((config_dir / "models.yaml").read_text(encoding="utf-8"))
    out = ["decisions.escalation.bootstrap_window_days=3650",  # the synth lake is in scope
           "decisions.escalation.max_rows_per_night=1500",  # bounds the stub round trips
           "decisions.change_link.decider_max_pairs=500",
           "decisions.escalation.llm_max_rows_per_night=0",  # the stub LLM only names clusters
           f"models.deciders.openjev.base_url={urls.decider}",
           "models.deciders.openjev.concurrency=4",  # the stub's listen backlog is small
           f"deploy.reasoning.port={_port(urls.llm)}", f"deploy.large.port={_port(urls.llm_large)}",
           f"deploy.openjev.port={_port(urls.decider)}",
           "deploy.openjev.image=razorback16/openjev@sha256:" + "b" * 64]  # fmt: skip
    for name, url in (
        ("reasoning.services.vllm-reasoning", urls.llm),
        ("decider.services.openjev", urls.decider),
        ("large.services.llamacpp-large", urls.llm_large),
    ):
        base = f"resilience.resilience.gpu.classes.{name}"
        out += [f"{base}.url={url}", f"{base}.health.path=/v1/models"]  # the stubs have no /health
    for key, client in models["models"]["clients"].items():
        if client.get("kind") == "openai_compat":
            url = urls.llm_large if client.get("gpu_class") == "large" else urls.llm
            out.append(f"models.models.clients.{key}.base_url={url}/v1")
    return out


@contextlib.contextmanager
def running_stubs(root: Path) -> Iterator[StubUrls]:
    """Start the stubs on loopback; stop them (also never-started ones) whatever happens."""
    with contextlib.ExitStack() as stack:
        decider = StubDeciderServer("oracle", truth_labels=root / TRUTH_LABELS)
        stack.callback(decider.stop)
        llms = [FakeLLMServer(BUILD_SCRIPTS), FakeLLMServer(BUILD_SCRIPTS)]
        for llm in llms:
            stack.callback(llm.stop)
        for server in (decider, *llms):
            server.start()
        yield StubUrls(decider.base_url, llms[0].base_url, llms[1].base_url)


class LlmFactory:
    """`run_enrichment`'s `LlmFactory`: the configured local client of `role` (the stub LLM).

    The registry's off-network guard is waived (`egress_enabled=True`) so the synth profile can
    route a role; every resolved client must therefore be `openai_compat` on a loopback base
    URL, else `BuildFixtureError`. `close` drops the registry and closes what clients hold.
    """

    def __init__(self) -> None:
        self._registry: LLMRegistry | None = None
        self._clients: list[Any] = []

    def __call__(self, role: Literal["enrich_decider", "cluster_namer"]) -> tuple[Any, str, int]:
        if self._registry is None:
            cfg = get_config()
            self._registry = LLMRegistry(cfg.models, profile="synth", egress_enabled=True)
        key = self._registry.model_for(role, "standard")
        cfg = self._registry.config(key)
        host = urlsplit(cfg.base_url or "").hostname
        if cfg.kind != "openai_compat" or host not in LOOPBACK:
            msg = f"build stub LLM: client {key} must be openai_compat on loopback"
            raise BuildFixtureError(msg)
        client = self._registry.client(key)
        self._clients.append(client)
        return client, cfg.model, cfg.max_concurrency

    def close(self) -> None:
        clients, self._clients, self._registry = self._clients, [], None
        for client in clients:
            closer = getattr(client, "close", None)
            if callable(closer):
                closer()


class HashEncoder(Encoder):
    """Deterministic CPU stand-in for bge-m3 (no model files): unit-norm sum of a seeded
    Gaussian per word plus a small one per text. Texts sharing words are close, yet none
    coincide (the vector index needs diverse rows; tiny-st collapsed `small` to 67 groups)."""

    def __init__(self) -> None:
        super().__init__(Path("hash-encoder"), model_name="test/hash")
        self._words: dict[str, np.ndarray] = {}

    def load(self, device: Device) -> None:
        self._device = device

    def encode(self, texts: Sequence[str], *, batch_size: int) -> np.ndarray:
        rows = (
            np.stack([self._vector(text) for text in texts])
            if texts
            else np.empty((0, EMBEDDING_DIM))
        )
        return rows.astype(np.float32)

    def _vector(self, text: str) -> np.ndarray:
        total = 0.3 * _gauss(text)
        for word in text.lower().split():
            if word not in self._words:
                self._words[word] = _gauss(word)
            total = total + self._words[word]
        return total / max(float(np.linalg.norm(total)), 1e-12)


def _gauss(key: str) -> np.ndarray:
    seed = int.from_bytes(hashlib.sha256(key.encode("utf-8")).digest()[:8], "big")
    return np.random.default_rng(seed).standard_normal(EMBEDDING_DIM)


def _gpu_for(urls: StubUrls) -> GpuSettings:
    """The shipped `resilience.gpu` with each service on its stub; the LLM stubs have no
    `/health`, so their health probe is `/v1/models`."""
    gpu = shipped_gpu_settings()
    by_name = {
        "vllm-reasoning": urls.llm,
        "openjev": urls.decider,
        "llamacpp-large": urls.llm_large,
    }
    classes = {}
    for cls, spec in gpu.classes.items():
        services = {
            name: svc.model_copy(
                update={
                    "url": by_name[name],
                    "health": svc.health.model_copy(update={"path": "/v1/models"}),
                }
            )
            for name, svc in spec.services.items()
        }
        classes[cls] = spec.model_copy(update={"services": services})
    return gpu.model_copy(update={"classes": classes})


class _StubGpuState:
    """`GpuStateReader` over `FakeGpu`'s service states: the loaded class is the class of the
    running service (`run_inline` has no worker row for `WorkerGpuState` to read)."""

    def __init__(self, fake: FakeGpu) -> None:
        self._fake = fake

    def loaded_class(self) -> GpuClass:
        running = sorted(self._fake.running())
        return cast("GpuClass", self._fake.class_of(running[0])) if running else "none"

    def service_healthy(self, name: ServiceName) -> bool:
        return name in self._fake.running()


def wire_enrichment(patch: pytest.MonkeyPatch, urls: StubUrls) -> None:
    """Real enrichment with nothing real behind it: `FakeGpu` compose over the stubs, the
    `HashEncoder`, no CUDA."""
    fake = FakeGpu(gpu=_gpu_for(urls), stubs={})
    namespace = types.SimpleNamespace(run=fake.run, TimeoutExpired=subprocess.TimeoutExpired,
                                      SubprocessError=subprocess.SubprocessError)  # fmt: skip
    patch.setattr(gpu_services, "subprocess", namespace)
    patch.setattr(gpu_services, "_gpu", lambda: fake.gpu)
    state = _StubGpuState(fake)
    patch.setattr(decide_stage, "gpu_state", lambda: state)
    encoder = HashEncoder()
    patch.setattr(_pipeline_stages, "get_encoder", lambda: encoder)
    patch.setattr(_pipeline_stages, "device", lambda: "cpu")
    patch.setattr(embed, "release_cuda", lambda: None)


def install_laya(patch: pytest.MonkeyPatch, data_root: Path, config_dir: Path) -> None:
    """An accepted fake Laya version, so OpenJev (the oracle stub) is asked for the rows Laya
    leaves below their threshold: `decide-primary` needs one (`laya_degraded` skips it)."""
    qsv = yaml.safe_load((config_dir / "decisions.yaml").read_text("utf-8"))["question_set_version"]
    root = data_root / "models" / "laya"
    directory = write_laya_version(root, LAYA_VERSION)
    calibration = {"decider": "laya", "decider_version": LAYA_VERSION, "question_set_version": qsv,
                   "fitted_at": "2026-09-01T00:00:00+00:00", "questions": {}}  # fmt: skip
    (directory / "calibration.json").write_text(json.dumps(calibration), encoding="utf-8")
    (root / "CURRENT").write_text(LAYA_VERSION, encoding="utf-8")
    patch.setitem(sys.modules, "laya", fake_laya_module(FakeLayaAgent()))
