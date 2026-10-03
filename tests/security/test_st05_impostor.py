"""Security test ST05-20 (TH05-20): an impostor model server on a loopback port (T05-27).

A process on the vLLM port answers with crafted responses. `TamperingLLMServer`
(`tests.support.impostor_llm`, a real loopback `FakeLLMServer`) serves them to the real
`OpenAICompatClient` over the real egress loopback client: a 5 MB text is an
`OutputValidationError` (plain and streamed), a served model name other than the configured
one is accepted with the `harness.llm.model_mismatch` warning, and tool arguments that are not
a JSON object are an `OutputValidationError`. The redirect, gzip and wrapped-refusal cases of
ST05-20 live in tests/unit/harness/test_llm_openai_compat.py.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs
from tests.support.dispatch_standin import use_test_config
from tests.support.impostor_llm import TamperingLLMServer

from herness.core import config as c
from herness.core.errors import OutputValidationError
from herness.core.types import LLMRequest, LLMResponse, Message, RequestMeta, TextPart
from herness.eval.scripted import LLMScript, ScriptBook
from herness.harness.llm.base import MAX_RESPONSE_TEXT_CHARS, Done
from herness.harness.llm.openai_compat import OpenAICompatClient
from herness.harness.llm.settings import ClientConfig

pytestmark = pytest.mark.unit

FIVE_MB = 5 * 1024 * 1024
_ZERO = {"input": "0", "output": "0", "cache_read": "0", "cache_write": "0"}
_TEXT_TURN = {"final": {"text": "an ordinary answer"}}
_TOOL_TURN = {"tool_calls": [{"name": "run_sql", "arguments": {"sql": "SELECT 1"}}]}


@pytest.fixture(autouse=True)
def _config(tmp_path: Path) -> Iterator[None]:
    use_test_config(tmp_path / "cfg")
    yield
    c.reset_config()


def _book(*turns: dict[str, Any]) -> ScriptBook:
    return ScriptBook([LLMScript.model_validate({"turns": list(turns), "source": "st05_20#0"})])


def _cfg(server: TamperingLLMServer) -> ClientConfig:
    return ClientConfig.model_validate(
        {
            "name": "local-30b", "kind": "openai_compat", "base_url": f"{server.base_url}/v1",
            "model": "qwen3-30b", "context_window": 32768, "max_output_tokens": 4096,
            "tokenizer": "estimate", "max_concurrency": 4, "price_per_mtok": _ZERO,
            "server": "vllm",
        }
    )  # fmt: skip


def _request() -> LLMRequest:
    meta = RequestMeta(
        run_id="run_1", task_id="task_1", role="analyst", model_role="analyst", step=0,
        request_key="task_1:0:step",
    )  # fmt: skip
    return LLMRequest(
        client="local-30b",
        messages=[Message(role="user", parts=[TextPart(text="How is Payments?")])],
        max_output_tokens=512,
        timeout_s=30,
        metadata=meta,
    )


def _complete(server: TamperingLLMServer) -> LLMResponse:
    return asyncio.run(OpenAICompatClient(_cfg(server)).acomplete(_request()))


def _five_mb_text(body: dict[str, Any]) -> dict[str, Any]:
    body["choices"][0]["message"]["content"] = "a" * FIVE_MB
    return body


def _impostor_model(body: dict[str, Any]) -> dict[str, Any]:
    body["model"] = "impostor-model"
    return body


def _array_arguments(body: dict[str, Any]) -> dict[str, Any]:
    body["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = json.dumps([1, 2])
    return body


def test_st05_20_five_mb_text_is_output_validation_error() -> None:
    """ST05-20 the impostor answers with a 5 MB text: `OutputValidationError` (the text cap is
    1,000,000 chars), and the error carries none of the text."""
    assert FIVE_MB > MAX_RESPONSE_TEXT_CHARS
    with (
        TamperingLLMServer(_book(_TEXT_TURN), tamper=_five_mb_text) as server,
        pytest.raises(OutputValidationError, match=r"^response text exceeds limit$") as info,
    ):
        _complete(server)
    assert info.value.context["client"] == "local-30b"
    assert "aaaa" not in str(info.value)


def test_st05_20_five_mb_streamed_text_is_output_validation_error() -> None:
    """ST05-20 the same 5 MB text streamed in 1 MB deltas: `OutputValidationError` while the
    buffer grows, before any `Done`."""
    events: list[object] = []

    async def collect(server: TamperingLLMServer) -> None:
        stream = OpenAICompatClient(_cfg(server)).astream(_request())
        async for event in stream:
            events.append(event)  # noqa: PERF401 - kept as they arrive, before the error

    with (
        TamperingLLMServer(_book(_TEXT_TURN), stream_text="a" * FIVE_MB) as server,
        pytest.raises(OutputValidationError, match=r"^response text exceeds limit$"),
    ):
        asyncio.run(collect(server))
    assert not any(isinstance(event, Done) for event in events)
    assert sum(len(getattr(event, "text", "")) for event in events) <= MAX_RESPONSE_TEXT_CHARS


def test_st05_20_wrong_model_name_logs_model_mismatch_warning() -> None:
    """ST05-20 the impostor names another model: the response is mapped and the WARNING
    `harness.llm.model_mismatch` names the client, the expected and the served model."""
    with (
        TamperingLLMServer(_book(_TEXT_TURN), tamper=_impostor_model) as server,
        capture_logs() as logs,
    ):
        response = _complete(server)
    assert (response.model, response.text) == ("impostor-model", "an ordinary answer")
    warnings = [e for e in logs if e["event"] == "harness.llm.model_mismatch"]
    assert len(warnings) == 1
    event = warnings[0]
    assert (event["log_level"], event["client"], event["expected"], event["actual"]) == (
        "warning",
        "local-30b",
        "qwen3-30b",
        "impostor-model",
    )


def test_st05_20_non_object_tool_arguments_are_output_validation_error() -> None:
    """ST05-20 the impostor sends tool-call arguments that are a JSON array, not an object:
    `OutputValidationError`; the arguments are never handed on."""
    with TamperingLLMServer(_book(_TOOL_TURN), tamper=_array_arguments) as server:
        with pytest.raises(OutputValidationError, match="arguments are not a JSON object"):
            _complete(server)
        assert server.completed == 1


def test_st05_20_untampered_server_is_accepted() -> None:
    """ST05-20 control: the same scripted turns without tampering map cleanly, no warning."""
    with TamperingLLMServer(_book(_TOOL_TURN, _TEXT_TURN)) as server, capture_logs() as logs:
        first = _complete(server)
        second = _complete(server)
    assert [call.arguments for call in first.tool_calls] == [{"sql": "SELECT 1"}]
    assert second.text == "an ordinary answer"
    assert not [e for e in logs if e["event"] == "harness.llm.model_mismatch"]
    assert first.cost_usd == Decimal(0)
