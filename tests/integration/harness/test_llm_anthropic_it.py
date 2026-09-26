"""Integration test for AnthropicClient against the real Messages API (IT05-09, U05-28).

Opt-in only: runs when ``HERNESS_ANTHROPIC_IT=1`` with the repository config, the ``hybrid``
profile, the ``secret:anthropic.api_key`` secret and the real egress guard (its
``async_http_client`` lands with T10-17). One small Haiku request: at most $0.50.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import JsonValue

from herness.core import config as c
from herness.core.types import LLMRequest, Message, RequestMeta, TextPart, ToolSpec
from herness.harness.llm.anthropic_client import AnthropicClient

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("HERNESS_ANTHROPIC_IT") != "1", reason="set HERNESS_ANTHROPIC_IT=1"
    ),
]

ROOT = Path(__file__).resolve().parents[3]
_MAX_COST = Decimal("0.50")


@pytest.fixture
def hybrid_config() -> Iterator[c.HernessConfig]:
    c.reset_config()
    yield c.init_config("hybrid", config_dir=ROOT / "config")
    c.reset_config()


def test_it05_09_real_request_with_tools_and_schema(hybrid_config: c.HernessConfig) -> None:
    """IT05-09 one real request with a tool and a schema: valid response, cost recorded."""
    cfg = hybrid_config.models.models.clients["claude-haiku"]
    client = AnthropicClient(cfg)
    schema: dict[str, JsonValue] = {
        "type": "object",
        "properties": {"answer": {"type": "integer"}},
        "required": ["answer"],
        "additionalProperties": False,
    }
    tool = ToolSpec(
        name="get_metric",
        description="Return a named metric value.",
        input_schema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
    )
    req = LLMRequest(
        client=cfg.name,
        messages=[Message(role="user", parts=[TextPart(text="What is 2 + 3? Answer in JSON.")])],
        tools=[tool],
        response_schema=schema,
        response_schema_name="Sum",
        max_output_tokens=512,
        thinking="off",
        timeout_s=60.0,
        metadata=RequestMeta(
            run_id="run_it0509",
            task_id=None,
            role="analyst",
            model_role="writer",
            step=0,
            request_key="run_it0509:0:step",
        ),
    )
    resp = asyncio.run(client.acomplete(req))
    assert resp.provider == "anthropic"
    assert resp.stop_reason in {"end_turn", "tool_use"}
    assert resp.usage.input_tokens > 0
    assert Decimal(0) < resp.cost_usd <= _MAX_COST
    if resp.stop_reason == "end_turn":
        assert resp.parsed == {"answer": 5}
    else:
        assert all(call.name == "get_metric" for call in resp.tool_calls)
