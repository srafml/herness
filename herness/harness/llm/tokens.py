"""Token counting and the one token estimate of Herness (impl 05 U05-23, R-17).

``count_tokens`` asks the tokenizer a client config names: the vLLM ``/tokenize`` endpoint
through the loopback-only client, or Anthropic ``messages.count_tokens`` through the egress
guard's client (TH05-13). Neither builds an HTTP client here (R-06). It never raises on a
counting failure: it logs ``harness.llm.token_count_fallback`` (client and error type only,
never the message) and returns ``estimate_tokens`` with ``exact=False``.
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Final, cast

import anthropic

from herness.core import egress as _egress
from herness.core.errors import OutputValidationError
from herness.core.ids import canonical_json
from herness.core.logging import get_logger
from herness.core.secrets import resolve
from herness.core.types import LLMRequest, Message, RequestMeta, SystemBlock, ToolSpec
from herness.harness.llm import _openai_map
from herness.harness.llm.anthropic_client import AnthropicClient
from herness.harness.llm.errors import find_egress_block

if TYPE_CHECKING:
    from herness.harness.llm.settings import ClientConfig

__all__ = ["count_tokens", "estimate_tokens"]

_VLLM_TIMEOUT_S: Final = 5.0
_ANTHROPIC_TIMEOUT_S: Final = 10.0
_TOKENIZE_MAX_BYTES: Final = 1_048_576  # a {"count": n} answer is tiny (w14-s05 ruling)
_ANTHROPIC_KEYS: Final = ("system", "messages", "tools")  # the count_tokens part of U05-27
# Only feeds AnthropicClient._build_params; never sent, logged or traced.
_COUNT_META: Final = RequestMeta(
    run_id="token_count",
    task_id=None,
    role="token_count",
    model_role="chat",
    step=0,
    request_key="token_count",
)

_log = get_logger("harness.llm")


def estimate_tokens(
    messages: Sequence[Message],
    tools: Sequence[ToolSpec] = (),
    system: Sequence[SystemBlock] = (),
) -> int:
    """``ceil(chars / 3.5)``: system texts, canonical JSON of each message's parts and tools."""
    chars = sum(len(block.text) for block in system)
    for message in messages:
        chars += len(canonical_json([part.model_dump(mode="json") for part in message.parts]))
    chars += sum(len(canonical_json(spec.model_dump(mode="json"))) for spec in tools)
    return (2 * chars + 6) // 7  # ceil(chars / 3.5) in exact integer arithmetic


def _checked(count: object, client: str) -> int:
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        msg = "token count missing or invalid"
        raise OutputValidationError(msg, client=client)
    return count


def _vllm_count(
    cfg: ClientConfig,
    messages: Sequence[Message],
    tools: Sequence[ToolSpec],
    system: Sequence[SystemBlock],
) -> int:
    """``POST <root>/tokenize`` on the loopback server (U05-23 step 2)."""
    root = cast("str", cfg.base_url).rstrip("/").removesuffix("/v1").rstrip("/")
    body: dict[str, Any] = {
        "model": cfg.model,
        "messages": _openai_map.chat_messages(system, messages),
    }
    if tools:
        body["tools"] = _openai_map.tool_dicts(tools)
    body["add_generation_prompt"] = True
    bearer = resolve(cfg.api_key) if cfg.api_key is not None else None  # R-53
    with _egress.loopback_http_client(
        root, timeout_s=_VLLM_TIMEOUT_S, bearer=bearer, max_response_bytes=_TOKENIZE_MAX_BYTES
    ) as http:
        response = http.post("/tokenize", json=body)
        response.raise_for_status()
        data = response.json()
    return _checked(data.get("count") if isinstance(data, dict) else None, cfg.name)


def _anthropic_count(
    cfg: ClientConfig,
    messages: Sequence[Message],
    tools: Sequence[ToolSpec],
    system: Sequence[SystemBlock],
) -> int:
    """``messages.count_tokens`` through the guard's client (U05-23 step 3)."""
    adapter = AnthropicClient(cfg)  # kind, egress and key checks; the key resolved once
    request = LLMRequest(
        client=cfg.name,
        system=list(system),
        messages=list(messages),
        tools=list(tools),
        max_output_tokens=cfg.max_output_tokens,
        thinking="off",
        timeout_s=_ANTHROPIC_TIMEOUT_S,
        metadata=_COUNT_META,
    )
    params = adapter._build_params(request)  # the U05-27 mapping, reused as is
    fields: dict[str, Any] = {key: params[key] for key in _ANTHROPIC_KEYS if key in params}
    api_key = adapter._api_key.get_secret_value()  # resolved once by the adapter
    with contextlib.ExitStack() as stack:
        http_client = _egress.get_guard().http_client("reasoning_final", "aggregated_evidence")
        stack.callback(http_client.close)  # closed even if the SDK init fails
        client = stack.enter_context(
            anthropic.Anthropic(
                api_key=api_key,
                max_retries=0,
                timeout=_ANTHROPIC_TIMEOUT_S,
                http_client=http_client,
            )
        )
        result = client.messages.count_tokens(model=cfg.model, **fields)
    return _checked(result.input_tokens, cfg.name)


def count_tokens(
    cfg: ClientConfig,
    messages: list[Message],
    tools: list[ToolSpec],
    system: list[SystemBlock],
) -> tuple[int, bool]:
    """``(tokens, exact)`` from the client's tokenizer; the estimate when it cannot count."""
    if cfg.tokenizer != "estimate":
        counter = _vllm_count if cfg.tokenizer == "vllm_endpoint" else _anthropic_count
        try:
            return counter(cfg, messages, tools, system), True
        except Exception as exc:  # noqa: BLE001 - U05-23: never raises on a counting failure
            cause = find_egress_block(exc) or exc  # an SDK-wrapped egress refusal is named
            _log.warning(
                "harness.llm.token_count_fallback",
                client=cfg.name,
                error_type=type(cause).__name__,
            )
    return estimate_tokens(messages, tools, system), False
