"""`200_org_team_service.sql` (impl 02 U02-116) and the CI merge of `110` it reads (U02-109).

IT02-09: org mode (group hierarchy, then departments by cost center). IT02-10: only the
configured CI classes become services; criticality comes from `cmdb_ci_service` only, and
without `cmdb_ci_service` files it is NULL and the build succeeds (R-60).
"""

from __future__ import annotations

import pytest
from tests.integration.model._core_build import DEPT, SVC, TEAM, build_core
from tests.integration.model._stg_lake import Row, at, commit
from tests.support.build_harness import BuildHarness

pytestmark = pytest.mark.integration

SN = "servicenow"


def _group(sys_id: str, parent: str | None = None, **fields: str | None) -> Row:
    return Row(
        sys_id, at(0), {"sys_id": sys_id, "name": sys_id.upper(), "parent": parent, **fields}
    )


def _orgs(harness: BuildHarness) -> list[tuple[object, ...]]:
    return harness.query(
        "SELECT org_id, name, parent_org_id, cost_center, source FROM core.org ORDER BY org_id"
    )


def _teams(harness: BuildHarness) -> list[tuple[object, ...]]:
    return harness.query(
        "SELECT team_id, name, org_id, source, active FROM core.team ORDER BY team_id"
    )


GROUPS = [
    _group("root", cost_center="CC1"),
    _group("a", "root", cost_center="CC3"),
    _group("a1", "a", cost_center="CC1"),
    _group("b", "root", active="false", cost_center=""),
    _group("lone", cost_center="CC9"),
    _group("orphan", "missing"),
]


def test_it02_09_group_hierarchy_mode(build_harness: BuildHarness) -> None:
    """IT02-09 no departments: groups with a child group are orgs (parent org when the
    parent is an org), groups without one are teams under their parent org."""
    commit(build_harness.layout.raw, SN, "sys_user_group", GROUPS)
    build_core(build_harness, hi=200)
    assert _orgs(build_harness) == [
        (TEAM + "a", "A", TEAM + "root", "CC3", SN),
        (TEAM + "root", "ROOT", None, "CC1", SN),
    ]
    assert _teams(build_harness) == [
        (TEAM + "a1", "A1", TEAM + "a", SN, True),
        (TEAM + "b", "B", TEAM + "root", SN, False),
        (TEAM + "lone", "LONE", None, SN, True),
        (TEAM + "orphan", "ORPHAN", None, SN, True),
    ]


def test_it02_09_department_mode_by_cost_center(build_harness: BuildHarness) -> None:
    """IT02-09 the same groups, then departments: departments are the orgs; every group is
    a team whose org is the department with its non-empty cost center (lowest sys_id)."""
    raw = build_harness.layout.raw
    commit(raw, SN, "sys_user_group", GROUPS)
    build_core(build_harness, hi=200)
    departments = [
        Row("d2", at(0), {"sys_id": "d2", "name": "Two", "cost_center": "CC1"}),
        Row("d1", at(0), {"sys_id": "d1", "name": "One", "cost_center": "CC1"}),
        Row("d3", at(0), {"sys_id": "d3", "name": "Three", "parent": "d1", "cost_center": "CC3"}),
        Row("d4", at(0), {"sys_id": "d4", "name": "Four", "parent": "gone", "cost_center": ""}),
    ]
    commit(raw, SN, "cmn_department", departments)
    build_core(build_harness, hi=200)
    assert _orgs(build_harness) == [
        (DEPT + "d1", "One", None, "CC1", SN),
        (DEPT + "d2", "Two", None, "CC1", SN),
        (DEPT + "d3", "Three", DEPT + "d1", "CC3", SN),
        (DEPT + "d4", "Four", None, None, SN),
    ]
    assert _teams(build_harness) == [
        (TEAM + "a", "A", DEPT + "d3", SN, True),
        (TEAM + "a1", "A1", DEPT + "d1", SN, True),
        (TEAM + "b", "B", None, SN, False),
        (TEAM + "lone", "LONE", None, SN, True),
        (TEAM + "orphan", "ORPHAN", None, SN, True),
        (TEAM + "root", "ROOT", DEPT + "d1", SN, True),
    ]


def _services(harness: BuildHarness) -> list[tuple[object, ...]]:
    return harness.query(
        "SELECT service_id, name, ci_class, criticality, business_owner_team_id, org_id, source"
        " FROM core.service ORDER BY service_id"
    )


def _ci_lake(harness: BuildHarness) -> None:
    raw = harness.layout.raw
    commit(
        raw,
        SN,
        "sys_user_group",
        [_group("org"), _group("own", "org"), _group("sup", "org")],
    )
    commit(
        raw,
        SN,
        "cmdb_ci",
        [
            Row("db1", at(0), {"name": "db01", "sys_class_name": "cmdb_ci_db"}),
            Row("app1", at(0), {"name": "app", "sys_class_name": "cmdb_ci_appl"}),
            Row(
                "s1",
                at(1),
                {
                    "name": "ci-name",
                    "sys_class_name": "cmdb_ci_service",
                    "owned_by": "own",
                    "support_group": "sup",
                    "busines_criticality": "4 - low",
                },
            ),
            Row(
                "biz",
                at(0),
                {
                    "name": "Biz",
                    "sys_class_name": "cmdb_ci_service_business",
                    "owned_by": "nobody",
                    "busines_criticality": "1 - most critical",
                },
            ),
        ],
    )


def test_it02_10_configured_classes_criticality_from_service_rows(
    build_harness: BuildHarness,
) -> None:
    """IT02-10 CIs of several classes: only configured classes become services; criticality
    comes from `cmdb_ci_service` rows only; the owner falls back to the support group."""
    _ci_lake(build_harness)
    commit(
        build_harness.layout.raw,
        SN,
        "cmdb_ci_service",
        [
            Row("s1", at(0), {"name": "Checkout", "busines_criticality": "2 - somewhat critical"}),
            Row(
                "s3",
                at(0),
                {
                    "name": "Search",
                    "owned_by": "u1",
                    "support_group": "sup",
                    "busines_criticality": "1 - most critical",
                },
            ),
        ],
    )
    build_core(build_harness, hi=200)
    assert _services(build_harness) == [
        (SVC + "biz", "Biz", "cmdb_ci_service_business", None, None, None, SN),
        (SVC + "s1", "Checkout", "cmdb_ci_service", 2, TEAM + "own", TEAM + "org", SN),
        (SVC + "s3", "Search", "cmdb_ci_service", 1, TEAM + "sup", TEAM + "org", SN),
    ]
    build_core(build_harness, hi=200, ci_classes=["cmdb_ci_db"])
    assert [row[0] for row in _services(build_harness)] == [SVC + "db1"]


def test_it02_10_no_cmdb_ci_service_files(build_harness: BuildHarness) -> None:
    """IT02-10 no `cmdb_ci_service` files: the build succeeds, services come from
    `cmdb_ci` rows of the configured classes, and every criticality is NULL (R-60)."""
    _ci_lake(build_harness)
    ran = build_core(build_harness, hi=200)
    assert "200_org_team_service.sql" in ran
    assert _services(build_harness) == [
        (SVC + "biz", "Biz", "cmdb_ci_service_business", None, None, None, SN),
        (SVC + "s1", "ci-name", "cmdb_ci_service", None, TEAM + "own", TEAM + "org", SN),
    ]
    cols = build_harness.query(
        "SELECT column_name, data_type FROM information_schema.columns"
        " WHERE table_schema = 'core' AND table_name = 'service' ORDER BY ordinal_position"
    )
    assert cols == [
        ("service_id", "VARCHAR"),
        ("name", "VARCHAR"),
        ("ci_class", "VARCHAR"),
        ("criticality", "SMALLINT"),
        ("business_owner_team_id", "VARCHAR"),
        ("org_id", "VARCHAR"),
        ("source", "VARCHAR"),
    ]


def test_it02_10_empty_lake_core_tables_exist(build_harness: BuildHarness) -> None:
    """IT02-10 (R-60) an empty lake: 200 runs, and `core.org`, `core.team`, `core.service`
    exist empty with their declared columns."""
    build_core(build_harness, hi=200)
    for table in ("org", "team", "service"):
        assert build_harness.query(f"SELECT count(*) FROM core.{table}") == [(0,)]  # noqa: S608
    types = build_harness.query(
        "SELECT table_name, column_name, data_type FROM information_schema.columns"
        " WHERE table_schema = 'core' AND table_name IN ('org', 'team')"
        " ORDER BY table_name, ordinal_position"
    )
    assert types == [
        ("org", "org_id", "VARCHAR"),
        ("org", "name", "VARCHAR"),
        ("org", "parent_org_id", "VARCHAR"),
        ("org", "cost_center", "VARCHAR"),
        ("org", "source", "VARCHAR"),
        ("team", "team_id", "VARCHAR"),
        ("team", "name", "VARCHAR"),
        ("team", "org_id", "VARCHAR"),
        ("team", "source", "VARCHAR"),
        ("team", "active", "BOOLEAN"),
    ]
