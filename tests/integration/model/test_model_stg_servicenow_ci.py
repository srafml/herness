"""ServiceNow CI and absent-entity staging of `110_stg_servicenow.sql` (impl 02 U02-109).

Split from `test_model_stg_servicenow.py`: the `stg.sn_ci` merge of cmdb_ci and
cmdb_ci_service (IT02-02) and the empty-table guarantees for absent entities (IT02-08).
"""

import pytest
from tests.integration.model._stg_lake import Row, at, build, cast_stats, columns, commit
from tests.support.build_harness import BuildHarness

pytestmark = pytest.mark.integration

SN = "servicenow"
INC_ENUM = {"servicenow.incident_state": {"1": "open", "2": "in_progress", "6": "resolved"}}
SN_TABLES = (
    "sn_incident",
    "sn_change_request",
    "sn_problem",
    "sn_ci",
    "sn_rel_ci",
    "sn_group",
    "sn_department",
    "sn_task_sla",
)


def _incident(number: str) -> dict[str, str | None]:
    return {"number": number, "state": "1", "priority": "3 - Moderate"}


def test_it02_02_ci_one_row_per_sys_id(build_harness: BuildHarness) -> None:
    """IT02-02 `stg.sn_ci`: one row per sys_id over cmdb_ci and cmdb_ci_service; the
    service row gives name and criticality (`busines_criticality` of cmdb_ci ignored)."""
    raw = build_harness.layout.raw
    commit(
        raw,
        SN,
        "cmdb_ci",
        [
            Row(
                "s1",
                at(3),
                {
                    "name": "ci-name",
                    "sys_class_name": "cmdb_ci_service",
                    "owned_by": "u9",
                    "busines_criticality": "4 - low",
                },
            ),
            Row("c1", at(1), {"name": "db01", "sys_class_name": "cmdb_ci_db", "company": "co"}),
            Row("c2", at(1), {"name": "db02", "busines_criticality": "1 - most critical"}),
        ],
    )
    commit(
        raw,
        SN,
        "cmdb_ci_service",
        [
            Row("s1", at(1), {"name": "Checkout", "busines_criticality": "1 - most critical"}),
            Row("s2", at(1), {"name": "Payments", "busines_criticality": "9 - bogus"}),
        ],
    )
    build(build_harness)
    rows = build_harness.query(
        "SELECT sys_id, record_id, name, ci_class, owned_by, company, criticality"
        " FROM stg.sn_ci ORDER BY sys_id"
    )
    assert rows == [
        ("c1", "servicenow:cmdb_ci:c1", "db01", "cmdb_ci_db", None, "co", None),
        ("c2", "servicenow:cmdb_ci:c2", "db02", None, None, None, None),
        ("s1", "servicenow:cmdb_ci:s1", "Checkout", "cmdb_ci_service", "u9", None, 1),
        ("s2", "servicenow:cmdb_ci_service:s2", "Payments", "cmdb_ci_service", None, None, None),
    ]
    assert cast_stats(build_harness, "sn_ci") == {"criticality": (2, 1)}


def test_it02_02_ci_service_fallbacks(build_harness: BuildHarness) -> None:
    """IT02-02 `stg.sn_ci`: a newer cmdb_ci row without a class takes the service row's
    class, and a service row without a name keeps the cmdb_ci row's name."""
    raw = build_harness.layout.raw
    commit(
        raw,
        SN,
        "cmdb_ci",
        [
            Row("s1", at(5), {"name": "ci-name", "owned_by": "u1"}),
            Row("s2", at(5), {"name": "cmdb-name", "sys_class_name": "cmdb_ci_service"}),
        ],
    )
    commit(
        raw,
        SN,
        "cmdb_ci_service",
        [
            Row("s1", at(1), {"name": "Checkout", "sys_class_name": "cmdb_ci_service_business"}),
            Row("s2", at(1), {"busines_criticality": "2 - somewhat critical"}),
        ],
    )
    build(build_harness)
    rows = build_harness.query(
        "SELECT sys_id, record_id, name, ci_class, owned_by, criticality FROM stg.sn_ci ORDER BY 1"
    )
    assert rows == [
        ("s1", "servicenow:cmdb_ci:s1", "Checkout", "cmdb_ci_service_business", "u1", None),
        ("s2", "servicenow:cmdb_ci:s2", "cmdb-name", "cmdb_ci_service", None, 2),
    ]


def test_it02_08_absent_optional_entities(build_harness: BuildHarness) -> None:
    """IT02-08 no cmn_department, task_sla, cmdb_ci_service files: the build succeeds, their
    staging tables are empty, CI criticality is NULL, and every staging table has the same
    columns as when the lake is empty (R-60)."""
    raw = build_harness.layout.raw
    commit(raw, SN, "incident", [Row("a", at(0), _incident("INC_A"))])
    commit(raw, SN, "cmdb_ci", [Row("c1", at(0), {"name": "db", "busines_criticality": "1"})])
    commit(raw, SN, "sys_user_group", [Row("g1", at(0), {"sys_id": "g1", "name": "Ops"})])
    ran = build(build_harness, enums=INC_ENUM)
    assert "110_stg_servicenow.sql" in ran
    for table in ("sn_department", "sn_task_sla"):
        assert build_harness.query(f"SELECT count(*) FROM stg.{table}") == [(0,)]  # noqa: S608
    assert build_harness.query("SELECT sys_id, criticality FROM stg.sn_ci") == [("c1", None)]
    populated = {t: columns(build_harness, t) for t in SN_TABLES}
    assert populated["sn_incident"][:4] == [
        ("record_id", "VARCHAR"),
        ("source_key", "VARCHAR"),
        ("source_updated_at", "TIMESTAMP WITH TIME ZONE"),
        ("number", "VARCHAR"),
    ]
    assert ("priority", "SMALLINT") in populated["sn_incident"]
    assert ("business_duration_s", "BIGINT") in populated["sn_incident"]
    assert ("criticality", "SMALLINT") in populated["sn_ci"]


def test_it02_08_empty_lake_same_columns(build_harness: BuildHarness) -> None:
    """IT02-08 an empty lake: every ServiceNow staging table exists, empty, with the columns
    and types of a populated build."""
    build(build_harness)
    empty = {t: columns(build_harness, t) for t in SN_TABLES}
    for table in SN_TABLES:
        assert build_harness.query(f"SELECT count(*) FROM stg.{table}") == [(0,)]  # noqa: S608
    raw = build_harness.layout.raw
    full = {"number": "N", "name": "n", "sys_id": "s", "task": "t", "parent": "p"}
    for entity in (
        "incident",
        "change_request",
        "problem",
        "cmdb_ci",
        "cmdb_ci_service",
        "cmdb_rel_ci",
        "sys_user_group",
        "cmn_department",
        "task_sla",
    ):
        commit(raw, SN, entity, [Row("k", at(0), full)])
    build(build_harness)
    assert {t: columns(build_harness, t) for t in SN_TABLES} == empty
    departments = build_harness.query(
        "SELECT record_id, sys_id, name, parent, cost_center FROM stg.sn_department"
    )
    assert departments == [("servicenow:cmn_department:k", "s", "n", "p", None)]
