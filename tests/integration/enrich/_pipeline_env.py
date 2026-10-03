"""Card-local small build for the `run_enrichment` tests (T03-28; IT03-01/04/05/08, FT03-04/06).

Spec 11's `small_build` is not in the tree (carry-over: re-point these tests to it). This
stands in for it: the `lake_small` raw lake plus `EXTRA` synthetic incidents, built through
impl 02's real `build_pipeline` handler (files 000-299, then `run_enrichment`, then 300-399)
on a migrated ops store, with a full config whose decisions use three shipped questions.
Collaborators are spec 11's and impl 03's test doubles: the loopback `StubDeciderServer`
(hash mode) as OpenJev, the 2-layer fake Laya agent with an accepted `CURRENT`, a scripted
LLM client (`tests/unit/enrich/_fake_llm.py`) through `llm_factory`, and the tiny
sentence-transformers model on the CPU as the encoder. `GpuView` lets impl 08's
`DeciderChain` see the fake job context's class and started services.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, Final, Literal, cast

import pytest
import yaml
from pydantic import JsonValue
from tests.support.build_harness import FakeJobContext
from tests.support.config_tree import write_full_config
from tests.support.fake_keyring import MemoryKeyring
from tests.support.fake_laya import FakeLayaAgent, fake_laya_module, write_laya_version
from tests.support.lake_small import FIXTURE, LAKE, SOURCES_YAML, Row, write_lake
from tests.support.make_tiny_st import TINY_ST
from tests.support.ops_store import OpsStoreHandle
from tests.support.stub_decider import StubDeciderServer
from tests.unit.enrich._fake_llm import FakeLLMClient

from herness.core import config as herness_config
from herness.core.errors import HernessError
from herness.core.jobs.handlers import run_handler
from herness.core.resilience import ProcessState, bind_ops_backend
from herness.core.types import GpuClass, JobOutcome, LLMRequest, ServiceName
from herness.enrich import _pipeline_stages, decide_stage, embed
from herness.enrich.embed import Encoder
from herness.model.build import make_build_pipeline_handler
from herness.store import warehouse
from herness.store.layout import DataLayout
from herness.store.ops.resilience import SqliteResilienceBackend

__all__ = ["EXTRA", "LAYA_VERSION", "QSV", "MergingContext", "PipelineEnv", "pipeline_env"]

QSV: Final = "qs-2026-10-01.1"
LAYA_VERSION: Final = "laya-20260901-1"
EXTRA: Final = 20  # synthetic incidents added to lake_small (two embed windows of 8 + rest)
REDACT_KEY: Final = bytes(range(32))  # built at run time: no secret literal in the tree
_KEEP: Final = ("root_cause", "change_caused", "business_impact")
_WORDS: Final = ("disk full", "login fails", "vpn drops", "printer jam", "backup failed")


def _lake() -> dict[tuple[str, str], list[Row]]:
    lake = {key: list(rows) for key, rows in LAKE.items()}
    for i in range(EXTRA):
        lake[("servicenow", "incident")].append(Row(f"x{i:02d}", 3, {
            "number": f"INC1{i:03d}", "opened_at": "2024-03-03 09:00:00",
            "priority": "3 - Moderate", "state": "1", "cmdb_ci": "c2", "assignment_group": "g2",
            "short_description": f"{_WORDS[i % 5]} on host {i}",
            "description": f"Users report {_WORDS[i % 5]} since the morning, case {i}.",
            "made_sla": "true",
        }))  # fmt: skip
    return lake


class MergingContext(FakeJobContext):
    """Like impl 02's T02-19b job-context view: `save_state` merges into the build's state."""

    def save_state(self, state: dict[str, JsonValue]) -> None:
        super().save_state({**self.load_state(), **state})


class SpyEncoder(Encoder):
    """The real tiny-st encoder on the CPU, recording every encoded text."""

    def __init__(self) -> None:
        super().__init__(TINY_ST, model_name="test/tiny")
        self.inputs: list[str] = []
        self.on_encode: list[Any] = []

    def encode(self, texts: Sequence[str], *, batch_size: int) -> Any:
        self.inputs.extend(texts)
        for hook in self.on_encode:
            hook()
        return super().encode(texts, batch_size=batch_size)


@dataclasses.dataclass
class GpuView:
    """`GpuStateReader` over the current fake job context (class and started services)."""

    ctx: FakeJobContext | None = None

    def loaded_class(self) -> GpuClass:
        return "none" if self.ctx is None else self.ctx.current_class

    def service_healthy(self, name: ServiceName) -> bool:
        return self.ctx is not None and name in self.ctx.services.running


def _vote(req: LLMRequest) -> dict[str, object]:
    """Every vote picks each question's first label (`root_cause`: 'software_defect')."""
    schema = cast("dict[str, Any]", req.response_schema or {})
    props: dict[str, Any] = schema.get("properties", {})
    return {qid: {"answer": spec["properties"]["answer"]["enum"][0]} for qid, spec in props.items()}


@dataclasses.dataclass
class PipelineEnv:
    root: Path
    data_root: Path
    layout: DataLayout
    stub: StubDeciderServer
    agent: FakeLayaAgent
    llm: FakeLLMClient
    encoder: SpyEncoder
    gpu: GpuView
    roles: list[str] = dataclasses.field(default_factory=list)

    def configure(self, *, qsv: str = QSV, edit: str | None = None) -> None:
        """Write and load the config; `edit` changes `change_caused`'s instructions."""
        cfg_dir = self.root / "cfg" / "config"
        if cfg_dir.parent.exists():
            shutil.rmtree(cfg_dir.parent)
        write_full_config(cfg_dir.parent)
        shutil.copyfile(FIXTURE / "mappings.yaml", cfg_dir / "mappings.yaml")
        (cfg_dir / "sources.yaml").write_text(SOURCES_YAML, encoding="utf-8")
        decisions = yaml.safe_load((cfg_dir / "decisions.yaml").read_text("utf-8"))
        decisions["question_set_version"] = qsv
        decisions["questions"] = [q for q in decisions["questions"] if q["id"] in _KEEP]
        if edit is not None:
            decisions["questions"][1]["instructions"] = edit
        decisions["change_link"]["use_decider"] = False
        decisions["embedding"]["batch_size"] = 8
        decisions["escalation"]["bootstrap_window_days"] = 3650  # the 2024 lake is in scope
        (cfg_dir / "decisions.yaml").write_text(yaml.safe_dump(decisions), encoding="utf-8")
        models = yaml.safe_load((cfg_dir / "models.yaml").read_text("utf-8"))
        models["deciders"]["openjev"]["base_url"] = self.stub.base_url
        (cfg_dir / "models.yaml").write_text(yaml.safe_dump(models), encoding="utf-8")
        herness = (cfg_dir / "herness.yaml").read_text("utf-8")
        image = "razorback16/openjev@sha256:" + "b" * 64
        (cfg_dir / "herness.yaml").write_text(herness.replace("<openjev-image>", image), "utf-8")
        herness_config.reset_config()
        env = {"HERNESS_PATHS__DATA": str(self.data_root)}
        herness_config.init_config("local", config_dir=cfg_dir, env=env)

    def factory(self, role: Literal["enrich_decider", "cluster_namer"]) -> tuple[Any, str, int]:
        self.roles.append(role)
        return self.llm, "local-30b/qwen3-test", 2

    def job(
        self, payload: dict[str, Any], **kw: Any
    ) -> tuple[JobOutcome | HernessError, FakeJobContext]:
        """Run one `build_pipeline` job (impl 02's real handler) with a fake job context."""
        ctx = FakeJobContext(payload, **kw)
        self.gpu.ctx = ctx
        return run_handler(ctx, make_build_pipeline_handler(llm_factory=self.factory)), ctx

    def calls(self) -> dict[str, int]:
        """Decider calls and encoded texts so far."""
        return {"laya": len(self.agent.batch_calls), "openjev": self.stub.calls,
                "llm": len(self.llm.requests), "embedded": len(self.encoder.inputs)}  # fmt: skip

    def query(self, build_id: str, sql: str) -> list[tuple[Any, ...]]:
        with warehouse.open_readonly(build_id, layout=self.layout) as con:
            return con.execute(sql).fetchall()


@pytest.fixture
def pipeline_env(
    tmp_path: Path,
    ops_store: OpsStoreHandle,
    reset_process_state: ProcessState,
    fake_keyring: MemoryKeyring,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[PipelineEnv]:
    """The small build environment (see the module doc); config loaded for `QSV`."""
    del reset_process_state
    bind_ops_backend(SqliteResilienceBackend())
    fake_keyring.store[("herness", "redact.hmac_key")] = REDACT_KEY.hex()
    data_root = ops_store.data_root
    write_lake(data_root / "raw", _lake())
    laya_root = data_root / "models" / "laya"
    directory = write_laya_version(laya_root, LAYA_VERSION)
    calibration = {"decider": "laya", "decider_version": LAYA_VERSION, "question_set_version": QSV,
                   "fitted_at": "2026-09-01T00:00:00+00:00", "questions": {}}  # fmt: skip
    (directory / "calibration.json").write_text(json.dumps(calibration), encoding="utf-8")
    (laya_root / "CURRENT").write_text(LAYA_VERSION, encoding="utf-8")
    agent = FakeLayaAgent()
    monkeypatch.setitem(sys.modules, "laya", fake_laya_module(agent))
    encoder = SpyEncoder()
    monkeypatch.setattr(_pipeline_stages, "get_encoder", lambda: encoder)
    monkeypatch.setattr(_pipeline_stages, "device", lambda: "cpu")
    monkeypatch.setattr(embed, "release_cuda", lambda: None)
    gpu = GpuView()
    monkeypatch.setattr(decide_stage, "gpu_state", lambda: gpu)
    with StubDeciderServer("hash") as stub:
        env = PipelineEnv(tmp_path, data_root, DataLayout.from_root(data_root), stub, agent,
                          FakeLLMClient(_vote), encoder, gpu)  # fmt: skip
        env.configure()
        yield env
    herness_config.reset_config()
