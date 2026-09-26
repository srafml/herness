"""Config and ops reference rows as DuckDB tables (impl 02 U02-87, U02-128).

Enum maps, service overrides, aliases, CI classes, deletion requests and approved mapping
suggestions reach the build as Arrow tables, never as SQL text (TH02-10), and the build
needs no DuckDB ``sqlite`` extension (DD02-02, TH02-12). Log fields carry counts only.
"""

from __future__ import annotations

import math
import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Final

import duckdb
import pyarrow as pa

from herness.core.errors import SchemaViolation
from herness.core.logging import get_logger
from herness.model.settings import BuildSettings, MappingsConfig

if TYPE_CHECKING:
    from herness.store.ops import ReviewItem

type _Row = tuple[object, ...]

_S: Final = pa.string()
_SCHEMAS: Final[Mapping[str, pa.Schema]] = {
    "enum_map": pa.schema([("domain", _S), ("source_value_lc", _S), ("canonical", _S)]),
    "service_override": pa.schema(
        [
            ("service_id", _S),
            ("team_id", _S),
            ("jira_project", _S),
            ("jira_component", _S),
            ("org_id", _S),
            ("role", _S),
        ]
    ),
    "service_alias": pa.schema([("alias_lc", _S), ("service_id", _S)]),
    "service_ci_class": pa.schema([("ci_class", _S)]),
    "deleted_record": pa.schema([("record_id", _S)]),
    "approved_mapping": pa.schema(
        [
            ("item_id", _S),
            ("subject_type", _S),
            ("jira_project", _S),
            ("jira_component", _S),
            ("team_id", _S),
            ("service_id", _S),
            ("score", pa.float64()),
        ]
    ),
}
_PREV_SCHEMA: Final = pa.schema([("table_name", _S), ("row_count", pa.int64())])
_SUBJECT_TYPES: Final = frozenset({"jira_component", "team"})
_OPTIONAL_TEXT: Final = ("jira_project", "jira_component", "team_id")
_TABLE_NAME_RE: Final = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")
_INT64_MAX: Final = 2**63 - 1

_log = get_logger("model.build")


def _unique(rows: Iterable[_Row]) -> list[_Row]:
    return list(dict.fromkeys(rows))


def _enum_rows(mappings: MappingsConfig) -> list[_Row]:
    return _unique(
        (domain, source.lower(), canonical)
        for domain, values in mappings.enums.items()
        for source, canonical in values.items()
    )


def _override_rows(mappings: MappingsConfig) -> list[_Row]:
    return [
        (o.service_id, o.team_id, o.jira_project, o.jira_component, o.org_id, o.role)
        for o in mappings.service_overrides
    ]


def _alias_rows(mappings: MappingsConfig) -> list[_Row]:
    return _unique(
        (alias.lower(), o.service_id) for o in mappings.service_overrides for alias in o.aliases
    )


def _is_score(value: object) -> bool:
    """A finite real number (not a bool), or absent."""
    if value is None:
        return True
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _approved_row(item: ReviewItem) -> _Row | None:
    """The ``stg.approved_mapping`` row of one item, or ``None`` when it is unusable."""
    payload = item.payload
    service_id, subject_type = payload.get("service_id"), payload.get("subject_type")
    if not isinstance(service_id, str) or not service_id or subject_type not in _SUBJECT_TYPES:
        return None
    optional = [payload.get(key) for key in _OPTIONAL_TEXT]
    score = payload.get("score")
    if any(v is not None and not isinstance(v, str) for v in optional) or not _is_score(score):
        return None
    jira_project, jira_component, team_id = optional
    value = None if score is None else float(score)  # type: ignore[arg-type]
    return (item.item_id, subject_type, jira_project, jira_component, team_id, service_id, value)


def _approved_rows(approved: Sequence[ReviewItem]) -> tuple[list[_Row], int]:
    rows: list[_Row] = []
    for item in approved:
        row = _approved_row(item)
        if row is not None:
            rows.append(row)
    return rows, len(approved) - len(rows)


def _arrow(schema: pa.Schema, rows: Sequence[_Row]) -> pa.Table:
    columns = list(zip(*rows, strict=True)) if rows else [()] * len(schema)
    return pa.table(
        [pa.array(list(col), type=f.type) for col, f in zip(columns, schema, strict=True)],
        schema=schema,
    )


def _register(con: duckdb.DuckDBPyConnection, name: str, table: pa.Table) -> None:
    """``stg.<name>`` := ``table`` via a registered Arrow view; ``name`` is a module constant."""
    view = f"_ref_{name}"
    try:
        con.register(view, table)
        try:
            con.execute(f"CREATE OR REPLACE TABLE stg.{name} AS SELECT * FROM {view}")  # noqa: S608 - constant names
        finally:
            con.unregister(view)
    except duckdb.Error as exc:
        msg = f"refdata registration failed: stg.{name}"
        raise SchemaViolation(msg) from exc


def register_reference_tables(
    con: duckdb.DuckDBPyConnection,
    *,
    mappings: MappingsConfig,
    build_cfg: BuildSettings,
    deleted_ids: Collection[str],
    approved: Sequence[ReviewItem],
) -> dict[str, int]:
    """Create the six ``stg`` reference tables (U02-87); return the row count per table.

    Approved items without a ``service_id``, with a ``subject_type`` outside
    {``jira_component``, ``team``} or with ill-typed payload fields are skipped and counted.
    Raises SchemaViolation when DuckDB rejects a table.
    """
    approved_rows, skipped = _approved_rows(approved)
    if skipped:
        _log.warning("model.build.mapping_skipped", count=skipped)
    rows: dict[str, list[_Row]] = {
        "enum_map": _enum_rows(mappings),
        "service_override": _override_rows(mappings),
        "service_alias": _alias_rows(mappings),
        "service_ci_class": _unique((c,) for c in build_cfg.service_ci_classes),
        "deleted_record": [(record_id,) for record_id in sorted(set(deleted_ids))],
        "approved_mapping": approved_rows,
    }
    counts: dict[str, int] = {}
    for name, table_rows in rows.items():
        _register(con, name, _arrow(_SCHEMAS[name], table_rows))
        counts[f"stg.{name}"] = len(table_rows)
    _log.info("model.build.refdata_registered", **{name: len(r) for name, r in rows.items()})
    return counts


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= _INT64_MAX


def register_prev_row_counts(
    con: duckdb.DuckDBPyConnection, counts: Mapping[str, int] | None
) -> None:
    """Create ``stg.prev_row_counts`` from the promoted build's row counts (U02-128).

    Empty when ``counts`` is ``None``; keys that are not ``schema.table`` identifiers and
    values that are not non-negative 64-bit integers are skipped. Raises SchemaViolation.
    """
    rows = sorted(
        (name, value)
        for name, value in (counts or {}).items()
        if _TABLE_NAME_RE.fullmatch(name) and _is_count(value)
    )
    _register(con, "prev_row_counts", _arrow(_PREV_SCHEMA, rows))
