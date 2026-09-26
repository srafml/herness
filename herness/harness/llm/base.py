"""LLM client contract (U05-20) and request parameter resolution (U05-21).

Design 05 §3.2, §5.1.1, §5.1.2, §5.5. This module holds no provider code: adapters (later
cards) implement ``LLMClient`` and its streaming and batch extensions; ``resolve_request_params``
is the single place the thinking, effort and temperature rules live so every adapter agrees.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal, Protocol, runtime_checkable

from herness.core.errors import OutputValidationError
from herness.core.logging import get_logger
from herness.core.types import LLMRequest, LLMResponse
from herness.harness.llm.settings import ClientConfig, RoleParams

_Depth = Literal["fast", "standard", "deep"]
_Thinking = Literal["on", "off"]

# R-38: the ``chat`` role (and every role not listed) gets purpose ``reasoning``; whether
# ``hybrid`` may carry it is decided by the egress guard from ``security.data_policy`` (impl 10).
EGRESS_PURPOSE_BY_MODEL_ROLE: Final[Mapping[str, Literal["reasoning", "reasoning_final"]]] = {
    "writer": "reasoning_final",
    "skeptic_final": "reasoning_final",
}

# Roles that borrow another role's ``RoleParams`` and thinking-under-``auto`` bucket.
BASE_ROLE: Final[Mapping[str, str]] = {
    "skeptic_final": "skeptic",
    "chat_off_hours": "chat",
    "judge": "planner",
    "triage": "chat",
}

# Adapter response bounds (spec constants table; program ruling on the adapter boundary): text
# over MAX_RESPONSE_TEXT_CHARS fails; text over MAX_RESPONSE_PART_CHARS (the TextPart cap) is
# cut so that it ends in TRUNCATION_MARKER; more than MAX_TOOL_CALLS tool calls fail.
MAX_RESPONSE_TEXT_CHARS: Final = 1_000_000
MAX_RESPONSE_PART_CHARS: Final = 200_000
MAX_TOOL_CALLS: Final = 64
TRUNCATION_MARKER: Final = "[truncated]"

_log = get_logger("harness.llm")

_AUTO_ON_FAST_STANDARD: Final = frozenset({"planner", "skeptic"})
_AUTO_ON_DEEP: Final = frozenset({"planner", "skeptic", "writer"})


@dataclass(frozen=True, slots=True)
class TextDelta:
    """One streamed text fragment."""

    text: str


@dataclass(frozen=True, slots=True)
class ToolCallDelta:
    """One streamed fragment of a tool call's arguments JSON."""

    id: str
    name: str
    arguments_json_fragment: str


@dataclass(frozen=True, slots=True)
class Done:
    """The final stream event; ``response`` equals what ``acomplete`` would have returned."""

    response: LLMResponse


type StreamEvent = TextDelta | ToolCallDelta | Done


@runtime_checkable
class LLMClient(Protocol):
    """The spec 00 §6 client contract every adapter implements."""

    name: str

    def complete(self, req: LLMRequest) -> LLMResponse: ...

    async def acomplete(self, req: LLMRequest) -> LLMResponse: ...


@runtime_checkable
class StreamCapable(Protocol):
    """Optional streaming extension; the last yielded event is always a ``Done``."""

    def astream(self, req: LLMRequest) -> AsyncIterator[StreamEvent]: ...


@runtime_checkable
class BatchCapable(Protocol):
    """Optional provider batch-API extension."""

    async def submit_batch(self, reqs: Sequence[LLMRequest]) -> str: ...

    async def collect_batch(
        self, batch_id: str, poll_s: float = 30.0
    ) -> dict[str, LLMResponse]: ...


@dataclass(frozen=True, slots=True)
class ResolvedParams:
    """The sampling parameters an adapter sends for one request (U05-21)."""

    temperature: float | None
    effort: str | None
    thinking: _Thinking
    downgraded: bool


def egress_purpose_for(model_role: str) -> Literal["reasoning", "reasoning_final"]:
    """The egress purpose of a model role; every role not listed is ``reasoning`` (R-38)."""
    return EGRESS_PURPOSE_BY_MODEL_ROLE.get(model_role, "reasoning")


def bound_response(text: str, n_tool_calls: int, *, client: str, field: str = "text") -> str:
    """Apply the adapter response bounds; return ``text``, cut to the part cap when longer."""
    if len(text) > MAX_RESPONSE_TEXT_CHARS:
        msg = "response text exceeds limit"
        raise OutputValidationError(msg, client=client)
    if n_tool_calls > MAX_TOOL_CALLS:
        msg = f"response has more than {MAX_TOOL_CALLS} tool calls"
        raise OutputValidationError(msg, client=client)
    if len(text) <= MAX_RESPONSE_PART_CHARS:
        return text
    _log.warning("harness.llm.response_truncated", client=client, field=field, length=len(text))
    return text[: MAX_RESPONSE_PART_CHARS - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER


def _thinking_from_auto(base_role: str, depth: _Depth) -> _Thinking:
    on_roles = _AUTO_ON_DEEP if depth == "deep" else _AUTO_ON_FAST_STANDARD
    return "on" if base_role in on_roles else "off"


def resolve_request_params(
    *,
    model_role: str,
    depth: _Depth,
    client: ClientConfig,
    role_params: RoleParams | None,
    fallback: RoleParams,
) -> ResolvedParams:
    """Resolve thinking, effort and temperature for one request (design §5.1.1, §5.1.2, §5.5).

    ``role_params`` is the caller's lookup of ``role_params[model_role]`` then
    ``role_params[BASE_ROLE[model_role]]``; ``fallback`` is the ``RoleSpec`` values.
    """
    params = role_params or fallback
    base_role = BASE_ROLE.get(model_role, model_role)
    thinking: _Thinking = (
        params.thinking if params.thinking != "auto" else _thinking_from_auto(base_role, depth)
    )
    if client.kind == "openai_compat" and not client.supports.thinking_toggle:
        thinking = "off"
    downgraded = False
    if client.kind == "anthropic" and client.thinking_mode == "adaptive_always":
        downgraded = thinking == "off"
        thinking = "on"
    effort = None
    if client.supports.effort:
        effort = "low" if downgraded else (params.effort or "medium")
    budget_thinking = client.thinking_mode == "budget" and thinking == "on"
    if not client.supports.sampling_params or budget_thinking:
        temperature = None
    else:
        temperature = params.temperature
    return ResolvedParams(
        temperature=temperature, effort=effort, thinking=thinking, downgraded=downgraded
    )
