"""`get_cluster` and `get_record` of the spec 05 warehouse tools (impl 05 U05-44, U05-45); private.

Split out of `warehouse_tools` (module budget; re-exported there). Every query is internal
constant SQL through `execute_recorded(guard=False)`, except the `get_record` row query, whose
identifiers come from the build's schema (allow-list) minus the blocked columns, DuckDB-quoted.
Ticket text reaches the model only from `enrich.text_redacted`, wrapped by `wrap_untrusted`
with its `record_id` (TH05-01); redact-on-read cells pass `redact_text` (TH05-05).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Final

from pydantic import JsonValue

from herness.core.errors import QueryError, ToolInputError
from herness.core.types import ToolContext, ToolResult
from herness.harness import _warehouse_tools_sql as ws
from herness.harness.sql_guard import _norm as norm_identifier
from herness.harness.tools import RecordedResult, execute_recorded, json_safe, wrap_untrusted

CLUSTER_ROW_SQL: Final = (
    "SELECT cluster_id, label, root_cause_category, size, first_seen, last_seen, top_terms,"
    " service_ids FROM enrich.cluster WHERE cluster_id = $cluster_id"
)
CLUSTER_COUNTS_SQL: Final = (
    "SELECT i.service_id, date_trunc('month', i.opened_at) AS month, count(*) AS incidents"
    " FROM enrich.cluster_member m JOIN core.incident i ON i.record_id = m.record_id"
    " WHERE m.cluster_id = $cluster_id GROUP BY i.service_id, date_trunc('month', i.opened_at)"
    " ORDER BY month, i.service_id"
)
CLUSTER_SAMPLE_SQL: Final = (
    "SELECT m.record_id, t.text FROM enrich.cluster_member m"
    " JOIN enrich.text_redacted t ON t.record_id = m.record_id WHERE m.cluster_id = $cluster_id"
    " ORDER BY m.membership_prob DESC, m.record_id LIMIT $n"
)
LOCATE_RECORD_SQL: Final = (
    "SELECT 'incident' AS entity FROM core.incident WHERE record_id = $record_id"
    " UNION ALL SELECT 'change' FROM core.change WHERE record_id = $record_id"
    " UNION ALL SELECT 'problem' FROM core.problem WHERE record_id = $record_id"
    " UNION ALL SELECT 'work_item' FROM core.work_item WHERE record_id = $record_id"
)
RECORD_TEXT_SQL: Final = "SELECT text FROM enrich.text_redacted WHERE record_id = $record_id"
RECORD_DECISIONS_SQL: Final = (
    "SELECT question, answer, probability FROM enrich.decision WHERE record_id = $record_id"
    " ORDER BY question"
)
RECORD_CLUSTERS_SQL: Final = (
    "SELECT cluster_id, membership_prob FROM enrich.cluster_member WHERE record_id = $record_id"
    " ORDER BY membership_prob DESC, cluster_id"
)
MAX_SAMPLE: Final = 20
CELL_CHARS: Final = 300
TEXT_SOURCE: Final = "enrich.text_redacted"
_RECORD_TABLES: Final = ("incident", "change", "problem", "work_item")
_CLUSTER_TABLES: Final = ("enrich.cluster", "enrich.cluster_member", "core.incident", TEXT_SOURCE)
_RECORD_SOURCES: Final = (
    *(f"core.{t}" for t in _RECORD_TABLES), TEXT_SOURCE, "enrich.decision", "enrich.cluster_member",
)  # fmt: skip


def require_tables(ctx: ToolContext, tables: Iterable[str]) -> None:
    """`QueryError` for a table missing from this build (never a fatal internal-SQL error)."""
    schema = ctx.warehouse.schema()
    for qualified in tables:
        schema_name, _, table = qualified.partition(".")
        if table not in schema.get(schema_name, {}):
            msg = f"table {qualified} is not in this build"
            raise QueryError(msg, hint="call list_tables")


def _cell(value: object) -> str:
    safe = json_safe(value)
    text = "NULL" if safe is None else safe if isinstance(safe, str) else json.dumps(safe)
    return ws.one_line(text, CELL_CHARS) or '""'


def row_lines(
    r: RecordedResult, *, record_id: str | None
) -> tuple[list[str], dict[str, JsonValue]]:
    """The first row as `column: value` lines (redacted, untrusted cells wrapped) and a dict."""
    safe = ws.redacted(r)
    lines: list[str] = []
    data: dict[str, JsonValue] = {}
    for name, value in zip(safe.columns, safe.rows[0], strict=True):
        text = _cell(value)
        if name in safe.untrusted_columns:
            text = wrap_untrusted(text, source="warehouse", record_id=record_id)
        lines.append(f"{name}: {text}")
        data[name] = json_safe(value)
    return lines, data


def _wrapped_text(record_id: str, text: object) -> str:
    return wrap_untrusted(str(text), source=TEXT_SOURCE, record_id=record_id)


# --- U05-44 get_cluster ----------------------------------------------------------------------


class GetCluster:
    """`get_cluster`: cluster row, member counts by service and month, redacted samples."""

    name = "get_cluster"
    description = (
        "Read one incident cluster: its row, member counts by service and month, and up to 20"
        " redacted sample texts (most typical members first)."
    )
    input_schema: dict[str, JsonValue] = ws.strict_schema(
        {
            "cluster_id": {"type": "string", "pattern": "^cl_[0-9A-HJKMNP-TV-Z]{26}$"},
            "sample": {"type": "integer", "minimum": 0, "maximum": MAX_SAMPLE},
        }
    )

    def __call__(self, ctx: ToolContext, **kwargs: JsonValue) -> ToolResult:
        cluster_id, sample = ws.text(kwargs, "cluster_id"), ws.integer(kwargs, "sample")
        if not 0 <= sample <= MAX_SAMPLE:
            msg = f"sample must be 0-{MAX_SAMPLE}"
            raise ToolInputError(msg)
        require_tables(ctx, _CLUSTER_TABLES)
        params: dict[str, JsonValue] = {"cluster_id": cluster_id}
        row = execute_recorded(ctx, CLUSTER_ROW_SQL, params, guard=False)
        if not row.rows:
            msg = f"cluster {cluster_id[:64]} not found"
            raise ToolInputError(msg)
        lines, cluster = row_lines(row, record_id=None)
        counts = ws.table_result(execute_recorded(ctx, CLUSTER_COUNTS_SQL, params, guard=False))
        parts = [f"query_id={row.query_id} cluster:", *lines, "member counts:", counts.content]
        query_ids = [row.query_id, *counts.query_ids]
        samples: list[JsonValue] = []
        if sample > 0:
            params["n"] = sample
            texts = execute_recorded(ctx, CLUSTER_SAMPLE_SQL, params, guard=False)
            parts.append(f"query_id={texts.query_id} samples={len(texts.rows)}")
            for record_id, text in texts.rows:
                parts += [str(record_id), _wrapped_text(str(record_id), text)]
                samples.append({"record_id": str(record_id), "text": str(text)})
            query_ids.append(texts.query_id)
        data: dict[str, JsonValue] = {"cluster": cluster, "counts": counts.data}
        data["samples"] = samples
        return ToolResult(ok=True, content="\n".join(parts), data=data, query_ids=query_ids)


# --- U05-45 get_record -----------------------------------------------------------------------


def _row_sql(ctx: ToolContext, table: str) -> str:
    """The located table's allow-listed columns minus the blocked ones, quoted (U05-45 step 2)."""
    prefix, blocked = f"core.{table}.", ws.blocked_columns()
    columns = [
        c
        for c in ctx.warehouse.schema()["core"][table]
        if norm_identifier(prefix + c) not in blocked
    ]
    select = ", ".join(ws.quote(c) for c in columns)
    return f"SELECT {select} FROM core.{ws.quote(table)} WHERE record_id = $record_id"  # noqa: S608 - allow-listed, quoted identifiers


def _section(title: str, result: ToolResult) -> list[str]:
    return [f"{title}:", result.content]


class GetRecord:
    """`get_record`: one `core` row without blocked columns, its redacted text, decisions and
    cluster membership (U05-45)."""

    name = "get_record"
    description = (
        "Read one incident, change, problem or work item by record_id: its non-text columns,"
        " its redacted text, the enrichment decisions and its cluster memberships."
    )
    input_schema: dict[str, JsonValue] = ws.strict_schema(
        {"record_id": {"type": "string", "pattern": "^[a-z0-9_]+:[a-z0-9_]+:[^\\s]{1,200}$"}}
    )

    def __call__(self, ctx: ToolContext, **kwargs: JsonValue) -> ToolResult:
        record_id = ws.text(kwargs, "record_id")
        require_tables(ctx, _RECORD_SOURCES)
        params: dict[str, JsonValue] = {"record_id": record_id}
        located = execute_recorded(ctx, LOCATE_RECORD_SQL, params, guard=False)
        if not located.rows or located.rows[0][0] not in _RECORD_TABLES:
            msg = f"record not found in build {ctx.build_id}"
            raise ToolInputError(msg)
        table = str(located.rows[0][0])
        row = execute_recorded(ctx, _row_sql(ctx, table), params, guard=False)
        lines, record = row_lines(row, record_id=record_id)
        text = execute_recorded(ctx, RECORD_TEXT_SQL, params, guard=False)
        body = str(text.rows[0][0]) if text.rows and text.rows[0][0] is not None else None
        decisions = ws.table_result(
            execute_recorded(ctx, RECORD_DECISIONS_SQL, params, guard=False)
        )
        clusters = ws.table_result(execute_recorded(ctx, RECORD_CLUSTERS_SQL, params, guard=False))
        parts = [f"query_id={row.query_id} record_id={record_id} table=core.{table}", *lines]
        parts += [f"text: query_id={text.query_id}"]
        parts += [_wrapped_text(record_id, body) if body is not None else "no redacted text"]
        parts += _section("decisions", decisions) + _section("clusters", clusters)
        query_ids = [located.query_id, row.query_id, text.query_id]
        query_ids += decisions.query_ids + clusters.query_ids
        data: dict[str, JsonValue] = {"table": f"core.{table}", "record": record, "text": body}
        data |= {"decisions": decisions.data, "clusters": clusters.data}
        return ToolResult(ok=True, content="\n".join(parts), data=data, query_ids=query_ids)
