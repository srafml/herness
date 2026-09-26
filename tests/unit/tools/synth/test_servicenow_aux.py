"""Tests for tools.synth.servicenow_aux (U11-77): UT11-113."""

from datetime import UTC, date, datetime
from typing import Any

import numpy as np
import pytest

from tools.synth.catalog import Catalog, build_catalog
from tools.synth.params import SynthParams, load_params
from tools.synth.servicenow_aux import gen_departments, gen_task_slas
from tools.synth.servicenow_common import pair, span_start, ts_pair

pytestmark = pytest.mark.unit

_END = date(2024, 1, 31)
_HEX32_LEN = 32
_PRIORITIES = (1, 2, 3, 4, 5)


@pytest.fixture(scope="module")
def params() -> SynthParams:
    return load_params(
        "tiny",
        start=date(2024, 1, 1),
        end=_END,
        sources=("servicenow",),
        dirty="none",
        fetch_mode="initial",
        params_file=None,
    )


@pytest.fixture(scope="module")
def cat(params: SynthParams) -> Catalog:
    return build_catalog(7, params)


def _incident(seq: int, priority: int, *, made_sla: str, resolved: bool = True) -> dict[str, Any]:
    """A minimal incident record carrying only the fields gen_task_slas reads."""
    updated = datetime(2024, 1, 2, 3, 0, 0, tzinfo=UTC)
    return {
        "sys_id": pair(f"{seq:032x}"),
        "priority": pair(str(priority), f"{priority} - x"),
        "resolved_at": ts_pair(updated if resolved else None),
        "made_sla": pair(made_sla),
        "sys_updated_on": ts_pair(updated),
    }


def _tombstone(seq: int) -> dict[str, Any]:
    return {"__tombstone__": True, "key": f"servicenow:incident:{seq:032x}", "deleted_at": "x"}


@pytest.fixture
def incidents() -> list[dict[str, Any]]:
    """One resolved incident per priority (alternating made_sla), an open record and a
    tombstone marker (UT11-113 setup)."""
    resolved = [
        _incident(i, p, made_sla="false" if p % 2 == 0 else "true")
        for i, p in enumerate(_PRIORITIES, start=1)
    ]
    open_record = _incident(90, 1, made_sla="true", resolved=False)
    return [*resolved, open_record, _tombstone(91)]


def test_ut11_113_departments_one_per_org(cat: Catalog, params: SynthParams) -> None:
    """UT11-113 one department per org with the org's sys_id and cost_center, no parent,
    sys_updated_on = start at 00:00:00Z."""
    rng = np.random.default_rng(1)
    rows = gen_departments(cat, params, rng)
    assert len(rows) == len(cat.orgs)
    stamp = ts_pair(span_start(params))
    for row, org in zip(rows, cat.orgs, strict=True):
        assert set(row) == {"sys_id", "name", "parent", "cost_center", "sys_updated_on"}
        assert row["sys_id"]["value"] == org.sys_id
        assert row["cost_center"]["value"] == org.cost_center
        assert row["parent"]["value"] == ""
        assert row["sys_updated_on"] == stamp
    assert stamp["value"].endswith(" 00:00:00")


def test_ut11_113_departments_pairs_are_value_display(cat: Catalog, params: SynthParams) -> None:
    """UT11-113 every department field is a {value, display_value} pair."""
    rows = gen_departments(cat, params, np.random.default_rng(2))
    for row in rows:
        assert all(set(v) == {"value", "display_value"} for v in row.values())


def test_ut11_113_task_sla_one_row_per_resolved_incident(
    params: SynthParams, incidents: list[dict[str, Any]]
) -> None:
    """UT11-113 one task_sla row per resolved incident; no row for open records or
    tombstones."""
    rows = gen_task_slas(incidents, params, np.random.default_rng(3))
    assert len(rows) == len(_PRIORITIES)
    tasks = {row["task"]["value"] for row in rows}
    assert tasks == {inc["sys_id"]["value"] for inc in incidents[: len(_PRIORITIES)]}


def test_ut11_113_task_sla_fields(params: SynthParams, incidents: list[dict[str, Any]]) -> None:
    """UT11-113 task_sla sys_id is 32 lowercase hex, sla is 'P<priority> resolution',
    stage is 'completed', sys_updated_on copies the incident's value."""
    rows = gen_task_slas(incidents, params, np.random.default_rng(4))
    by_task = {row["task"]["value"]: row for row in rows}
    for i, priority in enumerate(_PRIORITIES, start=1):
        row = by_task[f"{i:032x}"]
        sys_id = row["sys_id"]["value"]
        assert len(sys_id) == _HEX32_LEN
        assert sys_id == sys_id.lower()
        int(sys_id, 16)  # valid hex
        assert row["sla"]["value"] == f"P{priority} resolution"
        assert row["stage"]["value"] == "completed"
        assert row["sys_updated_on"] == ts_pair(datetime(2024, 1, 2, 3, 0, 0, tzinfo=UTC))


def test_ut11_113_task_sla_sys_ids_are_unique(
    params: SynthParams, incidents: list[dict[str, Any]]
) -> None:
    """UT11-113 task_sla sys_ids are freshly drawn, not copied from the incident."""
    rows = gen_task_slas(incidents, params, np.random.default_rng(5))
    assert len({row["sys_id"]["value"] for row in rows}) == len(rows)
    incident_ids = {inc["sys_id"]["value"] for inc in incidents[: len(_PRIORITIES)]}
    assert not incident_ids & {row["sys_id"]["value"] for row in rows}


def test_ut11_113_has_breached_matches_made_sla(
    params: SynthParams, incidents: list[dict[str, Any]]
) -> None:
    """UT11-113 has_breached is 'true' exactly when the incident's made_sla is 'false'."""
    rows = gen_task_slas(incidents, params, np.random.default_rng(6))
    by_task = {row["task"]["value"]: row for row in rows}
    for inc in incidents[: len(_PRIORITIES)]:
        row = by_task[inc["sys_id"]["value"]]
        expected = "true" if inc["made_sla"]["value"] == "false" else "false"
        assert row["has_breached"]["value"] == expected
    breached = [r["has_breached"]["value"] for r in rows]
    assert "true" in breached
    assert "false" in breached


def test_ut11_113_no_row_for_open_or_tombstone(
    params: SynthParams, incidents: list[dict[str, Any]]
) -> None:
    """UT11-113 no task_sla row for open records (no resolved_at) or tombstone markers."""
    rows = gen_task_slas(incidents, params, np.random.default_rng(7))
    tasks = {row["task"]["value"] for row in rows}
    assert f"{90:032x}" not in tasks
    assert f"{91:032x}" not in tasks


def test_ut11_113_empty_incidents_gives_no_rows(params: SynthParams) -> None:
    """UT11-113 an empty incident list yields no task_sla rows."""
    assert gen_task_slas([], params, np.random.default_rng(8)) == []
