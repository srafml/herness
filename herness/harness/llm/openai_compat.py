"""OpenAI-compatible adapter for vLLM, Ollama and llama.cpp (impl 05 U05-24, U05-25).

Design 05 §5.1.1. The instance holds no SDK client: every call opens one inside ``async with``,
so the adapter works across separate ``asyncio.run`` calls. The API key lives only on the
instance as ``SecretStr`` and reaches the wire only in the SDK's ``Authorization`` header
(TH05-15). Off-network clients send only through the egress guard's HTTP client; without it the
call fails closed (TH05-13). On-network clients send through ``egress.aloopback_http_client``,
which caps the body while it is read, refuses redirects and sets the per-phase timeouts; its
``response_too_large`` / ``unsupported_encoding`` refusals surface here as
``OutputValidationError``. Every call has a whole-call deadline, and responses are bounded and
checked before they reach the loop (TH05-20); ``astream`` bounds its buffers while it reads.
Mapping helpers live in the private siblings ``_openai_map`` and ``_openai_stream``.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Iterator
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol, cast

import openai
from pydantic import SecretStr, ValidationError

from herness.core import time as clock
from herness.core.config import get_config
from herness.core.egress import MAX_RESPONSE_BYTES, aloopback_http_client, get_guard
from herness.core.errors import (
    ConfigError,
    EgressBlocked,
    ModelUnavailable,
    OutputValidationError,
)
from herness.core.registry import register
from herness.core.secrets import resolve
from herness.core.types import LLMRequest, LLMResponse
from herness.harness.llm import _openai_map
from herness.harness.llm._openai_stream import StreamBuffers, next_chunk
from herness.harness.llm.base import Done, StreamEvent, egress_purpose_for
from herness.harness.llm.errors import find_egress_block, translate_openai_error

if TYPE_CHECKING:
    import httpx2
    from openai import AsyncStream
    from openai.types.chat import ChatCompletion, ChatCompletionChunk

    from herness.harness.llm.settings import ClientConfig

__all__ = ["OpenAICompatClient"]

_Server = Literal["vllm", "ollama", "llamacpp", "openai"]
_NO_KEY: Final = "EMPTY"
_PAYLOAD_CLASS: Final = "aggregated_evidence"
# Loopback refusals of the body itself (U10-59); reported as before T10-17 (TH05-20).
_BODY_REFUSALS: Final = frozenset({"response_too_large", "unsupported_encoding"})
_TOO_LARGE_MSG: Final = "response body exceeds limit"
# Same message translate_openai_error gives an openai.APITimeoutError (U05-30).
_DEADLINE_MSG: Final = "openai call failed: APITimeoutError"
_MALFORMED: Final = (TypeError, AttributeError, ValueError, ValidationError)


class _GuardWithHttpClient(Protocol):
    """The part of the egress guard this adapter needs (T10-17 ``async_http_client``)."""

    def async_http_client(
        self, purpose: str, payload_class: str, *, run_id: str, task_id: str | None
    ) -> httpx2.AsyncClient: ...


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

    def _to_openai_messages(self, req: LLMRequest) -> list[dict[str, Any]]:
        return _openai_map.chat_messages(req.system, req.messages)

    def _tool_params(self, req: LLMRequest) -> dict[str, object]:
        """``tools``, ``tool_choice`` and ``parallel_tool_calls``; all omitted without tools."""
        if not req.tools:
            return {}
        params: dict[str, object] = {
            "tools": _openai_map.tool_dicts(req.tools),
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
        except _MALFORMED:
            msg = "malformed response"
            raise OutputValidationError(msg, client=self.name) from None

    def _feed(self, buffers: StreamBuffers, chunk: ChatCompletionChunk) -> list[StreamEvent]:
        """``buffers.add``; a malformed chunk is ``OutputValidationError`` without its data."""
        try:
            return buffers.add(chunk)
        except _MALFORMED:
            msg = "malformed response"
            raise OutputValidationError(msg, client=self.name) from None

    def _http_client(self, req: LLMRequest) -> httpx2.AsyncClient:
        """The guard's client off-network (fails closed without it); else the loopback one."""
        if not self.cfg.off_network:
            # The SDK sends the key in its own Authorization header (TH05-15): no bearer here.
            return aloopback_http_client(
                cast("str", self.cfg.base_url),  # required for openai_compat (settings)
                timeout_s=req.timeout_s,
                bearer=None,
                max_response_bytes=MAX_RESPONSE_BYTES,
            )
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

    def _raise_body_refusal(self, exc: BaseException) -> None:
        """``OutputValidationError`` when ``exc`` is, or wraps, a loopback body refusal."""
        blocked = find_egress_block(exc)
        if blocked is not None and blocked.reason in _BODY_REFUSALS:
            raise OutputValidationError(_TOO_LARGE_MSG, client=self.name) from exc

    @contextlib.contextmanager
    def _translated(self) -> Iterator[None]:
        """U05-25 step 4 for both calls: deadline, body refusals, then U05-30 translation."""
        try:
            yield
        except TimeoutError:
            raise ModelUnavailable(_DEADLINE_MSG, client=self.name) from None
        except EgressBlocked as exc:
            self._raise_body_refusal(exc)
            raise
        except openai.OpenAIError as exc:
            self._raise_body_refusal(exc)
            raise translate_openai_error(exc) from exc

    @contextlib.asynccontextmanager
    async def _sdk(self, req: LLMRequest) -> AsyncIterator[openai.AsyncOpenAI]:
        """One SDK client over this call's HTTP client; both closed on every exit path."""
        async with contextlib.AsyncExitStack() as stack:
            http_client = self._http_client(req)
            stack.push_async_callback(http_client.aclose)  # closed even if the SDK init fails
            yield await stack.enter_async_context(
                openai.AsyncOpenAI(
                    base_url=self.cfg.base_url,
                    api_key=self._api_key.get_secret_value(),
                    timeout=req.timeout_s,
                    max_retries=0,
                    http_client=http_client,
                )
            )

    def _params(self, req: LLMRequest) -> dict[str, Any]:
        if req.client != self.name:
            msg = f"request for client {req.client[:64]} sent to client {self.name}"
            raise ConfigError(msg, client=self.name)
        return cast("dict[str, Any]", self._build_params(req))

    async def acomplete(self, req: LLMRequest) -> LLMResponse:
        """One non-streaming call; no retries (spec 08 retries)."""
        params = self._params(req)
        with self._translated():
            async with self._sdk(req) as client:
                t0 = clock.monotonic()
                async with asyncio.timeout(req.timeout_s):  # whole-call deadline (per-phase too)
                    raw = await client.chat.completions.create(**params)
                latency_ms = int((clock.monotonic() - t0) * 1000)
        return self._map_response(cast("ChatCompletion", raw), req, latency_ms)

    async def astream(self, req: LLMRequest) -> AsyncIterator[StreamEvent]:
        """Streamed chat (U05-26): raw deltas as they arrive, then exactly one ``Done``.

        The whole-call deadline is checked around each await of the SDK, never across a yield,
        so consumer time counts but the consumer is never cancelled (w14-s05 ruling).
        """
        params = self._params(req)
        params.update(stream=True, stream_options={"include_usage": True})
        deadline = asyncio.get_running_loop().time() + req.timeout_s
        buffers = StreamBuffers(self.name)
        with self._translated():
            async with self._sdk(req) as client:
                t0 = clock.monotonic()
                async with asyncio.timeout_at(deadline):
                    raw = await client.chat.completions.create(**params)
                stream = cast("AsyncStream[ChatCompletionChunk]", raw)
                async with stream:
                    while (chunk := await next_chunk(stream, deadline)) is not None:
                        for event in self._feed(buffers, chunk):
                            yield event
                latency_ms = int((clock.monotonic() - t0) * 1000)
        yield Done(self._map_response(buffers.completion(), req, latency_ms))

    def complete(self, req: LLMRequest) -> LLMResponse:
        """Blocking ``acomplete``; refused inside a running event loop (design §3.2)."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.acomplete(req))
        msg = "complete() called inside a running event loop; use acomplete()"
        raise RuntimeError(msg)
