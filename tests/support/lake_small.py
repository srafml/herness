"""The `lake_small` fixture (impl 02 §11; stand-in for T11-16, cut by the controller).

`tests/fixtures/lake_small/raw/` is a committed raw lake in the connector layout
(`<source>/<entity>/dt=YYYY-MM-DD/part-<ulid>.parquet`, written by the real `LakeWriter`)
holding a few synthetic ServiceNow, Jira and monitoring records: every core table of
000-299 gets rows. `mappings.yaml` beside it is the matching `mappings.yaml` section.
The goldens `tests/fixtures/golden/lake_small_counts.json` (row counts of `core.*`) and
`lake_small_core.json` (sorted `core.*` rows, text hashed) are what the build of this lake
produces (IT02-21).

Regenerate the lake and both goldens after an intended change (then review the diff):

    PYTHONUTF8=1 uv run python -m tests.support.lake_small --regen

Every value is invented; the records contain no personal data (`fixtures-pii-scan`).
"""

from __future__ import annotations

import base64
import dataclasses
import datetime
import decimal
import hashlib
import json
import shutil
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

import duckdb
import pyarrow as pa
import yaml

from herness.core import config as herness_config
from herness.core.config import HernessConfig
from herness.store.lake import LakeWriter

REPO: Final = Path(__file__).resolve().parents[2]
FIXTURE: Final = REPO / "tests" / "fixtures" / "lake_small"
GOLDEN_COUNTS: Final = REPO / "tests" / "fixtures" / "golden" / "lake_small_counts.json"
GOLDEN_CORE: Final = REPO / "tests" / "fixtures" / "golden" / "lake_small_core.json"
SOURCES_YAML: Final = "version: 1\nbuild:\n  threads: 2\n"

_UTC = datetime.UTC
_T0 = datetime.datetime(2024, 3, 1, 12, 0, tzinfo=_UTC)
_TS = pa.timestamp("us", tz="UTC")
_HASH_BYTES = 10  # 16 base32 characters: not a hex string (detect-secrets)


@dataclasses.dataclass(frozen=True)
class Row:
    """One lake row: source key, hours after `_T0`, text fields; `deleted` is a tombstone."""

    key: str
    hours: int
    fields: Mapping[str, str | None] = dataclasses.field(default_factory=dict)
    deleted: bool = False


def _j(value: object) -> str:
    return json.dumps(value)


def _issue(key: str, kind: str, status: str, category: str, **extra: str) -> dict[str, str]:
    fields = {
        "key": key,
        "issuetype": _j({"name": kind}),
        "project": _j({"key": key.split("-", maxsplit=1)[0]}),
        "status": _j({"name": status, "statusCategory": {"key": category}}),
        "created": "2024-03-01T09:00:00.000+0000",
        "summary": f"work on {key}",
    }
    return fields | extra


_STATUS_CHANGE: Final = {
    "created": "2024-03-02T09:00:00.000+0000",
    "items": [{"field": "status", "fromString": "To Do", "toString": "Done"}],
}
_INCIDENT_BASE: Final = {
    "number": "INC0001",
    "opened_at": "2024-03-01 09:00:00",
    "priority": "2 - High",
    "state": "1",
    "business_service": "s1",
    "assignment_group": "g3",
    "short_description": "Checkout slow",
}

LAKE: Final[dict[tuple[str, str], list[Row]]] = {
    ("servicenow", "cmn_department"): [
        Row("d1", 0, {"sys_id": "d1", "name": "Engineering", "cost_center": "CC100"}),
        Row("d2", 0, {"sys_id": "d2", "name": "Operations", "cost_center": "CC200"}),
    ],
    ("servicenow", "sys_user_group"): [
        Row("g1", 0, {"sys_id": "g1", "name": "Payments Squad", "cost_center": "CC100"}),
        Row("g2", 0, {"sys_id": "g2", "name": "Platform Squad", "cost_center": "CC100"}),
        Row("g3", 0, {"sys_id": "g3", "name": "Service Desk", "cost_center": "CC200"}),
    ],
    ("servicenow", "cmdb_ci_service"): [
        Row("s1", 0, {"name": "Checkout", "owned_by": "g1", "busines_criticality": "1 - most"}),
        Row("s2", 0, {"name": "Search", "support_group": "g2", "busines_criticality": "3 - less"}),
    ],
    ("servicenow", "cmdb_ci"): [
        Row("c1", 0, {"name": "web-01", "sys_class_name": "cmdb_ci_server"}),
        Row("c2", 0, {"name": "db-01", "sys_class_name": "cmdb_ci_server"}),
    ],
    ("servicenow", "cmdb_rel_ci"): [
        Row("r1", 0, {"parent": "s1", "child": "c1", "type_display": "Depends on::Used by"}),
        Row("r2", 0, {"parent": "s2", "child": "c2", "type_display": "Depends on::Used by"}),
    ],
    ("servicenow", "change_request"): [
        Row(
            "ch1",
            0,
            {
                "number": "CHG0001",
                "business_service": "s1",
                "assignment_group": "g2",
                "short_description": "Deploy checkout release",
                "type": "normal",
                "state_display": "Closed",
                "risk_display": "Moderate",
                "opened_at": "2024-02-28 10:00:00",
                "start_date": "2024-02-29 20:00:00",
                "end_date": "2024-02-29 22:00:00",
                "work_start": "2024-02-29 20:05:00",
                "work_end": "2024-02-29 21:30:00",
                "close_code": "successful",
            },
        ),
        Row(
            "ch2",
            1,
            {
                "number": "CHG0002",
                "cmdb_ci": "c2",
                "short_description": "Patch search index",
                "type": "emergency",
                "state_display": "Closed",
                "opened_at": "2024-03-02 08:00:00",
                "close_code": "unsuccessful",
            },
        ),
    ],
    ("servicenow", "problem"): [
        Row(
            "p1",
            0,
            {
                "number": "PRB0001",
                "business_service": "s1",
                "assignment_group": "g1",
                "opened_at": "2024-03-01 12:00:00",
                "state_display": "Open",
                "known_error": "false",
                "cause_notes": "Connection pool exhausted",
            },
        )
    ],
    ("servicenow", "incident"): [
        Row("i1", 0, _INCIDENT_BASE),
        Row(
            "i1",
            2,
            _INCIDENT_BASE
            | {
                "state": "6",
                "resolved_at": "2024-03-01 10:30:00",
                "closed_at": "2024-03-02 10:30:00",
                "assignment_group": "g1",
                "close_code": "Solved",
                "close_notes": "Restarted the pool",
                "problem_id": "p1",
                "caused_by": "ch1",
                "made_sla": "false",
                "business_duration": "5400",
                "reassignment_count": "1",
                "reopen_count": "0",
            },
        ),
        Row(
            "i2",
            1,
            {
                "number": "INC0002",
                "opened_at": "2024-03-02 09:00:00",
                "priority": "3 - Moderate",
                "state": "1",
                "cmdb_ci": "c2",
                "assignment_group": "g2",
                "short_description": "Search results stale",
                "made_sla": "true",
            },
        ),
        Row(
            "i3",
            1,
            {
                "number": "INC0003",
                "opened_at": "2024-03-02 11:00:00",
                "priority": "1 - Critical",
                "state": "7",
                "business_service": "s1",
                "resolved_at": "2024-03-02 11:45:00",
                "closed_at": "2024-03-03 11:45:00",
                "assignment_group": "g1",
                "short_description": "Payment errors",
                "description": "Card payments failing at the gateway",
                "made_sla": "true",
                "business_duration": "1970-01-01 00:45:00",
                "caused_by": "ch2",
            },
        ),
        Row("i4", 0, {"number": "INC0004", "opened_at": "2024-03-01 13:00:00"}),
        Row("i4", 1, deleted=True),
        Row(
            "i5",
            1,
            {
                "number": "INC0005",
                "opened_at": "2024-03-03 09:00:00",
                "priority": "4 - Low",
                "state": "1",
                "short_description": "Access request",
            },
        ),
    ],
    ("servicenow", "task_sla"): [
        Row("t1", 0, {"task": "i1", "has_breached": "true"}),
        Row("t2", 0, {"task": "i3", "has_breached": "false"}),
    ],
    ("jira", "issue"): [
        Row(
            "1",
            0,
            _issue(
                "PAY-1", "Story", "In Progress", "indeterminate", components=_j([{"name": "api"}])
            ),
        ),
        Row(
            "2",
            1,
            _issue(
                "PAY-2",
                "Bug",
                "Done",
                "done",
                resolutiondate="2024-03-02T10:00:00.000+0000",
                changelog=_j({"histories": [_STATUS_CHANGE]}),
                issuelinks=_j([{"type": {"name": "Blocks"}, "outwardIssue": {"key": "PAY-1"}}]),
            ),
        ),
        Row("3", 0, _issue("OPS-1", "Task", "To Do", "new")),
    ],
    ("monitoring", "event"): [
        Row(
            f"datadog:{key}",
            0,
            {
                "source_tool": "datadog",
                "event_key": key,
                "ts": ts,
                "service": service,
                "host": f"host-{key}",
                "severity_raw": severity,
                "title": f"alert {key}",
                "status": "resolved",
                "dedup_key": f"dd-{key}",
                "end_ts": end,
                "incident_ref": ref,
            },
        )
        for key, ts, end, service, severity, ref in (
            ("e1", "2024-03-01 08:55:00", "2024-03-01 10:00:00", "Checkout", "crit", "INC0001"),
            ("e2", "2024-03-02 08:50:00", None, "Search", "warn", None),
            ("e3", "2024-03-02 09:10:00", "2024-03-02 09:20:00", "Unknown", "warn", None),
        )
    ],
    ("monitoring", "metric_daily"): [
        Row(
            f"m{n}",
            0,
            {
                "source_tool": "datadog",
                "metric_name": "availability",
                "unit": "percent",
                "service": service,
                "date": date,
                "value": value,
            },
        )
        for n, (service, date, value) in enumerate(
            (
                ("Checkout", "2024-03-01", "99.5"),
                ("Search", "2024-03-01", "99.9"),
                ("Checkout", "2024-03-02", "98.7"),
            ),
            start=1,
        )
    ],
}

MAPPINGS: Final[dict[str, object]] = {
    "version": 1,
    "enums": {
        "servicenow.incident_state": {
            "1": "open",
            "2": "in_progress",
            "6": "resolved",
            "7": "closed",
        },
        "servicenow.change_type": {"normal": "normal", "emergency": "emergency"},
        "servicenow.change_close_code": {
            "successful": "successful",
            "unsuccessful": "unsuccessful",
        },
        "monitoring.severity": {"crit": "critical", "warn": "warning"},
        "jira.issue_type": {"Story": "story", "Bug": "bug", "Task": "task"},
        "jira.status_category_key": {"new": "todo", "indeterminate": "in_progress", "done": "done"},
    },
    "service_overrides": [
        {
            "service_id": "servicenow:cmdb_ci:s1",
            "team_id": "servicenow:sys_user_group:g1",
            "jira_project": "PAY",
            "role": "delivery",
        }
    ],
}


def _batch(source: str, entity: str, rows: Sequence[Row]) -> pa.RecordBatch:
    names = list(dict.fromkeys(name for row in rows for name in row.fields))
    stamps = [_T0 + datetime.timedelta(hours=r.hours) for r in rows]
    columns: dict[str, pa.Array] = {
        "_record_id": pa.array([f"{source}:{entity}:{r.key}" for r in rows], pa.string()),
        "_source": pa.array([source] * len(rows), pa.string()),
        "_entity": pa.array([entity] * len(rows), pa.string()),
        "_source_key": pa.array([r.key for r in rows], pa.string()),
        "_source_updated_at": pa.array(stamps, _TS),
        "_fetched_at": pa.array(stamps, _TS),
        "_deleted": pa.array([r.deleted for r in rows], pa.bool_()),
        "_payload": pa.array(
            [None if r.deleted else json.dumps(dict(r.fields)) for r in rows], pa.string()
        ),
    }
    for name in names:
        columns[name] = pa.array([r.fields.get(name) for r in rows], pa.string())
    return pa.RecordBatch.from_pydict(columns)


def write_lake(raw: Path, lake: Mapping[tuple[str, str], Sequence[Row]] = LAKE) -> None:
    """Write `lake` under `raw` through the real `LakeWriter`, one file set per entity."""
    for (source, entity), rows in lake.items():
        with LakeWriter(source, entity, root=raw) as writer:
            writer.write(_batch(source, entity, rows))
            writer.commit()


def install(data_root: Path) -> Path:
    """Copy the committed lake to `data_root/raw`; return the raw root."""
    raw = data_root / "raw"
    shutil.copytree(FIXTURE / "raw", raw, dirs_exist_ok=True)
    return raw


def load_config(root: Path, data_root: Path) -> HernessConfig:
    """Write a full config tree with the lake_small `mappings.yaml`, init and return it."""
    from tests.support.config_tree import write_full_config  # noqa: PLC0415 - test-only helper

    cfg_dir = write_full_config(root)
    shutil.copyfile(FIXTURE / "mappings.yaml", cfg_dir / "mappings.yaml")
    (cfg_dir / "sources.yaml").write_text(SOURCES_YAML, encoding="utf-8")
    env = {"HERNESS_PATHS__DATA": str(data_root)}
    return herness_config.init_config("local", config_dir=cfg_dir, env=env)


def _value(value: object) -> object:
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        digest = hashlib.sha256(value.encode("utf-8")).digest()[:_HASH_BYTES]
        return "h:" + base64.b32encode(digest).decode("ascii").lower()
    if isinstance(value, datetime.datetime | datetime.date):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, list | tuple):
        return [_value(v) for v in value]
    return repr(value)


def core_snapshot(con: duckdb.DuckDBPyConnection) -> dict[str, dict[str, object]]:
    """Every `core.*` base table: column names and rows (text hashed), rows sorted."""
    tables = con.execute(
        "SELECT table_name FROM duckdb_tables() WHERE schema_name = 'core' ORDER BY table_name"
    ).fetchall()
    out: dict[str, dict[str, object]] = {}
    for (table,) in tables:
        cursor = con.execute(f'SELECT * FROM core."{table}"')  # noqa: S608 - catalog names
        columns = [d[0] for d in cursor.description]
        rows = [[_value(v) for v in row] for row in cursor.fetchall()]
        rows.sort(key=lambda r: json.dumps(r, sort_keys=True))
        out[f"core.{table}"] = {"columns": columns, "rows": rows}
    return out


def _dump(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, indent=1, sort_keys=True) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")  # LF on every OS (mixed-line-ending)


def regenerate() -> None:
    """Rewrite the committed lake, `mappings.yaml` and both goldens."""
    from tests.support.build_harness import FakeJobContext  # noqa: PLC0415 - test-only

    from herness.model.build import run_build_pipeline  # noqa: PLC0415
    from herness.store import warehouse  # noqa: PLC0415
    from herness.store.layout import DataLayout  # noqa: PLC0415
    from herness.store.ops import reset_connections  # noqa: PLC0415
    from herness.store.ops.migrate import migrate  # noqa: PLC0415

    shutil.rmtree(FIXTURE / "raw", ignore_errors=True)
    write_lake(FIXTURE / "raw")
    mappings = yaml.safe_dump(MAPPINGS, sort_keys=True)
    (FIXTURE / "mappings.yaml").write_text(mappings, encoding="utf-8", newline="\n")
    with tempfile.TemporaryDirectory() as tmp:
        data_root = Path(tmp) / "data"
        data_root.mkdir()
        install(data_root)
        load_config(Path(tmp) / "cfg", data_root)
        reset_connections(path=data_root / "ops.sqlite")
        migrate()
        outcome = run_build_pipeline(FakeJobContext({"stages": ["build"]}))
        build_id = str(outcome.result["build_id"])
        reset_connections()
        with warehouse.open_readonly(build_id, layout=DataLayout.from_root(data_root)) as con:
            _dump(GOLDEN_CORE, core_snapshot(con))
        _dump(GOLDEN_COUNTS, outcome.result["row_counts"])
        herness_config.reset_config()


if __name__ == "__main__":
    if sys.argv[1:] != ["--regen"]:
        sys.stderr.write("usage: python -m tests.support.lake_small --regen\n")
        sys.exit(2)
    regenerate()
