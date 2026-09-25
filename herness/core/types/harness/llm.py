"""Provider-neutral message, request and response models (impl 05 U05-01 … U05-03).

Design 05 §4.2 and §4.3. Every model is frozen so the loop and the compactor never
edit an earlier turn in place (design §5.2.3). No field holds a secret (TH05-15).
"""

from decimal import ROUND_HALF_EVEN, Decimal
from itertools import pairwise
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

_COST_QUANTUM = Decimal("0.000001")


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TextPart(_Frozen):
    """Plain text content."""

    type: Literal["text"] = "text"
    text: str = Field(max_length=200_000)


class ToolCall(_Frozen):
    """One tool call requested by the model."""

    id: str = Field(min_length=1, max_length=128)
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    arguments: dict[str, JsonValue]
    raw_arguments: str | None = None

    @field_validator("arguments", mode="before")
    @classmethod
    def _arguments_object(cls, value: object) -> object:
        if not isinstance(value, dict):
            msg = "tool call arguments must be a JSON object"
            raise ValueError(msg)  # noqa: TRY004 - pydantic needs ValueError
        return value


class ToolCallPart(_Frozen):
    """An assistant part carrying one tool call."""

    type: Literal["tool_call"] = "tool_call"
    call: ToolCall


class ToolResultPart(_Frozen):
    """The result of one tool call; lives only in `tool` messages."""

    type: Literal["tool_result"] = "tool_result"
    tool_call_id: str
    content: str = Field(max_length=12_000)
    is_error: bool = False


class ReasoningPart(_Frozen):
    """Model reasoning; `opaque` is replayed only to the provider that produced it."""

    type: Literal["reasoning"] = "reasoning"
    text: str = ""
    provider: str
    opaque: dict[str, JsonValue] | None = None


type _Part = Annotated[
    TextPart | ToolCallPart | ToolResultPart | ReasoningPart, Field(discriminator="type")
]
_ALLOWED_PARTS: dict[str, tuple[type[BaseModel], ...]] = {
    "user": (TextPart,),
    "assistant": (TextPart, ToolCallPart, ReasoningPart),
    "tool": (ToolResultPart,),
}


class Message(_Frozen):
    """One conversation turn."""

    role: Literal["user", "assistant", "tool"]
    parts: list[_Part] = Field(min_length=1, max_length=256)
    kind: Literal["normal", "compaction_summary", "nudge"] = "normal"

    @model_validator(mode="after")
    def _role_parts(self) -> Self:
        allowed = _ALLOWED_PARTS[self.role]
        for part in self.parts:
            if not isinstance(part, allowed):
                msg = f"a {self.role} message cannot contain a {part.type} part"
                raise ValueError(msg)  # noqa: TRY004 - pydantic needs ValueError
        return self


class SystemBlock(_Frozen):
    """One block of the system prompt; `cache` marks a prompt-cache breakpoint."""

    text: str
    cache: bool = False


class ToolSpec(_Frozen):
    """A tool offered to the model."""

    name: str
    description: str = Field(max_length=1_024)
    input_schema: dict[str, JsonValue]
    strict: bool = True


class RequestMeta(_Frozen):
    """Request identity; `request_key` is `<task_id or run_id>:<step>:<call_kind>`."""

    run_id: str
    task_id: str | None
    role: str
    model_role: str
    step: int = Field(ge=0)
    request_key: str = Field(max_length=200)


class LLMRequest(_Frozen):
    """One provider-neutral request (design §4.2)."""

    client: str
    system: list[SystemBlock] = []
    messages: list[Message] = Field(min_length=1)
    tools: list[ToolSpec] = []
    tool_choice: Literal["auto", "none"] = "auto"
    parallel_tool_calls: bool = True
    response_schema: dict[str, JsonValue] | None = None
    response_schema_name: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
    max_output_tokens: int = Field(ge=1)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    thinking: Literal["off", "on", "auto"] = "auto"
    thinking_budget_tokens: int | None = None
    stop: list[str] = Field(default=[], max_length=4)
    seed: int | None = None
    timeout_s: float = Field(gt=0)
    metadata: RequestMeta

    @field_validator("tools")
    @classmethod
    def _sorted_unique_tools(cls, tools: list[ToolSpec]) -> list[ToolSpec]:
        ordered = sorted(tools, key=lambda spec: spec.name)
        for before, after in pairwise(ordered):
            if before.name == after.name:
                msg = f"duplicate tool name {after.name}"
                raise ValueError(msg)
        return ordered

    @model_validator(mode="after")
    def _schema_pairing(self) -> Self:
        if (self.response_schema is None) != (self.response_schema_name is None):
            msg = "response_schema and response_schema_name must be set together"
            raise ValueError(msg)
        return self


class Usage(_Frozen):
    """Token usage of one or more calls."""

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)

    def plus(self, other: "Usage") -> "Usage":
        """Return the field-wise sum of both usages."""
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
        )

    def prompt_total(self) -> int:
        """Return all prompt-side tokens: input plus cache read plus cache write."""
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens


class LLMResponse(_Frozen):
    """Normalized provider response with usage and cost (design §4.3)."""

    text: str
    tool_calls: list[ToolCall]
    parsed: dict[str, JsonValue] | None
    reasoning: list[ReasoningPart]
    stop_reason: Literal[
        "end_turn", "tool_use", "max_tokens", "stop_sequence", "refusal", "content_filter", "other"
    ]
    raw_stop_reason: str
    refusal_category: str | None
    usage: Usage
    cost_usd: Decimal = Field(ge=0)
    client: str
    model: str
    provider: Literal["openai_compat", "anthropic"]
    latency_ms: int
    request_id: str | None
    batch: bool = False

    @field_validator("cost_usd")
    @classmethod
    def _quantize_cost(cls, value: Decimal) -> Decimal:
        return value.quantize(_COST_QUANTUM, rounding=ROUND_HALF_EVEN)
