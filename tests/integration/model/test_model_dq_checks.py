"""`900_dq_checks.sql` and the DQ gate on crafted tables (impl 02 T02-20: U02-93, U02-94, U02-127).

IT02-28: each check row of `meta.dq_result` has the value, threshold, severity and result of
the U02-127 table; `evaluate_gate` reads them back, fails closed on an empty table and keeps
rows of other producers (spec 04 invariants) in its decision.
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Mapping

import pytest
import structlog
from tests.support.build_harness import BuildHarness, RefData

from herness.core.errors import SchemaViolation
from herness.model import meta
from herness.model.dq import DqOutcome, evaluate_gate
from herness.model.settings import DqSettings

pytestmark = pytest.mark.integration

STARTED = datetime.datetime(2026, 9, 1, 12, 0, tzinfo=datetime.UTC)
COUNTED = (
    "org",
    "team",
    "service",
    "service_map",
    "incident",
    "change",
    "problem",
    "event",
    "metric_daily",
    "work_item",
    "work_item_transition",
    "work_item_link",
)
FUTURE = (
    "incident.opened_at",
    "incident.resolved_at",
    "incident.closed_at",
    "change.opened_at",
    "change.actual_start",
    "change.actual_end",
    "problem.opened_at",
    "problem.resolved_at",
    "event.ts",
    "work_item.created_at",
    "work_item.resolved_at",
)
KEYED = ("org", "team", "service", "incident", "change", "problem", "work_item", "event")
KEYED_ALL = (*KEYED, "metric_daily")
# 12 row_count_drop + 2 service-null + 11 future + 1 resolved + 9 duplicate + coverage + metric
FIXED_CHECKS = 12 + 2 + 11 + 1 + 9 + 1 + 1

Row = tuple[str, float, float, bool]


def _setup(harness: BuildHarness, prev: Mapping[str, int] | None = None) -> None:
    harness.run(0, 299, refdata=RefData(prev_row_counts=prev))
    meta.insert_build_row(
        harness.con,
        build_id=harness.build_id,
        started_at=STARTED,
        git_sha="unknown",
        config_hash="cfg_0123456789abcdef",
        dataset_kind="real",
        source_watermarks={},
    )


def _dq(harness: BuildHarness, dq: DqSettings | None = None) -> dict[str, Row]:
    harness.run(900, 999, context=harness.context(dq=dq))
    rows = harness.query(
        "SELECT check_name, severity, value, threshold, passed FROM meta.dq_result"
        " WHERE json_extract_string(details, '$.producer') = 'dq900'"
    )
    out = {str(r[0]): (str(r[1]), float(r[2]), float(r[3]), bool(r[4])) for r in rows}  # type: ignore[arg-type]
    assert len(out) == len(rows), "one row per check"
    return out


def _details(harness: BuildHarness, check: str) -> dict[str, object]:
    [(raw,)] = harness.query("SELECT details FROM meta.dq_result WHERE check_name = ?", [check])
    loaded: dict[str, object] = json.loads(str(raw))
    return loaded


def _incidents(harness: BuildHarness, rows: list[tuple[object, ...]]) -> None:
    harness.con.executemany(
        "INSERT INTO core.incident (record_id, service_id, opened_at, resolved_at)"
        " VALUES (?, ?, ?, ?)",
        rows,
    )


def _ts(day: int, year: int = 2026) -> datetime.datetime:
    return datetime.datetime(year, 8, day, tzinfo=datetime.UTC)


def test_it02_28_empty_build_all_checks_pass(build_harness: BuildHarness) -> None:
    """IT02-28 empty `core` and no previous build: every fixed check is present and passes
    with value 0 (coverage 1.0), `no_previous_build` is set, no `cast_fail` rows."""
    _setup(build_harness)
    rows = _dq(build_harness)
    assert len(rows) == FIXED_CHECKS
    defaults = DqSettings()
    for table in COUNTED:
        name = f"row_count_drop:core.{table}"
        assert rows[name] == ("error", 0.0, defaults.row_count_drop_max, True), name
    assert _details(build_harness, "row_count_drop:core.incident") == {
        "producer": "dq900",
        "prev": 0,
        "cur": 0,
        "no_previous_build": True,
    }
    assert rows["incident_service_null"] == ("warn", 0.0, 0.30, True)
    assert rows["work_item_service_null"] == ("warn", 0.0, 0.40, True)
    for column in FUTURE:
        assert rows[f"future_timestamp:core.{column}"] == ("warn", 0.0, 0.0, True)
    assert rows["resolved_before_opened"] == ("warn", 0.0, 0.001, True)
    for table in KEYED_ALL:
        assert rows[f"duplicate_key:core.{table}"] == ("error", 0.0, 0.0, True)
    assert rows["decision_coverage_incident"] == ("warn", 1.0, 0.95, True)
    assert rows["metric_daily_unmapped_service"] == ("warn", 0.0, 0.05, True)
    assert not [name for name in rows if name.startswith("cast_fail:")]
    outcome = evaluate_gate(build_harness.con)
    assert outcome == DqOutcome(True, (), (), FIXED_CHECKS)


def _crafted(harness: BuildHarness) -> None:
    """Core, enrich and staging rows that make every kind of check fail once."""
    con = harness.con
    _incidents(
        harness,
        [
            ("i1", "s1", _ts(1), _ts(2)),
            ("i2", "s1", _ts(3), _ts(1)),  # resolved before opened
            ("i3", None, _ts(1), None),
            ("i4", "s1", datetime.datetime(2027, 1, 1, tzinfo=datetime.UTC), None),  # future
            ("i5", "s1", _ts(1), _ts(4)),
            ("i6", "s1", _ts(1), _ts(4)),
            ("i7", "s1", _ts(1), _ts(4)),
            ("i7", "s1", _ts(1), _ts(4)),  # duplicate key
        ],
    )
    con.execute("INSERT INTO core.org (org_id) VALUES ('o1'), ('o2'), ('o3')")
    con.execute(
        "INSERT INTO core.work_item (record_id, service_id) VALUES ('w1', 's1'), ('w2', NULL)"
    )
    con.execute(
        "INSERT INTO core.metric_daily (date, service_id, metric_name, value, source_tool)"
        " VALUES (DATE '2026-08-01', 's1', 'm', 1, 'dd'), (DATE '2026-08-01', 's1', 'm', 2, 'dd'),"
        " (DATE '2026-08-01', 's1', 'm', 3, 'other')"
    )
    con.execute("INSERT INTO enrich.text_redacted (record_id) VALUES ('i1'), ('i2'), ('zz')")
    con.execute("INSERT INTO enrich.decision (record_id) VALUES ('i1'), ('i1'), ('zz')")
    con.execute("DELETE FROM stg.cast_stats")
    con.execute(
        "INSERT INTO stg.cast_stats VALUES ('stg.sn_incident', 'opened_at', 100, 1),"
        " ('stg.sn_x', 'a', 0, 0), ('stg.sn_y', 'b', 50, 0), ('stg.sn_y', 'b', 50, 0)"
    )
    con.execute("DELETE FROM stg.build_counts WHERE name LIKE 'metric_daily%'")
    con.execute(
        "INSERT INTO stg.build_counts VALUES ('metric_daily_raw', 20), ('metric_daily_unmapped', 2)"
    )


def test_it02_28_crafted_checks_values(build_harness: BuildHarness) -> None:
    """IT02-28 crafted rows: each check's value, threshold, severity and result as U02-127
    (drop against the previous counts, shares, counts, duplicate keys, coverage, DD02-07)."""
    _setup(build_harness, prev={"core.incident": 10, "core.org": 2, "bad name": 5})
    _crafted(build_harness)
    rows = _dq(build_harness)
    assert rows["row_count_drop:core.incident"] == ("error", 0.2, 0.05, False)
    assert rows["row_count_drop:core.org"] == ("error", -0.5, 0.05, True)
    assert rows["row_count_drop:core.team"] == ("error", 0.0, 0.05, True)
    assert _details(build_harness, "row_count_drop:core.incident") == {
        "producer": "dq900",
        "prev": 10,
        "cur": 8,
    }
    assert rows["incident_service_null"] == ("warn", 0.125, 0.30, True)
    assert _details(build_harness, "incident_service_null") == {
        "producer": "dq900",
        "null_rows": 1,
        "rows": 8,
    }
    assert rows["work_item_service_null"] == ("warn", 0.5, 0.40, False)
    assert rows["cast_fail:stg.sn_incident.opened_at"] == ("warn", 0.01, 0.005, False)
    assert rows["cast_fail:stg.sn_y.b"] == ("warn", 0.0, 0.005, True)
    assert _details(build_harness, "cast_fail:stg.sn_y.b") == {
        "producer": "dq900",
        "failed": 0,
        "non_null": 100,
    }
    assert "cast_fail:stg.sn_x.a" not in rows
    assert rows["future_timestamp:core.incident.opened_at"] == ("warn", 1.0, 0.0, False)
    assert rows["future_timestamp:core.incident.resolved_at"] == ("warn", 0.0, 0.0, True)
    # both set: i1, i2, i5, i6, i7, i7 -> 1 of 6
    assert rows["resolved_before_opened"] == ("warn", 1 / 6, 0.001, False)
    assert _details(build_harness, "resolved_before_opened") == {"producer": "dq900", "rows": 6}
    assert rows["duplicate_key:core.incident"] == ("error", 1.0, 0.0, False)
    assert rows["duplicate_key:core.metric_daily"] == ("error", 1.0, 0.0, False)
    assert rows["duplicate_key:core.org"] == ("error", 0.0, 0.0, True)
    # incidents with text: i1 (decided), i2 (not); zz is no incident
    assert rows["decision_coverage_incident"] == ("warn", 0.5, 0.95, False)
    assert _details(build_harness, "decision_coverage_incident") == {
        "producer": "dq900",
        "covered": 1,
        "with_text": 2,
    }
    assert rows["metric_daily_unmapped_service"] == ("warn", 0.1, 0.05, False)
    assert _details(build_harness, "metric_daily_unmapped_service") == {
        "producer": "dq900",
        "unmapped": 2,
        "raw": 20,
    }
    assert len(rows) == FIXED_CHECKS + 2
    with structlog.testing.capture_logs() as logs:
        outcome = evaluate_gate(build_harness.con, build_id=build_harness.build_id)
    assert not outcome.passed
    assert outcome.failed_errors == (
        "duplicate_key:core.incident",
        "duplicate_key:core.metric_daily",
        "row_count_drop:core.incident",
    )
    assert set(outcome.failed_warnings) == {
        "work_item_service_null",
        "cast_fail:stg.sn_incident.opened_at",
        "future_timestamp:core.incident.opened_at",
        "resolved_before_opened",
        "decision_coverage_incident",
        "metric_daily_unmapped_service",
    }
    failed = [
        (e["check_name"], e["log_level"]) for e in logs if e["event"] == "model.dq.check_failed"
    ]
    assert ("row_count_drop:core.incident", "error") in failed
    assert ("resolved_before_opened", "warning") in failed
    assert len(failed) == 9
    [evaluated] = [e for e in logs if e["event"] == "model.dq.evaluated"]
    assert (evaluated["checks"], evaluated["failed_errors"], evaluated["failed_warnings"]) == (
        FIXED_CHECKS + 2,
        3,
        6,
    )


@pytest.mark.parametrize(
    ("null_rows", "expected"),
    [
        (2, ("warn", 0.25, 0.30, True)),
        (3, ("warn", 0.375, 0.30, False)),
        (5, ("error", 0.625, 0.60, False)),
    ],
)
def test_it02_28_incident_service_null_severity(
    build_harness: BuildHarness, null_rows: int, expected: Row
) -> None:
    """IT02-28 `incident_service_null`: `warn` with the warn threshold up to the error
    threshold, above it `error` with the error threshold and always failed."""
    _setup(build_harness)
    _incidents(
        build_harness,
        [(f"i{n}", None if n < null_rows else "s1", _ts(1), None) for n in range(8)],
    )
    assert _dq(build_harness)["incident_service_null"] == expected


def test_it02_28_thresholds_from_config(build_harness: BuildHarness) -> None:
    """IT02-28 thresholds come from the `dq` settings of the render context; incidents
    without any text row give coverage 0.0."""
    _setup(build_harness, prev={"core.incident": 10})
    _incidents(build_harness, [(f"i{n}", "s1", _ts(1), None) for n in range(8)])
    dq = DqSettings(row_count_drop_max=0.25, decision_coverage_min=0.0, future_timestamp_max=3)
    rows = _dq(build_harness, dq)
    assert rows["row_count_drop:core.incident"] == ("error", 0.2, 0.25, True)
    assert rows["decision_coverage_incident"] == ("warn", 0.0, 0.0, True)
    assert rows["future_timestamp:core.event.ts"] == ("warn", 0.0, 3.0, True)


def test_it02_28_rerun_replaces_own_rows_keeps_other_producers(
    build_harness: BuildHarness,
) -> None:
    """IT02-28 a re-run replaces only `dq900` rows; a spec 04 invariant row stays and counts
    in the gate (a failed spec 04 error blocks)."""
    _setup(build_harness)
    build_harness.con.execute(
        "INSERT INTO meta.dq_result VALUES ('invariant:x', 'error', 1, 0, false,"
        ' \'{"producer": "metrics"}\')'
    )
    _dq(build_harness)
    _dq(build_harness)
    [(total,)] = build_harness.query("SELECT count(*) FROM meta.dq_result")
    assert total == FIXED_CHECKS + 1
    outcome = evaluate_gate(build_harness.con)
    assert outcome == DqOutcome(False, ("invariant:x",), (), FIXED_CHECKS + 1)


def test_it02_28_gate_fails_closed(build_harness: BuildHarness) -> None:
    """IT02-28 (U02-94 step 2, TH02-17) no rows -> SchemaViolation; an unknown severity or a
    NULL result -> SchemaViolation; no check value text in the message."""
    build_harness.run(0, 99)
    with pytest.raises(SchemaViolation, match="no DQ results for build"):
        evaluate_gate(build_harness.con)
    build_harness.con.execute(
        "INSERT INTO meta.dq_result (check_name, severity, passed) VALUES ('a', 'fatal', false)"
    )
    with pytest.raises(SchemaViolation, match="invalid DQ result row"):
        evaluate_gate(build_harness.con)
    build_harness.con.execute("DELETE FROM meta.dq_result")
    build_harness.con.execute(
        "INSERT INTO meta.dq_result (check_name, severity, passed) VALUES ('a', 'warn', NULL)"
    )
    with pytest.raises(SchemaViolation, match="invalid DQ result row"):
        evaluate_gate(build_harness.con)
    build_harness.con.execute("DROP TABLE meta.dq_result")
    with pytest.raises(SchemaViolation, match="cannot read DQ results") as info:
        evaluate_gate(build_harness.con)
    assert info.value.__cause__ is None


def test_it02_28_outcome_invariant() -> None:
    """IT02-28 (U02-93) `passed` iff no failed error and at least one check."""
    assert DqOutcome(True, (), ("w",), 2).passed
    for bad in (
        {"passed": True, "failed_errors": ("e",), "failed_warnings": (), "checks": 1},
        {"passed": True, "failed_errors": (), "failed_warnings": (), "checks": 0},
        {"passed": False, "failed_errors": (), "failed_warnings": (), "checks": 1},
    ):
        with pytest.raises(ValueError, match="passed"):
            DqOutcome(**bad)  # type: ignore[arg-type]
