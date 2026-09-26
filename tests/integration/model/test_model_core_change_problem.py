"""`240_change.sql` (impl 02 U02-119) and `250_problem.sql` (U02-120).

IT02-14: change close codes map to the canonical outcome through the
`servicenow.change_close_code` enum in staging (U02-109); an unknown code gives NULL and is
counted as a cast failure of `stg.sn_change_request.outcome` in `stg.cast_stats`; a
canceled state without a close code gives `canceled`.
IT02-15: `core.problem` columns per U02-120.
"""

from __future__ import annotations

import datetime

import pytest
from tests.integration.model._core_build import SVC, TEAM, build_core
from tests.integration.model._stg_lake import UTC, Row, at, cast_stats, commit
from tests.support.build_harness import BuildHarness

pytestmark = pytest.mark.integration

SN = "servicenow"
TS = "TIMESTAMP WITH TIME ZONE"
CLOSE_CODES = {
    "successful": "successful",
    "successful with issues": "successful_with_issues",
    "unsuccessful": "unsuccessful",
    "backed out": "backed_out",
    "canceled": "canceled",
    "cancelled": "canceled",
}
ENUMS = {
    "enums": {
        "servicenow.change_close_code": CLOSE_CODES,
        "servicenow.change_type": {"normal": "normal", "emergency": "emergency"},
    }
}


def _ts(text: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(text).replace(tzinfo=UTC)


def _columns(harness: BuildHarness, table: str) -> list[tuple[object, ...]]:
    return harness.query(
        "SELECT column_name, data_type FROM information_schema.columns"
        " WHERE table_schema = 'core' AND table_name = ? ORDER BY ordinal_position",
        [table],
    )


def _change_lake(harness: BuildHarness) -> None:
    raw = harness.layout.raw
    changes = [
        ("c1", "Successful", "Closed"),
        ("c2", "Successful with issues", "Closed"),
        ("c3", "Backed Out", "Review"),
        ("c4", "Rolled back by vendor", "Closed"),  # unknown code -> NULL, counted
        ("c5", None, "Canceled"),  # no code, canceled state -> canceled
        ("c6", None, "CANCELLED"),  # British spelling, any case -> canceled
        ("c7", None, "Implement"),  # no code, open state -> NULL
        ("c8", "Unsuccessful", "Cancelled"),  # a staging outcome wins over the state
        ("c9", "Nonsense", "Canceled"),  # unknown code, canceled state -> canceled
    ]
    commit(
        raw,
        SN,
        "change_request",
        [
            Row(k, at(0), {"number": k.upper(), "close_code": code, "state_display": state})
            for k, code, state in changes
        ],
    )


def test_it02_14_outcome_mapping_unknown_and_canceled(build_harness: BuildHarness) -> None:
    """IT02-14 close codes incl. an unknown one and canceled states: the outcome is mapped;
    an unknown code gives NULL and is counted in `stg.cast_stats`; a canceled state with no
    mapped code gives `canceled`."""
    _change_lake(build_harness)
    build_core(build_harness, mappings=ENUMS, hi=250)
    got = build_harness.query('SELECT "number", outcome FROM core.change ORDER BY 1')
    assert got == [
        ("C1", "successful"),
        ("C2", "successful_with_issues"),
        ("C3", "backed_out"),
        ("C4", None),
        ("C5", "canceled"),
        ("C6", "canceled"),
        ("C7", None),
        ("C8", "unsuccessful"),
        ("C9", "canceled"),
    ]
    assert cast_stats(build_harness, "sn_change_request")["outcome"] == (6, 2)
    build_core(build_harness, mappings=ENUMS, hi=250)  # re-run: counts not doubled
    assert cast_stats(build_harness, "sn_change_request")["outcome"] == (6, 2)


def test_it02_14_change_columns(build_harness: BuildHarness) -> None:
    """IT02-14 one fully populated change: every column per U02-119 (type canonical,
    service from business_service, else the single service related to the CI)."""
    raw = build_harness.layout.raw
    commit(raw, SN, "cmdb_ci_service", [Row("s1", at(0), {"name": "Checkout"})])
    commit(raw, SN, "cmdb_rel_ci", [Row("r1", at(0), {"parent": "s1", "child": "ci1"})])
    full = {
        "number": "CHG1",
        "type": "Emergency",
        "state_display": "Closed",
        "state": "3",
        "risk_display": "High",
        "opened_at": "2024-03-01 08:00:00",
        "start_date": "2024-03-01 09:00:00",
        "end_date": "2024-03-01 10:00:00",
        "work_start": "2024-03-01 09:05:00",
        "work_end": "2024-03-01 09:55:00",
        "cmdb_ci": "ci1",
        "assignment_group": "g1",
        "close_code": "successful",
        "short_description": "Patch",
        "description": "Kernel patch",
    }
    other = {"number": "CHG2", "type": "Weird", "business_service": "s7", "cmdb_ci": "ci1"}
    commit(raw, SN, "change_request", [Row("k1", at(0), full), Row("k2", at(1), other)])
    build_core(build_harness, mappings=ENUMS, hi=250)
    assert _columns(build_harness, "change") == [
        ("record_id", "VARCHAR"),
        ("number", "VARCHAR"),
        ("state", "VARCHAR"),
        ("risk", "VARCHAR"),
        ("type", "VARCHAR"),
        ("opened_at", TS),
        ("planned_start", TS),
        ("planned_end", TS),
        ("actual_start", TS),
        ("actual_end", TS),
        ("service_id", "VARCHAR"),
        ("ci_id", "VARCHAR"),
        ("team_id", "VARCHAR"),
        ("outcome", "VARCHAR"),
        ("short_description", "VARCHAR"),
        ("description", "VARCHAR"),
        ("source_updated_at", TS),
    ]
    rows = build_harness.query('SELECT * FROM core.change ORDER BY "number"')
    assert rows == [
        (
            "servicenow:change_request:k1",
            "CHG1",
            "Closed",
            "High",
            "emergency",
            _ts("2024-03-01 08:00:00"),
            _ts("2024-03-01 09:00:00"),
            _ts("2024-03-01 10:00:00"),
            _ts("2024-03-01 09:05:00"),
            _ts("2024-03-01 09:55:00"),
            SVC + "s1",
            SVC + "ci1",
            TEAM + "g1",
            "successful",
            "Patch",
            "Kernel patch",
            at(0),
        ),
        (
            "servicenow:change_request:k2",
            "CHG2",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            SVC + "s7",
            SVC + "ci1",
            None,
            None,
            None,
            None,
            at(1),
        ),
    ]


def test_it02_15_problem_columns(build_harness: BuildHarness) -> None:
    """IT02-15 problem rows: `core.problem` columns per U02-120 (record IDs of service and
    team, display state first, typed known_error, raw root cause text)."""
    raw = build_harness.layout.raw
    rows = [
        Row(
            "p1",
            at(0),
            {
                "number": "PRB1",
                "business_service": "s1",
                "assignment_group": "g1",
                "opened_at": "2024-03-01 08:00:00",
                "resolved_at": "2024-03-03 08:00:00",
                "problem_state_display": "Resolved",
                "problem_state": "106",
                "known_error": "true",
                "cause_notes": "Disk full on db01",
            },
        ),
        Row(
            "p2",
            at(1),
            {"number": "PRB2", "business_service": "", "state": "101", "known_error": "maybe"},
        ),
    ]
    commit(raw, SN, "problem", rows)
    build_core(build_harness, hi=250)
    assert _columns(build_harness, "problem") == [
        ("record_id", "VARCHAR"),
        ("number", "VARCHAR"),
        ("opened_at", TS),
        ("resolved_at", TS),
        ("state", "VARCHAR"),
        ("service_id", "VARCHAR"),
        ("team_id", "VARCHAR"),
        ("known_error", "BOOLEAN"),
        ("root_cause_text", "VARCHAR"),
        ("source_updated_at", TS),
    ]
    assert build_harness.query('SELECT * FROM core.problem ORDER BY "number"') == [
        (
            "servicenow:problem:p1",
            "PRB1",
            _ts("2024-03-01 08:00:00"),
            _ts("2024-03-03 08:00:00"),
            "Resolved",
            SVC + "s1",
            TEAM + "g1",
            True,
            "Disk full on db01",
            at(0),
        ),
        ("servicenow:problem:p2", "PRB2", None, None, "101", None, None, None, None, at(1)),
    ]


def test_it02_15_empty_lake(build_harness: BuildHarness) -> None:
    """IT02-15 (R-60) an empty lake: `core.change` and `core.problem` exist empty."""
    build_core(build_harness, hi=250)
    for table in ("change", "problem"):
        assert build_harness.query(f"SELECT count(*) FROM core.{table}") == [(0,)]  # noqa: S608
        assert _columns(build_harness, table)
