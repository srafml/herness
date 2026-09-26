"""Tests for AnthropicClient.submit_batch / collect_batch and _anthropic_batch (U05-29)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest
import yaml
from pydantic import SecretStr
from structlog.testing import capture_logs
from tests.support.egress_harness import load
from tests.support.fake_keyring import MemoryKeyring
from tests.unit.harness.test_llm_anthropic import _violations

from herness.core import config as c
from herness.core import egress, secrets
from herness.core.errors import ConfigError, EgressBlocked, ModelUnavailable, OutputValidationError
from herness.core.types import LLMRequest, Message, RequestMeta, TextPart
from herness.harness.llm import _anthropic_batch as batch_mod
from herness.harness.llm import anthropic_client as ac
from herness.harness.llm.base import BatchCapable
from herness.harness.llm.settings import ClientConfig

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
_CLIENTS: dict[str, dict[str, Any]] = yaml.safe_load(
    (ROOT / "config" / "models.yaml").read_text(encoding="utf-8")
)["models"]["clients"]
_KEY = "sk-ant-test-" + "k" * 24  # pragma: allowlist secret
_Handler = Callable[[httpx2.Request], httpx2.Response]


def _cfg(key: str = "claude-opus") -> ClientConfig:
    return ClientConfig.model_validate({"name": key, **_CLIENTS[key]})


def _req(**kw: Any) -> LLMRequest:
    base: dict[str, Any] = {
        "client": "claude-opus",
        "messages": [Message(role="user", parts=[TextPart(text="hi")])],
        "max_output_tokens": 4000,
        "timeout_s": 30.0,
        "metadata": RequestMeta(
            run_id="run_1",
            task_id="task_1",
            role="analyst",
            model_role="writer",
            step=0,
            request_key="task_1:0:step",
        ),
    }
    return LLMRequest(**{**base, **kw})


def _no_batch(cfg: ClientConfig) -> ClientConfig:
    return cfg.model_copy(update={"supports": cfg.supports.model_copy(update={"batch": False})})


@pytest.fixture
def hybrid(
    tmp_path: Path, fake_keyring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    c.reset_config()
    load(tmp_path, "hybrid")
    monkeypatch.setattr(secrets, "resolve", lambda ref: SecretStr(_KEY))
    yield
    c.reset_config()


@pytest.fixture
def client(hybrid: None) -> ac.AnthropicClient:
    return ac.AnthropicClient(_cfg())


class _StubGuard:
    """Stands in for the T10-17 guard: hands out httpx2 clients over a mock transport."""

    def __init__(self, handler: _Handler) -> None:
        self.handler = handler
        self.calls: list[tuple[Any, ...]] = []
        self.requests: list[httpx2.Request] = []

    def _record(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self.handler(request)

    def async_http_client(
        self,
        purpose: str,
        payload_class: str,
        *,
        run_id: str | None = None,
        task_id: str | None = None,
        timeout: float = 120.0,
    ) -> httpx2.AsyncClient:
        self.calls.append((purpose, payload_class, run_id, task_id, timeout))
        return httpx2.AsyncClient(transport=httpx2.MockTransport(self._record))


def _install(monkeypatch: pytest.MonkeyPatch, handler: _Handler) -> _StubGuard:
    guard = _StubGuard(handler)
    monkeypatch.setattr(egress, "get_guard", lambda: guard)
    return guard


def _message_json(content: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": content,
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 1000, "output_tokens": 500},
    }
    return {**body, **kw}


def _raw(content: list[dict[str, Any]], **kw: Any) -> anthropic.types.Message:
    return anthropic.types.Message.model_validate(_message_json(content, **kw))


class _ClosableAsyncIter:
    """Wraps an async generator with a real ``close`` coroutine, like the SDK's decoder."""

    def __init__(self, items: list[Any]) -> None:
        self._items = list(items)
        self.closed = False

    def __aiter__(self) -> _ClosableAsyncIter:
        return self

    async def __anext__(self) -> Any:
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)

    async def close(self) -> None:
        self.closed = True


class _FakeBatches:
    """Stands in for ``client.messages.batches`` in ``AnthropicClient`` batch tests."""

    def __init__(
        self,
        *,
        create: Callable[[Any], Any] | None = None,
        retrieve: Callable[[str], Any] | None = None,
        results: Callable[[str], AsyncIterator[Any]] | None = None,
    ) -> None:
        self._create = create
        self._retrieve = retrieve
        self._results = results
        self.retrieve_calls: list[str] = []

    async def create(self, *, requests: Any) -> Any:
        assert self._create is not None
        return self._create(requests)

    async def retrieve(self, batch_id: str) -> Any:
        self.retrieve_calls.append(batch_id)
        assert self._retrieve is not None
        return self._retrieve(batch_id)

    async def results(self, batch_id: str) -> AsyncIterator[Any]:
        assert self._results is not None
        return self._results(batch_id)


class _FakeSdk:
    """The async context manager ``AnthropicClient._sdk`` returns, patched for these tests."""

    def __init__(self, batches: _FakeBatches) -> None:
        self.messages = SimpleNamespace(batches=batches)

    async def __aenter__(self) -> _FakeSdk:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def _patch_sdk(monkeypatch: pytest.MonkeyPatch, batches: _FakeBatches) -> None:
    monkeypatch.setattr(ac.AnthropicClient, "_sdk", lambda self, req: _FakeSdk(batches))


# --- pure _anthropic_batch helpers -------------------------------------------------------


def test_ut05_39_check_batch_supported_both_branches() -> None:
    """UT05-39 check_batch_supported raises for a client without batch support, else passes."""
    batch_mod.check_batch_supported(_cfg())
    with pytest.raises(ConfigError, match="does not support"):
        batch_mod.check_batch_supported(_no_batch(_cfg()))


def test_ut05_39_validate_batch_id_and_poll_interval() -> None:
    """UT05-39 batch_id must match msgbatch_...; poll_s must be at least 5."""
    batch_mod.validate_batch_id("msgbatch_abc123XYZ")
    with pytest.raises(ConfigError, match="invalid batch id"):
        batch_mod.validate_batch_id("not-a-batch-id")
    batch_mod.validate_poll_interval(5.0)
    with pytest.raises(ConfigError, match="poll_s"):
        batch_mod.validate_poll_interval(4.9)


def test_ut05_39_build_batch_requests_bounds_and_duplicates() -> None:
    """UT05-39 build_batch_requests: 1-10,000 requests, unique request_key, else ConfigError."""
    with pytest.raises(ConfigError, match="1-10000"):
        batch_mod.build_batch_requests([], lambda r: {})
    with pytest.raises(ConfigError, match="1-10000"):
        batch_mod.build_batch_requests([None] * 10_001, lambda r: {})
    reqs = [
        SimpleNamespace(metadata=SimpleNamespace(request_key="k1")),
        SimpleNamespace(metadata=SimpleNamespace(request_key="k1")),
    ]
    with pytest.raises(ConfigError, match="duplicate request_key"):
        batch_mod.build_batch_requests(reqs, lambda r: {"model": "x"})
    ok = [
        SimpleNamespace(metadata=SimpleNamespace(request_key="k1")),
        SimpleNamespace(metadata=SimpleNamespace(request_key="k2")),
    ]
    sdk_requests, pending = batch_mod.build_batch_requests(ok, lambda r: {"model": "x"})
    assert sdk_requests == [
        {"custom_id": "k1", "params": {"model": "x"}},
        {"custom_id": "k2", "params": {"model": "x"}},
    ]
    assert set(pending) == {"k1", "k2"}


def test_ut05_39_poll_until_ended_polls_until_ended() -> None:
    """UT05-39 poll_until_ended retrieves repeatedly, sleeping between, until ended."""
    statuses = iter(["in_progress", "in_progress", "ended"])
    calls: list[str] = []

    async def retrieve(batch_id: str) -> Any:
        calls.append(batch_id)
        return SimpleNamespace(processing_status=next(statuses))

    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def run() -> None:
        await batch_mod.poll_until_ended(
            "msgbatch_1", 30.0, retrieve=retrieve, sleep=sleep, monotonic=lambda: 0.0
        )

    asyncio.run(run())
    assert calls == ["msgbatch_1", "msgbatch_1", "msgbatch_1"]
    assert sleeps == [30.0, 30.0]


def test_ut05_39_poll_until_ended_timeout_raises_model_unavailable() -> None:
    """UT05-39 timeout after BATCH_MAX_WAIT_S (86,400 s) raises ModelUnavailable; no real wait."""
    ticks = iter([0.0, 90_000.0])

    async def retrieve(batch_id: str) -> Any:
        return SimpleNamespace(processing_status="in_progress")

    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def run() -> None:
        await batch_mod.poll_until_ended(
            "msgbatch_1", 30.0, retrieve=retrieve, sleep=sleep, monotonic=lambda: next(ticks)
        )

    with pytest.raises(ModelUnavailable, match="msgbatch_1 not ended after 86400 s"):
        asyncio.run(run())
    assert sleeps == []
    assert batch_mod.BATCH_MAX_WAIT_S == 86_400


def test_ut05_39_collect_results_maps_logs_and_closes() -> None:
    """UT05-39 succeeded results are mapped; others logged at WARNING with the three fields."""
    lines = [
        SimpleNamespace(custom_id="k1", result=SimpleNamespace(type="succeeded", message="m1")),
        SimpleNamespace(custom_id="k2", result=SimpleNamespace(type="errored")),
        SimpleNamespace(custom_id="k3", result=SimpleNamespace(type="expired")),
        SimpleNamespace(custom_id="k4", result=SimpleNamespace(type="canceled")),
        SimpleNamespace(custom_id="k5", result=SimpleNamespace(type="succeeded", message="m5")),
    ]
    pending = {"k1": "req1", "k5": "req5"}
    wrapper = _ClosableAsyncIter(lines)

    async def run() -> tuple[dict[str, Any], list[str]]:
        return await batch_mod.collect_results(
            "msgbatch_1", wrapper, pending, lambda msg, req: f"resp:{msg}:{req}"
        )

    with capture_logs() as logs:
        out, seen = asyncio.run(run())
    assert out == {"k1": "resp:m1:req1", "k5": "resp:m5:req5"}
    assert seen == ["k1", "k2", "k3", "k4", "k5"]
    assert wrapper.closed is True
    failed = [e for e in logs if e["event"] == "harness.llm.batch_item_failed"]
    assert [(e["custom_id"], e["result_type"]) for e in failed] == [
        ("k2", "errored"),
        ("k3", "expired"),
        ("k4", "canceled"),
    ]
    for entry in failed:
        assert entry["log_level"] == "warning"
        assert entry["batch_id"] == "msgbatch_1"
        assert "message" not in entry  # no error text, no secrets (TH05-15)
        assert "error" not in entry


def test_ut05_39_collect_results_over_10000_raises_output_validation() -> None:
    """UT05-39 more than 10,000 result lines fails closed with OutputValidationError."""

    async def gen() -> AsyncIterator[Any]:
        for i in range(10_001):
            yield SimpleNamespace(
                custom_id=f"k{i}", result=SimpleNamespace(type="succeeded", message=f"m{i}")
            )

    async def run() -> None:
        await batch_mod.collect_results("msgbatch_1", gen(), {}, lambda msg, req: msg)

    with pytest.raises(OutputValidationError, match="more than 10000"):
        asyncio.run(run())


def test_ut05_39_batch_module_passes_st05_13_ast_lint() -> None:
    """UT05-39 the private batch sibling module builds no unguarded SDK or httpx2 client."""
    path = Path(batch_mod.__file__)
    assert _violations(path.read_text(encoding="utf-8"), "x.py") == []


# --- AnthropicClient.submit_batch / collect_batch ---------------------------------------


def test_ut05_39_isinstance_batch_capable(client: ac.AnthropicClient) -> None:
    """UT05-39 AnthropicClient satisfies the BatchCapable protocol."""
    assert isinstance(client, BatchCapable)


def test_ut05_39_submit_batch_supported_check(hybrid: None) -> None:
    """UT05-39 submit_batch on a client without batch support raises ConfigError, no network."""
    no_batch = ac.AnthropicClient(_no_batch(_cfg()))
    with pytest.raises(ConfigError, match="does not support"):
        asyncio.run(no_batch.submit_batch([_req()]))


def test_ut05_39_collect_batch_supported_check(hybrid: None) -> None:
    """UT05-39 collect_batch on a client without batch support raises ConfigError, no network."""
    no_batch = ac.AnthropicClient(_no_batch(_cfg()))
    with pytest.raises(ConfigError, match="does not support"):
        asyncio.run(no_batch.collect_batch("msgbatch_1"))


def test_ut05_39_collect_batch_id_and_poll_s_validation(client: ac.AnthropicClient) -> None:
    """UT05-39 collect_batch validates batch_id and poll_s before any network call."""
    with pytest.raises(ConfigError, match="invalid batch id"):
        asyncio.run(client.collect_batch("not-a-batch-id"))
    with pytest.raises(ConfigError, match="poll_s"):
        asyncio.run(client.collect_batch("msgbatch_abc123", poll_s=1.0))


def test_ut05_39_submit_builds_custom_id_params_returns_id(
    client: ac.AnthropicClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT05-39 submit_batch builds custom_id/params for every request and returns the batch id."""
    body = {
        "id": "msgbatch_abc123XYZ",
        "type": "message_batch",
        "processing_status": "in_progress",
        "request_counts": {
            "processing": 2,
            "succeeded": 0,
            "errored": 0,
            "canceled": 0,
            "expired": 0,
        },
        "created_at": "2024-01-01T00:00:00Z",
        "expires_at": "2024-01-02T00:00:00Z",
        "results_url": None,
    }
    guard = _install(monkeypatch, lambda request: httpx2.Response(200, json=body))
    req1 = _req()
    req2 = _req(
        metadata=RequestMeta(
            run_id="run_1",
            task_id="task_1",
            role="analyst",
            model_role="writer",
            step=1,
            request_key="task_1:1:step",
        )
    )

    batch_id = asyncio.run(client.submit_batch([req1, req2]))

    assert batch_id == "msgbatch_abc123XYZ"
    [request] = guard.requests
    assert request.url.path == "/v1/messages/batches"
    sent = json.loads(request.content)
    assert [r["custom_id"] for r in sent["requests"]] == ["task_1:0:step", "task_1:1:step"]
    assert sent["requests"][0]["params"] == client._build_params(req1)
    assert guard.calls == [("reasoning", "aggregated_evidence", None, None, 600.0)]
    assert set(client._pending) == {"task_1:0:step", "task_1:1:step"}


def test_ut05_39_submit_batch_egress_blocked_fails_closed(
    client: ac.AnthropicClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT05-39 a guard without async_http_client fails submit_batch closed; pending unchanged."""
    monkeypatch.setattr(egress, "get_guard", object)
    with pytest.raises(EgressBlocked):
        asyncio.run(client.submit_batch([_req()]))
    assert client._pending == {}


def test_ut05_39_submit_batch_sdk_error_translated(
    client: ac.AnthropicClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT05-39 an SDK error from create() is translated per U05-30; pending stays empty."""

    def boom(requests: Any) -> Any:
        request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages/batches")
        raise anthropic.APIConnectionError(message="boom", request=request)

    _patch_sdk(monkeypatch, _FakeBatches(create=boom))
    with pytest.raises(ModelUnavailable):
        asyncio.run(client.submit_batch([_req()]))
    assert client._pending == {}


def test_ut05_39_collect_batch_succeeded_batch_cost_and_pending_cleared(
    client: ac.AnthropicClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT05-39 collect_batch maps succeeded results with batch=True, half cost, pending cleared."""
    req = _req()
    message = _raw([{"type": "text", "text": "ok"}])
    retrieves = iter(["in_progress", "ended"])
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    def result_lines(batch_id: str) -> AsyncIterator[Any]:
        async def gen() -> AsyncIterator[Any]:
            yield SimpleNamespace(
                custom_id="task_1:0:step",
                result=SimpleNamespace(type="succeeded", message=message),
            )

        return gen()

    batches = _FakeBatches(
        retrieve=lambda bid: SimpleNamespace(processing_status=next(retrieves)),
        results=result_lines,
    )
    _patch_sdk(monkeypatch, batches)

    async def run() -> dict[str, Any]:
        async with client._pending_lock:
            client._pending["task_1:0:step"] = req
        return await client.collect_batch("msgbatch_1", poll_s=30.0)

    responses = asyncio.run(run())

    assert set(responses) == {"task_1:0:step"}
    resp = responses["task_1:0:step"]
    assert resp.batch is True
    assert str(resp.cost_usd) == "0.007000"  # (1000*4.00 + 500*20.00)/1e6 * 0.5
    assert sleeps == [30.0]
    assert batches.retrieve_calls == ["msgbatch_1", "msgbatch_1"]
    assert client._pending == {}


def test_ut05_39_collect_batch_unknown_custom_id_parsed_none(
    client: ac.AnthropicClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT05-39 a succeeded result for a key not pending (another process) maps with parsed None."""
    message = _raw([{"type": "text", "text": '{"a": 1}'}])

    def result_lines(batch_id: str) -> AsyncIterator[Any]:
        async def gen() -> AsyncIterator[Any]:
            yield SimpleNamespace(
                custom_id="other_process:0:step",
                result=SimpleNamespace(type="succeeded", message=message),
            )

        return gen()

    batches = _FakeBatches(
        retrieve=lambda bid: SimpleNamespace(processing_status="ended"), results=result_lines
    )
    _patch_sdk(monkeypatch, batches)

    responses = asyncio.run(client.collect_batch("msgbatch_1"))

    assert responses["other_process:0:step"].parsed is None
    assert responses["other_process:0:step"].text == '{"a": 1}'


def test_ut05_39_collect_batch_errored_expired_canceled_absent_and_logged(
    client: ac.AnthropicClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT05-39 errored/expired/canceled results are absent from the dict, logged at WARNING."""

    def result_lines(batch_id: str) -> AsyncIterator[Any]:
        async def gen() -> AsyncIterator[Any]:
            yield SimpleNamespace(custom_id="k_err", result=SimpleNamespace(type="errored"))
            yield SimpleNamespace(custom_id="k_exp", result=SimpleNamespace(type="expired"))
            yield SimpleNamespace(custom_id="k_can", result=SimpleNamespace(type="canceled"))

        return gen()

    batches = _FakeBatches(
        retrieve=lambda bid: SimpleNamespace(processing_status="ended"), results=result_lines
    )
    _patch_sdk(monkeypatch, batches)

    with capture_logs() as logs:
        responses = asyncio.run(client.collect_batch("msgbatch_1"))

    assert responses == {}
    failed = [e for e in logs if e["event"] == "harness.llm.batch_item_failed"]
    assert [(e["custom_id"], e["result_type"], e["log_level"]) for e in failed] == [
        ("k_err", "errored", "warning"),
        ("k_exp", "expired", "warning"),
        ("k_can", "canceled", "warning"),
    ]


def test_ut05_39_collect_batch_over_10000_results_raises(
    client: ac.AnthropicClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT05-39 collect_batch fails closed with OutputValidationError over 10,000 result lines."""

    def result_lines(batch_id: str) -> AsyncIterator[Any]:
        async def gen() -> AsyncIterator[Any]:
            for i in range(10_001):
                yield SimpleNamespace(custom_id=f"k{i}", result=SimpleNamespace(type="errored"))

        return gen()

    batches = _FakeBatches(
        retrieve=lambda bid: SimpleNamespace(processing_status="ended"), results=result_lines
    )
    _patch_sdk(monkeypatch, batches)
    with pytest.raises(OutputValidationError, match="more than 10000"):
        asyncio.run(client.collect_batch("msgbatch_1"))


def test_ut05_39_collect_batch_timeout_raises_model_unavailable(
    client: ac.AnthropicClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT05-39 collect_batch times out after BATCH_MAX_WAIT_S with no real waiting."""
    ticks = iter([0.0, 90_000.0])
    monkeypatch.setattr(ac.clock, "monotonic", lambda: next(ticks))
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    batches = _FakeBatches(retrieve=lambda bid: SimpleNamespace(processing_status="in_progress"))
    _patch_sdk(monkeypatch, batches)

    with pytest.raises(ModelUnavailable, match="msgbatch_1 not ended after 86400 s"):
        asyncio.run(client.collect_batch("msgbatch_1"))
    assert sleeps == []


def test_ut05_39_collect_batch_sdk_error_translated(
    client: ac.AnthropicClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT05-39 an SDK error from retrieve() during collect is translated per U05-30."""

    def boom(batch_id: str) -> Any:
        request = httpx2.Request("GET", "https://api.anthropic.com/v1/messages/batches/x")
        raise anthropic.APIConnectionError(message="boom", request=request)

    _patch_sdk(monkeypatch, _FakeBatches(retrieve=boom))
    with pytest.raises(ModelUnavailable):
        asyncio.run(client.collect_batch("msgbatch_1"))
