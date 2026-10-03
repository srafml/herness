"""Source-shaped records to raw-lake Arrow batches (U11-18, impl 01 raw column contract, R-59).

ServiceNow records go through `herness.connectors.rows.flatten_record` with the connector's
fetch-field list and display pairs; Jira issues through `herness.connectors.jira.flatten_issue`
(the single implementation of the Jira columns), so the generator writes exactly what the
connectors write. Monitoring rows use the impl 01 `EVENT_COLUMNS` / `METRIC_COLUMNS`. A
field that no live row of the batch carries is left out (schema drift: `u_business_impact`
before month 25, the cost custom field in the drift Jira shard), the way a source that
lacks a field produces files without its column. Tombstone markers
(`{"__tombstone__": True, "key", "deleted_at"}`) become `_deleted = true` rows with a NULL
`_payload` and NULL fields.
"""

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final

import pyarrow as pa

from herness.connectors import jira as _jira
from herness.connectors.base import METADATA_SCHEMA, record_id
from herness.connectors.monitoring.base import EVENT_COLUMNS, METRIC_COLUMNS
from herness.connectors.rows import flatten_record, parse_source_timestamp
from herness.core.errors import SchemaViolation
from tools.synth.params import SynthUsageError

__all__ = [
    "MAX_ROWS",
    "SERVICENOW_FIELDS",
    "SYNTH_CUSTOM_FIELD_IDS",
    "source_updated_at",
    "to_lake_batch",
]

Rec = Mapping[str, Any]
Flat = dict[str, str | None]

MAX_ROWS: Final = 131_072
SYNTH_CUSTOM_FIELD_IDS: Final = ("customfield_10016", "customfield_10050", "customfield_10060")
# T11-16: replace with the synth profile's `sources.servicenow` field list
# (config/profiles/synth.yaml); until then, the generator's field set per entity (U11-07,
# U11-77, design §5.1.2), so generator and connector produce the same columns.
SERVICENOW_FIELDS: Final[Mapping[str, tuple[str, ...]]] = {
    "sys_user_group": ("name", "parent", "manager", "cost_center", "active", "type"),
    "cmn_department": ("name", "parent", "cost_center"),
    "cmdb_ci": ("name", "owned_by", "support_group", "cost_center", "company"),
    "cmdb_ci_service": ("name", "sys_class_name", "owned_by", "support_group", "cost_center",
                        "company", "busines_criticality"),
    "cmdb_rel_ci": ("parent", "child", "type"),
    "incident": ("number", "opened_at", "u_acknowledged_at", "resolved_at", "closed_at",
                 "priority", "state", "business_service", "cmdb_ci", "assignment_group",
                 "reassignment_count", "reopen_count", "short_description", "description",
                 "close_notes", "close_code", "problem_id", "caused_by", "made_sla",
                 "business_duration", "u_customer_impact_minutes", "u_business_impact"),
    "change_request": ("number", "type", "state", "risk", "opened_at", "start_date", "end_date",
                       "work_start", "work_end", "business_service", "cmdb_ci",
                       "assignment_group", "close_code", "short_description", "description"),
    "problem": ("number", "opened_at", "resolved_at", "state", "business_service",
                "assignment_group", "known_error", "cause_notes"),
    "task_sla": ("task", "sla", "stage", "has_breached"),
}  # fmt: skip
_WATERMARK: Final = "sys_updated_on"
_MONITORING: Final = {"event": EVENT_COLUMNS, "metric_daily": METRIC_COLUMNS}
_DAY: Final = timedelta(days=1)
_UTC_US: Final = METADATA_SCHEMA.field("_fetched_at").type


def _payload(record: Rec) -> str:
    return json.dumps(record, separators=(",", ":"), sort_keys=True)


def _fail(field: str, source: str, entity: str) -> SchemaViolation:
    return SchemaViolation(f"missing key field {field}", source=source, entity=entity)


def _fetch_fields(entity: str) -> tuple[str, ...]:
    """The connector's head columns (`sys_id`, `sys_updated_on`, `sys_class_name` on
    `cmdb_ci`) then the entity's fields, without duplicates."""
    if entity not in SERVICENOW_FIELDS:
        msg = f"unknown servicenow entity {entity}"
        raise SynthUsageError(msg, key="entity")
    head = ["sys_id", _WATERMARK] + (["sys_class_name"] if entity == "cmdb_ci" else [])
    return tuple(dict.fromkeys([*head, *SERVICENOW_FIELDS[entity]]))


def _value(record: Rec, field: str) -> object:
    value = record.get(field)
    return value.get("value") if isinstance(value, Mapping) else value


def source_updated_at(source: str, entity: str, record: Rec) -> datetime:
    """`_source_updated_at` of a record: `sys_updated_on`, Jira `updated`, event `ts`, metric
    `date` end of day (next midnight, as the monitoring connector), tombstone `deleted_at`."""
    if record.get("__tombstone__"):
        return parse_source_timestamp(record.get("deleted_at"), field="deleted_at")
    if source == "jira":
        fields = record.get("fields")
        value = fields.get("updated") if isinstance(fields, Mapping) else None
        return parse_source_timestamp(value, field="updated")
    if source == "monitoring" and entity == "metric_daily":
        day = date.fromisoformat(str(record.get("date")))
        return datetime(day.year, day.month, day.day, tzinfo=UTC) + _DAY
    field = "ts" if source == "monitoring" else _WATERMARK
    return parse_source_timestamp(_value(record, field), field=field)


def _key(source: str, entity: str, record: Rec) -> str:
    if record.get("__tombstone__"):
        field, value = "key", record.get("key")
    elif source == "servicenow":
        field, value = "sys_id", _value(record, "sys_id")
    elif source == "jira":
        field, value = "id", record.get("id")
    else:
        field, value = "_source_key", record.get("_source_key")
    if not isinstance(value, str) or not value:
        raise _fail(field, source, entity)
    return value


def _bare(record: Rec) -> Rec:
    """`priority` rendered as the bare value (`"2"`) in its display too (schema drift)."""
    priority = record.get("priority")
    if not isinstance(priority, Mapping):
        return record
    return {**record, "priority": {"value": priority["value"], "display_value": priority["value"]}}


def _present(fields: Sequence[str], records: Sequence[Rec]) -> list[str]:
    return [f for f in fields if any(f in rec for rec in records)]


def _flatten_all(source: str, entity: str, live: Sequence[Rec]) -> list[Flat]:
    if source == "servicenow":
        fields = _present(_fetch_fields(entity), live)
        return [flatten_record(rec, fields=fields, display_pairs=True) for rec in live]
    if source == "jira":
        bodies = [rec["fields"] for rec in live]
        custom = _present(SYNTH_CUSTOM_FIELD_IDS, bodies)
        return [
            _jira.flatten_issue(
                rec,
                changelog=rec["changelog"]["histories"],
                remotelinks=rec["remotelinks"],
                custom_field_ids=custom,
            )
            for rec in live
        ]
    return [flatten_record(rec, fields=_MONITORING[entity]) for rec in live]


def _check(source: str, entity: str, n: int) -> None:
    if source not in {"servicenow", "jira", "monitoring"}:
        msg = f"unknown source {source}"
        raise SynthUsageError(msg, key="source")
    if source == "monitoring" and entity not in _MONITORING:
        msg = f"unknown monitoring entity {entity}"
        raise SynthUsageError(msg, key="entity")
    if n > MAX_ROWS:
        msg = f"at most {MAX_ROWS} rows per batch"
        raise SynthUsageError(msg, key="rows")


def to_lake_batch(
    source: str, entity: str, rows: Sequence[tuple[Rec, datetime]], *, bare_priority: bool
) -> pa.RecordBatch:
    """One raw-lake batch of `(record, _fetched_at)` rows: the 8 metadata columns, then
    the flattened string columns (union across rows in first-seen order, missing NULL).
    `bare_priority` renders ServiceNow `priority` as the bare value (drift, month 30+)."""
    _check(source, entity, len(rows))
    records = [_bare(r) if bare_priority and source == "servicenow" else r for r, _ in rows]
    keys = [_key(source, entity, rec) for rec in records]
    dead = [bool(rec.get("__tombstone__")) for rec in records]
    live = [rec for rec, gone in zip(records, dead, strict=True) if not gone]
    flat = iter(_flatten_all(source, entity, live) if live else [])
    cols: list[Flat] = [{} if gone else next(flat) for gone in dead]
    names = list(dict.fromkeys(name for row in cols for name in row))
    payloads = [None if gone else _payload(rec) for rec, gone in zip(records, dead, strict=True)]
    times = [source_updated_at(source, entity, rec) for rec in records]
    arrays: list[pa.Array] = [
        pa.array([record_id(source, entity, k) for k in keys], pa.string()),
        pa.array([source] * len(rows), pa.string()),
        pa.array([entity] * len(rows), pa.string()),
        pa.array(keys, pa.string()),
        pa.array(times, _UTC_US),
        pa.array([at for _, at in rows], _UTC_US),
        pa.array(dead, pa.bool_()),
        pa.array(payloads, pa.string()),
    ]
    arrays += [pa.array([row.get(name) for row in cols], pa.string()) for name in names]
    schema = pa.schema([*METADATA_SCHEMA, *(pa.field(n, pa.string()) for n in names)])
    return pa.RecordBatch.from_arrays(arrays, schema=schema)
