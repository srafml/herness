"""`220_service_map.sql` (impl 02 U02-117): `core.service_map` and `stg.service_name_lookup`.

IT02-11: overrides, CMDB owners/support groups and approved suggestions compete for the
same identity keys; precedence is override > cmdb > suggestion. Suggestions come from a
real ops store through `approved_mapping_suggestions` (U02-60), so pending and rejected
items never reach the build (LLM04 control).
"""

from __future__ import annotations

import datetime

import pytest
from tests.integration.model._core_build import DEPT, SVC, TEAM, build_core
from tests.integration.model._stg_lake import Row, at, commit
from tests.support.build_harness import BuildHarness
from tests.support.ops_store import OpsStoreHandle

from herness.store.ops import (
    approved_mapping_suggestions,
    create_review_item,
    decide_review_item,
    shared,
)

pytestmark = pytest.mark.integration

SN = "servicenow"
USER = "ab" * 16
NOW = datetime.datetime(2026, 9, 26, 10, 0, tzinfo=datetime.UTC)
ROOT = TEAM + "root"

MAPPINGS = {
    "service_overrides": [
        {
            "service_id": SVC + "s1",
            "team_id": TEAM + "t1",
            "org_id": DEPT + "d9",
            "role": "owner",
            "aliases": ["Search", "Checkout-App"],
        },
        {
            "service_id": SVC + "s2",
            "team_id": TEAM + "t2",
            "jira_project": "PAY",
            "jira_component": "api",
            "role": "delivery",
            "aliases": ["Payments-API"],
        },
    ]
}


def _lake(harness: BuildHarness) -> None:
    raw = harness.layout.raw
    groups = [("root", None), ("t1", "root"), ("t2", "root"), ("t3", "root")]
    commit(
        raw,
        SN,
        "sys_user_group",
        [Row(g, at(0), {"sys_id": g, "name": g, "parent": p}) for g, p in groups],
    )
    services = [
        ("s1", "Checkout", "t1", "t2"),
        ("s2", "Payments", "t2", "t2"),
        ("s3", "Dup", "t3", None),
        ("s4", "DUP", None, None),
        ("s5", "Search", None, None),
    ]
    commit(
        raw,
        SN,
        "cmdb_ci_service",
        [
            Row(k, at(0), {"name": name, "owned_by": own, "support_group": sup})
            for k, name, own, sup in services
        ],
    )


def _suggestion(status: str | None, **payload: object) -> str:
    item_id = create_review_item("mapping_suggestion", payload, now=NOW)
    if status is not None:
        decide_review_item(item_id, status, decided_by=USER, now=NOW)  # type: ignore[arg-type]
    return item_id


def _suggestions() -> None:
    jira = "jira_component"
    _suggestion(
        "approved",
        subject_type=jira,
        jira_project="PAY",
        jira_component="api",
        team_id=TEAM + "t1",
        service_id=SVC + "s1",
        score=0.9,
    )  # loses to override
    _suggestion(
        "approved", subject_type="team", team_id=TEAM + "t2", service_id=SVC + "s1", score=0.6
    )  # loses to the CMDB support group
    _suggestion(
        "approved", subject_type=jira, jira_project="WEB", service_id=SVC + "s4", score=0.5
    )  # loses to the lower service_id below
    _suggestion(
        "approved",
        subject_type=jira,
        jira_project="WEB",
        team_id=TEAM + "t3",
        service_id=SVC + "s3",
        score=0.8,
    )
    _suggestion(
        "approved", subject_type="team", team_id=TEAM + "t3", service_id=SVC + "s2", score=0.7
    )
    _suggestion(
        None,
        subject_type=jira,
        jira_project="NEW",
        jira_component="x",
        service_id=SVC + "s1",
        score=0.99,
    )  # pending
    _suggestion(
        "rejected", subject_type=jira, jira_project="GONE", service_id=SVC + "s1", score=0.99
    )
    _suggestion(
        None, subject_type="team", team_id=TEAM + "t3", service_id=SVC + "s5", score=0.99
    )  # pending


def test_it02_11_precedence_pending_ignored_lookup(
    ops_store: OpsStoreHandle, build_harness: BuildHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT02-11 override, CMDB owner, approved and pending suggestions for overlapping keys:
    precedence override > cmdb > suggestion; pending ignored; lookup excludes ambiguous
    names."""
    monkeypatch.setattr(shared, "audit", lambda *_args, **_fields: None)  # no config tree
    _lake(build_harness)
    _suggestions()
    approved = approved_mapping_suggestions()
    assert len(approved) == 5
    build_core(build_harness, mappings=MAPPINGS, approved=approved)
    rows = build_harness.query(
        "SELECT service_id, team_id, jira_project, jira_component, org_id, role, link_source,"
        " confidence FROM core.service_map ORDER BY service_id, role, team_id, jira_project"
    )
    assert rows == [
        (SVC + "s1", TEAM + "t1", None, None, DEPT + "d9", "owner", "override", 1.0),
        (SVC + "s1", TEAM + "t2", None, None, ROOT, "support", "cmdb", 1.0),
        (SVC + "s2", TEAM + "t2", "PAY", "api", ROOT, "delivery", "override", 1.0),
        (SVC + "s2", TEAM + "t2", None, None, ROOT, "owner", "cmdb", 1.0),
        (SVC + "s2", TEAM + "t3", None, None, ROOT, "support", "suggested_approved", 0.7),
        (SVC + "s3", TEAM + "t3", "WEB", None, ROOT, "delivery", "suggested_approved", 0.8),
        (SVC + "s3", TEAM + "t3", None, None, ROOT, "owner", "cmdb", 1.0),
    ]
    lookup = build_harness.query(
        "SELECT name_lc, service_id FROM stg.service_name_lookup ORDER BY name_lc"
    )
    assert lookup == [
        ("checkout", SVC + "s1"),
        ("checkout-app", SVC + "s1"),
        ("payments", SVC + "s2"),
        ("payments-api", SVC + "s2"),
        ("search", SVC + "s1"),
    ]


def test_it02_11_no_suggestions_empty_lake(build_harness: BuildHarness) -> None:
    """IT02-11 (D1 default: CMDB plus overrides) an empty lake, no suggestions and no
    overrides: 220 runs and both tables exist empty with their declared columns; a
    re-run keeps them so."""
    build_core(build_harness)
    build_core(build_harness)
    for table in ("core.service_map", "stg.service_name_lookup"):
        assert build_harness.query(f"SELECT count(*) FROM {table}") == [(0,)]  # noqa: S608
    cols = build_harness.query(
        "SELECT table_name, column_name, data_type FROM information_schema.columns"
        " WHERE table_name IN ('service_map', 'service_name_lookup')"
        " ORDER BY table_name, ordinal_position"
    )
    assert cols == [
        ("service_map", "service_id", "VARCHAR"),
        ("service_map", "team_id", "VARCHAR"),
        ("service_map", "jira_project", "VARCHAR"),
        ("service_map", "jira_component", "VARCHAR"),
        ("service_map", "org_id", "VARCHAR"),
        ("service_map", "role", "VARCHAR"),
        ("service_map", "link_source", "VARCHAR"),
        ("service_map", "confidence", "DOUBLE"),
        ("service_name_lookup", "name_lc", "VARCHAR"),
        ("service_name_lookup", "service_id", "VARCHAR"),
    ]
