"""Private sibling of `tests.support.builds` (U11-48, T11-34 spec note): the loopback stubs a
build is enriched with, started for one build and stopped after it, always.

`running_stubs(root)` starts `StubDeciderServer("oracle", truth_labels=<root>/truth/
truth_labels.parquet)` (OpenJev) and `FakeLLMServer(tests/fixtures/llm_scripts/build)`
(cluster naming) on loopback and yields their URLs; both are stopped in `finally`.
`stub_overrides` points `deciders.openjev.base_url` and every `openai_compat`
`models.clients.<key>.base_url` at them; `llm_factory` gives `run_enrichment` the real
`cluster_namer` client (an HTTP client on the stub LLM, never a real model server).
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import subprocess
import sys
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final, Literal, cast

import pytest
import yaml
from tests.support.fake_gpu import FakeGpu, shipped_gpu_settings
from tests.support.fake_laya import FakeLayaAgent, fake_laya_module, write_laya_version
from tests.support.fake_llm import FakeLLMServer
from tests.support.make_tiny_st import TINY_ST
from tests.support.stub_decider import StubDeciderServer

from herness.core.config import get_config
from herness.core.jobs import gpu_services
from herness.core.resilience.settings import GpuSettings
from herness.core.types import GpuClass, ServiceName
from herness.enrich import _pipeline_stages, decide_stage, embed
from herness.enrich.embed import Encoder
from herness.harness.llm.registry import LLMRegistry

__all__ = [
    "BUILD_SCRIPTS",
    "STUBS",
    "STUB_SECRETS",
    "StubSet",
    "StubUrls",
    "install_laya",
    "llm_factory",
    "running_stubs",
    "stub_overrides",
    "wire_enrichment",
]  # fmt: skip

BUILD_SCRIPTS: Final = Path(__file__).resolve().parents[1] / "fixtures" / "llm_scripts" / "build"
STUB_SECRETS: Final = ("vllm.api_key", "openjev_api_key")  # the keyring names the clients read
LAYA_VERSION: Final = "laya-20260901-1"
TRUTH_LABELS: Final = "truth/truth_labels.parquet"


@dataclasses.dataclass(frozen=True)
class StubSet:
    """What a build was enriched with (stable descriptors; a reused build starts no stubs)."""

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
    """`init_config` overrides: OpenJev and every client on a stub (C09:
    a served client's and a service's port equal its deploy port, which are pairwise
    distinct, so the large class has its own stub)."""
    models = yaml.safe_load((config_dir / "models.yaml").read_text(encoding="utf-8"))
    out = ["decisions.escalation.bootstrap_window_days=3650",  # the synth lake is in scope
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


def llm_factory(role: Literal["enrich_decider", "cluster_namer"]) -> tuple[Any, str, int]:
    """`run_enrichment`'s `LlmFactory`: the configured local client of `role` (the stub LLM).

    The registry's off-network guard is waived (`egress_enabled=True`) only so the synth
    profile can route a role: it builds the one client of `role`, never an anthropic one.
    """
    registry = LLMRegistry(get_config().models, profile="synth", egress_enabled=True)
    key = registry.model_for(role, "standard")
    cfg = registry.config(key)
    return registry.client(key), cfg.model, cfg.max_concurrency


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
        services = {}
        for name, svc in spec.services.items():
            health = svc.health.model_copy(update={"path": "/v1/models"})
            services[name] = svc.model_copy(update={"url": by_name[name], "health": health})
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
    """Run the real enrichment on a CPU box with nothing real behind it: compose is answered by
    `FakeGpu` over the stubs, the tiny sentence-transformers fixture is the encoder (no bge-m3
    download) and no CUDA is touched."""
    fake = FakeGpu(gpu=_gpu_for(urls), stubs={})
    namespace = types.SimpleNamespace(run=fake.run, TimeoutExpired=subprocess.TimeoutExpired,
                                      SubprocessError=subprocess.SubprocessError)  # fmt: skip
    patch.setattr(gpu_services, "subprocess", namespace)
    patch.setattr(gpu_services, "_gpu", lambda: fake.gpu)
    state = _StubGpuState(fake)
    patch.setattr(decide_stage, "gpu_state", lambda: state)
    encoder = Encoder(TINY_ST, model_name="test/tiny")
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
