"""Tool registry, dispatch, recorded SQL and formatting (impl 05 U05-33-U05-36, U05-48).

Design 05 §3.4, §5.3, §5.4.4-§5.4.5. `ToolRegistry` / `tool_registry` hold the process-wide
tools, whose names come only from `TOOL_OWNERS` (U05-33, TH05-07); `dispatch` runs the tool
calls of one assistant message (U05-34, flow F05-03). `execute_recorded` runs one query on the
task's build and records its `evidence` and `evidence_use` rows (flow F05-04); `format_result`
renders the compact table the model sees; `wrap_untrusted` builds the one R-20
`<untrusted_data>` block (TH05-01). The internals live in the private `_tools_record`,
`_tools_dispatch` and `_tools_schema` modules. `json_safe` is the U05-35 step 6 JSON-safe cell
conversion, re-exported for the Verifier's re-run samples.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal, Protocol, runtime_checkable

from pydantic import JsonValue

from herness.core import time as clock
from herness.core.errors import ConfigError
from herness.core.ids import normalize_sql
from herness.core.resilience import retry_call
from herness.core.resilience.metrics import record_histogram
from herness.core.types import (
    AsyncTool,
    Evidence,
    LoopState,
    Tool,
    ToolCall,
    ToolContext,
    ToolResult,
    ToolSpec,
)
from herness.harness import _tools_dispatch as dsp
from herness.harness import _tools_record as rec
from herness.harness._tools_dispatch import MAX_TOOL_ARGUMENT_CHARS, MAX_TOOL_CALLS_PER_MESSAGE
from herness.harness._tools_record import json_safe
from herness.harness._tools_schema import check_tool_schema
from herness.harness.sql_guard import SqlGuard

if TYPE_CHECKING:  # roles.base imports wrap_untrusted from here
    from herness.harness.roles.base import RoleSpec

__all__ = [
    "MAX_TOOL_ARGUMENT_CHARS",
    "MAX_TOOL_CALLS_PER_MESSAGE",
    "TOOL_CONTENT_MAX_CHARS",
    "TOOL_OWNERS",
    "AsyncTool",
    "RecordedResult",
    "SqlGuard",
    "Tool",
    "ToolRegistry",
    "dispatch",
    "execute_recorded",
    "format_result",
    "json_safe",
    "tool_registry",
    "wrap_untrusted",
]

# Equals the private `_TOOL_CONTENT_MAX_CHARS` that bounds `ToolResult.content` (§3.9).
TOOL_CONTENT_MAX_CHARS: Final = 12_000
CELL_MAX_CHARS: Final = 80
ARBITRARY_ROWS_LINE: Final = "rows shown are arbitrary; add ORDER BY"
_ATTR_DROP_RE: Final = re.compile(r"[^A-Za-z0-9:_.-]")
_QUERY_SECONDS: Final = "herness_harness_sql_query_seconds"


@dataclass(frozen=True, slots=True)
class RecordedResult:
    """One recorded query: the first `return_rows` rows in result order, plus flags (U05-35).

    `rows` hold the DuckDB cells as fetched (Decimal, date, datetime stay typed) so that
    `format_result` can apply its cell rules; the evidence sample holds the JSON-safe form.
    Each call returns its own lists (cache hits are copies), so callers may replace or edit them.
    """

    query_id: str
    columns: list[str]
    types: list[str]
    rows: list[tuple[object, ...]]
    row_count: int
    truncated: bool
    ordered: bool
    untrusted_columns: frozenset[str] = field(default_factory=frozenset)
    redact_columns: frozenset[str] = field(default_factory=frozenset)


@runtime_checkable
class _ResultCache(Protocol):
    """The warehouse result cache of `DuckWarehouse` (U05-38); fakes may lack it."""

    def cache_get(self, query_id: str) -> tuple[RecordedResult, Evidence] | None: ...
    def cache_put(self, query_id: str, value: tuple[RecordedResult, Evidence]) -> None: ...


def _copy(result: RecordedResult) -> RecordedResult:
    """A copy with fresh lists, so no caller can mutate a cached result (rows are tuples)."""
    return replace(
        result, columns=list(result.columns), types=list(result.types), rows=list(result.rows)
    )


def execute_recorded(
    ctx: ToolContext, sql: str, params: dict[str, JsonValue], *, guard: bool = True
) -> RecordedResult:
    """Execute `sql` on `ctx.build_id` and record evidence and its use (design §5.4.4).

    Raises `ToolInputError` (params), `QueryError` (guard, timeout, size, DuckDB),
    `StoreBusy` after the `sqlite_write` retries, `ConfigError` for internal SQL failing
    the guard.
    """
    started = clock.monotonic()
    qid = rec.query_id_for(sql, params, ctx.build_id)
    guarded = rec.check_sql(ctx, sql, guard=guard)
    cache = ctx.warehouse if isinstance(ctx.warehouse, _ResultCache) else None
    hit = cache.cache_get(qid) if cache is not None else None
    if hit is not None:
        result, ev = _copy(hit[0]), hit[1]
    else:
        run = rec.run_query(ctx, sql, params)
        redact = rec.output_names(run.columns, guarded.redact_output_columns)
        result = RecordedResult(
            query_id=qid,
            columns=run.columns,
            types=run.types,
            rows=run.rows,
            row_count=run.row_count,
            truncated=run.row_count > len(run.rows),
            ordered=guarded.ordered,
            untrusted_columns=rec.output_names(run.columns, guarded.untrusted_output_columns),
            redact_columns=redact,
        )
        ev = Evidence(
            query_id=qid,
            run_id=ctx.run_id,
            build_id=ctx.build_id,
            sql=normalize_sql(sql),
            params=params,
            result_hash=run.result_hash,
            row_count=run.row_count,
            result_sample=rec.sample_rows(run.columns, run.rows, redact),
            executed_at=clock.now(),
            duration_ms=run.duration_ms,
        )
    retry_call("sqlite_write", ctx.ops.record_evidence, ev)
    retry_call(
        "sqlite_write", ctx.ops.record_evidence_use, qid, ctx.run_id, ctx.task_id, clock.now()
    )
    if hit is None and cache is not None and result.row_count <= ctx.sql_limits.return_rows:
        cache.cache_put(qid, (_copy(result), ev))
    tool = "run_sql" if guard else "internal"
    elapsed = clock.monotonic() - started
    record_histogram(_QUERY_SECONDS, elapsed, component="harness", labels={"tool": tool})
    return result


def wrap_untrusted(text: str, /, *, source: str, record_id: str | None = None) -> str:
    """Delimit untrusted text in the R-20 block; the text cannot close it (U05-48, TH05-01)."""
    body = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    src = _ATTR_DROP_RE.sub("", source)
    rid = _ATTR_DROP_RE.sub("", record_id or "")
    return f'<untrusted_data source="{src}" record_id="{rid}">{body}</untrusted_data>'


def _escape(text: str) -> str:
    one_line = text.replace("\r\n", "\\n").replace("\r", "\\n").replace("\n", "\\n")
    return one_line.replace("|", "\\|")


def _utc_seconds(value: datetime) -> str:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cell_text(value: object) -> str:  # noqa: PLR0911 - one return per cell rule of U05-36
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return format(value, ".6g")
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return _utc_seconds(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, list | dict):
        return _escape(json.dumps(json_safe(value), separators=(",", ":"), ensure_ascii=False))
    return _escape(value if isinstance(value, str) else str(value))


def _cell(value: object, *, untrusted: bool) -> str:
    text = _cell_text(value)
    if len(text) > CELL_MAX_CHARS:
        # no dangling backslash of a half-cut `\n` / `\|` escape before the ellipsis
        text = text[: CELL_MAX_CHARS - 1].rstrip("\\") + "…"
    return wrap_untrusted(text, source="warehouse") if untrusted else text


def _header(result: RecordedResult, shown: int) -> str:
    truncated = "yes" if shown < result.row_count else "no"
    ordered = "yes" if result.ordered else "no"
    return (
        f"query_id={result.query_id} rows={result.row_count} shown={shown}"
        f" truncated={truncated} ordered={ordered}"
    )


def _assemble(result: RecordedResult, lines: Sequence[str], shown: int) -> str:
    names = " | ".join(_escape(name) for name in result.columns)
    parts = [_header(result, shown), names, " | ".join(_escape(t) for t in result.types)]
    parts.extend(lines[:shown])
    if shown < result.row_count and not result.ordered:
        parts.append(ARBITRARY_ROWS_LINE)
    return "\n".join(parts)


def format_result(
    result: RecordedResult, /, *, max_chars: int = TOOL_CONTENT_MAX_CHARS
) -> tuple[str, int]:
    """Model-facing compact table and the number of rows shown (U05-36); ≤ `max_chars`."""
    flags = [name in result.untrusted_columns for name in result.columns]
    lines = [
        " | ".join(_cell(v, untrusted=u) for v, u in zip(row, flags, strict=True))
        for row in result.rows
    ]
    shown = len(lines)
    content = _assemble(result, lines, shown)
    while len(content) > max_chars and shown > 0:
        shown -= 1
        content = _assemble(result, lines, shown)
    if len(content) > max_chars:  # headers alone are too long: hard cut keeps the bound
        content = content[: max(max_chars - 1, 0)] + "…"[:max_chars]
    return content, shown


# --- U05-33 tool registry -------------------------------------------------------------------

type _Owner = Literal["05", "06", "07"]
type _AnyTool = Tool | AsyncTool

# The only tool names that can ever exist (TH05-07): no URL, email, shell or write tool.
TOOL_OWNERS: Final[Mapping[str, _Owner]] = MappingProxyType(
    {
        "list_tables": "05",
        "describe_table": "05",
        "run_sql": "05",
        "get_metric": "05",
        "get_scores": "05",
        "get_cluster": "05",
        "get_record": "05",
        "semantic_search": "05",
        "recall_memory": "07",
        "propose_memory": "07",
        "post_finding": "06",
        "list_findings": "06",
        "request_subtask": "06",
        "escalate": "06",
    }
)


class ToolRegistry:
    """Process-wide tools by name, only names of `TOOL_OWNERS` (U05-33, design §3.4)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tools: dict[str, _AnyTool] = {}

    def register(self, tool: _AnyTool, *, owner: _Owner) -> None:
        """Add `tool`; idempotent for the same object; `ConfigError` on any rule broken."""
        name = tool.name
        expected = TOOL_OWNERS.get(name)
        if expected is None:
            msg = f"tool {name[:64]} is not a known tool name"
            raise ConfigError(msg)
        if expected != owner:
            msg = f"tool {name} is owned by {expected}, not {owner}"
            raise ConfigError(msg)
        check_tool_schema(tool)
        with self._lock:
            current = self._tools.get(name)
            if current is tool:
                return
            if current is not None:
                msg = f"tool {name} already registered"
                raise ConfigError(msg)
            self._tools[name] = tool

    def names(self) -> list[str]:
        """Registered tool names, sorted."""
        with self._lock:
            return sorted(self._tools)

    def resolve(
        self, role: RoleSpec, names: Sequence[str], task_tools: Mapping[str, _AnyTool]
    ) -> list[_AnyTool]:
        """The tools for one task in `sorted(names)` order (duplicates once); task tools win."""
        extra = set(names) - set(role.allowed_tools)
        if extra:
            msg = f"tools {', '.join(sorted(extra))} not allowed for role {role.name}"
            raise ConfigError(msg)
        for key, task_tool in task_tools.items():  # TH05-22: spec 06 names, strict schemas
            if TOOL_OWNERS.get(key) != "06" or task_tool.name != key:
                msg = f"task tool {key[:64]} must be a spec 06 tool registered under its name"
                raise ConfigError(msg)
            check_tool_schema(task_tool)
        with self._lock:
            registered = dict(self._tools)
        resolved: list[_AnyTool] = []
        for name in sorted(set(names)):
            tool = task_tools[name] if name in task_tools else registered.get(name)
            if tool is None:
                msg = f"tool {name} not registered"
                raise ConfigError(msg)
            resolved.append(tool)
        return resolved

    def tool_specs(self, tools: Sequence[_AnyTool]) -> list[ToolSpec]:
        """The model-facing specs, `strict=True`; registration enforced R-26 (D05-10)."""
        return [
            ToolSpec(
                name=t.name, description=t.description, input_schema=t.input_schema, strict=True
            )
            for t in tools
        ]


class _Process:
    """The process `ToolRegistry` (ENG §2.3 exception; reset by `reset_harness_state`)."""

    lock: Final = threading.Lock()
    registry: ToolRegistry | None = None


def tool_registry() -> ToolRegistry:
    """The process-wide registry, created on first call."""
    with _Process.lock:
        if _Process.registry is None:
            _Process.registry = ToolRegistry()
        return _Process.registry


def _reset_tool_registry() -> None:
    """Drop the process registry; only the `reset_harness_state` test fixture calls this."""
    with _Process.lock:
        _Process.registry = None


# --- U05-34 dispatch ------------------------------------------------------------------------


class _LoopHooksLike(Protocol):
    """`LoopHooks` (T05-22/23) is passed through untouched; any object satisfies this."""


async def dispatch(
    ctx: ToolContext,
    tools: Mapping[str, _AnyTool],
    calls: list[ToolCall],
    hooks: _LoopHooksLike | None,
    state: LoopState,
    /,
) -> list[ToolResult]:
    """Run the tool calls of one assistant message; one result per call, in call order.

    Only a `FatalError` escapes (after the sibling calls are cancelled); every other failure
    is an `ok=False` result (flow F05-03, design §5.3).
    """
    del hooks  # passed through by the loop; dispatch does not consult it (U05-34 signature)
    slots = [dsp.precheck(ctx, tools, state, i, call) for i, call in enumerate(calls)]
    await dsp.execute(ctx, slots)
    return [dsp.finish(ctx, state, slot) for slot in slots]
