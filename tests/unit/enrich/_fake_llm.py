"""A test-local scripted LLM client for the LLM decider tests (UT03-58 ... UT03-62, ST03-01).

Carry-over (impl 11 U11-42, program ruling T03-15): switch to
`tests.support.fake_llm.FakeLLMClient` once it lands (`_judge_fixtures.py` precedent).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping
from decimal import Decimal

from herness.core.types import LLMRequest, LLMResponse, Usage

type Reply = Mapping[str, object] | str | BaseException
type Script = Callable[[LLMRequest], Reply]


def response(reply: Mapping[str, object] | str, *, as_text: bool = False) -> LLMResponse:
    """An `end_turn` response; a mapping is the parsed output (text only when ``as_text``)."""
    parsed = dict(reply) if isinstance(reply, Mapping) and not as_text else None
    text = reply if isinstance(reply, str) else json.dumps(dict(reply))
    return LLMResponse.model_validate(
        {
            "text": text, "tool_calls": [], "parsed": parsed, "reasoning": [],
            "stop_reason": "end_turn", "raw_stop_reason": "stop", "refusal_category": None,
            "usage": Usage(input_tokens=10, output_tokens=5), "cost_usd": Decimal(0),
            "client": "local-decider", "model": "qwen-test", "provider": "openai_compat",
            "latency_ms": 3, "request_id": None,
        }
    )  # fmt: skip


class FakeLLMClient:
    """Scripted client: ``script(req)`` gives each reply; every request is recorded."""

    name = "local-decider"

    def __init__(self, script: Script, *, as_text: bool = False) -> None:
        self._script = script
        self._as_text = as_text
        self._lock = threading.Lock()
        self.requests: list[LLMRequest] = []

    def _reply(self, req: LLMRequest) -> LLMResponse:
        with self._lock:
            self.requests.append(req)
        reply = self._script(req)
        if isinstance(reply, BaseException):
            raise reply
        return response(reply, as_text=self._as_text)

    def complete(self, req: LLMRequest) -> LLMResponse:
        return self._reply(req)

    async def acomplete(self, req: LLMRequest) -> LLMResponse:
        return self._reply(req)

    def first_requests(self) -> list[LLMRequest]:
        """The vote requests, without the repair requests (those end with a repair turn)."""
        return [r for r in self.requests if len(r.messages) == 1]


def votes_by_seed(replies: Mapping[int, Mapping[str, object] | str]) -> Script:
    """A script answering by the request's ``seed`` (repairs keep the seed of their vote)."""

    def script(req: LLMRequest) -> Reply:
        assert req.seed is not None
        return replies[req.seed]

    return script
