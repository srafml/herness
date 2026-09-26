"""ServiceNow staging `110_stg_servicenow.sql` (impl 02 U02-109; IT02-02 … IT02-08).

Lake files are written with the real `LakeWriter`; each test builds files 000-199 on a temp
DuckDB build through `build_harness` with real reference tables (U02-87).
"""

import datetime

import pytest
from tests.integration.model._stg_lake import (
    UTC,
    Row,
    at,
    build,
    cast_stats,
    columns,
    commit,
    open_writer,
)
from tests.support.build_harness import BuildHarness

from herness.store import lake
from herness.store.ops import create_deletion_request, deleted_record_ids, set_deletion_status

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


def _incident(number: str, **fields: str | None) -> dict[str, str | None]:
    base: dict[str, str | None] = {"number": number, "state": "1", "priority": "3 - Moderate"}
    base.update(fields)
    return base


def _numbers(harness: BuildHarness) -> list[str]:
    return [str(r[0]) for r in harness.query('SELECT "number" FROM stg.sn_incident ORDER BY 1')]


def test_it02_02_latest_incident_version_wins(build_harness: BuildHarness) -> None:
    """IT02-02 three versions and one identical re-emit of an incident: the latest version
    wins, one row, typed per U02-109; a re-run of the stage keeps one cast_stats row."""
    raw = build_harness.layout.raw
    v1 = _incident("INC0001", opened_at="2024-03-01 10:00:00", state="1", priority="4 - Low")
    v2 = _incident("INC0001", opened_at="2024-03-01 10:00:00", state="2", priority="2 - High")
    v3 = _incident(
        "INC0001",
        opened_at="2024-03-01 10:00:00",
        resolved_at="2024-03-02 11:30:00",
        state="6",
        priority="1 - Critical",
        business_service="svc1",
        cmdb_ci="ci1",
        assignment_group="grp1",
        problem_id="",
        reassignment_count="2",
        reopen_count="0",
        short_description="Checkout down",
        close_code="Solved",
        made_sla="false",
        business_duration="1970-01-01 02:00:00",
    )
    commit(raw, SN, "incident", [Row("a1", at(0), v1)])
    commit(raw, SN, "incident", [Row("a1", at(1), v2)])
    commit(raw, SN, "incident", [Row("a1", at(2), v3)])
    commit(raw, SN, "incident", [Row("a1", at(2), v3, fetched=at(5))])  # identical re-emit

    build(build_harness, enums=INC_ENUM)

    rows = build_harness.query(
        "SELECT record_id, source_key, source_updated_at, state, priority, opened_at,"
        " resolved_at, closed_at, business_service, cmdb_ci, assignment_group, problem_id,"
        " reassignment_count, reopen_count, short_description, close_code, made_sla,"
        " business_duration_s, acknowledged_at, customer_impact_minutes FROM stg.sn_incident"
    )
    assert rows == [
        (
            "servicenow:incident:a1",
            "a1",
            at(2),
            "resolved",
            1,
            datetime.datetime(2024, 3, 1, 10, tzinfo=UTC),
            datetime.datetime(2024, 3, 2, 11, 30, tzinfo=UTC),
            None,
            "svc1",
            "ci1",
            "grp1",
            None,
            2,
            0,
            "Checkout down",
            "Solved",
            False,
            7200,
            None,
            None,
        )
    ]
    stats = cast_stats(build_harness, "sn_incident")
    assert stats["state"] == (1, 0)
    assert stats["closed_at"] == (0, 0)
    assert "acknowledged_at" not in stats
    build(build_harness, enums=INC_ENUM, lo=110, hi=110)  # re-run: idempotent
    assert cast_stats(build_harness, "sn_incident")["closed_at"] == (0, 0)


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


def test_it02_03_tombstones(build_harness: BuildHarness) -> None:
    """IT02-03 latest version a tombstone: dropped; older tombstone then newer live: kept."""
    raw = build_harness.layout.raw
    commit(
        raw, SN, "incident", [Row("x", at(0), _incident("INC_X")), Row("y", at(0), deleted=True)]
    )
    commit(
        raw, SN, "incident", [Row("x", at(1), deleted=True), Row("y", at(1), _incident("INC_Y"))]
    )
    build(build_harness, enums=INC_ENUM)
    assert _numbers(build_harness) == ["INC_Y"]


def test_it02_04_deletion_requests(ops_store: object, build_harness: BuildHarness) -> None:
    """IT02-04 deletion requests `running`, `done`, `pending`: the first two records are
    removed from staging, the `pending` one is kept (TH02-16, staging part)."""
    raw = build_harness.layout.raw
    keys = ("run", "done", "pend", "none")
    commit(raw, SN, "incident", [Row(k, at(0), _incident(f"INC_{k}")) for k in keys])
    commit(raw, SN, "problem", [Row("run", at(0), {"number": "PRB_run"})])
    requests = {
        k: create_deletion_request(
            record_id=f"servicenow:incident:{k}",
            requested_by="a" * 32,
            reason_ref="TICKET-1",
            now=at(9),
        )
        for k in ("run", "done", "pend")
    }
    set_deletion_status(requests["run"].request_id, "running")
    set_deletion_status(requests["done"].request_id, "running")
    set_deletion_status(requests["done"].request_id, "done", completed_at=at(10))
    deleted = deleted_record_ids(SN, "incident")
    assert deleted == ["servicenow:incident:done", "servicenow:incident:run"]
    build(build_harness, enums=INC_ENUM, deleted_ids=deleted)
    assert _numbers(build_harness) == ["INC_none", "INC_pend"]
    # the filter is by record ID: the problem with the same source key stays
    assert build_harness.query('SELECT "number" FROM stg.sn_problem') == [("PRB_run",)]


def test_it02_05_unknown_column_and_absent_custom_field(build_harness: BuildHarness) -> None:
    """IT02-05 a file with an unknown column and custom fields configured but absent from
    the lake: the build succeeds and the custom columns are NULL."""
    raw = build_harness.layout.raw
    commit(raw, SN, "incident", [Row("a", at(0), _incident("INC_A", u_brand_new="zzz"))])
    custom = {
        "servicenow": {"acknowledged_at": "u_acknowledged_at", "customer_impact_minutes": "u_imp"}
    }
    build(build_harness, enums=INC_ENUM, custom_fields=custom)
    rows = build_harness.query(
        "SELECT acknowledged_at, customer_impact_minutes FROM stg.sn_incident"
    )
    assert rows == [(None, None)]
    stats = cast_stats(build_harness, "sn_incident")
    assert stats["acknowledged_at"] == (0, 0)
    assert stats["customer_impact_minutes"] == (0, 0)


def test_it02_05_custom_fields_present(build_harness: BuildHarness) -> None:
    """IT02-05 configured custom fields present in the lake are typed with cast flags."""
    raw = build_harness.layout.raw
    rows = [
        Row("a", at(0), _incident("INC_A", u_ack="2024-03-01T10:05:00Z", u_imp="12.5")),
        Row("b", at(0), _incident("INC_B", u_ack="yesterday", u_imp="lots")),
    ]
    commit(raw, SN, "incident", rows)
    custom = {"servicenow": {"acknowledged_at": "u_ack", "customer_impact_minutes": "u_imp"}}
    build(build_harness, enums=INC_ENUM, custom_fields=custom)
    got = build_harness.query(
        'SELECT acknowledged_at, customer_impact_minutes FROM stg.sn_incident ORDER BY "number"'
    )
    assert got == [(datetime.datetime(2024, 3, 1, 10, 5, tzinfo=UTC), 12.5), (None, None)]
    stats = cast_stats(build_harness, "sn_incident")
    assert stats["acknowledged_at"] == (2, 1)
    assert stats["customer_impact_minutes"] == (2, 1)


def test_it02_06_temp_files_not_staged(
    build_harness: BuildHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT02-06 a dot-prefixed temp file in a partition (an uncommitted writer's flush):
    its rows are not staged."""
    monkeypatch.setattr(lake, "BUFFER_ROWS", 1)
    raw = build_harness.layout.raw
    commit(raw, SN, "incident", [Row("live", at(0), _incident("INC_LIVE"))])
    writer = open_writer(raw, SN, "incident", [Row("tmp", at(1), _incident("INC_TMP"))])
    try:
        temps = [p for p in (raw / SN / "incident").rglob(".*") if p.is_file()]
        assert temps, "the uncommitted writer must have flushed a temp file"
        build(build_harness, enums=INC_ENUM)
        assert _numbers(build_harness) == ["INC_LIVE"]
    finally:
        writer.abort()


def test_it02_07_cast_stats_bad_timestamps(build_harness: BuildHarness) -> None:
    """IT02-07 3 bad timestamps of 100: `stg.cast_stats` failed 3, non_null 100."""
    bad = {7: "31/02/2024", 42: "1709294400000", 99: "not a date"}
    rows = [
        Row(f"k{i}", at(0), _incident(f"INC{i:04d}", opened_at=bad.get(i, "2024-03-01 09:00:00")))
        for i in range(100)
    ]
    commit(build_harness.layout.raw, SN, "incident", rows)
    build(build_harness, enums=INC_ENUM)
    stats = cast_stats(build_harness, "sn_incident")
    assert stats["opened_at"] == (100, 3)
    assert stats["state"] == (100, 0)
    assert build_harness.query("SELECT count(*) FROM stg.sn_incident WHERE opened_at IS NULL") == [
        (3,)
    ]


def test_it02_07_typed_columns_of_other_entities(build_harness: BuildHarness) -> None:
    """IT02-07 typed columns of change, problem, group and task_sla, with unmapped enum
    values counted as cast failures of their column."""
    raw = build_harness.layout.raw
    commit(
        raw,
        SN,
        "change_request",
        [
            Row(
                "c1",
                at(0),
                {
                    "number": "CHG1",
                    "type": "Normal",
                    "state": "3",
                    "state_display": "Closed",
                    "risk": "4",
                    "opened_at": "2024-03-01 08:00:00",
                    "start_date": "2024-03-02 08:00:00",
                    "end_date": "2024-03-02 09:00:00",
                    "work_start": "2024-03-02 08:05:00",
                    "work_end": "bad",
                    "close_code": "successful",
                },
            ),
            Row("c2", at(0), {"number": "CHG2", "type": "odd", "close_code": "weird"}),
        ],
    )
    commit(
        raw,
        SN,
        "problem",
        [
            Row(
                "p1",
                at(0),
                {
                    "number": "PRB1",
                    "problem_state": "3",
                    "state_display": "Fix in progress",
                    "known_error": "true",
                    "cause_notes": "disk full",
                    "opened_at": "2024-03-01 08:00:00",
                },
            )
        ],
    )
    commit(raw, SN, "sys_user_group", [Row("g1", at(0), {"sys_id": "g1", "active": "maybe"})])
    commit(raw, SN, "task_sla", [Row("t1", at(0), {"task": "a1", "has_breached": "1"})])
    commit(raw, SN, "cmdb_rel_ci", [Row("r1", at(0), {"parent": "p", "child": "c", "type": "x"})])
    enums = {
        "servicenow.change_type": {"normal": "normal"},
        "servicenow.change_close_code": {"Successful": "successful"},
    }
    build(build_harness, enums=enums)
    changes = build_harness.query(
        'SELECT "number", type, state, risk, planned_start, actual_end, outcome'
        ' FROM stg.sn_change_request ORDER BY "number"'
    )
    assert changes == [
        (
            "CHG1",
            "normal",
            "Closed",
            "4",
            datetime.datetime(2024, 3, 2, 8, tzinfo=UTC),
            None,
            "successful",
        ),
        ("CHG2", None, None, None, None, None, None),
    ]
    assert cast_stats(build_harness, "sn_change_request") == {
        "type": (2, 1),
        "opened_at": (1, 0),
        "planned_start": (1, 0),
        "planned_end": (1, 0),
        "actual_start": (1, 0),
        "actual_end": (1, 1),
        "outcome": (2, 1),
    }
    problems = build_harness.query("SELECT state, known_error, root_cause_text FROM stg.sn_problem")
    assert problems == [("Fix in progress", True, "disk full")]
    assert build_harness.query("SELECT sys_id, active FROM stg.sn_group") == [("g1", None)]
    assert cast_stats(build_harness, "sn_group") == {"active": (1, 1)}
    assert build_harness.query("SELECT task, has_breached FROM stg.sn_task_sla") == [("a1", True)]
    assert build_harness.query("SELECT parent, child, type FROM stg.sn_rel_ci") == [("p", "c", "x")]


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
