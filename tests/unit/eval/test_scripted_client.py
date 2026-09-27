"""Tests for herness.eval.scripted_client: ScriptedLLMClient and ScriptedRegistry (T11-22).

UT11-68 (U11-40 half) drives `complete`, `acomplete` and `astream` over tool, output and
fault turns and checks that no tokenizer endpoint or HTTP client is ever reached; UT11-69
covers `ops_dedup_key_resolver` over a tmp ops store and the `ScriptedRegistry` wrapper.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest

from herness.core import egress
from herness.core.errors import ModelUnavailable, OutputValidationError, RateLimited
from herness.core.types import (
    LLMRequest,
    Message,
    RequestMeta,
    SystemBlock,
    TaskSpec,
    TextPart,
    ToolResultPart,
)
from herness.eval import scripted_client as sc
from herness.eval.scripted import LLMScript, ScriptBook, ScriptMismatch
from herness.eval.scripted_client import ScriptedLLMClient, ScriptedRegistry, ops_dedup_key_resolver
from herness.harness.llm import tokens
from herness.harness.llm.base import Done, LLMClient, StreamCapable, TextDelta, ToolCallDelta
from herness.harness.llm.registry import LLMRegistry
from herness.harness.llm.settings import ModelsConfig
from herness.harness.llm.tokens import estimate_tokens
from herness.store.ops import core, runs
from herness.store.ops.runs import RunRow

pytestmark = pytest.mark.unit

_QID = "q_00000000000000aa"
_TABLE = (
    f"query_id={_QID} rows=1 shown=1 truncated=no ordered=yes\n"
    "team_name | incidents | mttr_hours\n"
    "VARCHAR | BIGINT | DOUBLE\n"
    "Payments | 42 | 7.5"
)
_LONG_TEXT = "The scripted analyst found that Payments needs attention now."


def _book(*scripts: dict[str, Any]) -> ScriptBook:
    return ScriptBook(
        [LLMScript.model_validate({**s, "source": f"t#{i}"}) for i, s in enumerate(scripts)]
    )


def _scripts() -> list[dict[str, Any]]:
    return [
        {
            "match": {"role": "analyst"},
            "turns": [
                {"tool_calls": [{"name": "run_sql", "arguments": {"sql": "select 1"}}]},
                {
                    "final": {
                        "output": {
                            "text": "{{row.team_name}} had [[n1]] incidents",
                            "numbers_from": "last_tool_result",
                        }
                    }
                },
                {"final": {"text": _LONG_TEXT}},
            ],
        },
        {
            "match": {"role": "faulty"},
            "turns": [{"tool_calls": [{"name": "run_sql", "arguments": {}}]}],
            "faults": [
                {"at": 0, "kind": "http_500"},
                {"at": 1, "kind": "http_429"},
                {"at": 2, "kind": "hang"},
                {"at": 3, "kind": "disconnect"},
                {"at": 4, "kind": "malformed_json"},
            ],
        },
        {
            "match": {"role": "writer"},
            "turns": [{"final": {"text": "report"}}],
            "faults": [{"at": 0, "kind": "malformed_json"}],
        },
        {"match": {"role": "gap"}, "turns": [{"final": {"text": "{{row.team_name}}"}}]},
        {"match": {"model_role": "eval_judge", "dedup_key": "q07"}, "turns": [_final("judged")]},
        {"match": {"dedup_key": "chat"}, "turns": [_final("chatted")]},
        {"match": {"dedup_key": "00000000000000ab"}, "turns": [_final("task")]},
        {"match": {"dedup_key": "*"}, "turns": [_final("any")] * 5},
    ]


def _final(text: str) -> dict[str, Any]:
    return {"final": {"text": text}}


def _meta(role: str = "analyst", **over: Any) -> RequestMeta:
    values: dict[str, Any] = {
        "run_id": "run_1",
        "task_id": None,
        "role": role,
        "model_role": role,
        "step": 0,
        "request_key": "run_1:0:call",
    }
    values.update(over)
    return RequestMeta.model_validate(values)


def _req(meta: RequestMeta | None = None, *extra: Message) -> LLMRequest:
    return LLMRequest(
        client="local-30b",
        system=[SystemBlock(text="You are an analyst.")],
        messages=[Message(role="user", parts=[TextPart(text="How is Payments?")]), *extra],
        max_output_tokens=512,
        timeout_s=30,
        metadata=meta or _meta(),
    )


def _tool_msg() -> Message:
    return Message(role="tool", parts=[ToolResultPart(tool_call_id="call_0_0", content=_TABLE)])


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record any tokenizer endpoint or HTTP client construction (none may happen)."""
    reached: list[str] = []

    def _trap(name: str) -> Callable[..., Any]:
        def _fail(*_a: Any, **_k: Any) -> Any:
            reached.append(name)
            raise AssertionError(name)

        return _fail

    monkeypatch.setattr(tokens, "_vllm_count", _trap("vllm_tokenize"))
    monkeypatch.setattr(tokens, "_anthropic_count", _trap("anthropic_count_tokens"))
    monkeypatch.setattr(egress, "loopback_http_client", _trap("loopback_http_client"))
    monkeypatch.setattr(egress, "aloopback_http_client", _trap("aloopback_http_client"))
    monkeypatch.setattr(egress, "get_guard", _trap("egress_guard"))
    # traps only: construction of any HTTP client fails the test
    monkeypatch.setattr(httpx.Client, "__init__", _trap("httpx.Client"))  # noqa: TID251
    monkeypatch.setattr(httpx.AsyncClient, "__init__", _trap("httpx.AsyncClient"))  # noqa: TID251
    return reached


# ---------------------------------------------------------------- UT11-68 ScriptedLLMClient


def test_ut11_68_is_llm_client_and_stream_capable() -> None:
    """UT11-68 the scripted client satisfies the LLMClient and StreamCapable protocols."""
    client = ScriptedLLMClient(_book(*_scripts()))
    assert client.name == "fake"
    assert isinstance(client, LLMClient)
    assert isinstance(client, StreamCapable)


def test_ut11_68_complete_tool_then_output_turn(no_network: list[str]) -> None:
    """UT11-68 tool turn then output turn: fields per the U11-40 response rules."""
    client = ScriptedLLMClient(_book(*_scripts()), name="scripted-a")
    first = client.complete(_req())
    assert first.stop_reason == "tool_use"
    assert first.raw_stop_reason == "tool_use"
    assert [(c.id, c.name, c.arguments) for c in first.tool_calls] == [
        ("call_0_0", "run_sql", {"sql": "select 1"})
    ]
    assert (first.text, first.parsed, first.reasoning) == ("", None, [])
    assert (first.client, first.model, first.provider) == (
        "scripted-a",
        "scripted",
        "openai_compat",
    )
    assert (first.latency_ms, first.request_id, first.batch) == (0, "fake-0", False)
    assert first.cost_usd == Decimal("0")
    assert first.refusal_category is None
    req = _req(None, _tool_msg())
    expected_in = estimate_tokens(req.messages, (), req.system)
    assert first.usage.input_tokens == estimate_tokens(_req().messages, (), _req().system)
    assert first.usage.output_tokens == 0

    second = client.complete(req)
    assert second.stop_reason == "end_turn"
    assert second.request_id == "fake-1"
    assert second.parsed is not None
    assert second.parsed["text"] == "Payments had [[n1]] incidents"
    numbers = second.parsed["numbers"]
    assert isinstance(numbers, list)
    assert numbers[0] == {
        "id": "n1",
        "value": 42,
        "unit": "count",
        "query_id": _QID,
        "column": "incidents",
        "row_key": None,
        "format": None,
    }
    assert second.parsed["query_ids"] == [_QID]
    assert second.text.startswith('{"numbers":')
    out_msg = Message(role="assistant", parts=[TextPart(text=second.text)])
    assert second.usage.input_tokens == expected_in
    assert second.usage.output_tokens == estimate_tokens([out_msg])
    assert no_network == []


def test_ut11_68_acomplete_matches_complete() -> None:
    """UT11-68 `acomplete` returns what `complete` returns for the same script position."""
    sync_resp = ScriptedLLMClient(_book(*_scripts())).complete(_req())
    async_resp = asyncio.run(ScriptedLLMClient(_book(*_scripts())).acomplete(_req()))
    assert async_resp == sync_resp


def test_ut11_68_faults_in_process() -> None:
    """UT11-68 http_500/hang/disconnect → ModelUnavailable, http_429 → RateLimited(1.0)."""
    client = ScriptedLLMClient(_book(*_scripts()))
    req = _req(_meta("faulty"))
    with pytest.raises(ModelUnavailable):
        client.complete(req)
    with pytest.raises(RateLimited) as limited:
        client.complete(req)
    assert limited.value.retry_after == 1.0
    with pytest.raises(ModelUnavailable):
        client.complete(req)
    with pytest.raises(ModelUnavailable):
        client.complete(req)
    with pytest.raises(OutputValidationError):  # malformed_json on a tool-call turn
        client.complete(req)
    after = client.complete(req)  # the turn pointer did not move during the faults
    assert after.tool_calls[0].name == "run_sql"
    assert after.request_id == "fake-5"


def test_ut11_68_malformed_json_on_text_turn_returns_truncated_text() -> None:
    """UT11-68 malformed_json on a non-tool turn returns `{"truncated": ` with parsed None."""
    client = ScriptedLLMClient(_book(*_scripts()))
    bad = client.complete(_req(_meta("writer")))
    assert bad.text == '{"truncated": '
    assert bad.parsed is None
    assert bad.tool_calls == []
    assert bad.stop_reason == "end_turn"
    assert client.complete(_req(_meta("writer"))).text == "report"


def test_ut11_68_malformed_json_after_exhaustion_is_not_a_tool_turn() -> None:
    """UT11-68 a malformed_json fault past the last turn is served as truncated text."""
    book = _book({"turns": [_final("one")], "faults": [{"at": 1, "kind": "malformed_json"}]})
    client = ScriptedLLMClient(book)
    assert client.complete(_req()).text == "one"
    assert client.complete(_req()).text == '{"truncated": '


def test_ut11_68_astream_ends_with_one_done_equal_to_acomplete(no_network: list[str]) -> None:
    """UT11-68 stream: 16-char TextDeltas, one ToolCallDelta per call, then one Done."""

    async def _collect(client: ScriptedLLMClient, req: LLMRequest) -> list[Any]:
        return [event async for event in client.astream(req)]

    streamed = ScriptedLLMClient(_book(*_scripts()))
    plain = ScriptedLLMClient(_book(*_scripts()))
    for req in (_req(), _req(None, _tool_msg()), _req()):
        events = asyncio.run(_collect(streamed, req))
        expected = asyncio.run(plain.acomplete(req))
        assert isinstance(events[-1], Done)
        assert sum(isinstance(e, Done) for e in events) == 1
        assert events[-1].response == expected
        texts = [e.text for e in events if isinstance(e, TextDelta)]
        assert "".join(texts) == expected.text
        assert all(len(t) == 16 for t in texts[:-1])
        assert all(0 < len(t) <= 16 for t in texts)
        calls = [e for e in events if isinstance(e, ToolCallDelta)]
        assert [(c.id, c.name) for c in calls] == [(c.id, c.name) for c in expected.tool_calls]
        assert all(c.arguments_json_fragment.startswith("{") for c in calls)
    assert no_network == []


def test_ut11_68_complete_inside_running_loop_raises() -> None:
    """UT11-68 `complete` refuses to run inside a running event loop (spec 05)."""
    client = ScriptedLLMClient(_book(*_scripts()))

    async def _inner() -> None:
        client.complete(_req())

    with pytest.raises(RuntimeError, match="running event loop"):
        asyncio.run(_inner())
    assert client._book.calls("analyst", "*") == 0


def test_ut11_68_script_mismatch_propagates_with_call_identity() -> None:
    """UT11-68 no matching script, and a template gap, raise ScriptMismatch with the call."""
    client = ScriptedLLMClient(_book(*_scripts()[:4]))
    with pytest.raises(ScriptMismatch) as none:
        client.complete(_req(_meta("nobody")))
    assert (none.value.role, none.value.dedup_key) == ("nobody", "*")
    assert len(none.value.prompt_hash) == 16
    with pytest.raises(ScriptMismatch) as gap:
        client.complete(_req(_meta("gap")))
    assert (gap.value.role, gap.value.model_role, gap.value.dedup_key) == ("gap", "gap", "*")
    assert gap.value.call_index == 0


def test_ut11_68_dedup_key_rules() -> None:
    """UT11-68 judge id from request_key, resolver for task ids, `chat`, else `*`."""
    seen: list[str | None] = []

    def resolver(task_id: str | None) -> str | None:
        seen.append(task_id)
        return "00000000000000ab" if task_id == "task_known" else None

    book = _book(*_scripts()[4:])
    client = ScriptedLLMClient(book, dedup_key_resolver=resolver)
    judge = _meta("judge", model_role="eval_judge", request_key="judge:q07:deadbeef")
    assert client.complete(_req(judge)).text == "judged"
    assert client.complete(_req(_meta("chat"))).text == "chatted"
    assert client.complete(_req(_meta("analyst", task_id="task_known"))).text == "task"
    assert client.complete(_req(_meta("analyst", task_id="task_other"))).text == "any"
    odd = _meta("judge", model_role="eval_judge", request_key="not-a-judge-key")
    assert client.complete(_req(odd)).text == "any"
    assert seen == ["task_known", "task_other"]
    assert book.calls("judge", "q07") == 1
    assert book.calls("chat", "chat") == 1
    assert book.calls("analyst", "00000000000000ab") == 1
    assert book.calls("analyst", "*") == 1
    assert book.calls("judge", "*") == 1


def test_ut11_68_request_meta_dedup_key_field_wins() -> None:
    """UT11-68 once DD11-02 adds `RequestMeta.dedup_key`, that field is used first."""
    meta = _meta("analyst", task_id="task_known")
    object.__setattr__(meta, "dedup_key", "chat")  # simulate the DD11-02 field
    client = ScriptedLLMClient(_book(*_scripts()[4:]), dedup_key_resolver=lambda _t: "nope")
    assert client.complete(_req(meta)).text == "chatted"


def test_ut11_68_estimator_config_is_offline() -> None:
    """UT11-68 the token count config names the offline estimator, never a tokenizer endpoint."""
    assert sc._ESTIMATE_CFG.tokenizer == "estimate"
    assert sc._ESTIMATE_CFG.kind == "openai_compat"
    assert not sc._ESTIMATE_CFG.off_network


# ---------------------------------------------------------------- UT11-69 registry + resolver

_ZERO = {"input": "0", "output": "0", "cache_read": "0", "cache_write": "0"}


def _inner_registry() -> LLMRegistry:
    client = {
        "kind": "openai_compat",
        "base_url": "http://127.0.0.1:8000/v1",
        "model": "local-30b",
        "context_window": 32768,
        "max_output_tokens": 4096,
        "tokenizer": "estimate",
        "max_concurrency": 4,
        "price_per_mtok": dict(_ZERO),
    }
    cfg = ModelsConfig.model_validate(
        {
            "models": {
                "clients": {"local-30b": client, "local-large": {**client, "model": "large"}},
                "roles": {"planner": "local-30b", "writer": "local-30b"},
                "fallback": {"writer": ["local-30b", "local-large"]},
                "depth_overrides": {"deep": {"roles": {"planner": "local-large"}}},
            },
            "harness": {},
        }
    )
    return LLMRegistry(cfg, profile="test", egress_enabled=False)


def test_ut11_69_registry_routes_every_key_to_the_scripted_client() -> None:
    """UT11-69 `client` is always the scripted client; config and routing delegate to inner."""
    inner = _inner_registry()
    scripted = ScriptedLLMClient(_book(*_scripts()))
    registry = ScriptedRegistry(inner, scripted)
    assert registry.client("local-30b") is scripted
    assert registry.client("local-large") is scripted
    assert registry.client("claude-opus") is scripted
    assert registry.chain_for("writer", "standard") == ["local-30b", "local-large"]
    assert registry.chain_for("writer", "standard") == inner.chain_for("writer", "standard")
    assert registry.model_for("planner", "deep") == "local-large"
    assert registry.config("local-30b") == inner.config("local-30b")
    assert registry.role_params("writer") is None


def _write(fn: Callable[[sqlite3.Connection], Any]) -> Any:
    return core.run_write(fn, op="test_scripted_client")


def test_ut11_69_ops_resolver_reads_task_dedup_key(ops_store: object) -> None:
    """UT11-69 the resolver returns the task row's `spec.dedup_key`; missing task → None."""
    run_id = "run_01J00000000000000000000000"
    task_id = "task_01J00000000000000000000001"
    now = datetime(2026, 9, 26, 10, 0, tzinfo=UTC)
    run = RunRow(
        run_id=run_id,
        kind="funding_review",
        depth="standard",
        profile="default",
        build_id="b_1",
        status="created",
        started_at=now,
        finished_at=None,
        token_usage={},
        cost_usd=Decimal("0"),
        config_hash="c" * 16,
        meta={},
    )
    spec = TaskSpec.model_validate(
        {
            "task_id": task_id,
            "run_id": run_id,
            "role": "analyst",
            "objective": "Assess delivery",
            "scope": {"entity_type": "team", "entity_ids": ["t1"]},
            "tools": ["run_sql"],
            "budget": {"max_steps": 5, "max_tokens": 1_000, "wall_clock_s": 60},
            "model_role": "analyst",
            "dedup_key": "00000000000000ab",
        }
    )
    _write(lambda conn: runs.insert_run(conn, run))
    _write(lambda conn: runs.insert_tasks(conn, [spec], now=now))

    resolver = ops_dedup_key_resolver()
    assert resolver(task_id) == "00000000000000ab"
    assert resolver("task_01J00000000000000000000009") is None
    assert resolver(None) is None

    client = ScriptedLLMClient(_book(*_scripts()[4:]), dedup_key_resolver=resolver)
    assert client.complete(_req(_meta("analyst", task_id=task_id))).text == "task"
