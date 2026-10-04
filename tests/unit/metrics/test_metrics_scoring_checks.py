"""Tests for the score.* checks of step `check` and the scoring metric samples (impl 04 U04-56,
U04-59, U04-60; T04-21).

One module-scoped tiny build (`metrics_tiny`, small catalog) runs every step but `check`; each
test copies that file, plants (or not) one violation and runs `run_check_step` once. A check's
query_id is fixed per build and inputs, so every planted change precedes the first check run.
"""

import datetime
import json
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb
import pytest
from structlog.testing import capture_logs
from tests.support.build_harness import FakeJobContext
from tests.support.metrics_scoring import patches, save_as_file, small_catalog, tiny_with_facts
from tests.support.metrics_tiny import BUILD_ID, tiny_weights

from herness.core.errors import SchemaViolation
from herness.core.resilience._state import ProcessState
from herness.metrics import _scoring_checks
from herness.metrics.context import StepContext, StepResult
from herness.metrics.scoring import STEPS, run_check_step, run_scoring

pytestmark = pytest.mark.unit

BEFORE_CHECK = [s for s in STEPS if s not in ("validate", "check")]
SEVERITY = {name: severity for name, severity, _ in _scoring_checks.CHECKS}
UNCONFIRMED_TABLES = ("score.funding", "score.org", "score.action_lever")
SCORE_CHECKS = (
    "score_share_sum",
    "score_share_range",
    "score_pain_total",
    "score_confidence_range",
    "score_funding_rank_unique",
    "score_org_z_finite",
    "score_portfolio_budget",
    "score_portfolio_parent_child",
    "score_portfolio_blockers",
    "score_evidence_coverage",
    "score_unconfirmed_rows",
)
_FIRST_RECORD = "record_id = (SELECT min(record_id) FROM score.funding_attribution)"
_FIRST_ORG = (
    "(entity_type, entity_id, metric) = (SELECT (entity_type, entity_id, metric)"
    " FROM score.org ORDER BY ALL {} LIMIT 1)"
)
_SELECT_BOTH = "UPDATE score.portfolio SET selected = true WHERE scenario = 'lean'"
# One unconfirmed action_lever row (the tiny build yields none) carrying its own query id.
_LEVER_ROW = (
    "INSERT INTO score.action_lever (unconfirmed, query_ids) VALUES (true, ['q_forged00000000a4'])"
)
# (check, planting statements, expected value; None = count the unconfirmed rows)
VIOLATIONS: list[tuple[str, tuple[str, ...], float | None]] = [
    (
        "score_share_sum",
        (
            "INSERT INTO score.funding_attribution SELECT * REPLACE ('W9' AS candidate_id)"  # noqa: S608 - module constants
            f" FROM score.funding_attribution WHERE {_FIRST_RECORD}",
        ),
        1.0,
    ),
    (
        "score_share_range",
        (f"UPDATE score.funding_attribution SET share = 0 WHERE {_FIRST_RECORD}",),  # noqa: S608 - module constants
        1.0,
    ),
    ("score_confidence_range", ("UPDATE score.funding SET confidence = 1.5",), 2.0),
    ("score_funding_rank_unique", ("UPDATE score.funding SET rank = 1",), 1.0),
    (
        "score_org_z_finite",
        (
            "UPDATE score.org SET z_score = CAST('Infinity' AS DOUBLE)"  # noqa: S608 - module constants
            f" WHERE {_FIRST_ORG.format('')}",
            "UPDATE score.org SET trend_slope = CAST('NaN' AS DOUBLE)"  # noqa: S608 - module constants
            f" WHERE {_FIRST_ORG.format('DESC')}",
        ),
        2.0,
    ),
    (
        "score_portfolio_budget",
        (  # lean budget 1000000: floors sum to 999999, U04-60 ceilings to 1000001
            "UPDATE score.funding SET effort_cost_usd = 500000.40 WHERE candidate_id = 'W1'",
            "UPDATE score.funding SET effort_cost_usd = 499999.70 WHERE candidate_id = 'W2'",
            _SELECT_BOTH,
        ),
        1.0,
    ),
    ("score_portfolio_parent_child", (_SELECT_BOTH,), 1.0),
    (
        "score_portfolio_blockers",
        (
            "INSERT INTO core.work_item_link VALUES ('PAY-1', 'PAY-2', 'blocks')",
            "UPDATE score.portfolio SET selected = true"
            " WHERE scenario IN ('lean', 'base') AND candidate_id = 'W2'",
        ),
        2.0,
    ),
    (
        "score_evidence_coverage",
        (
            "UPDATE score.funding SET query_ids = ['q_forged00000000a1']",
            "UPDATE score.funding_attribution SET query_id = 'q_forged00000000a2'",
            "UPDATE score.org SET query_ids = list_append(query_ids, 'q_forged00000000a3')",
            _LEVER_ROW,
            "UPDATE score.portfolio SET query_ids = list_append(query_ids, 'q_forged00000000a5')",
        ),
        5.0,  # one distinct forged id per score table: dropping any branch changes the count
    ),
    ("score_unconfirmed_rows", ("UPDATE score.org SET unconfirmed = true", _LEVER_ROW), None),
]


def _step_context() -> StepContext:
    weights = tiny_weights()
    as_of = datetime.date(2026, 4, 1)
    return StepContext(
        BUILD_ID, small_catalog(), weights, as_of, weights.business_timezone, frozenset()
    )


@pytest.fixture(scope="module")
def scored_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The tiny build after every step but `check`."""
    path = tmp_path_factory.mktemp("scored") / "scored.duckdb"
    with pytest.MonkeyPatch.context() as mp:
        for obj, name, value in patches(small_catalog()):
            mp.setattr(obj, name, value)
        con = tiny_with_facts()
        try:
            run_scoring(BUILD_ID, steps=BEFORE_CHECK, con=con)
            save_as_file(con, path)
        finally:
            con.close()
    return path


@pytest.fixture
def scored(scored_file: Path, tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    """A private copy of the scored build file, opened writable."""
    copy = tmp_path / "copy.duckdb"
    shutil.copyfile(scored_file, copy)
    con = duckdb.connect(str(copy))
    try:
        yield con
    finally:
        con.close()


def _check(con: duckdb.DuckDBPyConnection) -> StepResult:
    con.execute("BEGIN TRANSACTION")
    result = run_check_step(con, _step_context())
    con.execute("COMMIT")
    return result


def _row(con: duckdb.DuckDBPyConnection, name: str) -> tuple[Any, ...]:
    rows = con.execute(
        "SELECT severity, value, threshold, passed, details FROM meta.dq_result"
        " WHERE check_name = ?",
        [name],
    ).fetchall()
    assert len(rows) == 1
    return tuple(rows[0])


def _count(con: duckdb.DuckDBPyConnection, sql: str) -> int:
    row = con.execute(sql).fetchone()
    assert row is not None
    return int(row[0])


def _unconfirmed(con: duckdb.DuckDBPyConnection, tables: tuple[str, ...]) -> int:
    sql = "SELECT count(*) FROM {} WHERE unconfirmed"
    return sum(_count(con, sql.format(t)) for t in tables)


# --- UT04-115 pass fixtures ------------------------------------------------------------------


@pytest.mark.parametrize("name", SCORE_CHECKS)
def test_ut04_115_score_check_passes_on_clean_build(
    scored: duckdb.DuckDBPyConnection, name: str
) -> None:
    """UT04-115 (pass) every score.* check runs (not skipped) and passes on the scored tiny
    build; `score_unconfirmed_rows` passes once no row is unconfirmed."""
    if name == "score_unconfirmed_rows":
        for table in UNCONFIRMED_TABLES:
            scored.execute(f"UPDATE {table} SET unconfirmed = false")  # noqa: S608 - module constants
    result = _check(scored)
    severity, value, threshold, passed, details = _row(scored, name)
    assert (severity, passed) == (SEVERITY[name], True)
    assert set(json.loads(details)) == {"query_id", "n_bad"}
    assert value <= threshold
    assert result.failed_checks == []
    if name == "score_pain_total":
        assert threshold > 0.01
    else:
        assert (value, threshold) == (0.0, 0.0)


# --- UT04-115 fail fixtures ------------------------------------------------------------------


@pytest.mark.parametrize(("name", "plant", "expected"), VIOLATIONS, ids=[v[0] for v in VIOLATIONS])
def test_ut04_115_score_check_detects_violation(
    scored: duckdb.DuckDBPyConnection, name: str, plant: tuple[str, ...], expected: float | None
) -> None:
    """UT04-115 (fail) one planted violation per score.* check: its dq row fails with the
    U04-60 value; an `error` check is a failed check, the `warn` one a report warning."""
    for sql in plant:
        scored.execute(sql)
    if expected is None:
        expected = float(_unconfirmed(scored, UNCONFIRMED_TABLES))
        assert expected > 0
    with capture_logs() as logs:
        result = _check(scored)
    severity, value, _threshold, passed, details = _row(scored, name)
    assert (severity, value, passed) == (SEVERITY[name], expected, False)
    assert json.loads(details)["n_bad"] == int(expected)
    if severity == "error":
        assert name in result.failed_checks
    else:
        assert result.warnings == [f"check {name} failed"]
    failed = [e["check_name"] for e in logs if e["event"] == "metrics.scoring.check_failed"]
    assert name in failed


def test_ut04_115_pain_total_fails_beyond_tolerance(scored: duckdb.DuckDBPyConnection) -> None:
    """UT04-115 `score_pain_total`: doubling one record's shares attributes more than the
    window total; value = annualized excess > 0.01 + 1e-9 x total (DD04-11), stored as the
    row's threshold."""
    double = f"UPDATE score.funding_attribution SET share = share * 2 WHERE {_FIRST_RECORD}"  # noqa: S608 - module constant
    scored.execute(double)
    result = _check(scored)
    _severity, value, threshold, passed, details = _row(scored, "score_pain_total")
    assert passed is False
    assert value > threshold > 0.01
    assert json.loads(details)["n_bad"] == 1
    assert "score_pain_total" in result.failed_checks


def test_ut04_115_pain_total_counts_noise_events(scored: duckdb.DuckDBPyConnection) -> None:
    """UT04-115 `score_pain_total` recomputes noise-event records too (U04-64 rec_cost): a
    noise event in the window that no candidate is attributed makes the attributed sum fall
    short of the window total by exactly its annualized triage cost (value < 0, passes)."""
    binds = _step_context().binds()
    severity = sorted(binds["d_noise_severities"])[0]  # type: ignore[call-overload]
    scored.execute(
        "INSERT INTO core.event (event_id, ts, service_id, severity, incident_id)"
        " VALUES ('E_NOISE', TIMESTAMPTZ '2026-03-15 12:00:00+00', 'S1', ?, NULL)",
        [severity],
    )
    _check(scored)
    _severity, value, threshold, passed, _details = _row(scored, "score_pain_total")
    usd = float(binds["w_triage_minutes"]) / 60 * float(binds["w_engineer_hour"])  # type: ignore[arg-type]
    row = scored.execute(
        "SELECT greatest(1, least(?, date_diff('day', CAST(min(opened_at) AT TIME ZONE ? AS DATE),"
        " DATE '2026-04-01'))) FROM metrics.incident_fact WHERE NOT excluded",
        [binds["s_window_days"], binds["tz"]],
    ).fetchone()
    assert row is not None
    assert passed is True
    assert value == pytest.approx(-usd * 365 / row[0])
    assert value < 0 < threshold


def test_ut04_115_pain_total_within_tolerance(scored: duckdb.DuckDBPyConnection) -> None:
    """UT04-115 `score_pain_total` compares unrounded DOUBLE values under the DD04-11
    tolerance: a 1e-10 share excess (inside the share tolerance too) still passes."""
    scored.execute(
        f"UPDATE score.funding_attribution SET share = share + 1e-10 WHERE {_FIRST_RECORD}"  # noqa: S608 - module constants
    )
    result = _check(scored)
    _severity, value, threshold, passed, _details = _row(scored, "score_pain_total")
    assert passed is True
    assert 0 < value <= threshold
    assert result.failed_checks == []


def test_ut04_115_optional_tables_only_when_present(scored: duckdb.DuckDBPyConnection) -> None:
    """UT04-115 (U04-59 missing inputs) with only the funding step's score tables present,
    the score.* half of `score_evidence_coverage` and `score_unconfirmed_rows` read just
    those; the org and portfolio checks are skipped as passed."""
    for table in ("score.org", "score.action_lever", "score.portfolio"):
        scored.execute(f"DROP TABLE {table}")
    scored.execute("UPDATE score.funding SET query_ids = ['q_forged00000000b1']")
    _check(scored)
    assert _row(scored, "score_evidence_coverage")[1:4] == (1.0, 0.0, False)
    unconfirmed = _unconfirmed(scored, ("score.funding",))
    assert _row(scored, "score_unconfirmed_rows")[1] == float(unconfirmed)
    for name in ("score_org_z_finite", "score_portfolio_budget", "score_portfolio_blockers"):
        assert _row(scored, name)[3:] == (True, '{"skipped":true}')
    row = scored.execute(
        "SELECT params FROM meta.evidence"
        " WHERE json_extract_string(params, '$.template.name') = 'checks:score_evidence_coverage'"
    ).fetchone()
    assert row is not None
    template = json.loads(row[0])["template"]
    assert (template["has_score_funding"], template["has_score_portfolio"]) == (True, False)


# --- U04-56 / U04-59 metric samples ----------------------------------------------------------


def test_ut04_115_metric_samples_recorded(
    reset_process_state: ProcessState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT04-115 (U04-56, U04-59) a full `run_scoring` records one
    `herness_metrics_step_duration_seconds` sample per step run (label `step`, component
    `metrics`) and counts `herness_metrics_check_failures_total` per failed check (label
    `check`); with no ops store bound the samples stay in the process buffer."""
    for obj, name, value in patches(small_catalog()):
        monkeypatch.setattr(obj, name, value)
    con = tiny_with_facts()
    try:
        ctx = FakeJobContext()
        run_scoring(BUILD_ID, steps=["metrics"], con=con, ctx=ctx)
        con.execute("UPDATE metrics.metric_value SET value = 2 WHERE unit = 'ratio'")
        resumed = FakeJobContext(state=ctx.saved_states[-1])  # metrics is not rerun
        with pytest.raises(SchemaViolation, match="score_metric_ratio_range"):
            run_scoring(BUILD_ID, con=con, ctx=resumed)
        buffer = reset_process_state.metric_buffer
    finally:
        con.close()
    durations = [
        (dict(key[1]), key[2], value)
        for key, value in buffer.histograms
        if key[0] == "herness_metrics_step_duration_seconds"
    ]
    expected_steps = [s for s in STEPS if s != "validate"]
    assert [(labels, component) for labels, component, _ in durations] == [
        ({"step": s}, "metrics") for s in expected_steps
    ]
    assert all(value >= 0 for _, _, value in durations)
    failures = {
        dict(key[1])["check"]: total
        for key, total in buffer.counters.items()
        if key[0] == "herness_metrics_check_failures_total"
    }
    assert failures == {"score_metric_ratio_range": 1.0, "score_unconfirmed_rows": 1.0}
