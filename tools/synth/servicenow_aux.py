"""ServiceNow department and task SLA records (U11-77, R-60).

`gen_departments` writes `cmn_department` (one row per catalog org) and `gen_task_slas`
writes `task_sla` (one row per resolved incident), the two entities impl 01 syncs and
impl 02 staging reads alongside `cmdb_ci_service`. Both reuse the `{value, display_value}`
pair shape and internal timestamp format of `tools.synth.servicenow_common` so the rows
match the rest of the ServiceNow generators (U11-07).

Tombstone markers are not yet produced in this tree (`tools.synth.dirty`, U11-21, is a
later card); per the algorithm in `docs/impl/11-testing-eval-synthetic-data.impl.md`
(U11-21 `apply_dirty`), a tombstone is `{"__tombstone__": True, "key": ..., "deleted_at":
...}` with no incident fields. `gen_task_slas` treats any record carrying that flag, and
any record with no `resolved_at` value (open records), as having no `task_sla` row.
"""

from collections.abc import Sequence
from typing import cast

import numpy as np

from tools.synth.catalog_rows import Catalog
from tools.synth.params import SynthParams
from tools.synth.servicenow_common import Record, new_sys_id, pair, span_start, ts_pair

__all__ = ["gen_departments", "gen_task_slas"]


def gen_departments(cat: Catalog, params: SynthParams, rng: np.random.Generator) -> list[Record]:
    """`cmn_department` rows: one per `OrgRow`, no parent, stamped at the span start.

    Deterministic given the catalog and params; `rng` is unused (kept for the shared
    `gen_*(cat, params, rng)` generator signature)."""
    del rng
    stamp = ts_pair(span_start(params))
    return [
        {
            "sys_id": pair(org.sys_id),
            "name": pair(org.name),
            "parent": pair(""),
            "cost_center": pair(org.cost_center),
            "sys_updated_on": stamp,
        }
        for org in cat.orgs
    ]


def _is_resolved(incident: dict[str, object]) -> bool:
    if incident.get("__tombstone__"):
        return False
    resolved = incident.get("resolved_at")
    return isinstance(resolved, dict) and bool(resolved.get("value"))


def gen_task_slas(
    incidents: Sequence[dict[str, object]], params: SynthParams, rng: np.random.Generator
) -> list[Record]:
    """`task_sla` rows: one per incident with a `resolved_at` value; tombstone markers and
    open records (no `resolved_at`) get no row."""
    del params
    rows: list[Record] = []
    for incident in incidents:
        if not _is_resolved(incident):
            continue
        rec = cast(Record, incident)
        breached = rec["made_sla"]["value"] == "false"
        rows.append(
            {
                "sys_id": pair(new_sys_id(rng)),
                "task": pair(rec["sys_id"]["value"]),
                "sla": pair(f"P{rec['priority']['value']} resolution"),
                "stage": pair("completed"),
                "has_breached": pair("true" if breached else "false"),
                "sys_updated_on": rec["sys_updated_on"],
            }
        )
    return rows
