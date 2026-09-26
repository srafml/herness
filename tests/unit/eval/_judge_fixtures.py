"""Shared helpers for the rubric-judge tests (U11-61): a local scripted LLM client.

Carry-over: switch to `tests.support.fake_llm.FakeLLMClient` (U11-42) once it lands.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Sequence
from decimal import Decimal
from pathlib import Path

from herness.core.types import LLMRequest, LLMResponse, Usage
from herness.eval.judge import RubricJudge
from herness.harness.llm.settings import ClientConfig
from herness.harness.tracing import Tracer

MODEL = "judge-model-a"
CRITERIA = ("clarity", "actionability")

type Reply = dict[str, object] | str | Exception


def client_cfg(name: str = "local-judge", model: str = MODEL) -> ClientConfig:
    """A loopback `openai_compat` judge client entry."""
    zero = {"input": "0", "output": "0", "cache_read": "0", "cache_write": "0"}
    return ClientConfig.model_validate(
        {
            "name": name, "kind": "openai_compat", "base_url": "http://127.0.0.1:8001/v1",
            "model": model, "context_window": 32_768, "max_output_tokens": 4_096,
            "tokenizer": "estimate", "max_concurrency": 4, "price_per_mtok": zero,
        }
    )  # fmt: skip


def response(reply: dict[str, object] | str, model: str = MODEL) -> LLMResponse:
    """An `end_turn` response; a dict is the parsed output, a str is raw text only."""
    parsed = reply if isinstance(reply, dict) else None
    text = json.dumps(reply) if isinstance(reply, dict) else reply
    return LLMResponse.model_validate(
        {
            "text": text, "tool_calls": [], "parsed": parsed, "reasoning": [],
            "stop_reason": "end_turn", "raw_stop_reason": "stop", "refusal_category": None,
            "usage": Usage(input_tokens=10, output_tokens=5), "cost_usd": Decimal(0),
            "client": "local-judge", "model": model, "provider": "openai_compat",
            "latency_ms": 7, "request_id": None,
        }
    )  # fmt: skip


def scores(value: int = 4, criteria: Sequence[str] = CRITERIA) -> dict[str, object]:
    """A judge reply giving `value` to every criterion."""
    return {"scores": dict.fromkeys(criteria, value), "rationale": "clear and actionable"}


class FakeJudgeClient:
    """Scripted `LLMClient`: serves `replies` in order, the last one repeating; records calls."""

    name = "local-judge"

    def __init__(self, *replies: Reply) -> None:
        self._replies = list(replies) or [scores()]
        self._lock = threading.Lock()
        self.requests: list[LLMRequest] = []

    @property
    def calls(self) -> int:
        return len(self.requests)

    def complete(self, req: LLMRequest) -> LLMResponse:
        with self._lock:
            index = len(self.requests)
            self.requests.append(req)
        reply = self._replies[min(index, len(self._replies) - 1)]
        if isinstance(reply, Exception):
            raise reply
        return response(reply)

    async def acomplete(self, req: LLMRequest) -> LLMResponse:
        return self.complete(req)


def make_judge(
    client: FakeJudgeClient,
    cache_dir: Path,
    *,
    redact: Callable[[str], str | None] = lambda text: text,
    tracer: Tracer | None = None,
    model: str = MODEL,
) -> RubricJudge:
    """A judge over `client` with temperature 0."""
    return RubricJudge(
        client,
        client_cfg(model=model),
        cache_dir=cache_dir,
        temperature=0.0,
        tracer=tracer,
        redact=redact,
    )


def prompt_of(req: LLMRequest) -> str:
    """The rendered judge prompt of a request (its only system block)."""
    assert len(req.system) == 1
    return req.system[0].text
