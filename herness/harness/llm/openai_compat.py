"""OpenAI-compatible adapter for vLLM, Ollama and llama.cpp (impl 05 U05-24, U05-25).

Design 05 §5.1.1. The instance holds no SDK client: every call opens one inside ``async with``,
so the adapter works across separate ``asyncio.run`` calls. The API key lives only on the
instance as ``SecretStr`` and reaches the wire only in the SDK's ``Authorization`` header
(TH05-15). Off-network clients send only through the egress guard's HTTP client; without it the
call fails closed (TH05-13). On-network responses are byte-capped while read, every call has a
whole-call deadline, and responses are bounded and checked before they reach the loop
(TH05-20). Mapping helpers live in the private sibling ``_openai_map``.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol, cast

import httpx2
import openai
from pydantic import SecretStr, ValidationError

from herness.core import time as clock
from herness.core.config import get_config
from herness.core.egress import MAX_RESPONSE_BYTES, get_guard
from herness.core.errors import ConfigError, ModelUnavailable, OutputValidationError
from herness.core.registry import register
from herness.core.secrets import resolve
from herness.core.types import LLMRequest, LLMResponse
from herness.harness.llm import _openai_map
from herness.harness.llm.base import egress_purpose_for
from herness.harness.llm.errors import translate_openai_error

if TYPE_CHECKING:
    import httpx
    from openai.types.chat import ChatCompletion

    from herness.harness.llm.settings import ClientConfig

__all__ = ["OpenAICompatClient"]

_Server = Literal["vllm", "ollama", "llamacpp", "openai"]
_NO_KEY: Final = "EMPTY"
_PAYLOAD_CLASS: Final = "aggregated_evidence"
_IDENTITY: Final = "identity"
# Same message translate_openai_error gives an openai.APITimeoutError (U05-30).
_DEADLINE_MSG: Final = "openai call failed: APITimeoutError"


# openai 3.x accepts a legacy httpx client too (T10-17 may return one); typing only.
type _HttpClient = httpx.AsyncClient | httpx2.AsyncClient  # noqa: TID251 - type alias only


class _GuardWithHttpClient(Protocol):
    """The part of the egress guard this adapter needs (T10-17 ``async_http_client``)."""

    def async_http_client(
        self, purpose: str, payload_class: str, *, run_id: str, task_id: str | None
    ) -> _HttpClient: ...


def _too_large() -> OutputValidationError:
    return OutputValidationError("response body exceeds limit")


class _CappedStream(httpx2.AsyncByteStream):
    """Counts body bytes while they are read; raises once they pass ``MAX_RESPONSE_BYTES``."""

    def __init__(self, inner: httpx2.AsyncByteStream, headers: httpx2.Headers) -> None:
        self._inner = inner
        self._headers = headers

    async def __aiter__(self) -> AsyncIterator[bytes]:
        length = self._headers.get("content-length", "")
        if length.isdigit() and int(length) > MAX_RESPONSE_BYTES:
            raise _too_large()  # rejected before any byte is read
        if self._headers.get("content-encoding", _IDENTITY).lower() != _IDENTITY:
            raise _too_large()  # identity was requested: a compressed body cannot be bounded
        total = 0
        async for chunk in self._inner:
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise _too_large()
            yield chunk

    async def aclose(self) -> None:
        await self._inner.aclose()


class _CappedTransport(httpx2.AsyncBaseTransport):
    """Loopback transport whose responses are byte-capped (TH05-20)."""

    def __init__(self) -> None:
        self._inner = httpx2.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        response = await self._inner.handle_async_request(request)
        stream = _CappedStream(cast("httpx2.AsyncByteStream", response.stream), response.headers)
        return httpx2.Response(
            response.status_code,
            headers=response.headers,
            stream=stream,
            extensions=response.extensions,
            request=request,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()


def _capped_http_client() -> httpx2.AsyncClient:
    """The HTTP client of on-network calls: uncompressed, byte-capped responses."""
    return httpx2.AsyncClient(transport=_CappedTransport(), headers={"Accept-Encoding": _IDENTITY})


@register("llm_client", "openai_compat")
class OpenAICompatClient:
    """``LLMClient`` for OpenAI-compatible servers (vLLM, Ollama, llama.cpp, openai)."""

    name: str
    cfg: ClientConfig
    server: _Server

    def __init__(self, cfg: ClientConfig) -> None:
        if cfg.kind != "openai_compat":
            msg = f"client {cfg.name} is not an openai_compat client"
            raise ConfigError(msg, client=cfg.name)
        if cfg.off_network:
            config = get_config()
            if not config.security.egress.enabled:
                msg = (
                    f"client {cfg.name} is off-network but egress is disabled"
                    f" in profile {config.profile}"
                )
                raise ConfigError(msg, client=cfg.name)
        self.name = cfg.name
        self.cfg = cfg
        # The settings validator fills ``server`` for every openai_compat client (D05-08).
        self.server = cfg.server or "llamacpp"
        self._api_key = resolve(cfg.api_key) if cfg.api_key is not None else SecretStr(_NO_KEY)

    def _to_openai_messages(self, req: LLMRequest) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        if req.system:
            out.append({"role": "system", "content": "\n\n".join(b.text for b in req.system)})
        for message in req.messages:
            out.extend(_openai_map.message_dicts(message))
        return out

    def _tool_params(self, req: LLMRequest) -> dict[str, object]:
        """``tools``, ``tool_choice`` and ``parallel_tool_calls``; all omitted without tools."""
        if not req.tools:
            return {}
        params: dict[str, object] = {
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": spec.name,
                        "description": spec.description,
                        "parameters": spec.input_schema,
                    },
                }
                for spec in req.tools
            ],
            "parallel_tool_calls": req.parallel_tool_calls,
        }
        if self.server != "ollama":
            params["tool_choice"] = req.tool_choice
        elif req.tool_choice == "auto":
            params["tool_choice"] = "auto"
        return params

    def _thinking_params(self, req: LLMRequest) -> dict[str, object]:
        thinking = req.thinking == "on"
        if thinking and req.response_schema is not None and self.cfg.reasoning_parser is None:
            msg = (
                f"client {self.name}: thinking with a response schema needs a reasoning parser"
                " (models.clients.<key>.reasoning_parser)"
            )
            raise ConfigError(msg, client=self.name)
        if self.server == "vllm":
            return {"extra_body": {"chat_template_kwargs": {"enable_thinking": thinking}}}
        if self.server == "ollama":
            return {"extra_body": {"think": thinking}}
        return {}

    def _build_params(self, req: LLMRequest) -> dict[str, object]:
        params: dict[str, object] = {
            "model": self.cfg.model,
            "messages": self._to_openai_messages(req),
            "max_tokens": req.max_output_tokens,
        }
        params.update(self._tool_params(req))
        if req.response_schema is not None:
            params["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": req.response_schema_name, "schema": req.response_schema},
            }
        params.update(self._thinking_params(req))
        if req.seed is not None:
            params["seed"] = req.seed
        if req.stop:
            params["stop"] = list(req.stop)
        if req.temperature is not None:
            params["temperature"] = req.temperature
        return params

    def _map_response(self, raw: ChatCompletion, req: LLMRequest, latency_ms: int) -> LLMResponse:
        """Map one completion; a malformed shape is ``OutputValidationError`` without its data."""
        try:
            return _openai_map.map_response(raw, req, self.cfg, latency_ms)
        except (TypeError, AttributeError, ValueError, ValidationError):
            msg = "malformed response"
            raise OutputValidationError(msg, client=self.name) from None

    def _http_client(self, req: LLMRequest) -> _HttpClient:
        """The guard's client off-network (fails closed without it); else a byte-capped one."""
        if not self.cfg.off_network:
            return _capped_http_client()
        guard = get_guard()
        if not hasattr(guard, "async_http_client"):
            msg = f"client {self.name} is off-network but the egress guard has no HTTP client"
            raise ConfigError(msg, client=self.name)
        meta = req.metadata
        return cast("_GuardWithHttpClient", guard).async_http_client(
            egress_purpose_for(meta.model_role),
            _PAYLOAD_CLASS,
            run_id=meta.run_id,
            task_id=meta.task_id,
        )

    async def _send(self, req: LLMRequest, params: dict[str, Any]) -> tuple[ChatCompletion, int]:
        async with contextlib.AsyncExitStack() as stack:
            http_client = self._http_client(req)
            stack.push_async_callback(http_client.aclose)  # closed even if the SDK init fails
            client = await stack.enter_async_context(
                openai.AsyncOpenAI(
                    base_url=self.cfg.base_url,
                    api_key=self._api_key.get_secret_value(),
                    timeout=req.timeout_s,
                    max_retries=0,
                    # openai 3.x accepts a legacy httpx client too; it is typed httpx2 only.
                    http_client=cast("httpx2.AsyncClient", http_client),
                )
            )
            t0 = clock.monotonic()
            async with asyncio.timeout(req.timeout_s):  # whole-call deadline (per-phase above)
                raw = await client.chat.completions.create(**params)
            return cast("ChatCompletion", raw), int((clock.monotonic() - t0) * 1000)

    async def acomplete(self, req: LLMRequest) -> LLMResponse:
        """One non-streaming call; no retries (spec 08 retries)."""
        if req.client != self.name:
            msg = f"request for client {req.client[:64]} sent to client {self.name}"
            raise ConfigError(msg, client=self.name)
        params = cast("dict[str, Any]", self._build_params(req))
        try:
            raw, latency_ms = await self._send(req, params)
        except TimeoutError:
            raise ModelUnavailable(_DEADLINE_MSG, client=self.name) from None
        except openai.OpenAIError as exc:
            raise translate_openai_error(exc) from exc
        return self._map_response(raw, req, latency_ms)

    def complete(self, req: LLMRequest) -> LLMResponse:
        """Blocking ``acomplete``; refused inside a running event loop (design §3.2)."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.acomplete(req))
        msg = "complete() called inside a running event loop; use acomplete()"
        raise RuntimeError(msg)
