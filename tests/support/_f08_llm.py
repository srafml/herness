"""Model-side helpers of the impl 08 fault suite (T08-24: FT08-01, FT08-02, FT08-03, ST08-14).

A two-candidate chain registry (`local-30b` on the reasoning class, then `local-small-cpu`),
requests with stable metadata, the real `OpenAICompatClient` pointed at a `FakeLLMServer`, a
recording wrapper around any completer, and the stored `resilience_event` rows of one kind.
Nothing here injects a fault: every FT08 test does that through `fault_env` (R-40).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import pytest
from tests.support import loop_standin as ls
from tests.support import warehouse_tools_build as wb
from tests.support.dispatch_standin import use_test_config

from herness.core import config as c
from herness.core import redact as r
from herness.core.redact_directory import NameDirectory
from herness.core.resilience import bind_ops_backend
from herness.core.settings import RedactionConfig
from herness.core.types import (
    AgentResult,
    LLMRequest,
    LLMResponse,
    Message,
    RequestMeta,
    TextPart,
    ToolContext,
)
from herness.eval.scripted import LLMScript, ScriptBook
from herness.harness import hooks as h
from herness.harness import loop
from herness.harness import warehouse_tools as wt
from herness.harness.llm.openai_compat import OpenAICompatClient
from herness.harness.llm.settings import ClientConfig
from herness.harness.roles import base
from herness.harness.roles.base import get_role
from herness.harness.warehouse import DuckWarehouse
from herness.store.ops.core import read_all
from herness.store.ops.resilience import SqliteResilienceBackend

PRIMARY: Final = "local-30b"
FALLBACK: Final = "local-small-cpu"
CHAIN: Final = (PRIMARY, FALLBACK)
_ZERO: Final = {"input": "0", "output": "0", "cache_read": "0", "cache_write": "0"}
ANSWER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {"n": {"type": "integer"}},
    "required": ["n"],
    "additionalProperties": False,
}


ANALYST_OUTPUT: Final[dict[str, Any]] = {"summary": "ok", "unknowns": [], "suggested_followups": []}


def last_text(req: LLMRequest) -> str:
    """The text of the last message of `req` (its text parts joined)."""
    return "".join(getattr(part, "text", "") for part in req.messages[-1].parts)


def book(*scripts: dict[str, Any]) -> ScriptBook:
    """A script book from inline scripts (each matches every call unless it says otherwise)."""
    return ScriptBook(
        [LLMScript.model_validate({**s, "source": f"f08#{i}"}) for i, s in enumerate(scripts)]
    )


def text_turns(n: int, text: str = "noted") -> dict[str, Any]:
    """One script of `n` plain final text turns."""
    return {"turns": [{"final": {"text": text}}] * n}


@dataclass(frozen=True)
class Info:
    """A `ClientInfo` of the spec 05 default clients (only the fields the chain reads)."""

    gpu_class: str | None
    off_network: bool = False
    timeout_s: float = 30.0
    model: str = "scripted"
    base_url: str | None = None
    api_key: str | None = None


@dataclass
class Registry:
    """`ChainRegistry` whose `analyst` chain is `CHAIN` at every depth, unless `chains`
    names the chain of a `(model_role, depth)`."""

    infos: dict[str, Info] = field(
        default_factory=lambda: {PRIMARY: Info("reasoning"), FALLBACK: Info(None)}
    )
    chains: dict[tuple[str, str], list[str]] | None = None

    def chain_for(self, model_role: str, depth: str) -> list[str]:
        if self.chains is not None:
            return list(self.chains.get((model_role, depth), []))
        return list(CHAIN) if model_role == "analyst" else []

    def config(self, name: str) -> Info:
        return self.infos[name]


class LoopRegistry:
    """Chain and client registry of the loop's `HarnessHooks`: the `analyst` chain is the
    one in-process client `fake` (a CPU client: `llm_local` policy)."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def chain_for(self, model_role: str, depth: str) -> list[str]:
        del depth
        return ["fake"] if model_role == "analyst" else []

    def config(self, name: str) -> Info:
        del name
        return Info(None, model="small-cpu-model")

    def client(self, name: str) -> Any:
        del name
        return self._client


class Gpu:
    """`GpuStateReader`: the reasoning class is loaded and every service is healthy."""

    def loaded_class(self) -> str:
        return "reasoning"

    def service_healthy(self, name: str) -> bool:
        del name
        return True


def request(
    run_id: str, task_id: str | None, *, schema: dict[str, Any] | None = None, step: int = 0
) -> LLMRequest:
    """An analyst request of `run_id` / `task_id` (the first candidate is `PRIMARY`)."""
    return LLMRequest(
        client=PRIMARY,
        messages=[Message(role="user", parts=[TextPart(text="Summarise the incident load.")])],
        response_schema=schema,
        response_schema_name=None if schema is None else "Answer",
        max_output_tokens=64,
        timeout_s=30.0,
        metadata=RequestMeta(
            run_id=run_id,
            task_id=task_id,
            role="analyst",
            model_role="analyst",
            step=step,
            request_key=f"{task_id or run_id}:{step}:f08",
        ),
    )


def openai_client(base_url: str) -> OpenAICompatClient:
    """The real OpenAI-compatible adapter of `PRIMARY` against a loopback stub (`/v1`)."""
    cfg = ClientConfig.model_validate(
        {
            "name": PRIMARY, "kind": "openai_compat", "base_url": f"{base_url}/v1",
            "model": "qwen3-30b", "context_window": 32768, "max_output_tokens": 4096,
            "tokenizer": "estimate", "max_concurrency": 4, "price_per_mtok": _ZERO,
            "server": "vllm", "timeout_s": 30,
        }
    )  # fmt: skip
    return OpenAICompatClient(cfg)


class Recording:
    """Wraps a completer; records every request it is asked to complete. Other attributes
    are the inner client's."""

    def __init__(self, inner: Any, name: str) -> None:
        self.inner, self.name = inner, name
        self.requests: list[LLMRequest] = []

    async def acomplete(self, req: LLMRequest) -> LLMResponse:
        self.requests.append(req)
        response: LLMResponse = await self.inner.acomplete(req)
        return response

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


def configure(tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Full test config (design 08 §7 defaults), the caller's migrated `ops_store` bound as
    the resilience backend and a fixed-key test redactor; the caller resets the config."""
    use_test_config(tmp / "cfg")
    bind_ops_backend(SqliteResilienceBackend())
    directory = NameDirectory.from_files(None, (), None)
    redactor = r.Redactor(RedactionConfig(directory_file=None), bytes(range(32)), directory)
    monkeypatch.setattr(r._State, "redactor", redactor)


@contextmanager
def loop_env(tmp: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[DuckWarehouse]:
    """The IT05 loop set-up: `configure`, tmp prompts and the warehouse tools registered on
    the opened stand-in build."""
    configure(tmp, monkeypatch)
    prompts = ls.write_prompts(tmp / "prompts", monkeypatch)
    (prompts / "analyst_general.md").write_text("# Analyst\nInvestigate.\n", encoding="utf-8")
    monkeypatch.setattr(base, "_catalog_describe", list)
    wb.patch_redaction(monkeypatch)
    wt.register_warehouse_tools()
    try:
        with wb.opened(tmp / "wh") as wh:
            yield wh
    finally:
        c.reset_config()
        get_role("analyst_general").__dict__.pop("_prompts", None)  # cached tmp prompt texts


def run_loop(client: Any, ctx: ToolContext, hooks: h.HarnessHooks) -> AgentResult:
    """One `analyst_general` task through the real spec 05 loop."""
    role = get_role("analyst_general")
    return asyncio.run(
        loop.run_agent(role, {"question": "incidents?"}, ctx, client, ls.profile(), hooks)
    )


def hooks(tracer: Any, **kw: Any) -> h.HarnessHooks:
    """Real `HarnessHooks` (spec 08 policy) with no registry, gates or compactor."""
    values: dict[str, Any] = {
        "registry": None, "gates": {}, "chain": None, "compactor": None, "task_id": None,
        "phase": None, "stop": None, "on_text_delta": None, "tracer": tracer,
    }  # fmt: skip
    return h.HarnessHooks(**(values | kw))


def events(kind: str) -> list[dict[str, Any]]:
    """Stored `resilience_event` rows of `kind`: target, run/task ids and the decoded detail."""
    rows = read_all(
        "SELECT target, run_id, task_id, detail FROM resilience_event WHERE kind = ?"
        " ORDER BY ts, event_id",
        (kind,),
    )
    return [
        {"target": r["target"], "run_id": r["run_id"], "task_id": r["task_id"]}
        | json.loads(str(r["detail"]))
        for r in rows
    ]


def tool_texts(req: LLMRequest) -> list[str]:
    """Text of every tool-result part in the history of `req`, in order."""
    return [
        str(getattr(part, "content", ""))
        for message in req.messages
        if message.role == "tool"
        for part in message.parts
    ]
