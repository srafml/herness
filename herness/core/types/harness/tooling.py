"""Tool protocols, tool context and results, store handles (impl 05 U05-04 … U05-09).

Design 05 §4.4. Defined in core so `ToolContext.task_tools` and the handles can be typed
without an upward import; `herness.harness.tools` re-exports `Tool` and `AsyncTool`.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol, Self, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from herness.core.errors import ConfigError, HernessError
from herness.core.types.harness.evidence import Evidence, _UtcDatetime

# Max model-facing tool content (§3.9). Private because the type ownership check (OWN011)
# admits only registered public names here; the public TOOL_CONTENT_MAX_CHARS lives in
# herness.harness.tools (U05-36).
_TOOL_CONTENT_MAX_CHARS = 12_000
_ERROR_MESSAGE_MAX_CHARS = 2_000
_ERROR_HINT_MAX_CHARS = 500

type _QueryId = Annotated[str, Field(pattern=r"^q_[0-9a-f]{16}$")]
type _FindingId = Annotated[str, Field(pattern=r"^fnd_[0-9A-HJKMNP-TV-Z]{26}$")]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolErrorInfo(_Frozen):
    """The taxonomy error behind a failed tool call; `type` is the error class name."""

    type: str
    message: str = Field(max_length=_ERROR_MESSAGE_MAX_CHARS)
    hint: str | None = Field(max_length=_ERROR_HINT_MAX_CHARS)


class ToolResult(_Frozen):
    """Tool outcome; `content` is model-facing, `data` never is."""

    tool_call_id: str = ""
    name: str = ""
    ok: bool
    content: str
    data: dict[str, JsonValue] | None = None
    query_ids: list[_QueryId] = []
    finding_ids: list[_FindingId] = []
    row_count: int | None = Field(default=None, ge=0, strict=True)
    truncated: bool = False
    error: ToolErrorInfo | None = None
    duration_ms: int = Field(default=0, ge=0, strict=True)

    @model_validator(mode="before")
    @classmethod
    def _truncate_content(cls, data: Any) -> Any:  # noqa: ANN401 - pydantic before-validator
        if isinstance(data, dict):
            content = data.get("content")
            if isinstance(content, str) and len(content) > _TOOL_CONTENT_MAX_CHARS:
                cut = content[: _TOOL_CONTENT_MAX_CHARS - 1] + "…"
                return {**data, "content": cut, "truncated": True}
        return data

    @model_validator(mode="after")
    def _ok_matches_error(self) -> Self:
        if self.ok == (self.error is not None):
            msg = "ok must be False exactly when error is set"
            raise ValueError(msg)
        return self

    @classmethod
    def from_error(cls, exc: HernessError, *, hint: str | None = None) -> "ToolResult":
        """Build the model-facing error result; the exception's details are never copied."""
        kind = type(exc).__name__
        message = str(exc)[:_ERROR_MESSAGE_MAX_CHARS]
        chosen = (hint if hint is not None else exc.hint) or None
        if chosen is not None:
            chosen = chosen[:_ERROR_HINT_MAX_CHARS]
        content = f"ERROR {kind}: {message}"
        if chosen is not None:
            content += f"\nHINT: {chosen}"
        error = ToolErrorInfo(type=kind, message=message, hint=chosen)
        return cls(ok=False, content=content, error=error)


class SqlLimits(_Frozen):
    """Per-task SQL limits."""

    return_rows: int = Field(default=200, ge=1, strict=True)
    scan_rows: int = Field(default=1_000_000, ge=1, strict=True)
    timeout_s: float = Field(gt=0, strict=True, allow_inf_nan=False)
    max_attempts_per_query: int = Field(default=3, ge=1, strict=True)


class Budgets(_Frozen):
    """Per-task budgets; built only by `TaskBudget.to_budgets(now)` of impl 06 (R-23)."""

    max_steps: int = Field(ge=1, le=200, strict=True)
    max_tokens: int = Field(ge=1_000, strict=True)
    max_cost_usd: Decimal = Field(ge=0)
    wall_clock_s: int = Field(ge=1, le=86_400, strict=True)
    deadline: _UtcDatetime | None = None


@runtime_checkable
class Tool(Protocol):
    """A synchronous tool (spec 00 §6); `dispatch` runs it in a worker thread."""

    @property
    def name(self) -> str: ...
    @property
    def description(self) -> str: ...
    @property
    def input_schema(self) -> dict[str, JsonValue]: ...

    def __call__(self, ctx: "ToolContext", **kwargs: JsonValue) -> "ToolResult": ...


@runtime_checkable
class AsyncTool(Protocol):
    """The async twin of `Tool`; `dispatch` awaits it on the event loop."""

    @property
    def name(self) -> str: ...
    @property
    def description(self) -> str: ...
    @property
    def input_schema(self) -> dict[str, JsonValue]: ...

    async def __call__(self, ctx: "ToolContext", **kwargs: JsonValue) -> "ToolResult": ...


@runtime_checkable
class BudgetLedger(Protocol):
    """Run-level budget, implemented by impl 06 `RunBudget`; safe under concurrent tasks."""

    def charge(self, tokens_in: int, tokens_out: int, cost_usd: Decimal) -> None:
        """Charge one call; raise `BudgetExceeded` when the run cap is reached."""
        ...

    def snapshot(self) -> dict[str, JsonValue]: ...


@runtime_checkable
class TraceEmitter(Protocol):
    """Trace interface implemented by `herness.harness.tracing.Tracer` (U05-69, R-66)."""

    @property
    def run_id(self) -> str: ...
    @property
    def task_id(self) -> str | None: ...

    def emit(self, type: str, /, **fields: object) -> str:  # noqa: A002 - name fixed by U05-07
        """Emit one trace event and return its new span_id."""
        ...


@runtime_checkable
class WarehouseHandle(Protocol):
    """Read-only view of one warehouse build; `cursor()` is a DuckDB cursor for this thread."""

    @property
    def build_id(self) -> str: ...
    @property
    def path(self) -> Path: ...

    def cursor(self) -> object: ...
    def schema(self) -> Mapping[str, Mapping[str, Mapping[str, str]]]: ...
    def table_comment(self, qualified: str) -> str: ...


@runtime_checkable
class OpsHandle(Protocol):
    """Port to the ops store `evidence` area (R-04), bound by the composition root."""

    def record_evidence(self, ev: Evidence) -> bool: ...
    def record_evidence_use(
        self, query_id: str, run_id: str, task_id: str | None, used_at: datetime
    ) -> bool: ...
    def get_evidence(self, query_id: str) -> Evidence | None: ...
    def finding_statuses(self, finding_ids: Sequence[str]) -> dict[str, str]: ...


class VectorHit(_Frozen):
    """One ticket returned by a vector search."""

    record_id: str
    entity: str
    service_id: str | None
    opened_at: _UtcDatetime | None
    similarity: float = Field(allow_inf_nan=False)


@runtime_checkable
class VectorHandle(Protocol):
    """Ticket vector search, implemented by `herness.store.vectors` (T02-08)."""

    def search_tickets(
        self, vector: Sequence[float], k: int, *, entity: str | None, service_id: str | None
    ) -> list[VectorHit]: ...


class ToolContext(BaseModel):
    """Everything a tool may use, fixed per task (design §4.4); handles never serialize."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    run_id: str
    task_id: str
    build_id: str
    role: str
    specialty: str
    depth: Literal["fast", "standard", "deep"]
    profile: str
    tool_names: list[str]
    task_tools: dict[str, Tool | AsyncTool] = Field(default={}, exclude=True)
    warehouse: WarehouseHandle = Field(exclude=True)
    ops: OpsHandle = Field(exclude=True)
    vectors: VectorHandle = Field(exclude=True)
    budgets: Budgets
    ledger: BudgetLedger = Field(exclude=True)
    sql_limits: SqlLimits
    text_access: Literal["redacted_only"] = "redacted_only"
    egress_purpose: Literal["reasoning", "reasoning_final"] | None = None
    tracer: TraceEmitter = Field(exclude=True)

    @model_validator(mode="after")
    def _same_build(self) -> Self:
        if self.build_id != self.warehouse.build_id:
            msg = (
                f"tool context build_id {self.build_id} != warehouse build_id "
                f"{self.warehouse.build_id}"
            )
            raise ConfigError(msg)
        return self
