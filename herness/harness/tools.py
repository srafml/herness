"""Recorded SQL execution and model-facing formatting (impl 05 U05-35, U05-36, U05-48).

Design 05 §5.4.4-§5.4.5. `execute_recorded` runs one query on the task's build and records
its `evidence` and `evidence_use` rows (flow F05-04); `format_result` renders the compact
table the model sees; `wrap_untrusted` builds the one R-20 `<untrusted_data>` block
(TH05-01). The execution internals live in the private `_tools_record` module; the tool
registry and dispatch (U05-33, U05-34) join this module in later cards. `json_safe` is the
U05-35 step 6 JSON-safe cell conversion, re-exported for the Verifier's re-run samples.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Final, Protocol, runtime_checkable

from pydantic import JsonValue

from herness.core import time as clock
from herness.core.ids import normalize_sql
from herness.core.resilience import retry_call
from herness.core.resilience.metrics import record_histogram
from herness.core.types import AsyncTool, Evidence, Tool, ToolContext
from herness.harness import _tools_record as rec
from herness.harness._tools_record import json_safe
from herness.harness.sql_guard import SqlGuard

__all__ = [
    "TOOL_CONTENT_MAX_CHARS",
    "AsyncTool",
    "RecordedResult",
    "SqlGuard",
    "Tool",
    "execute_recorded",
    "format_result",
    "json_safe",
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
