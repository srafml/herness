"""`230_incident.sql` (impl 02 U02-118): `core.incident`.

IT02-12: `sla_breached` from `task_sla` when the incident has rows there, else from
`made_sla`; `customer_impact_minutes` and `acknowledged_at` only from configured custom
fields (never estimated; MTTA disabled when not configured).
IT02-13: an incident without `business_service` gets the single service related to its CI
through `cmdb_rel_ci`; a CI related to two services gives NULL.
"""

from __future__ import annotations

import datetime

import pytest
from tests.integration.model._core_build import SVC, TEAM, build_core
from tests.integration.model._stg_lake import UTC, Row, at, commit
from tests.support.build_harness import BuildHarness

pytestmark = pytest.mark.integration

SN = "servicenow"
CUSTOM = {"servicenow": {"acknowledged_at": "u_ack", "customer_impact_minutes": "u_impact"}}
ENUMS = {"enums": {"servicenow.incident_state": {"1": "open", "6": "resolved"}}}
INCIDENT_COLUMNS = [
    ("record_id", "VARCHAR"),
    ("number", "VARCHAR"),
    ("opened_at", "TIMESTAMP WITH TIME ZONE"),
    ("acknowledged_at", "TIMESTAMP WITH TIME ZONE"),
    ("resolved_at", "TIMESTAMP WITH TIME ZONE"),
    ("closed_at", "TIMESTAMP WITH TIME ZONE"),
    ("priority", "SMALLINT"),
    ("state", "VARCHAR"),
    ("service_id", "VARCHAR"),
    ("ci_id", "VARCHAR"),
    ("team_id", "VARCHAR"),
    ("reassignment_count", "INTEGER"),
    ("reopen_count", "INTEGER"),
    ("short_description", "VARCHAR"),
    ("description", "VARCHAR"),
    ("close_notes", "VARCHAR"),
    ("close_code", "VARCHAR"),
    ("problem_id", "VARCHAR"),
    ("caused_by_change_id", "VARCHAR"),
    ("sla_breached", "BOOLEAN"),
    ("business_duration_s", "BIGINT"),
    ("customer_impact_minutes", "DOUBLE"),
    ("content_hash", "VARCHAR"),
    ("source_updated_at", "TIMESTAMP WITH TIME ZONE"),
]


def _ts(text: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(text).replace(tzinfo=UTC)


def _sla_lake(harness: BuildHarness) -> None:
    raw = harness.layout.raw
    incidents = [
        ("a1", "true", "12:05", "30.5"),  # task_sla has a breach -> True despite made_sla
        ("a2", "false", "12:10", None),  # task_sla without a breach -> False
        ("a3", "false", None, "0"),  # no task_sla -> NOT made_sla -> True
        ("a4", "true", None, "abc"),  # no task_sla -> False; bad impact value -> NULL
        ("a5", None, None, None),  # no task_sla, no made_sla -> NULL
    ]
    commit(
        raw,
        SN,
        "incident",
        [
            Row(
                key,
                at(0),
                {
                    "number": key.upper(),
                    "opened_at": "2024-03-01 12:00:00",
                    "made_sla": made,
                    "u_ack": None if ack is None else f"2024-03-01 {ack}:00",
                    "u_impact": impact,
                },
            )
            for key, made, ack, impact in incidents
        ],
    )
    slas = [("x1", "a1", "false"), ("x2", "a1", "true"), ("x3", "a2", "false")]
    slas += [("x4", "a2", "false"), ("x5", "zz", "true")]
    commit(
        raw,
        SN,
        "task_sla",
        [Row(k, at(0), {"task": task, "has_breached": b}) for k, task, b in slas],
    )


def _sla_rows(harness: BuildHarness) -> list[tuple[object, ...]]:
    return harness.query(
        "SELECT number, sla_breached, customer_impact_minutes, acknowledged_at"
        " FROM core.incident ORDER BY number"
    )


def test_it02_12_sla_impact_ack_with_custom_fields(build_harness: BuildHarness) -> None:
    """IT02-12 incidents with and without task_sla rows, custom fields configured:
    `sla_breached` = bool_or(has_breached) when task_sla rows exist, else NOT made_sla;
    impact and acknowledged_at are the custom field values (bad impact stays NULL)."""
    _sla_lake(build_harness)
    build_core(build_harness, custom_fields=CUSTOM, hi=250)
    assert _sla_rows(build_harness) == [
        ("A1", True, 30.5, _ts("2024-03-01 12:05:00")),
        ("A2", False, None, _ts("2024-03-01 12:10:00")),
        ("A3", True, 0.0, None),
        ("A4", False, None, None),
        ("A5", None, None, None),
    ]


def test_it02_12_custom_fields_absent(build_harness: BuildHarness) -> None:
    """IT02-12 custom fields not configured: `acknowledged_at` (MTTA disabled) and
    `customer_impact_minutes` are NULL although the lake has the columns."""
    _sla_lake(build_harness)
    build_core(build_harness, hi=250)
    assert _sla_rows(build_harness) == [
        ("A1", True, None, None),
        ("A2", False, None, None),
        ("A3", True, None, None),
        ("A4", False, None, None),
        ("A5", None, None, None),
    ]


def test_it02_12_no_task_sla_entity(build_harness: BuildHarness) -> None:
    """IT02-12 (R-60) no task_sla in the lake: `sla_breached` falls back to NOT made_sla
    for every incident, and the build succeeds."""
    commit(
        build_harness.layout.raw,
        SN,
        "incident",
        [
            Row("b1", at(0), {"number": "B1", "made_sla": "false"}),
            Row("b2", at(0), {"number": "B2", "made_sla": "true"}),
        ],
    )
    ran = build_core(build_harness, hi=250)
    assert "230_incident.sql" in ran
    got = build_harness.query("SELECT number, sla_breached FROM core.incident ORDER BY 1")
    assert got == [("B1", True), ("B2", False)]


def test_it02_12_columns_and_references(build_harness: BuildHarness) -> None:
    """IT02-12 one fully populated incident: every column per U02-118 (record IDs of the
    references, canonical state, latest assignment group, NULL content_hash), typed."""
    fields = {
        "number": "INC0001",
        "opened_at": "2024-03-01 10:00:00",
        "resolved_at": "2024-03-01 11:00:00",
        "closed_at": "2024-03-02 11:00:00",
        "priority": "2 - High",
        "state": "6",
        "business_service": " s1 ",
        "cmdb_ci": "ci1",
        "assignment_group": "g1",
        "problem_id": "p1",
        "caused_by": "c1",
        "reassignment_count": "3",
        "reopen_count": "1",
        "short_description": "Checkout down",
        "description": "Long text",
        "close_notes": "Restarted",
        "close_code": "Solved (Permanently)",
        "made_sla": "true",
        "business_duration": "3600",
    }
    later = dict(fields, assignment_group="g2")
    raw = build_harness.layout.raw
    commit(raw, SN, "incident", [Row("i1", at(0), fields)])
    commit(raw, SN, "incident", [Row("i1", at(1), later)])
    build_core(build_harness, mappings=ENUMS, hi=250)
    cols = build_harness.query(
        "SELECT column_name, data_type FROM information_schema.columns"
        " WHERE table_schema = 'core' AND table_name = 'incident' ORDER BY ordinal_position"
    )
    assert cols == INCIDENT_COLUMNS
    assert build_harness.query("SELECT * FROM core.incident") == [
        (
            "servicenow:incident:i1",
            "INC0001",
            _ts("2024-03-01 10:00:00"),
            None,
            _ts("2024-03-01 11:00:00"),
            _ts("2024-03-02 11:00:00"),
            2,
            "resolved",
            SVC + "s1",
            SVC + "ci1",
            TEAM + "g2",
            3,
            1,
            "Checkout down",
            "Long text",
            "Restarted",
            "Solved (Permanently)",
            "servicenow:problem:p1",
            "servicenow:change_request:c1",
            False,
            3600,
            None,
            None,
            at(1),
        )
    ]


def test_it02_12_empty_lake(build_harness: BuildHarness) -> None:
    """IT02-12 (R-60) an empty lake: `core.incident` exists empty with its columns."""
    build_core(build_harness, hi=250)
    assert build_harness.query("SELECT count(*) FROM core.incident") == [(0,)]
    cols = build_harness.query(
        "SELECT column_name, data_type FROM information_schema.columns"
        " WHERE table_schema = 'core' AND table_name = 'incident' ORDER BY ordinal_position"
    )
    assert cols == INCIDENT_COLUMNS


def _service_lake(harness: BuildHarness) -> None:
    raw = harness.layout.raw
    commit(
        raw,
        SN,
        "cmdb_ci_service",
        [Row(k, at(0), {"name": k.upper()}) for k in ("s1", "s2", "s3")],
    )
    commit(
        raw,
        SN,
        "cmdb_ci",
        [Row(k, at(0), {"name": k, "sys_class_name": "cmdb_ci_server"}) for k in ("ci1", "ci2")],
    )
    rels = [
        ("r1", "s1", "ci1"),
        ("r2", "ci2", "ci1"),  # parent is not a service: ignored
        ("r3", "s1", "ci1"),  # a second relation to the same service
        ("r4", "s1", "ci2"),
        ("r5", "s2", "ci2"),
    ]
    commit(
        raw,
        SN,
        "cmdb_rel_ci",
        [
            Row(k, at(0), {"parent": p, "child": c, "type": "Depends on::Used by"})
            for k, p, c in rels
        ],
    )
    incidents = [
        ("n1", None, "ci1"),  # CI related to one service -> s1
        ("n2", "", "ci2"),  # CI related to two services -> NULL
        ("n3", "s3", "ci2"),  # business_service wins
        ("n4", None, "ci9"),  # CI related to no service -> NULL
        ("n5", None, None),  # neither -> NULL
    ]
    commit(
        raw,
        SN,
        "incident",
        [
            Row(k, at(0), {"number": k.upper(), "business_service": bs, "cmdb_ci": ci})
            for k, bs, ci in incidents
        ],
    )


def test_it02_13_service_from_ci_relation(build_harness: BuildHarness) -> None:
    """IT02-13 incident without a service whose CI is related to one service gets it; a CI
    related to two services (or none) gives NULL; `business_service` wins when set."""
    _service_lake(build_harness)
    build_core(build_harness, hi=250)
    got = build_harness.query("SELECT number, service_id, ci_id FROM core.incident ORDER BY 1")
    assert got == [
        ("N1", SVC + "s1", SVC + "ci1"),
        ("N2", None, SVC + "ci2"),
        ("N3", SVC + "s3", SVC + "ci2"),
        ("N4", None, SVC + "ci9"),
        ("N5", None, None),
    ]
    build_core(build_harness, hi=250)  # re-run: same rows (idempotent)
    assert build_harness.query("SELECT count(*) FROM core.incident") == [(5,)]
