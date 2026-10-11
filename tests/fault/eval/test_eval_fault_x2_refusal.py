"""Spec 11 fault case X2 (impl 11 FT11-02; F11-11; R-40; T11-33).

An org review in the `hybrid` test profile (the full test tree, whose `herness.yaml` records
the hybrid approval; real `EgressGuard` with the test redactor; fake API keys) on the T06-22
run driver (real steps, Blackboard, Writer step; scripted planner, analysts and Writer). The
Skeptic makes its model call for real: the final structured request the spec 05 loop builds
(`_loop_steps.fresh_state` + `build_request`: the role's system blocks and task input) goes
through the task's `HarnessHooks.call`, i.e. the real `ModelChain` with retry, fallback and
the run tracer, to the real registry clients. Both wires are answered by the spec 11 router
(`respx_router`: `local-30b` over the OpenAI-compatible loopback wire, `claude-opus` through
the real `AnthropicClient` and egress guard). In `hybrid` the single challenge round of a
`fast` run is the final one, so the Skeptic's model role is `skeptic_final`, chain
`[claude-opus, local-30b]`. The JSON plan (`tests.support.faults`, enabled in this process)
is `llm.call error:ModelRefused count=1 role=skeptic`.

With one finding challenged the row passes: the refused head falls back to `local-30b`.
With two, the un-faulted Skeptic call must reach `claude-opus`; two product gaps stop it, each
pinned by its own strict xfail (probes: fixing either turns exactly its xfail into XPASS):
T06-29 gap E (`route_task` keeps the role spec's model role `skeptic`, so the request's
egress purpose is `reasoning`, refused in hybrid) and T06-29 gap A (the build id in the role
context line reads as a phone number to the guard's re-scan).
"""

from __future__ import annotations

import asyncio
import dataclasses
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar, Final

import pytest
from pydantic import SecretStr
from tests.integration.harness import _swarm_run_env as e
from tests.support import faults
from tests.support.egress_harness import egress_lines, make_guard
from tests.support.fake_keyring import MemoryKeyring
from tests.support.fake_llm import ScriptRouter, respx_router
from tests.unit.harness._blackboard_env import test_redactor

from herness.core import config as c
from herness.core import egress, secrets
from herness.core.resilience import process_state
from herness.core.resilience._state import NOT_LOADED
from herness.core.types import SKEPTIC_CHECKS, AgentResult
from herness.eval.scripted import LLMScript, ScriptBook
from herness.harness import _loop_steps
from herness.harness.llm import openai_compat
from herness.harness.llm.registry import LLMRegistry
from herness.harness.swarm import challenge
from herness.harness.swarm.run import Swarm
from herness.store.ops import get_task, read_all, select_tasks

pytestmark = pytest.mark.fault

drv = e.drv  # fixtures
__all__ = ["drv", "test_redactor"]

X2_RULE: Final = {
    "point": "llm.call",
    "action": "error:ModelRefused",
    "count": 1,
    "role": "skeptic",
}
_UPHOLD: Final = {
    "checks": [{"check": k, "result": "pass", "note": ""} for k in SKEPTIC_CHECKS],
    "verdict": "uphold", "required_actions": [],
}  # fmt: skip


class _Tracer(e.Tracer):
    """The driver's recording tracer, keeping every run's instance."""

    made: ClassVar[list[_Tracer]] = []

    def __init__(self, run_id: str) -> None:
        super().__init__(run_id)
        _Tracer.made.append(self)


@dataclasses.dataclass
class _Skeptic:
    """The Skeptic's `run_agent`: its script (an `uphold` of the finding it challenges) is
    rendered when the execution starts, because the finding id exists only at run time
    (the T06-26 precedent); then one real model call (`_call`)."""

    router: ScriptRouter | None = None
    # test-side sidestep of gap E only (the gap-A row): the task's model role on its spec
    sidestep_model_role: bool = False

    async def __call__(  # noqa: PLR0913 - spec 05 run_agent signature
        self, role_spec: Any, task_input: Any, ctx: Any, client: Any, config: Any, hooks: Any,
        *, resume_from: Any = None,
    ) -> AgentResult:  # fmt: skip
        del resume_from
        task = get_task(ctx.task_id)
        assert task is not None
        assert self.router is not None
        output = _UPHOLD | {"finding_id": task.spec.inputs.finding_ids[0]}
        script = {"match": {}, "turns": [{"final": {"output": output}}] * 4, "source": "x2#0"}
        self.router.book = ScriptBook([LLMScript.model_validate(script)])
        if self.sidestep_model_role:
            role_spec = dataclasses.replace(role_spec, model_role=task.spec.model_role)
        return await _call(role_spec, task_input, ctx, client, config, hooks)


async def _call(
    role_spec: Any, task_input: Any, ctx: Any, client: Any, config: Any, hooks: Any
) -> AgentResult:  # fmt: skip
    """One real model call: the loop's final structured request through `hooks.call`."""
    system = role_spec.system_blocks(ctx)
    state = _loop_steps.fresh_state(role_spec, task_input, config, [], system, None)
    req = _loop_steps.build_request(role_spec, config, system, [], state, ctx, wrap_up=False,
                                    final=True)  # fmt: skip
    resp, parsed = await hooks.call(client, req, state, role_spec.output_model)
    assert parsed is not None
    return AgentResult(
        status="completed", stop_reason="final", output=parsed.model_dump(mode="json"), steps=1,
        usage=resp.usage, cost_usd=Decimal(0), query_ids=[], finding_ids=[],
    )  # fmt: skip


@dataclasses.dataclass
class _X2:
    """One X2 run: the run result, its tasks, the router and the hybrid config."""

    res: Any
    router: ScriptRouter
    hcfg: c.HernessConfig

    def skeptics(self) -> list[Any]:
        return [t for t in select_tasks(self.res.run_id) if t.role == "skeptic"]

    def wires(self) -> list[str]:
        return [
            "anthropic" if "anthropic" in str(r.url) else "openai" for r in self.router.requests
        ]

    def egress_purposes(self) -> set[object]:
        """The purpose of every guard decision on a call to the Anthropic API."""
        return {line.get("purpose") for line in egress_lines(Path(self.hcfg.paths.logs))}


def _run_x2(
    drv_: e.Drv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, t2: int,
    sidestep_model_role: bool = False,
) -> _X2:  # fmt: skip
    """The hybrid org review (fast: one, final, challenge round) under the X2 plan."""
    drv_.set_pipeline(t1=1, t2=t2)
    hcfg = c.init_config("hybrid", (f"paths.data={drv_.hcfg.paths.data.as_posix()}",),
                         config_dir=drv_.cfg_root / "config", env={})  # fmt: skip
    guard = make_guard(hcfg)
    monkeypatch.setattr(egress, "get_guard", lambda: guard)
    for module in (secrets, openai_compat):
        monkeypatch.setattr(module, "resolve", lambda _ref: SecretStr("sk-test-fake"))
    base_url = str(hcfg.models.models.clients["local-30b"].base_url)
    router = respx_router(ScriptBook([]), openai_base_url=base_url).install(monkeypatch)
    monkeypatch.setattr(challenge, "run_agent", _Skeptic(router, sidestep_model_role))
    monkeypatch.setattr(_Tracer, "made", [])
    llms = LLMRegistry(hcfg.models, profile="hybrid", egress_enabled=True)
    deps = dataclasses.replace(drv_.deps, hcfg=hcfg, tracer_factory=_Tracer)  # type: ignore[arg-type]
    swarm = Swarm(hcfg.pipelines, llms, drv_.memory, deps=deps)  # type: ignore[arg-type]
    drv_.swarms.append(swarm)
    for key, value in faults.fault_env(faults.write_fault_plan(tmp_path, [X2_RULE])).items():
        monkeypatch.setenv(key, value)
    state = process_state()
    state.fault_plan = NOT_LOADED
    state.faults_enabled = False
    res = asyncio.run(swarm.start(drv_.request("org_review", depth="fast", profile="hybrid")))
    return _X2(res, router, hcfg)


def _fallbacks() -> list[dict[str, object]]:
    return [f for t in _Tracer.made for (kind, _, f) in t.events if kind == "fallback"]


def test_ft11_02_refused_skeptic_call_falls_back_once(
    drv: e.Drv, tmp_path: Path, fake_keyring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FT11-02 X2 `llm.call error:ModelRefused count=1 role=skeptic` with one finding to
    challenge: the Skeptic falls back from `claude-opus` to the next chain entry
    (`local-30b`), which answers; exactly one `fallback` trace event (reason `refusal`)."""
    del fake_keyring
    run = _run_x2(drv, tmp_path, monkeypatch, t2=0)

    skeptics = run.skeptics()
    assert [(t.spec.model_role, t.status) for t in skeptics] == [("skeptic_final", "done")]
    assert run.res.status == "done"
    # exactly one `fallback` trace event, from the refused head to the next chain entry
    assert _fallbacks() == [  # the bound tracer adds the task and role (T06-29 gap D)
        {"task_id": skeptics[0].task_id, "role": "skeptic", "from_profile": "claude-opus",
         "to_profile": "local-30b", "reason": "refusal"}
    ]  # fmt: skip
    [row] = read_all("SELECT target, task_id FROM resilience_event WHERE kind = 'fallback'", ())
    assert (row["target"], row["task_id"]) == ("claude-opus", skeptics[0].task_id)
    assert run.wires() == ["openai"]  # the refused head never reached the wire
    plan = process_state().fault_plan
    assert plan is not None
    assert plan is not NOT_LOADED
    assert plan.counters == [2]  # two skeptic calls met the rule: the refused one, the fallback


def test_ft11_02_final_skeptic_calls_use_the_reasoning_final_purpose(
    drv: e.Drv, tmp_path: Path, fake_keyring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FT11-02 X2 with two findings challenged: every guard decision on the un-faulted
    `skeptic_final` calls to `claude-opus` has the `reasoning_final` purpose of the route."""
    del fake_keyring
    run = _run_x2(drv, tmp_path, monkeypatch, t2=1)
    assert [t.spec.model_role for t in run.skeptics()] == ["skeptic_final"] * 2
    assert run.egress_purposes() == {"reasoning_final"}


def test_ft11_02_other_skeptic_calls_reach_claude_one_fallback_in_the_run(
    drv: e.Drv, tmp_path: Path, fake_keyring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FT11-02 X2 with two findings challenged (gap E sidestepped on the test side only: the
    Skeptic's request carries its task's model role): only the first Skeptic call is
    refused (`count=1`), so the run has exactly one `fallback` trace event and the other
    Skeptic call is answered by `claude-opus` through the egress guard."""
    del fake_keyring
    run = _run_x2(drv, tmp_path, monkeypatch, t2=1, sidestep_model_role=True)
    skeptics = run.skeptics()
    assert len(skeptics) == 2
    assert {t.status for t in skeptics} == {"done"}
    assert len(_fallbacks()) == 1
    assert sorted(run.wires()) == ["anthropic", "openai"]
