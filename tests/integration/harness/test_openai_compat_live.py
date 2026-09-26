"""IT05-08: OpenAICompatClient against a real vLLM or Ollama server (U05-25).

Skipped unless ``HERNESS_LLM_URL`` is set (a loopback ``.../v1`` URL). ``HERNESS_LLM_MODEL``
names the served model (default ``qwen3-30b``) and ``HERNESS_LLM_SERVER`` the server kind
(``vllm`` or ``ollama``, default ``vllm``). The server must run with tool calling enabled.
"""

from __future__ import annotations

import asyncio
import os
from decimal import Decimal

import pytest

from herness.core.types import LLMRequest, Message, RequestMeta, TextPart, ToolSpec
from herness.harness.llm.openai_compat import OpenAICompatClient
from herness.harness.llm.settings import ClientConfig, PricePerMTok

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("HERNESS_LLM_URL"), reason="HERNESS_LLM_URL not set"),
]

_ZERO = Decimal("0")


def _client() -> OpenAICompatClient:
    cfg = ClientConfig(
        name="live",
        kind="openai_compat",
        base_url=os.environ["HERNESS_LLM_URL"],
        model=os.environ.get("HERNESS_LLM_MODEL", "qwen3-30b"),
        context_window=32768,
        max_output_tokens=1024,
        tokenizer="estimate",
        max_concurrency=1,
        server=os.environ.get("HERNESS_LLM_SERVER", "vllm"),  # type: ignore[arg-type]
        price_per_mtok=PricePerMTok(input=_ZERO, output=_ZERO, cache_read=_ZERO, cache_write=_ZERO),
    )
    return OpenAICompatClient(cfg)


def _request(**updates: object) -> LLMRequest:
    data: dict[str, object] = {
        "client": "live",
        "messages": [Message(role="user", parts=[TextPart(text="How many rows has table t?")])],
        "max_output_tokens": 512,
        "thinking": "off",
        "timeout_s": 120.0,
        "metadata": RequestMeta(
            run_id="it",
            task_id=None,
            role="planner",
            model_role="planner",
            step=0,
            request_key="it:0:loop",
        ),
    }
    data.update(updates)
    return LLMRequest(**data)  # type: ignore[arg-type]


def test_it05_08_live_tool_call_and_schema_output() -> None:
    """IT05-08 a real server returns valid tool calls and schema output."""
    client = _client()
    tool = ToolSpec(
        name="count_rows",
        description="Count the rows of a table.",
        input_schema={
            "type": "object",
            "properties": {"table": {"type": "string"}},
            "required": ["table"],
        },
    )
    tool_resp = asyncio.run(client.acomplete(_request(tools=[tool])))
    assert tool_resp.tool_calls, "expected at least one tool call"
    assert all(call.name == "count_rows" for call in tool_resp.tool_calls)
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    }
    schema_resp = asyncio.run(
        client.acomplete(_request(response_schema=schema, response_schema_name="Answer"))
    )
    assert schema_resp.parsed is not None
    assert isinstance(schema_resp.parsed.get("answer"), str)
