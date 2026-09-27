"""SQL constants and shared helpers of `herness.harness.warehouse_tools` (impl 05 U05-39-U05-46).

Private; split out so `warehouse_tools.py` keeps room for all eight tools within its budget.
Holds the internal constant SQL of the tools (run with `guard=False`, still guard-checked with
`allow_catalog` by `execute_recorded`), the typed argument access for tool keyword arguments
(dispatch already validated them against the schema; direct calls are checked here), the
strict-compatible schema builder (R-26), DuckDB identifier quoting, and the table result
shared by the SQL-backed tools: redact-on-read cells through `redact_text` (TH05-05), then
`format_result` (which wraps untrusted text) and the JSON-safe `data`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from types import MappingProxyType
from typing import Final

from pydantic import JsonValue

from herness.core.config import get_config
from herness.core.errors import ToolInputError
from herness.core.redact import redact_text
from herness.core.types import ToolResult
from herness.harness.tools import RecordedResult, format_result, json_safe

DESCRIPTION_CHARS: Final = 120

LIST_TABLES_SQL: Final = (
    "SELECT schema_name, table_name, estimated_size, comment FROM duckdb_tables()"
    " WHERE schema_name IN ('core', 'enrich', 'metrics', 'score', 'meta')"
    " AND ($schema IS NULL OR schema_name = $schema) ORDER BY schema_name, table_name"
)
DESCRIBE_COLUMNS_SQL: Final = (
    "SELECT column_name, data_type, is_nullable, comment FROM duckdb_columns()"
    " WHERE schema_name = $schema AND table_name = $table ORDER BY column_index"
)
SCORES_SQL: Final[Mapping[str, str]] = MappingProxyType(
    {
        "funding": "SELECT candidate_id, candidate_type, title, annual_pain_usd,"
        " addressable_pain_usd, expected_reduction, n_incidents, confidence, strategic_weight,"
        " effort_cost_usd, priority, wsjf, rank, unconfirmed, flags, query_ids FROM score.funding"
        " WHERE ($entity_id IS NULL OR candidate_id = $entity_id)"
        " ORDER BY rank, candidate_id LIMIT $top",
        "org": "SELECT entity_type, entity_id, metric, value, peer_group, peer_median, z_score,"
        " trend_slope, sample_size, composite, rank, unconfirmed, flags, query_ids FROM score.org"
        " WHERE ($entity_id IS NULL OR entity_id = $entity_id)"
        " ORDER BY rank, entity_id, metric LIMIT $top",
        "action_lever": "SELECT entity_type, entity_id, metric, target_kind, current_value,"
        " target_value, delta_usd, unconfirmed, query_ids FROM score.action_lever"
        " WHERE ($entity_id IS NULL OR entity_id = $entity_id)"
        " ORDER BY delta_usd DESC, entity_id, metric LIMIT $top",
        "portfolio": "SELECT scenario, budget_usd, candidate_id, selected, order_rank,"
        " expected_impact_usd, solver_status, flags, query_ids FROM score.portfolio"
        " WHERE ($scenario IS NULL OR scenario = $scenario)"
        " AND ($entity_id IS NULL OR candidate_id = $entity_id)"
        " ORDER BY scenario, order_rank NULLS LAST, candidate_id LIMIT $top",
    }
)


# --- argument access -------------------------------------------------------------------------


def _arg(args: Mapping[str, JsonValue], name: str) -> JsonValue:
    if name not in args:
        msg = f"missing argument {name}"
        raise ToolInputError(msg)
    return args[name]


def opt_text(args: Mapping[str, JsonValue], name: str) -> str | None:
    """A string-or-null argument; `ToolInputError` when missing or of another type."""
    value = _arg(args, name)
    if value is not None and not isinstance(value, str):
        msg = f"argument {name} must be a string or null"
        raise ToolInputError(msg)
    return value


def text(args: Mapping[str, JsonValue], name: str) -> str:
    """A string argument; `ToolInputError` when missing, null or of another type."""
    value = opt_text(args, name)
    if value is None:
        msg = f"argument {name} must be a string"
        raise ToolInputError(msg)
    return value


def integer(args: Mapping[str, JsonValue], name: str) -> int:
    """An integer argument (not a boolean); `ToolInputError` otherwise."""
    value = _arg(args, name)
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"argument {name} must be an integer"
        raise ToolInputError(msg)
    return value


def strict_schema(props: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """A strict-compatible object schema (R-26): every property required, no extras."""
    return {
        "type": "object",
        "properties": dict(props),
        "required": list(props),
        "additionalProperties": False,
    }


# --- catalog text, identifiers, config -------------------------------------------------------


def one_line(value: object, limit: int = DESCRIPTION_CHARS) -> str:
    """Catalog text on one line, cut to `limit`, `|` escaped (the content column separator)."""
    flat = " ".join(str(value or "").split())[:limit]
    return flat.replace("|", "\\|")


def quote(identifier: str) -> str:
    """DuckDB identifier quoting."""
    return '"' + identifier.replace('"', '""') + '"'


def blocked_columns() -> frozenset[str]:
    """`harness.sql.blocked_columns` of the process config, lower-case."""
    return frozenset(c.lower() for c in get_config().models.harness.sql.blocked_columns)


# --- table results ---------------------------------------------------------------------------


def _redact_cell(value: object) -> object:
    if value is None:
        return None
    raw = value if isinstance(value, str) else json.dumps(json_safe(value), ensure_ascii=False)
    return redact_text(raw)


def redacted(result: RecordedResult) -> RecordedResult:
    """`result` with the cells of its redact-on-read output columns redacted (TH05-05)."""
    if not result.redact_columns:
        return result
    flags = [name in result.redact_columns for name in result.columns]
    rows = [
        tuple(_redact_cell(v) if f else v for v, f in zip(row, flags, strict=True))
        for row in result.rows
    ]
    return replace(result, rows=rows)


def table_result(result: RecordedResult) -> ToolResult:
    """Redact, format and wrap one recorded table (U05-41 steps 2-4).

    `data = {columns, rows (JSON-safe, <= return_rows), row_count, truncated}` where
    `truncated` says rows were dropped by `return_rows`; `ToolResult.truncated` says the
    content shows fewer rows than the query returned.
    """
    safe = redacted(result)
    content, shown = format_result(safe)
    rows: list[JsonValue] = [[json_safe(cell) for cell in row] for row in safe.rows]
    columns: list[JsonValue] = list(safe.columns)
    data: dict[str, JsonValue] = {
        "columns": columns,
        "rows": rows,
        "row_count": safe.row_count,
        "truncated": safe.truncated,
    }
    return ToolResult(
        ok=True,
        content=content,
        data=data,
        query_ids=[safe.query_id],
        row_count=safe.row_count,
        truncated=shown < safe.row_count,
    )
