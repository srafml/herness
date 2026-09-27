"""The spec 05 warehouse tools (impl 05 U05-39-U05-43; design 05 §5.4.2).

`list_tables`, `describe_table`, `run_sql` and `get_scores` (T05-17); `get_metric`,
`get_cluster`, `get_record` and `semantic_search` follow in T05-18. Every row a model sees
comes through `execute_recorded`, so it carries a `query_id` and is recorded as evidence.
Agent SQL (`run_sql`) always runs under the full `SqlGuard` (`guard=True`); the other tools
run internal constant SQL (`guard=False`, still guard-checked with `allow_catalog`). The only
SQL built at call time is the `describe_table` sample, whose identifiers come from the
build's schema (allow-list) and are DuckDB-quoted. Cells of redact-on-read columns pass
`redact_text` before the model or `data` sees them (TH05-05); untrusted text is wrapped by
`format_result`. Tools run in dispatch worker threads and share no mutable state. The SQL
constants and shared helpers live in the private `_warehouse_tools_sql`.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

from pydantic import JsonValue
from rapidfuzz import fuzz, process

from herness.core.errors import QueryError, ToolInputError
from herness.core.types import Tool, ToolContext, ToolResult
from herness.harness import _warehouse_tools_sql as ws
from herness.harness._warehouse_tools_sql import DESCRIBE_COLUMNS_SQL, LIST_TABLES_SQL, SCORES_SQL
from herness.harness.sql_guard import ALLOWED_SCHEMAS
from herness.harness.tools import (
    RecordedResult,
    ToolRegistry,
    execute_recorded,
    json_safe,
    tool_registry,
)

__all__ = [
    "BLOCKED_MARK",
    "DESCRIBE_COLUMNS_SQL",
    "LIST_TABLES_SQL",
    "SCORES_SQL",
    "DescribeTable",
    "GetScores",
    "ListTables",
    "RunSql",
    "register_warehouse_tools",
]

SAMPLE_LIMIT: Final = 5
BLOCKED_MARK: Final = "BLOCKED (use enrich.text_redacted)"
_SCHEMA_ENUM: Final[list[JsonValue]] = ["core", "enrich", "metrics", "score", "meta"]


# --- U05-39 list_tables ----------------------------------------------------------------------


class ListTables:
    """`list_tables`: allowed-schema tables with row counts and descriptions (U05-39)."""

    name = "list_tables"
    description = (
        "List warehouse tables with estimated row counts and one-line descriptions."
        " Pass a schema (core, enrich, metrics, score, meta) or null for all."
    )
    input_schema: dict[str, JsonValue] = ws.strict_schema(
        {"schema": {"type": ["string", "null"], "enum": [*_SCHEMA_ENUM, None]}}
    )

    def __call__(self, ctx: ToolContext, **kwargs: JsonValue) -> ToolResult:
        schema = ws.opt_text(kwargs, "schema")
        r = execute_recorded(ctx, LIST_TABLES_SQL, {"schema": schema}, guard=False)
        tables: list[JsonValue] = []
        lines: list[str] = []
        for schema_name, table_name, rows, comment in r.rows:
            if schema_name not in ALLOWED_SCHEMAS:  # the SQL filters too; never leak a name
                continue
            qualified, description = f"{schema_name}.{table_name}", ws.one_line(comment)
            lines.append(f"{qualified} | rows={rows} | {description}")
            tables.append({"table": qualified, "rows": json_safe(rows), "description": description})
        header = f"query_id={r.query_id} tables={len(lines)}"
        return ToolResult(
            ok=True,
            content="\n".join([header, *lines]),
            data={"tables": tables},
            query_ids=[r.query_id],
            row_count=len(lines),
            truncated=r.truncated,
        )


# --- U05-40 describe_table -------------------------------------------------------------------


def _missing_table(ctx: ToolContext, table: str) -> ToolInputError:
    """`ToolInputError` with the 3 closest allowed table names (never a name outside them)."""
    choices = sorted(
        f"{schema}.{name}"
        for schema, tables in ctx.warehouse.schema().items()
        if schema in ALLOWED_SCHEMAS
        for name in tables
    )
    best = process.extract(table, choices, scorer=fuzz.WRatio, limit=3)
    hint = "closest: " + ", ".join(m[0] for m in best) if best else "call list_tables"
    msg = f"table {table[:128]} does not exist"
    return ToolInputError(msg, hint=hint)


def _column_lines(r: RecordedResult, blocked: Iterable[str]) -> tuple[list[str], list[JsonValue]]:
    """One content line and one `data` entry per column; blocked columns marked."""
    marked = frozenset(blocked)
    lines: list[str] = []
    columns: list[JsonValue] = []
    for name, dtype, nullable, comment in r.rows:
        is_blocked = str(name).lower() in marked
        description = BLOCKED_MARK if is_blocked else ws.one_line(comment)
        null_text = "yes" if nullable else "no"
        lines.append(f"{ws.one_line(name, 128)} | {dtype} | {null_text} | {description}")
        entry: dict[str, JsonValue] = {"name": str(name), "type": str(dtype)}
        entry |= {"nullable": bool(nullable), "description": description, "blocked": is_blocked}
        columns.append(entry)
    return lines, columns


class DescribeTable:
    """`describe_table`: columns, blocked-column marks and 5 sample rows (U05-40)."""

    name = "describe_table"
    description = (
        "Describe one warehouse table: columns with type, nullability and description, blocked"
        " free-text columns marked, and 5 sample rows without blocked columns."
    )
    input_schema: dict[str, JsonValue] = ws.strict_schema(
        {
            "table": {
                "type": "string",
                "pattern": "^(core|enrich|metrics|score|meta)\\.[a-z_]+$",
                "maxLength": 128,
            }
        }
    )

    def __call__(self, ctx: ToolContext, **kwargs: JsonValue) -> ToolResult:
        table = ws.text(kwargs, "table")
        schema_name, _, table_name = table.partition(".")
        allowed = ctx.warehouse.schema().get(schema_name, {}).get(table_name)
        if schema_name not in ALLOWED_SCHEMAS or allowed is None:
            raise _missing_table(ctx, table)
        params: dict[str, JsonValue] = {"schema": schema_name, "table": table_name}
        cols = execute_recorded(ctx, DESCRIBE_COLUMNS_SQL, params, guard=False)
        prefix = f"{schema_name}.{table_name}."
        blocked = {c.removeprefix(prefix) for c in ws.blocked_columns() if c.startswith(prefix)}
        lines, columns = _column_lines(cols, blocked)
        header = f"query_id={cols.query_id} table={table} columns={len(lines)}"
        content, query_ids = "\n".join([header, *lines]), [cols.query_id]
        data: dict[str, JsonValue] = {"table": table, "columns": columns, "sample": None}
        described = [str(row[0]).lower() for row in cols.rows]
        shown = [c for c in described if c in allowed and c not in blocked]  # allow-list only
        if shown:
            select = ", ".join(ws.quote(c) for c in shown)
            source = f"{ws.quote(schema_name)}.{ws.quote(table_name)}"
            sample_sql = f"SELECT {select} FROM {source} LIMIT {SAMPLE_LIMIT}"  # noqa: S608 - allow-listed, quoted identifiers
            sample = ws.table_result(execute_recorded(ctx, sample_sql, {}, guard=False))
            content += "\nsample rows:\n" + sample.content
            query_ids += sample.query_ids
            data["sample"] = sample.data
        return ToolResult(ok=True, content=content, data=data, query_ids=query_ids)


# --- U05-41 run_sql --------------------------------------------------------------------------


class RunSql:
    """`run_sql`: one guarded, read-only, recorded SELECT (U05-41)."""

    name = "run_sql"
    description = (
        "Run one read-only SELECT on the warehouse. Qualify tables as schema.table; free-text"
        " columns are blocked (join enrich.text_redacted on record_id). State the purpose."
    )
    input_schema: dict[str, JsonValue] = ws.strict_schema(
        {
            "sql": {"type": "string", "minLength": 1, "maxLength": 8000},
            "purpose": {"type": "string", "maxLength": 200},
        }
    )

    def __call__(self, ctx: ToolContext, **kwargs: JsonValue) -> ToolResult:
        sql = ws.text(kwargs, "sql")
        ws.text(kwargs, "purpose")  # only in the sampled trace args (dispatch traces them)
        return ws.table_result(execute_recorded(ctx, sql, {}, guard=True))


# --- U05-43 get_scores -----------------------------------------------------------------------


class GetScores:
    """`get_scores`: rows of `score.<kind>` with their stored `query_ids` (U05-43)."""

    name = "get_scores"
    description = (
        "Read precomputed scores: funding candidates, org comparisons, action levers or the"
        " portfolio (scenario only for portfolio). Rows carry their stored query_ids."
    )
    input_schema: dict[str, JsonValue] = ws.strict_schema(
        {
            "kind": {"type": "string", "enum": ["funding", "org", "action_lever", "portfolio"]},
            "entity_id": {"type": ["string", "null"], "maxLength": 200},
            "top": {"type": "integer", "minimum": 1, "maximum": 100},
            "scenario": {"type": ["string", "null"], "maxLength": 64},
        }
    )

    def __call__(self, ctx: ToolContext, **kwargs: JsonValue) -> ToolResult:
        kind, top = ws.text(kwargs, "kind"), ws.integer(kwargs, "top")
        entity_id, scenario = ws.opt_text(kwargs, "entity_id"), ws.opt_text(kwargs, "scenario")
        sql = SCORES_SQL.get(kind)
        if sql is None:
            msg = "kind must be funding, org, action_lever or portfolio"
            raise ToolInputError(msg)
        if scenario is not None and kind != "portfolio":
            msg = "scenario applies only to kind portfolio"
            raise ToolInputError(msg)
        if kind not in ctx.warehouse.schema().get("score", {}):  # never a fatal internal-SQL error
            msg = f"table score.{kind} is not in this build"
            raise QueryError(msg, hint="call list_tables with schema score")
        params: dict[str, JsonValue] = {"entity_id": entity_id, "top": top}
        if kind == "portfolio":
            params["scenario"] = scenario
        return ws.table_result(execute_recorded(ctx, sql, params, guard=False))


# --- registration ----------------------------------------------------------------------------

_TOOLS: Final[tuple[Tool, ...]] = (ListTables(), DescribeTable(), RunSql(), GetScores())


def register_warehouse_tools(registry: ToolRegistry | None = None) -> None:
    """Register the spec 05 warehouse tools with owner "05"; idempotent (same instances)."""
    target = registry if registry is not None else tool_registry()
    for tool in _TOOLS:
        target.register(tool, owner="05")
