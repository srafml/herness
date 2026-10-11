"""Real agent loop and real traces for chosen roles of a `drv` run (T07-24; not a test module).

`use_real_loop` replaces the driver's scripted `run_agent` of the named roles by the real spec 05
`run_agent` over a spec 11 `FakeLLMClient` (R-65): the swarm step's role spec, task input, tool
context, profile and hooks are the real ones; only the client is the scripted fake, which the
driver's `LLMRegistry.client` returns for every key (its name set to the routed profile, as
`tests.support.planner_env` does). Every other role keeps the
driver's scripted agent. `full_tracer` is the real spec 05 `Tracer` writing
`<paths.data>/traces/<run_id>.jsonl` with every review payload sampled (`payload_sample_rate`
review 1.0 instead of 0.1), so a row can read the exact `llm_call` payloads. `script` builds an
`LLMScript` per role from plain turns. No side effects at import.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tests.support.fake_llm import FakeLLMClient

from herness.core.config import get_config
from herness.eval.scripted import LLMScript, ScriptBook
from herness.harness import loop
from herness.harness.swarm import challenge, planner, scheduler, writer
from herness.harness.tracing import Tracer

if TYPE_CHECKING:
    import pytest
    from tests.integration.harness._swarm_run_env import Drv

__all__ = ["full_tracer", "llm_calls", "script", "use_real_loop"]

_MODULES = (planner, scheduler, challenge, writer)


def script(role: str, turns: Sequence[Mapping[str, Any]]) -> LLMScript:
    """One script answering every call of `role` with `turns` in order."""
    return LLMScript.model_validate(
        {"match": {"role": role}, "turns": list(turns), "source": f"t07_24#{role}"}
    )


def use_real_loop(
    drv: Drv, monkeypatch: pytest.MonkeyPatch, scripts: Sequence[LLMScript], roles: set[str]
) -> FakeLLMClient:
    """The real `run_agent` for `roles` on one scripted client; other roles stay scripted."""
    client = FakeLLMClient(ScriptBook(list(scripts)))
    scripted = drv.agents

    async def agent(  # noqa: PLR0913 - spec 05 run_agent signature
        role_spec: Any, task_input: Any, ctx: Any, routed: Any, config: Any, hooks: Any,
        *, resume_from: Any = None,
    ) -> Any:  # fmt: skip
        if ctx.role not in roles:
            return await scripted(role_spec, task_input, ctx, routed, config, hooks,
                                  resume_from=resume_from)  # fmt: skip
        return await loop.run_agent(role_spec, task_input, ctx, routed, config, hooks,
                                    resume_from)  # fmt: skip

    def route(name: str) -> FakeLLMClient:
        client.name = name  # run_agent checks that the routed profile names the client
        return client

    monkeypatch.setattr(drv.llms, "client", route)  # the route and the hooks' fallback chain
    for module in _MODULES:
        monkeypatch.setattr(module, "run_agent", agent)
    return client


def full_tracer(run_id: str) -> Tracer:
    """The real run tracer (`Tracer.for_run` settings) with every review payload sampled."""
    cfg = get_config()
    settings = cfg.models.harness.trace
    rates = dict(settings.payload_sample_rate) | {"review": 1.0}
    return Tracer(run_id, build_id=None, run_kind="review",
                  traces_dir=Path(cfg.paths.data) / "traces",
                  settings=settings.model_copy(update={"payload_sample_rate": rates}))  # fmt: skip


def llm_calls(data_root: Path, run_id: str) -> list[dict[str, Any]]:
    """The run's `llm_call` trace events in file order (the tracer is closed at run end)."""
    path = data_root / "traces" / f"{run_id}.jsonl"
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return [ev for ev in events if ev.get("type") == "llm_call"]
