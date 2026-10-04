"""A synthetic ServiceNow of any size for the connector benchmarks (impl 01 BT01-01, BT01-03).

T11-15 (``tools.synth.api_pages.write_api_pages``) is not on the tree, so this card-local
stand-in generates the Table API answers the way ``tests.support.sn_cassettes`` shapes them
(``sysparm_display_value=all`` pairs, synthetic host, ``synthetic`` credentials, R-67), but
page by page from arithmetic instead of from an in-memory table: incident ``i`` has the
``sys_id`` ``key(i)`` and ``sys_updated_on = T0 + i`` seconds, so any window, offset and key
listing of up to millions of rows is answered in constant memory. It is served through the
``httpx2.MockTransport`` pool of ``tests.unit.connectors._servicenow_env`` (respx patches only
``httpx``), so the production HTTP layer, auth and connector run for real and no socket opens.
"""

from __future__ import annotations

import datetime
import json
import os
import re
from dataclasses import dataclass, field
from typing import Final

import httpx2
import pyarrow as pa
from tests.support.sn_cassettes import T0, TABLE_PATH, sn_time

from herness.connectors.base import METADATA_SCHEMA

ROWS_ENV: Final = "HERNESS_BT01_ROWS"  # a dry run may use fewer rows; the default is the spec
KEYS_ENV: Final = "HERNESS_BT01_KEYS"
SPEC_ROWS: Final = 1_000_000
SPEC_KEYS: Final = 5_000_000
_WINDOW: Final = re.compile(r"^sys_updated_on(>=|<)(.*)$")
_STATES: Final = (("1", "New"), ("2", "In Progress"), ("6", "Resolved"), ("7", "Closed"))
_FMT: Final = "%Y-%m-%d %H:%M:%S"


def rows_wanted() -> int:
    """Rows of the BT01-01 dataset (spec: 1,000,000)."""
    return int(os.environ.get(ROWS_ENV, SPEC_ROWS))


def keys_wanted() -> int:
    """Keys of the BT01-03 reconcile (spec: 5,000,000)."""
    return int(os.environ.get(KEYS_ENV, SPEC_KEYS))


def key(i: int) -> str:
    """The 32-hex ``sys_id`` of record ``i`` (ascending in ``i``, mostly zeros)."""
    return f"{i:032x}"


def _pair(value: str, display: str | None = None) -> dict[str, str]:
    return {"value": value, "display_value": value if display is None else display}


def _incident(i: int) -> dict[str, dict[str, str]]:
    state, label = _STATES[i % 4]
    stamp = sn_time(T0 + datetime.timedelta(seconds=i))
    opened = sn_time(T0 + datetime.timedelta(seconds=max(0, i - 3600)))
    priority = str(i % 5 + 1)
    return {
        "sys_id": _pair(key(i)),
        "sys_updated_on": _pair(stamp),
        "number": _pair(f"INC{i:07d}"),
        "opened_at": _pair(opened),
        "priority": _pair(priority, f"{priority} - P{priority}"),
        "state": _pair(state, label),
        "assignment_group": _pair(key(i % 97), f"Synthetic Group {i % 97}"),
    }


def _bound(query: str, op: str, default: int) -> int:
    """The record index named by the ``sys_updated_on`` ``op`` clause of ``query``."""
    for clause in query.split("^"):
        found = _WINDOW.match(clause)
        if found is not None and found.group(1) == op:
            moment = datetime.datetime.strptime(found.group(2), _FMT).replace(tzinfo=datetime.UTC)
            return max(0, int((moment - T0).total_seconds()))
    return default


@dataclass
class SyntheticServiceNow:
    """``rows`` incidents (windows by ``sys_updated_on``); ``keys`` is the length of the key
    listing (keys ``key(0)`` to ``key(keys - 1)``)."""

    rows: int
    keys: int = 0
    served_rows: int = 0
    requests: list[str] = field(default_factory=list)

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        table = request.url.path.removeprefix(TABLE_PATH)
        params = request.url.params
        offset, limit = int(params.get("sysparm_offset", "0")), int(params["sysparm_limit"])
        query = params.get("sysparm_query", "")
        self.requests.append(table)
        if table != "incident":  # sys_audit_delete: nothing was ever deleted
            return httpx2.Response(200, json={"result": []})
        if params.get("sysparm_fields") == "sys_id":  # the key listing (reconcile)
            result = [{"sys_id": key(i)} for i in range(offset, min(self.keys, offset + limit))]
        else:
            lo, hi = _bound(query, ">=", 0), _bound(query, "<", self.rows)
            first = lo + offset
            end = min(hi, self.rows, first + limit)
            result = [_incident(i) for i in range(first, max(first, end))]
            self.served_rows += len(result)
        return httpx2.Response(200, content=json.dumps({"result": result}).encode())


def metadata_batch(lo: int, hi: int) -> pa.RecordBatch:
    """Metadata-only lake rows for keys ``lo ..< hi`` (live, no entity columns)."""
    keys = [key(i) for i in range(lo, hi)]
    n = len(keys)
    stamp = pa.array([T0] * n, METADATA_SCHEMA.field("_source_updated_at").type)
    columns = [
        pa.array([f"servicenow:incident:{k}" for k in keys], pa.string()),
        pa.array(["servicenow"] * n, pa.string()),
        pa.array(["incident"] * n, pa.string()),
        pa.array(keys, pa.string()),
        stamp,
        stamp,
        pa.array([False] * n, pa.bool_()),
        pa.array(["{}"] * n, pa.string()),
    ]
    return pa.RecordBatch.from_arrays(columns, schema=METADATA_SCHEMA)


_ENTITY_COLUMNS: Final = ("number", "opened_at", "priority", "state", "assignment_group")


def incident_batch(lo: int, hi: int) -> pa.RecordBatch:
    """Lake rows shaped like the ServiceNow connector's: the metadata, the five configured
    entity columns and the full record as the ``_payload`` JSON, for keys ``lo ..< hi``."""
    base = metadata_batch(lo, hi)
    records = [_incident(i) for i in range(lo, hi)]
    payload = pa.array([json.dumps(r, separators=(",", ":")) for r in records], pa.string())
    columns = [*base.columns[:-1], payload]
    fields = list(METADATA_SCHEMA)
    for name in _ENTITY_COLUMNS:
        columns.append(pa.array([r[name]["value"] for r in records], pa.string()))
        fields.append(pa.field(name, pa.string()))
    return pa.RecordBatch.from_arrays(columns, schema=pa.schema(fields))
