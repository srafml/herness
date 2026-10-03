"""Tests for the funding attribution (impl 04 U04-64, U04-66 steps 1-2; T04-14).

Each test builds a small graph (`tests.support.metrics_funding`) on the empty `metrics_tiny`
DDL with real stage 400 facts, runs `run_funding_step` and reads `score.funding_attribution`.
Shipped weights: cluster 0.8, root cause 0.6, service 0.3; noise 3 min x 100 USD/h = 5 USD,
backout 4 h x 100 USD/h = 400 USD. A 60-minute priority-1 incident costs 10000 USD.
"""

from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import duckdb
import pytest
from tests.support.metrics_funding import (
    COLUMNS,
    attribution,
    incident,
    item,
    member,
    mention,
    share_sums,
    step_context,
    warehouse,
)
from tests.support.metrics_tiny import BUILD_ID, tiny_weights

from herness.metrics.evidence import WRITABLE_TABLES
from herness.metrics.funding import ATTRIBUTION_TABLE, FUNDING_TABLE, run_funding_step
from herness.metrics.render import render_named
from herness.metrics.settings import WeightsConfig

pytestmark = pytest.mark.unit

EVIDENCE_SQL_CAP = 20_000


@pytest.fixture
def closing() -> Iterator[list[duckdb.DuckDBPyConnection]]:
    """Connections appended here are closed after the test."""
    cons: list[duckdb.DuckDBPyConnection] = []
    yield cons
    for con in cons:
        con.close()


def _rows_for(rows: list[dict[str, Any]], record_id: str) -> list[dict[str, Any]]:
    return [r for r in rows if r["record_id"] == record_id]


def _assert_shares_sum_to_one(con: duckdb.DuckDBPyConnection) -> None:
    assert all(abs(s - 1.0) <= 1e-9 for s in share_sums(con))


# --- UT04-75 direct links and the step adapter --------------------------------------------------


def test_ut04_75_incident_linked_to_two_epics_splits_evenly(
    closing: list[duckdb.DuckDBPyConnection],
) -> None:
    """UT04-75 an incident linked directly to two epics gets shares 0.5/0.5 at tier 1."""
    con = warehouse(
        {
            "core.incident": [incident("I1", service="S9")],
            "core.work_item": [item("A", "PAY-1"), item("B", "PAY-2")],
            "core.work_item_link": [mention("PAY-1", "I1"), mention("PAY-2", "I1", reverse=True)],
        }
    )
    closing.append(con)
    rows = attribution(con)
    assert rows == [
        dict(zip(COLUMNS, (k, "I1", "incident", 1, 1.0, 0.5, Decimal("5000.00"), 1.0), strict=True))
        for k in ("A", "B")
    ]


def test_ut04_75_run_funding_step_records_evidence(
    closing: list[duckdb.DuckDBPyConnection],
) -> None:
    """UT04-75 the step stores the attribution with its query_id and one evidence row."""
    con = warehouse(
        {
            "core.incident": [incident("I1", service="S9")],
            "core.work_item": [item("A", "PAY-1"), item("B", "PAY-2")],
            "core.work_item_link": [mention("PAY-1", "I1"), mention("PAY-2", "I1")],
        }
    )
    closing.append(con)
    sc = step_context()
    result = run_funding_step(con, sc)
    # Step 3 (T04-15) also stores score.funding: candidates A and B.
    assert result.row_counts == {ATTRIBUTION_TABLE: 2, FUNDING_TABLE: 2}
    assert (result.warnings, result.flags, result.failed_checks) == ([], [], [])
    ids = con.execute(f"SELECT DISTINCT query_id FROM {ATTRIBUTION_TABLE}").fetchall()  # noqa: S608
    assert len(ids) == 1
    qid = ids[0][0]
    evidence = con.execute(
        "SELECT producer, row_count, sql, params FROM meta.evidence WHERE query_id = ?", [qid]
    ).fetchall()
    assert len(evidence) == 1
    producer, row_count, sql, params = evidence[0]
    assert (producer, row_count) == ("score", 2)
    assert len(sql) < EVIDENCE_SQL_CAP
    assert '"name":"funding_attribution"' in params
    repeat = run_funding_step(con, sc).row_counts
    assert repeat == {ATTRIBUTION_TABLE: 2, FUNDING_TABLE: 2}  # repeatable
    again = con.execute(f"SELECT DISTINCT query_id FROM {ATTRIBUTION_TABLE}").fetchall()  # noqa: S608
    assert again == ids


def test_ut04_75_output_schema_has_no_free_form_column(
    closing: list[duckdb.DuckDBPyConnection],
) -> None:
    """UT04-75 stored columns and types are the U04-64 signature plus query_id (TH04-04)."""
    con = warehouse({})
    closing.append(con)
    assert attribution(con) == []
    described = con.execute(f"DESCRIBE {ATTRIBUTION_TABLE}").fetchall()
    assert [(r[0], r[1]) for r in described] == [
        ("candidate_id", "VARCHAR"),
        ("record_id", "VARCHAR"),
        ("record_kind", "VARCHAR"),
        ("tier", "INTEGER"),
        ("weight", "DOUBLE"),
        ("share", "DOUBLE"),
        ("pain_usd", "DECIMAL(18,2)"),
        ("quality", "DOUBLE"),
        ("query_id", "VARCHAR"),
    ]
    assert ATTRIBUTION_TABLE in WRITABLE_TABLES


def test_ut04_75_binds_and_rendered_size() -> None:
    """UT04-75 the template uses only typed binds and renders under the evidence SQL cap."""
    sc = step_context()
    rendered = render_named("funding_attribution", {}, sc.binds())
    assert len(rendered.sql) < EVIDENCE_SQL_CAP
    assert "$unconfirmed" not in rendered.sql
    assert set(rendered.bind) == {
        *("as_of", "as_of_ts", "tz", "s_window_days", "s_min_direct_links"),
        *("s_cluster_weight", "s_root_cause_weight", "s_service_weight"),
        *("d_noise_severities", "d_failure_outcomes"),
        *("w_triage_minutes", "w_engineer_hour", "w_backout_hours"),
        *("w_cf_min_incidents", "w_cf_min_pain", "w_cf_max_linked_share"),
    }
    assert rendered.template == {"name": "funding_attribution"}


# --- UT04-76 best tier only ------------------------------------------------------------------


def test_ut04_76_direct_cluster_and_service_paths_keep_tier_one(
    closing: list[duckdb.DuckDBPyConnection],
) -> None:
    """UT04-76 with direct, cluster and service paths only the tier-1 path is kept."""
    con = warehouse(
        {
            "core.incident": [incident("I1"), incident("I2", service="S9"), incident("I3")],
            "core.work_item": [
                item("A", "PAY-1"),
                item("A1", "PAY-11", kind="story", parent="PAY-1"),
                item("B", "PAY-2"),
                item("C", "PAY-3", service="S1"),
            ],
            # I1 rolls up from a story to epic A; B is linked to I2 of the same cluster.
            "core.work_item_link": [mention("PAY-11", "I1"), mention("PAY-2", "I2")],
            "enrich.cluster_member": [member("I1", "C1"), member("I2", "C1", 0.8)],
        }
    )
    closing.append(con)
    rows = attribution(con)
    assert [(r["candidate_id"], r["tier"], r["share"]) for r in _rows_for(rows, "I1")] == [
        ("A", 1, 1.0)
    ]
    assert [(r["candidate_id"], r["tier"]) for r in _rows_for(rows, "I2")] == [("B", 1)]
    # I3 (no cluster, on S1) has only the service path.
    assert [(r["candidate_id"], r["tier"], r["weight"]) for r in _rows_for(rows, "I3")] == [
        ("C", 3, pytest.approx(0.3))
    ]
    _assert_shares_sum_to_one(con)


def test_ut04_76_cluster_path_needs_min_direct_links(
    closing: list[duckdb.DuckDBPyConnection],
) -> None:
    """UT04-76 a cluster path weighs cluster_weight x membership and beats the service path."""
    con = warehouse(
        {
            "core.incident": [incident("I1", service="S9"), incident("I2")],
            "core.work_item": [item("A", "PAY-1"), item("C", "PAY-3", service="S1")],
            "core.work_item_link": [mention("PAY-1", "I1")],
            "enrich.cluster_member": [member("I1", "C1"), member("I2", "C1", 0.6)],
        }
    )
    closing.append(con)
    rows = _rows_for(attribution(con), "I2")
    assert [(r["candidate_id"], r["tier"], r["share"]) for r in rows] == [("A", 2, 1.0)]
    assert rows[0]["weight"] == pytest.approx(0.8 * 0.6)
    assert rows[0]["quality"] == pytest.approx(0.6)
    assert rows[0]["pain_usd"] == Decimal("10000.00")


# --- UT04-77 root cause ------------------------------------------------------------------------


def _root_cause_rows(latest: str) -> dict[str, list[dict[str, object]]]:
    decision = {"record_id": "D", "question": "root_cause", "decider": "m1"}
    return {
        "core.incident": [incident("I1")],
        "core.work_item": [item("D", "PAY-4", service="S1"), item("E", "PAY-5", service="S1")],
        "enrich.cluster_member": [member("I1", "C1", 0.9)],
        "enrich.cluster": [
            {"cluster_id": "C1", "root_cause_category": "config", "service_ids": ["S1"]}
        ],
        "enrich.decision": [
            {**decision, "answer": "network", "probability": 0.9,
             "decided_at": "2026-01-01 00:00:00+00"},
            {**decision, "answer": latest, "probability": 0.5,
             "decided_at": "2026-02-01 00:00:00+00"},
            {**decision, "question": "other", "answer": "config", "probability": 1.0,
             "decided_at": "2026-03-01 00:00:00+00"},
        ],
    }  # fmt: skip


def test_ut04_77_root_cause_decision_matching_cluster(
    closing: list[duckdb.DuckDBPyConnection],
) -> None:
    """UT04-77 the latest root-cause decision matching the cluster gives a tier-2 path."""
    con = warehouse(_root_cause_rows("config"))
    closing.append(con)
    rows = attribution(con)
    assert [(r["candidate_id"], r["tier"], r["share"]) for r in rows] == [("D", 2, 1.0)]
    assert rows[0]["weight"] == pytest.approx(0.6 * 0.9 * 0.5)
    assert rows[0]["quality"] == pytest.approx(0.9 * 0.5)


def test_ut04_77_non_matching_latest_decision_falls_to_service(
    closing: list[duckdb.DuckDBPyConnection],
) -> None:
    """UT04-77 an older matching decision is ignored; both service paths split the incident."""
    con = warehouse(_root_cause_rows("storage"))
    closing.append(con)
    rows = attribution(con)
    assert [(r["candidate_id"], r["tier"], r["share"]) for r in rows] == [
        ("D", 3, 0.5),
        ("E", 3, 0.5),
    ]


# --- UT04-78 leaf-most ------------------------------------------------------------------------


def test_ut04_78_epic_under_initiative_keeps_epic_only(
    closing: list[duckdb.DuckDBPyConnection],
) -> None:
    """UT04-78 epic under an initiative, both service-matched: only the epic path remains."""
    service_map = [
        {"service_id": "S1", "jira_project": "PAY", "jira_component": None, "confidence": 0.8},
        {"service_id": "S2", "jira_project": "PAY", "jira_component": "api", "confidence": 0.8},
        {"service_id": "S3", "jira_project": "PAY", "jira_component": "web", "confidence": 0.9},
    ]
    con = warehouse(
        {
            "core.incident": [incident("I1"), incident("I2", service="S2")],
            "core.work_item": [
                item("N", "PAY-1", kind="initiative", status="todo", components=["api"]),
                item("E", "PAY-2", parent="PAY-1", service="S1", components=["web"]),
            ],
            "core.service_map": service_map,
        }
    )
    closing.append(con)
    rows = attribution(con)
    # I1 on S1: initiative via the project map (0.8), epic via service_id (1.0) -> epic only.
    i1 = _rows_for(rows, "I1")
    assert [(r["candidate_id"], r["tier"], r["share"]) for r in i1] == [("E", 3, 1.0)]
    assert (i1[0]["weight"], i1[0]["quality"]) == (pytest.approx(0.3), pytest.approx(0.5))
    # I2 on S2: only the initiative lists component api -> initiative at 0.3 x 0.8.
    i2 = _rows_for(rows, "I2")
    assert [(r["candidate_id"], r["tier"], r["share"]) for r in i2] == [("N", 3, 1.0)]
    assert (i2[0]["weight"], i2[0]["quality"]) == (pytest.approx(0.24), pytest.approx(0.4))


# --- UT04-79 cluster-fix candidates ------------------------------------------------------------


def _cluster_fix_weights() -> WeightsConfig:
    w = tiny_weights()
    cf = w.cluster_fix.model_copy(
        update={"min_incidents_12m": 2, "min_annual_pain_usd": 50000, "max_linked_share": 0.2}
    )
    return w.model_copy(update={"cluster_fix": cf})


def test_ut04_79_cluster_fix_candidate_only_above_thresholds(
    closing: list[duckdb.DuckDBPyConnection],
) -> None:
    """UT04-79 only the cluster over all three thresholds becomes a cluster_fix candidate."""
    first = "2026-01-01 10:00:00-05"  # observed_days = 90 at as_of 2026-04-01
    incidents = [
        incident("I1", opened=first, service="S9"),  # CA: 2 x 10000 -> annual 81111
        incident("I2", service="S9"),
        incident("I3", service="S9"),  # CB: one incident only
        incident("I4", service="S9", priority=4),  # CC: priority 4 -> no pain
        incident("I5", service="S9", priority=4),
        incident("I6", service="S9"),  # CD: linked share 1 in pass 1
        incident("I7", service="S9"),
        incident("I8", service="S9", minutes=36),  # CE: 2 x 6000 -> annual 48667
        incident("I9", service="S9", minutes=36),
    ]
    clusters = {"I1": "CA", "I2": "CA", "I3": "CB", "I4": "CC", "I5": "CC"}
    clusters |= {"I6": "CD", "I7": "CD", "I8": "CE", "I9": "CE"}
    weights = _cluster_fix_weights()
    con = warehouse(
        {
            "core.incident": incidents,
            "core.work_item": [item("X", "PAY-1")],
            "core.work_item_link": [mention("PAY-1", "I6")],
            "enrich.cluster_member": [member(i, c, 0.75) for i, c in clusters.items()],
        },
        weights,
    )
    closing.append(con)
    rows = attribution(con, weights)
    fixes = [r for r in rows if r["candidate_id"].startswith("cluster_fix:")]
    assert [(r["candidate_id"], r["record_id"], r["tier"], r["share"]) for r in fixes] == [
        ("cluster_fix:CA", "I1", 2, 1.0),
        ("cluster_fix:CA", "I2", 2, 1.0),
    ]
    assert all(r["weight"] == pytest.approx(0.8 * 0.75) for r in fixes)
    assert all(r["quality"] == pytest.approx(0.75) for r in fixes)
    assert {(r["record_id"], r["candidate_id"], r["tier"]) for r in rows} - {
        (r["record_id"], r["candidate_id"], r["tier"]) for r in fixes
    } == {("I6", "X", 1), ("I7", "X", 2)}


def test_ut04_79_no_incidents_no_rows(closing: list[duckdb.DuckDBPyConnection]) -> None:
    """UT04-79 with no incidents there is no cluster and no attribution row."""
    weights = _cluster_fix_weights().model_copy()
    con = warehouse({"core.work_item": [item("X", "PAY-1", service="S1")]}, weights)
    closing.append(con)
    assert attribution(con, weights) == []


# --- UT04-86 events and changes -----------------------------------------------------------------


def test_ut04_86_noise_events_and_failed_change_tier_three(
    closing: list[duckdb.DuckDBPyConnection],
) -> None:
    """UT04-86 unlinked noise events and failed changes get tier 3 only, usd per formula."""
    ev = {"source_tool": "datadog", "service_id": "S1", "status": "closed"}
    ch = {"state": "closed", "type": "normal", "service_id": "S1", "team_id": "T1"}
    con = warehouse(
        {
            "core.work_item": [item("A", "PAY-1", service="S1"), item("B", "PAY-2")],
            "core.event": [
                {**ev, "event_id": "E1", "ts": "2026-02-01 10:00:00-05", "severity": "major"},
                {**ev, "event_id": "E2", "ts": "2026-02-01 10:00:00-05", "severity": "info"},
                {**ev, "event_id": "E3", "ts": "2026-02-01 10:00:00-05", "severity": "major",
                 "incident_id": "I9"},
                {**ev, "event_id": "E4", "ts": "2025-03-01 10:00:00-05", "severity": "major"},
            ],
            "core.change": [
                {**ch, "record_id": "CH1", "opened_at": "2026-02-01 08:00:00-05",
                 "actual_end": "2026-02-01 09:00:00-05", "outcome": "unsuccessful"},
                {**ch, "record_id": "CH2", "opened_at": "2026-02-01 08:00:00-05",
                 "actual_end": "2026-02-01 09:00:00-05", "outcome": "successful"},
                {**ch, "record_id": "CH3", "opened_at": "2025-01-01 08:00:00-05",
                 "actual_end": "2025-01-01 09:00:00-05", "outcome": "unsuccessful"},
            ],
            # A link naming an event ID is not an incident mention path.
            "core.work_item_link": [
                {"from_key": "PAY-2", "to_key": "E1", "link_type": "mentions_incident"}
            ],
        }
    )  # fmt: skip
    closing.append(con)
    rows = attribution(con)
    assert rows == [
        dict(zip(COLUMNS, ("A", rid, kind, 3, 0.3, 1.0, usd, 0.5), strict=True))
        for rid, kind, usd in (
            ("CH1", "change", Decimal("400.00")),
            ("E1", "event", Decimal("5.00")),
        )
    ]


def test_ut04_86_step_uses_build_and_bind_values(
    closing: list[duckdb.DuckDBPyConnection],
) -> None:
    """UT04-86 the noise cost follows the triage and engineer-hour binds of the step."""
    w = tiny_weights()
    toil = w.toil.model_copy(update={"triage_minutes_per_alert": 6})
    weights = w.model_copy(update={"toil": toil})
    ev = {"source_tool": "datadog", "service_id": "S1", "severity": "minor"}
    con = warehouse(
        {
            "core.work_item": [item("A", "PAY-1", service="S1")],
            "core.event": [{**ev, "event_id": "E1", "ts": "2026-03-31 23:00:00-04"}],
        },
        weights,
    )
    closing.append(con)
    rows = attribution(con, weights)
    assert [(r["record_id"], r["pain_usd"]) for r in rows] == [("E1", Decimal("10.00"))]
    assert step_context(weights).build_id == BUILD_ID
