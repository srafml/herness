"""Shared helpers for the chat session memory tests (T07-21): chat seeds, fakes, deps.

Not a test module. Chat rows are written through the spec 09 area functions on the migrated
`ops_store` of the test; the writer is the real `MemoryWriter` of `_write_env`; model calls go
through the T11-23 `FakeLLMClient` over an in-code `ScriptBook` that records every request.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

from tests.support.fake_llm import FakeLLMClient
from tests.support.harness_fakes import RecordingTracer
from tests.unit.harness.memory._compactor_support import profile
from tests.unit.harness.memory._write_env import AUTHOR, NOW, Env, make_writer

from herness.core.ids import new_ulid
from herness.core.resilience import TracerLike
from herness.core.types import LLMRequest, LLMResponse
from herness.eval.scripted import LLMScript, ScriptBook, ScriptFault, ScriptTurn
from herness.harness.llm.base import LLMClient
from herness.harness.llm.settings import ClientConfig
from herness.harness.memory.chat import ChatDeps
from herness.harness.memory.settings import ChatMemoryConfig
from herness.store.ops import chat

CLIENT = "local-30b"
# Years, ISO dates, quarters and query ids (the shape of reports.allowed_numeral_patterns).
ALLOWED = (
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(r"\b(?:19|20)\d{2}-Q[1-4]\b"),
    re.compile(r"\b(?:19|20)\d{2}\b"),
    re.compile(r"\bq_[0-9a-f]{16}\b"),
)


def correction(confidence: float = 0.8, **fields: Any) -> dict[str, Any]:
    """A classification output (a correction unless overridden)."""
    out: dict[str, Any] = {
        "is_correction": True,
        "statement": "The payments service is owned by the platform team.",
        "entities": [{"type": "service", "id": "svc_payments"}],
        "effective_date": None,
        "suggested_action": "mapping_suggestion",
        "confidence": confidence,
    }
    return out | fields


NOT_CORRECTION = correction(0.95, is_correction=False, statement="")


class ChatLLM(FakeLLMClient):
    """The fake over JSON outputs (or faults) in call order; records every request."""

    def __init__(self, *outputs: dict[str, Any], faults: Sequence[ScriptFault] = ()) -> None:
        turns = [ScriptTurn(final={"output": out}) for out in outputs] or [
            ScriptTurn(final={"output": {}})
        ]
        super().__init__(ScriptBook([LLMScript(turns=turns, faults=list(faults), source="t#0")]))
        self.requests: list[LLMRequest] = []
        self.refuse = False
        self.delay_s = 0.0

    async def acomplete(self, req: LLMRequest) -> LLMResponse:
        self.requests.append(req)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        resp = await super().acomplete(req)
        if self.refuse:
            return resp.model_copy(update={"stop_reason": "refusal", "refusal_category": "cyber"})
        return resp

    def bodies(self, schema_name: str) -> list[str]:
        """The user text of every request with the given response schema name."""
        return [
            r.messages[0].parts[0].text  # type: ignore[union-attr]
            for r in self.requests
            if r.response_schema_name == schema_name
        ]


@dataclass
class Models:
    """`ChatModels` stub: every role routes to one client."""

    llm: LLMClient
    config_: ClientConfig = field(default_factory=profile)
    asked: list[tuple[str, str]] = field(default_factory=list)

    def model_for(self, model_role: str, depth: str) -> str:
        self.asked.append((model_role, depth))
        return CLIENT

    def client(self, name: str) -> LLMClient:
        assert name == CLIENT
        return self.llm

    def config(self, name: str) -> ClientConfig:
        assert name == CLIENT
        return self.config_


@dataclass
class ChatEnv:
    """Deps plus handles on the writer env, the fake model and the tracer."""

    deps: ChatDeps
    env: Env
    llm: ChatLLM
    tracer: RecordingTracer
    models: Models


def make_deps(
    tmp_path: Path,
    llm: ChatLLM,
    *,
    cfg: ChatMemoryConfig | None = None,
    config_: ClientConfig | None = None,
) -> ChatEnv:
    """`ChatDeps` over the real writer, the fake model and a recording tracer."""
    env = make_writer(tmp_path)
    tracer = RecordingTracer("run_x", None)
    models = Models(llm, config_ or profile())
    deps = ChatDeps(
        cfg=cfg or ChatMemoryConfig(),
        llms=models,
        writer=env.writer,
        redactor=env.redactor,
        allowed=ALLOWED,
        tracer=cast("TracerLike", tracer),  # its emit is positional-only
    )
    return ChatEnv(deps, env, llm, tracer, models)


class Clock:
    """Strictly increasing message times, one second apart."""

    def __init__(self) -> None:
        self.n = 0

    def __call__(self) -> Any:
        self.n += 1
        return NOW + timedelta(seconds=self.n)


def new_run() -> str:
    return "run_" + new_ulid()


def session(user_ref: str = AUTHOR) -> str:
    return chat.create_chat_session(user_ref, now=NOW)


def user_msg(session_id: str, text: str, tick: Clock) -> str:
    message_id = chat.append_chat_message(session_id, "user", text, now=tick())
    assert message_id is not None
    return message_id


def answer(
    session_id: str, reply_to: str, tick: Clock, *, run_id: str, text: str = "an answer"
) -> str:
    """A done assistant row of `run_id` answering `reply_to`."""
    row = chat.upsert_assistant_placeholder(session_id, reply_to=reply_to, now=tick())
    assert row is not None
    assert chat.update_chat_message(row["message_id"], status="done", content=text, run_id=run_id)
    return row["message_id"]


def turn(session_id: str, text: str, tick: Clock, *, reply: str = "an answer") -> tuple[str, str]:
    """One user message and its done answer: (user message id, run id)."""
    run_id = new_run()
    message_id = user_msg(session_id, text, tick)
    answer(session_id, message_id, tick, run_id=run_id, text=reply)
    return message_id, run_id
