"""Security test ST05-15 (TH05-15): API keys stay out of bodies, traces, logs and errors
(T05-27).

Sentinel API key values are stored in the (fake) keyring under the names the model clients
reference (`secret:vllm.api_key`, `secret:anthropic.api_key`) and resolved by the real
adapters. A scripted loop run (`run_agent` over `OpenAICompatClient` on `respx_router`, a real
`Tracer` at payload rate 1.0, every log event captured before any scrubbing), one Anthropic
call through the real egress guard, and provider errors whose bodies echo the key: the key
reaches the wire only in the SDK's auth header, never a request body, a trace line, a log
event or an exception string. The sentinels are built at runtime (detect-secrets baseline).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
import yaml
from pydantic import SecretStr
from structlog.testing import capture_logs
from tests.support import loop_standin as ls
from tests.support.dispatch_standin import SyncTool, strict_schema, use_test_config
from tests.support.egress_harness import load, make_guard
from tests.support.fake_keyring import MemoryKeyring
from tests.support.fake_llm import respx_router
from tests.support.harness_fakes import FakeOps

from herness.core import config as c
from herness.core import egress, egress_clients, secrets
from herness.core import redact as r
from herness.core.errors import AuthError, ConfigError, HernessError, ModelUnavailable
from herness.core.redact_directory import NameDirectory
from herness.core.settings import RedactionConfig
from herness.core.types import LLMRequest, Message, RequestMeta, TextPart
from herness.eval.scripted import LLMScript, ScriptBook
from herness.harness import hooks as h
from herness.harness import loop
from herness.harness.llm.anthropic_client import AnthropicClient
from herness.harness.llm.openai_compat import OpenAICompatClient
from herness.harness.llm.settings import ClientConfig
from herness.harness.tools import tool_registry
from herness.harness.tracing import Tracer

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
VLLM_KEY = "sk-" + "st0515" + "Vq8LmZ2xR7" + "sentinelvllm"  # built at runtime (detect-secrets)
ANTHROPIC_KEY = "sk-ant-" + "st0515" + "Kp4Tw9Hn3" + "sentinel"  # built at runtime
SENTINELS = (VLLM_KEY, ANTHROPIC_KEY)
RUN_ID = "run_01J8ST05150000000000000000"
OPENAI_URL = "http://127.0.0.1:8000/v1/chat/completions"
_SCRIPT = {
    "turns": [
        {"tool_calls": [{"name": "run_sql", "arguments": {"sql": "SELECT 1 AS n"}}]},
        {"final": {"text": "one row"}},
        {"final": {"output": {"answer": "one"}}},
    ],
    "source": "st05_15#0",
}


def _absent(text: str, where: str) -> None:
    for key in SENTINELS:
        assert key not in text, where


@pytest.fixture
def keys(tmp_path: Path, fake_keyring: MemoryKeyring) -> Iterator[MemoryKeyring]:
    """The test config (profile local) and both sentinel keys in the keyring."""
    fake_keyring.store[("herness", "vllm.api_key")] = VLLM_KEY
    fake_keyring.store[("herness", "anthropic.api_key")] = ANTHROPIC_KEY
    use_test_config(tmp_path / "cfg")
    yield fake_keyring
    c.reset_config()


def _local_30b() -> ClientConfig:
    cfg = c.get_config().models.models.clients["local-30b"]
    assert cfg.api_key == "secret:vllm.api_key"  # pragma: allowlist secret - a reference
    return cfg


def test_st05_15_scripted_run_key_only_in_auth_header(
    keys: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST05-15 a scripted loop run over `OpenAICompatClient` (key `secret:vllm.api_key`):
    every captured request carries the key only as `Authorization: Bearer <key>`; it is absent
    from every request body, every trace line (payloads sampled) and every log event."""
    del keys
    ls.write_prompts(tmp_path / "prompts", monkeypatch)
    directory = NameDirectory.from_files(None, (), None)  # payloads are redacted before writing
    redactor = r.Redactor(RedactionConfig(directory_file=None), bytes(range(32)), directory)
    monkeypatch.setattr(r._State, "redactor", redactor)
    sql_tool = SyncTool("run_sql", schema=strict_schema({"sql": {"type": "string"}}))
    tool_registry().register(sql_tool, owner="05")
    router = respx_router(ScriptBook([LLMScript.model_validate(_SCRIPT)])).install(monkeypatch)
    cfg = _local_30b()
    settings = c.get_config().models.harness.trace.model_copy(
        update={"payload_sample_rate": {"eval": 1.0, "chat": 1.0, "review": 1.0}}
    )
    tracer = Tracer(
        RUN_ID, build_id=None, run_kind="eval", traces_dir=tmp_path / "traces", settings=settings
    )
    view = tracer.bind(task_id="task_1", role="analyst_general")
    ctx = ls.loop_ctx(tool_names=("run_sql",), ops=FakeOps(), tracer=view)
    hooks = h.HarnessHooks(
        registry=None, gates={}, chain=None, compactor=None, task_id=None, phase=None,  # type: ignore[arg-type]
        stop=None, on_text_delta=None, tracer=view,
    )  # fmt: skip
    role = ls.demo_role(tools=("run_sql",))
    with capture_logs() as logs:
        try:
            result = asyncio.run(
                loop.run_agent(role, {"q": "x"}, ctx, OpenAICompatClient(cfg), cfg, hooks)
            )
        finally:
            tracer.close()
    assert result.status == "completed"
    assert len(sql_tool.calls) == 1
    assert [str(req.url) for req in router.requests] == [OPENAI_URL] * 3
    for req in router.requests:
        assert req.headers["authorization"] == f"Bearer {VLLM_KEY}"
        _absent(req.content.decode("utf-8"), "request body")
        _absent(str(req.url), "request url")
        others = {k: v for k, v in req.headers.items() if k.lower() != "authorization"}
        _absent(json.dumps(others), "other headers")
    trace = (tmp_path / "traces" / f"{RUN_ID}.jsonl").read_text(encoding="utf-8")
    assert '"payload"' in trace
    _absent(trace, "trace file")
    _absent(repr(logs), "log events")


@pytest.fixture
def hybrid(
    tmp_path: Path, fake_keyring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Profile hybrid (egress on, api.anthropic.com allowed) with a real guard, and the
    Anthropic sentinel key in the keyring."""
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    c.reset_config()
    guard = make_guard(load(tmp_path, "hybrid"))
    monkeypatch.setattr(egress, "get_guard", lambda: guard)
    fake_keyring.store[("herness", "anthropic.api_key")] = ANTHROPIC_KEY
    yield
    c.reset_config()


def _request(client: str) -> LLMRequest:
    meta = RequestMeta(
        run_id="run_1", task_id="task_1", role="writer", model_role="writer", step=0,
        request_key="task_1:0:step",
    )  # fmt: skip
    return LLMRequest(
        client=client,
        messages=[Message(role="user", parts=[TextPart(text="Summarise the findings.")])],
        max_output_tokens=256,
        timeout_s=30,
        metadata=meta,
    )


def test_st05_15_anthropic_key_only_in_x_api_key_header(
    hybrid: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST05-15 one Anthropic call through the real egress guard (key
    `secret:anthropic.api_key`): the key is only the `x-api-key` header value; absent from the
    body, the URL, the other headers and the log events."""
    del hybrid
    clients = yaml.safe_load((ROOT / "config" / "models.yaml").read_text(encoding="utf-8"))
    cfg = ClientConfig.model_validate(
        {"name": "claude-opus", **clients["models"]["clients"]["claude-opus"]}
    )
    script = {"turns": [{"final": {"text": "summary"}}], "source": "st05_15#1"}
    router = respx_router(ScriptBook([LLMScript.model_validate(script)])).install(monkeypatch)
    with capture_logs() as logs:
        response = asyncio.run(AnthropicClient(cfg).acomplete(_request("claude-opus")))
    assert response.text == "summary"
    (req,) = router.requests
    assert req.headers["x-api-key"] == ANTHROPIC_KEY
    _absent(req.content.decode("utf-8"), "request body")
    _absent(str(req.url), "request url")
    others = {k: v for k, v in req.headers.items() if k.lower() != "x-api-key"}
    _absent(json.dumps(others), "other headers")
    _absent(repr(logs), "log events")


def _echo(status: int) -> Any:
    """A model server that echoes the presented key in its error body."""

    def handle(request: httpx2.Request) -> httpx2.Response:
        presented = request.headers.get("authorization", "")
        body = {"error": {"message": f"key rejected: {presented}", "type": "auth"}}
        return httpx2.Response(status, json=body, request=request)

    return handle


@pytest.mark.parametrize(
    ("status", "error"),
    [(401, AuthError), (403, AuthError), (400, ConfigError), (500, ModelUnavailable)],
)
def test_st05_15_error_echoing_key_is_dropped_from_exception(
    keys: MemoryKeyring,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    error: type[HernessError],
) -> None:
    """ST05-15 a model server answering an error whose body echoes the presented key: the
    translated exception's message, repr, args and context hold no key; nothing logged does."""
    del keys
    monkeypatch.setattr(
        egress_clients.httpx2,
        "AsyncHTTPTransport",
        lambda **_kw: httpx2.MockTransport(_echo(status)),
    )
    with capture_logs() as logs, pytest.raises(error) as info:
        asyncio.run(OpenAICompatClient(_local_30b()).acomplete(_request("local-30b")))
    err = info.value
    for text in (str(err), repr(err), repr(err.args), json.dumps(err.context, default=str)):
        _absent(text, f"exception {status}")
    _absent(repr(logs), "log events")
    assert err.message == f"openai call failed: {type(err.__cause__).__name__} HTTP {status}"


def test_st05_15_resolved_key_is_a_secret_str(keys: MemoryKeyring) -> None:
    """ST05-15 the resolved key lives on the adapter only as `SecretStr` (its repr and str
    mask it), so no `repr`/`str` of the client or its config can carry it."""
    del keys
    client = OpenAICompatClient(_local_30b())
    assert isinstance(client._api_key, SecretStr)
    assert client._api_key.get_secret_value() == VLLM_KEY
    for text in (repr(client._api_key), str(client._api_key), repr(client.cfg), repr(vars(client))):
        _absent(text, "client repr")
    assert VLLM_KEY in secrets.known_values()
