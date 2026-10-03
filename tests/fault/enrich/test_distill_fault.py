"""D7 fallback teacher for distillation (FT03-05; T03-32, design 03 §5.8 decision D7).

With ``deciders.openjev.enabled: false`` (or an OpenJev that fails to start) the round
completes with the real `LlmDecider` (3 votes) over a scripted client inside a nested
``reasoning`` GPU scope; manifest and ``eval.json`` record ``teacher: "llm"``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Literal

import pytest
from tests.support.ops_store import OpsStoreHandle
from tests.unit.enrich._distill_support import DistillEnv, FakeCtx, build_env, read_json
from tests.unit.enrich._fake_llm import FakeLLMClient, Reply
from tests.unit.enrich._openjev_support import jev_env

from herness.core.errors import ConfigError
from herness.core.resilience import ProcessState
from herness.core.types import LLMRequest
from herness.enrich import distill
from herness.enrich.deciders.llm import CompletionClient
from herness.enrich.distill import LlmFactory, run_distill
from herness.enrich.laya_models import verify_model_dir
from herness.enrich.settings import DecidersSettings

pytestmark = pytest.mark.fault

__all__ = ["jev_env"]  # the fixture is used by name


def _vote(req: LLMRequest) -> Reply:
    """Every vote answers each asked question with its first label."""
    schema = req.response_schema or {}
    props = schema["properties"]
    assert isinstance(props, dict)
    return {qid: {"answer": spec["properties"]["answer"]["enum"][0]}  # type: ignore[index]
            for qid, spec in props.items()}  # fmt: skip


@pytest.fixture
def env(
    jev_env: ProcessState, ops_store: OpsStoreHandle, monkeypatch: pytest.MonkeyPatch
) -> DistillEnv:
    del jev_env
    built = build_env(ops_store.data_root, monkeypatch)
    built.cfg.models.deciders = DecidersSettings.model_validate(
        {"laya": {"device": "cpu", "dtype": "fp32"}, "openjev": {"enabled": False}}
    )
    return built


def _factory(client: FakeLLMClient, roles: list[str]) -> LlmFactory:
    def factory(
        role: Literal["enrich_decider", "cluster_namer"],
    ) -> tuple[CompletionClient, str, int]:
        roles.append(role)
        return client, "local/qwen-test", 4

    return factory


def test_ft03_05_openjev_disabled_distills_with_the_llm_teacher(env: DistillEnv) -> None:
    """FT03-05 ``deciders.openjev.enabled: false``: completes with the LLM teacher; manifest
    and eval.json record ``teacher: "llm"``; reasoning nested in decider (R-43)."""
    client, roles, ctx = FakeLLMClient(_vote), [], FakeCtx()
    report = run_distill(
        round_kind="initial", ctx=ctx.as_ctx(), llm_factory=_factory(client, roles)
    )

    assert roles == ["enrich_decider"]
    assert (report.teacher, report.teacher_version) == ("llm", "local/qwen-test")
    assert report.version is not None
    assert report.n_sample == 160  # whole pool: sample_size_llm_teacher is larger
    manifest = verify_model_dir(env.paths, report.version)
    assert (manifest.teacher, manifest.teacher_version) == ("llm", "local/qwen-test")
    evaluated = read_json(env.paths.laya_dir(report.version) / "eval.json")
    assert {e["teacher"]["decider"] for e in evaluated["questions"].values()} == {"llm"}
    assert ctx.events == ["enter:decider", "enter:reasoning", "exit:reasoning", "exit:decider"]
    assert len(client.first_requests()) == 3 * (160 + len(env.gold))  # 3 votes per record
    assert not set(env.teacher_rows().column("content_hash").to_pylist()) & set(env.gold)
    assert {r["decider"] for r in env.teacher_rows().to_pylist()} == {"llm"}


def test_ft03_05_openjev_start_failure_falls_back(env: DistillEnv) -> None:
    """FT03-05 OpenJev enabled but failing to start: stopped, then the LLM teacher runs."""
    env.cfg.models.deciders = DecidersSettings.model_validate(
        {"laya": {"device": "cpu", "dtype": "fp32"}}
    )
    ctx = FakeCtx(fail_start=True)
    report = run_distill(round_kind="initial", ctx=ctx.as_ctx(),
                         llm_factory=_factory(FakeLLMClient(_vote), []))  # fmt: skip
    assert report.teacher == "llm"
    assert ctx.events[:4] == ["enter:decider", "start:openjev", "stop:openjev", "enter:reasoning"]


def test_ft03_05_off_network_client_skips_the_reasoning_scope(
    env: DistillEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FT03-05 an off-network LLM profile does not take the local reasoning class."""
    del env
    chains = SimpleNamespace(config=lambda name: SimpleNamespace(off_network=True))
    monkeypatch.setattr(distill, "process_state", lambda: SimpleNamespace(chains=chains))
    ctx = FakeCtx()
    report = run_distill(round_kind="initial", ctx=ctx.as_ctx(),
                         llm_factory=_factory(FakeLLMClient(_vote), []))  # fmt: skip
    assert report.teacher == "llm"
    assert ctx.events == ["enter:decider", "exit:decider"]


def test_ft03_05_unknown_client_enters_the_reasoning_scope(
    env: DistillEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FT03-05 a client the chain registry does not know is treated as local (scope entered)."""
    del env

    def unknown(name: str) -> object:
        msg = f"unknown client {name}"
        raise ConfigError(msg)

    state = SimpleNamespace(chains=SimpleNamespace(config=unknown))
    monkeypatch.setattr(distill, "process_state", lambda: state)
    ctx = FakeCtx()
    run_distill(
        round_kind="initial", ctx=ctx.as_ctx(), llm_factory=_factory(FakeLLMClient(_vote), [])
    )
    assert "enter:reasoning" in ctx.events
