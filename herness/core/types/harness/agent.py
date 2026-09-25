"""Loop signals and limits, loop state, loop checkpoint and agent result (U05-13 … U05-16).

Design 05 §4.8 and §5.2.2. L0 cannot import the L4 token estimator, so `LoopState` receives
it as a callable (R-17): `herness.harness.llm.tokens.estimate_tokens` is the only one.
"""

from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from typing import Annotated, Literal, NotRequired, Self, TypedDict

from pydantic import BaseModel, Field, JsonValue, PrivateAttr, model_validator, with_config

from herness.core.errors import ConfigError, SchemaViolation
from herness.core.ids import canonical_json, sha256_hex
from herness.core.types.harness.llm import (
    LLMResponse,
    Message,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    Usage,
    _Part,
)
from herness.core.types.harness.tooling import ToolResult, _FindingId, _QueryId

# Loop-signal messages (impl 05 §3.10). Private because the type ownership check (OWN011)
# admits only registered public names here; the public REPEAT_NUDGE, NO_PROGRESS_NUDGE and
# ERROR_STREAK_NUDGE are re-exported with the loop constants by herness.harness.loop (U05-58).
_REPEAT_NUDGE = (
    "You repeated a tool call you already made. Use the earlier result or change the arguments."
)
_NO_PROGRESS_NUDGE = (
    "No new evidence in the last {n} steps. Change your approach or give your final answer."
)
_ERROR_STREAK_NUDGE = (
    "Your last {n} tool calls failed. Read the hints and simplify, or give your final answer."
)
_CHECKPOINT_VERSIONS: frozenset[int] = frozenset({1})

type _Count = Annotated[int, Field(ge=0, strict=True)]
type _Signature = Annotated[str, Field(pattern=r"^[0-9a-f]{16}$")]
type _CostText = Annotated[str, Field(pattern=r"^[0-9]{1,15}(\.[0-9]{1,6})?$")]
type _Estimator = Callable[[Sequence[Message]], int]


class _Frozen(BaseModel, extra="forbid", frozen=True):
    pass


class LoopSignal(_Frozen):
    """A loop signal, consumed by the spec 08 `loop_signal_policy`."""

    cause: Literal["repeat", "no_progress", "error_streak"]
    message: str


class LoopLimits(_Frozen):
    """Loop thresholds; `0 < soft_tokens < hard_tokens <= context_budget_tokens`."""

    no_progress_steps: int = Field(default=4, ge=1, strict=True)
    error_streak: int = Field(default=3, ge=1, strict=True)
    context_budget_tokens: int = Field(ge=1, strict=True)
    soft_tokens: int = Field(ge=1, strict=True)
    hard_tokens: int = Field(ge=1, strict=True)
    fixed_tokens: int = Field(default=0, ge=0, strict=True)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if not self.soft_tokens < self.hard_tokens <= self.context_budget_tokens:
            msg = "loop limits need soft_tokens < hard_tokens <= context_budget_tokens"
            raise ValueError(msg)
        return self

    @classmethod
    def for_client(
        cls,
        context_window: int,
        max_effective_context: int | None,
        max_output_tokens: int,
        *,
        no_progress_steps: int,
        error_streak: int,
        fixed_tokens: int,
    ) -> "LoopLimits":
        """Derive the limits of one client (spec 07 §5.3, design §3.3)."""
        eff = min(context_window, max_effective_context or context_window)
        safety = max(1024, (3 * eff) // 100)
        budget = eff - max_output_tokens - safety
        if budget <= 0:
            msg = "context budget non-positive for client"
            raise ConfigError(msg)
        soft, hard = (70 * budget) // 100, (85 * budget) // 100
        if not 0 < soft < hard:
            msg = "context budget too small for client"
            raise ConfigError(msg)
        return cls(
            no_progress_steps=no_progress_steps,
            error_streak=error_streak,
            context_budget_tokens=budget,
            soft_tokens=soft,
            hard_tokens=hard,
            fixed_tokens=fixed_tokens,
        )


# Typed shapes of LoopCheckpoint.state and .budget: still plain dicts at runtime, so readers
# index them as the spec's dict[str, int] and dict[str, JsonValue] (U05-15), but validated.
@with_config(extra="forbid")
class _State(TypedDict):
    step: _Count
    nudges: _Count
    loop_signals: NotRequired[_Count]


@with_config(extra="forbid")
class _Budget(TypedDict):
    tokens_in: _Count
    tokens_out: _Count
    cost_usd: _CostText
    steps: NotRequired[_Count]


class LoopCheckpoint(_Frozen):
    """Typed view of the `loop` key of the task checkpoint envelope (R-21, R-29)."""

    v: Literal[1] = 1
    query_ids: list[_QueryId]
    finding_ids: list[_FindingId]
    budget: _Budget
    state: _State
    seen_signatures: list[_Signature] = []
    scratchpad: str | None = None

    @classmethod
    def from_envelope(cls, envelope: Mapping[str, JsonValue]) -> "LoopCheckpoint | None":
        """Read the `loop` key; `None` when absent (the task restarts from its spec)."""
        loop = envelope.get("loop")
        if loop is None:
            return None
        try:
            cp = cls.model_validate(loop)
            return cp.model_copy(update={"scratchpad": _scratchpad(envelope)})
        except (ValueError, TypeError, SchemaViolation) as exc:
            msg = "task checkpoint loop invalid"
            raise SchemaViolation(msg) from exc


def _scratchpad(envelope: Mapping[str, JsonValue]) -> str | None:
    value = envelope.get("scratchpad")
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, dict):
        return canonical_json(value)
    msg = "scratchpad must be a string or an object"
    raise TypeError(msg)


class LoopState(BaseModel, extra="forbid", validate_assignment=False):
    """The loop's working state; built only through `fresh` (U05-14)."""

    messages: list[Message]
    step: _Count = 0
    tokens_in: _Count = 0
    tokens_out: _Count = 0
    cost_usd: Decimal = Decimal(0)
    query_ids: list[str] = []
    finding_ids: list[str] = []
    last_usage: Usage | None = None
    nudges: _Count = 0
    loop_signals: _Count = 0

    _limits: LoopLimits = PrivateAttr()
    _estimator: _Estimator = PrivateAttr()
    _usage_total: Usage = PrivateAttr(default_factory=Usage)
    _seen: dict[str, int] = PrivateAttr(default_factory=dict)
    _repeat_in_step: bool = PrivateAttr(default=False)
    _error_streak: int = PrivateAttr(default=0)
    _steps_without_progress: int = PrivateAttr(default=0)
    _progress_in_step: bool = PrivateAttr(default=False)
    _sql_failures: dict[str, int] = PrivateAttr(default_factory=dict)
    _n_msgs_at_last_call: int = PrivateAttr(default=0)
    _gate_wait_ms: int = PrivateAttr(default=0)
    _stopping: bool = PrivateAttr(default=False)
    _budget_warned: bool = PrivateAttr(default=False)
    _wrap_up_sent: bool = PrivateAttr(default=False)

    @classmethod
    def fresh(cls, first: Message, *, limits: LoopLimits, estimator: _Estimator) -> "LoopState":
        """(1) A new state holding only `first`, all counters zero."""
        state = cls(messages=[first])
        state._limits = limits
        state._estimator = estimator
        return state

    def restore(self, cp: LoopCheckpoint) -> None:
        """(2) Set counters, ids, tokens, cost and seen signatures from a checkpoint."""
        if cp.v not in _CHECKPOINT_VERSIONS:
            msg = f"loop checkpoint version {cp.v} unsupported"
            raise SchemaViolation(msg)
        self.step = cp.state["step"]
        self.nudges = cp.state["nudges"]
        self.loop_signals = cp.state.get("loop_signals", 0)
        self.tokens_in = cp.budget["tokens_in"]
        self.tokens_out = cp.budget["tokens_out"]
        self.cost_usd = Decimal(cp.budget["cost_usd"])
        self.query_ids = list(dict.fromkeys(cp.query_ids))
        self.finding_ids = list(dict.fromkeys(cp.finding_ids))
        self._seen = dict.fromkeys(cp.seen_signatures, self.step)

    def tokens_used(self) -> int:
        """(3) Input plus output tokens charged so far."""
        return self.tokens_in + self.tokens_out

    def charge(self, resp: LLMResponse) -> tuple[int, int]:
        """(4) Add one response's usage and cost; return `(tokens_in, tokens_out)`."""
        tin, tout = resp.usage.prompt_total(), resp.usage.output_tokens
        self.tokens_in, self.tokens_out = self.tokens_in + tin, self.tokens_out + tout
        self.cost_usd += resp.cost_usd
        self._usage_total = self._usage_total.plus(resp.usage)
        self.last_usage = resp.usage
        self._n_msgs_at_last_call = len(self.messages)
        return tin, tout

    def append_assistant(self, resp: LLMResponse) -> None:
        """(5) Append the assistant turn; reasoning is replayed only to Anthropic."""
        parts: list[_Part] = []
        if resp.provider == "anthropic":
            parts.extend(resp.reasoning)
        if resp.text:
            parts.append(TextPart(text=resp.text))
        parts.extend(ToolCallPart(call=call) for call in resp.tool_calls)
        self.messages.append(Message(role="assistant", parts=parts or [TextPart(text="")]))

    def append_tool_results(self, results: list[ToolResult]) -> None:
        """(6) Append one tool message; track new ids and the error streak."""
        if not results:
            return
        parts: list[_Part] = [
            ToolResultPart(tool_call_id=r.tool_call_id, content=r.content, is_error=not r.ok)
            for r in results
        ]
        self.messages.append(Message(role="tool", parts=parts))
        for result in results:
            self._progress_in_step |= _add_new(self.query_ids, result.query_ids)
            self._progress_in_step |= _add_new(self.finding_ids, result.finding_ids)
            self._error_streak = 0 if result.ok else self._error_streak + 1

    def add_nudge(self, text: str, *, counts: bool = True) -> None:
        """(7) Append a `user` nudge; only counting nudges increment `nudges`."""
        self.messages.append(Message(role="user", parts=[TextPart(text=text)], kind="nudge"))
        if counts:
            self.nudges += 1

    @staticmethod
    def call_signature(name: str, arguments: Mapping[str, JsonValue]) -> str:
        """(8) 16 hex of SHA-256 over the canonical JSON of name and arguments (R-14)."""
        return sha256_hex(canonical_json({"name": name, "arguments": arguments}))[:16]

    def check_repeat(self, sig: str) -> int | None:
        """(9) The step at which `sig` was first seen, else None; a hit flags a repeat."""
        first = self._seen.get(sig)
        self._repeat_in_step |= first is not None
        return first

    def remember_call(self, sig: str) -> None:
        """(10) Record the first step at which `sig` was called."""
        self._seen.setdefault(sig, self.step)

    def note_sql_failure(self, key: str) -> int:
        """(11) Count one failure of a normalized SQL key; return the new count."""
        self._sql_failures[key] = self._sql_failures.get(key, 0) + 1
        return self._sql_failures[key]

    def sql_failures(self, key: str) -> int:
        """(11) Failures counted for a normalized SQL key."""
        return self._sql_failures.get(key, 0)

    def loop_signal(self) -> LoopSignal | None:
        """(12) Evaluate after `step += 1`: repeat, then error streak, then no progress."""
        progress, self._progress_in_step = self._progress_in_step, False
        self._steps_without_progress = 0 if progress else self._steps_without_progress + 1
        signal: LoopSignal | None = None
        if self._repeat_in_step:
            signal = LoopSignal(cause="repeat", message=_REPEAT_NUDGE)
        elif self._error_streak >= self._limits.error_streak:
            text = _ERROR_STREAK_NUDGE.format(n=self._error_streak)
            signal = LoopSignal(cause="error_streak", message=text)
            self._error_streak = 0
        elif self._steps_without_progress >= self._limits.no_progress_steps:
            text = _NO_PROGRESS_NUDGE.format(n=self._steps_without_progress)
            signal = LoopSignal(cause="no_progress", message=text)
            self._steps_without_progress = 0
        self._repeat_in_step = False
        if signal is not None:
            self.loop_signals += 1
        return signal

    def est_input_tokens(self) -> int:
        """(13) Input-token estimate: last usage plus new messages, else from scratch (R-17)."""
        if self.last_usage is not None:
            new = self.messages[self._n_msgs_at_last_call :]
            return self.last_usage.prompt_total() + self._estimator(new)
        return self._limits.fixed_tokens + self._estimator(self.messages)

    def limits(self) -> LoopLimits:
        """(13a) The limits this state was built with."""
        return self._limits

    def replace_messages(self, new: list[Message]) -> None:
        """(14) Replace the conversation (compaction); the next estimate starts afresh."""
        self.messages = list(new)
        self._n_msgs_at_last_call = 0
        self.last_usage = None

    def note_gate_wait(self, ms: int) -> None:
        """(15) Add call-gate wait time."""
        self._gate_wait_ms += ms

    def gate_wait_ms(self) -> int:
        """(15) Total call-gate wait time in milliseconds."""
        return self._gate_wait_ms

    def total_usage(self) -> Usage:
        """(16) Field-wise sum of every charged usage."""
        return self._usage_total

    def to_checkpoint(self) -> dict[str, JsonValue]:
        """(17) The `loop` value: the `LoopCheckpoint` fields other than `scratchpad` (R-21)."""
        return {
            "v": 1,
            "query_ids": list(self.query_ids),
            "finding_ids": list(self.finding_ids),
            "budget": {
                "tokens_in": self.tokens_in,
                "tokens_out": self.tokens_out,
                "cost_usd": str(self.cost_usd),
                "steps": self.step,
            },
            "state": {"step": self.step, "nudges": self.nudges, "loop_signals": self.loop_signals},
            "seen_signatures": [*sorted(self._seen)],
        }


def _add_new(target: list[str], ids: list[str]) -> bool:
    new = [i for i in dict.fromkeys(ids) if i not in target]
    target.extend(new)
    return bool(new)


class AgentResult(_Frozen):
    """Result of one `run_agent` call (design §4.8); completed exactly when final."""

    status: Literal["completed", "partial", "failed"]
    stop_reason: Literal[
        "final", "max_steps", "task_tokens", "task_budget", "repeat_call", "no_progress",
        "error_streak", "wall_clock", "cancelled", "budget", "refusal", "error",
    ]  # fmt: skip
    output: dict[str, JsonValue] | None
    steps: _Count
    usage: Usage
    cost_usd: Decimal = Field(ge=0)
    query_ids: list[str]
    finding_ids: list[str]
    error: str | None = None

    @model_validator(mode="after")
    def _completed_iff_final(self) -> Self:
        if (self.status == "completed") != (self.stop_reason == "final"):
            msg = "status is completed exactly when stop_reason is final"
            raise ValueError(msg)
        return self
