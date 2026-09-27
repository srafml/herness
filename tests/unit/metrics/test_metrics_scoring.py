"""Tests for herness.metrics.scoring (impl 04 U04-55 … U04-60; T04-13).

`metrics_tiny` with stage 400 facts; the build started 2026-04-01 06:00 UTC, so `as_of` is
2026-04-01. Most tests use a small catalog (five metrics) to stay fast; UT04-110/UT04-118 run
the shipped catalog once per module. The steps funding/org/levers/portfolio are not built yet
(T04-21): a requested one is skipped with a warning and never checkpointed.
"""

import ast
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb
import pytest
from pydantic import JsonValue
from structlog.testing import capture_logs
from tests.support.build_harness import FakeJobContext
from tests.support.metrics_scoring import CFG_HASH, patches, small_catalog, tiny_with_facts
from tests.support.metrics_tiny import BUILD_ID, shipped_catalog

from herness.core.errors import ConfigError, SchemaViolation
from herness.metrics import _scoring_checks, scoring
from herness.metrics.context import ScoringReport, StepContext
from herness.metrics.scoring import (
    STEPS,
    missing_required_columns,
    run_check_step,
    run_metrics_step,
    run_scoring,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
PERIODS = ("week", "month", "quarter", "t12w", "t12m")
UNBUILT = ("funding", "org", "levers", "portfolio")
CHECK_NAMES = [name for name, _, _ in _scoring_checks.CHECKS]


def _apply(mp: pytest.MonkeyPatch, triples: list[tuple[object, str, object]]) -> None:
    for obj, name, value in triples:
        mp.setattr(obj, name, value)


def _dq(con: duckdb.DuckDBPyConnection, name: str) -> list[tuple[Any, ...]]:
    return con.execute(
        "SELECT severity, value, threshold, passed, details FROM meta.dq_result"
        " WHERE check_name = ?",
        [name],
    ).fetchall()


def _tables(con: duckdb.DuckDBPyConnection) -> set[str]:
    return _scoring_checks.existing_tables(con)


def _count(con: duckdb.DuckDBPyConnection, sql: str) -> int:
    row = con.execute(sql).fetchone()
    assert row is not None
    return int(row[0])


def _state(done: list[str], *, build_id: str = BUILD_ID, cfg_hash: str = CFG_HASH) -> JsonValue:
    steps: list[JsonValue] = list(done)
    return {"build_id": build_id, "config_hash": cfg_hash, "steps_done": steps}


@pytest.fixture
def tiny(monkeypatch: pytest.MonkeyPatch) -> Iterator[duckdb.DuckDBPyConnection]:
    """`metrics_tiny` with facts; scoring reads the small catalog."""
    _apply(monkeypatch, patches(small_catalog()))
    con = tiny_with_facts()
    try:
        yield con
    finally:
        con.close()


@pytest.fixture(scope="module")
def full() -> Iterator[tuple[duckdb.DuckDBPyConnection, ScoringReport, FakeJobContext, list[Any]]]:
    """One `run_scoring` over the shipped catalog: (connection, report, context, logs)."""
    with pytest.MonkeyPatch.context() as mp:
        _apply(mp, patches(shipped_catalog()))
        con = tiny_with_facts()
        ctx = FakeJobContext()
        with capture_logs() as logs:
            report = run_scoring(BUILD_ID, con=con, ctx=ctx)
        try:
            yield con, report, ctx, logs
        finally:
            con.close()


# --- UT04-110 steps, full run, report -------------------------------------------------------


def test_ut04_110_steps_fixed_order() -> None:
    """UT04-110 STEPS is the design 04 §3.3 order."""
    assert STEPS == ("validate", "metrics", "funding", "org", "levers", "portfolio", "check")


def test_ut04_110_full_run_steps_in_order_and_counts(full: Any) -> None:
    """UT04-110 the shipped catalog on the tiny build: steps in order, report counts from SQL,
    the unbuilt steps skipped with warnings, the checkpoint saved after each step."""
    con, report, ctx, logs = full
    assert report.build_id == BUILD_ID
    assert report.steps_done == ["validate", "metrics", "check"]
    assert report.row_counts == {
        "metrics.metric_value": _count(con, "SELECT count(*) FROM metrics.metric_value"),
        "meta.dq_result": len(CHECK_NAMES),
    }
    assert report.row_counts["metrics.metric_value"] > 0
    assert set(report.duration_ms) == {"metrics", "check"}
    assert report.flags == []
    assert report.warnings == [f"step {s} is not available yet; skipped" for s in UNBUILT]
    events = [(e["event"], e.get("step")) for e in logs if e["event"].startswith("metrics.scoring")]
    assert events == [
        ("metrics.scoring.step_started", "metrics"),
        ("metrics.scoring.step_completed", "metrics"),
        *[("metrics.scoring.step_unavailable", s) for s in UNBUILT],
        ("metrics.scoring.step_started", "check"),
        ("metrics.scoring.step_completed", "check"),
        ("metrics.scoring.completed", None),
    ]
    assert [s["scoring"] for s in ctx.saved_states] == [
        _state(["metrics"]),
        _state(["metrics", "check"]),
    ]
    assert ctx.heartbeats == ["scoring:metrics", "scoring:check"]
    rows = con.execute(
        "SELECT check_name, passed FROM meta.dq_result WHERE check_name LIKE 'score_%'"
    ).fetchall()
    assert sorted(rows) == sorted((name, True) for name in CHECK_NAMES)


# --- UT04-118 metrics step coverage ----------------------------------------------------------


def test_ut04_118_every_metric_grain_period_recorded(full: Any) -> None:
    """UT04-118 every enabled metric x grain x 5 periods has recorded evidence (producer
    metrics), and every stored row's query_id is one of those evidence rows."""
    con = full[0]
    catalog = shipped_catalog()
    expected = {
        (name, grain, period)
        for name in catalog.names()
        for grain in catalog.get(name).grains
        for period in PERIODS
    }
    recorded: set[tuple[str, str, str]] = set()
    for (params,) in con.execute(
        "SELECT params FROM meta.evidence WHERE producer = 'metrics'"
    ).fetchall():
        template = json.loads(params)["template"]
        recorded.add((template["name"], template["entity_type"], template["period"]))
    assert recorded == expected
    orphans = _count(
        con,
        "SELECT count(*) FROM metrics.metric_value v WHERE NOT EXISTS (SELECT 1 FROM"
        " meta.evidence e WHERE e.query_id = v.query_id AND e.producer = 'metrics')",
    )
    assert orphans == 0
    stored = con.execute("SELECT DISTINCT metric, entity_type, period FROM metrics.metric_value")
    assert {tuple(r) for r in stored.fetchall()} <= expected


def test_ut04_118_rows_hash_matches_evidence(tiny: duckdb.DuckDBPyConnection) -> None:
    """UT04-118 each stored query's rows re-hash to its evidence result_hash; a rerun of the
    step rewrites the table without duplicate rows."""
    from herness.metrics.evidence import result_hash  # noqa: PLC0415 - local to this test

    run_scoring(BUILD_ID, steps=["metrics"], con=tiny)
    first = sorted(tiny.execute("SELECT * FROM metrics.metric_value").fetchall(), key=repr)
    evidence = dict(
        tiny.execute(
            "SELECT query_id, result_hash FROM meta.evidence WHERE producer = 'metrics'"
        ).fetchall()
    )
    for qid, digest in evidence.items():
        res = tiny.execute(
            "SELECT * EXCLUDE (query_id) FROM metrics.metric_value WHERE query_id = ?", [qid]
        )
        columns = [(str(d[0]), str(d[1])) for d in res.description or ()]
        assert result_hash(columns, res.fetchall()) == digest
    run_scoring(BUILD_ID, steps=["metrics"], con=tiny)
    again = sorted(tiny.execute("SELECT * FROM metrics.metric_value").fetchall(), key=repr)
    assert again == first
    assert _count(tiny, "SELECT count(*) FROM meta.evidence WHERE producer = 'metrics'") == len(
        evidence
    )


def test_ut04_118_query_error_becomes_schema_violation(
    tiny: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT04-118 a QueryError of one metric query names metric, grain and period."""
    from herness.core.errors import QueryError  # noqa: PLC0415 - local to this test

    def boom(*_a: object, **_k: object) -> None:
        msg = "Binder Error: boom"
        raise QueryError(msg, query_id="q_0000000000000000")

    monkeypatch.setattr(scoring, "run_recorded", boom)
    sc = _step_context(tiny)
    with pytest.raises(SchemaViolation, match=r"^metric \w+ grain \w+ period week failed: Binder"):
        run_metrics_step(tiny, sc)


def _step_context(con: duckdb.DuckDBPyConnection) -> StepContext:
    import datetime  # noqa: PLC0415 - local helper

    from tests.support.metrics_tiny import tiny_weights  # noqa: PLC0415 - local helper

    weights = tiny_weights()
    return StepContext(
        BUILD_ID,
        small_catalog(),
        weights,
        datetime.date(2026, 4, 1),
        weights.business_timezone,
        frozenset(),
    )


# --- UT04-111 requested steps -----------------------------------------------------------------


def test_ut04_111_only_requested_steps(tiny: duckdb.DuckDBPyConnection) -> None:
    """UT04-111 steps=["org", "levers"]: only those (plus validate); both unbuilt, so skipped
    with warnings; no metric table written and nothing checkpointed."""
    ctx = FakeJobContext()
    with capture_logs() as logs:
        report = run_scoring(BUILD_ID, steps=["levers", "org"], con=tiny, ctx=ctx)
    assert report.steps_done == ["validate"]
    assert report.warnings == [
        "step org is not available yet; skipped",
        "step levers is not available yet; skipped",
    ]
    assert "metrics.metric_value" not in _tables(tiny)
    assert ctx.saved_states == []
    unavailable = [e["step"] for e in logs if e["event"] == "metrics.scoring.step_unavailable"]
    assert unavailable == ["org", "levers"]


def test_ut04_111_unknown_step(tiny: duckdb.DuckDBPyConnection) -> None:
    """UT04-111 an unknown step name is a ConfigError before anything runs."""
    with pytest.raises(ConfigError, match=r"^unknown scoring step bogus$"):
        run_scoring(BUILD_ID, steps=["bogus"], con=tiny)
    assert _dq(tiny, "score_metric_disabled") == []


def test_ut04_111_check_only_skips_missing_inputs(tiny: duckdb.DuckDBPyConnection) -> None:
    """UT04-111 steps=["check"] before metrics ran: checks over metric_value are recorded as
    passed with details.skipped (information_schema pre-check), the fact check runs."""
    report = run_scoring(BUILD_ID, steps=["check"], con=tiny)
    assert report.steps_done == ["validate", "check"]
    skipped = {
        name for name in CHECK_NAMES if json.loads(_dq(tiny, name)[0][4]).get("skipped") is True
    }
    assert skipped == {
        "score_metric_ratio_range",
        "score_metric_pct_range",
        "score_metric_negative",
        "score_metric_count_integral",
        "score_evidence_coverage",
    }
    assert _dq(tiny, "score_metric_pct_range") == [("error", None, 0.0, True, '{"skipped":true}')]
    severity, value, threshold, passed, details = _dq(tiny, "score_fact_duration_negative")[0]
    assert (severity, value, threshold, passed) == ("error", 0.0, 0.0, True)
    assert set(json.loads(details)) == {"query_id", "n_bad"}


# --- UT04-112 checkpoint ----------------------------------------------------------------------


def test_ut04_112_done_steps_skipped(tiny: duckdb.DuckDBPyConnection) -> None:
    """UT04-112 a matching checkpoint skips `metrics`; other state keys are kept."""
    ctx = FakeJobContext(state={"stages_done": ["ingest"], "scoring": _state(["metrics"])})
    with capture_logs() as logs:
        report = run_scoring(BUILD_ID, steps=["metrics", "check"], con=tiny, ctx=ctx)
    assert report.steps_done == ["validate", "metrics", "check"]
    assert set(report.duration_ms) == {"check"}
    assert "metrics.metric_value" not in _tables(tiny)
    assert {"event": "metrics.scoring.step_skipped", "step": "metrics"}.items() <= next(
        e for e in logs if e["event"] == "metrics.scoring.step_skipped"
    ).items()
    assert ctx.saved_states[-1] == {
        "stages_done": ["ingest"],
        "scoring": _state(["metrics", "check"]),
    }


@pytest.mark.parametrize(
    "saved",
    [
        _state(["metrics"], cfg_hash="cfg_00000000000000bb"),
        _state(["metrics"], build_id="20260101-000000-OTHER1"),
        "not a mapping",
        {"build_id": BUILD_ID, "config_hash": CFG_HASH, "steps_done": "metrics"},
    ],
)
def test_ut04_112_stale_checkpoint_reruns(tiny: duckdb.DuckDBPyConnection, saved: Any) -> None:
    """UT04-112 a checkpoint of another build or config (or a malformed one) is ignored."""
    ctx = FakeJobContext(state={"scoring": saved})
    report = run_scoring(BUILD_ID, steps=["metrics"], con=tiny, ctx=ctx)
    assert set(report.duration_ms) == {"metrics"}
    assert "metrics.metric_value" in _tables(tiny)


def test_ut04_112_yield_after_step(tiny: duckdb.DuckDBPyConnection) -> None:
    """UT04-112 `should_yield()` after a step returns the report flagged `yielded`; the next
    call resumes from the checkpoint."""
    ctx = FakeJobContext(yield_after=0)
    report = run_scoring(BUILD_ID, con=tiny, ctx=ctx)
    assert report.flags == ["yielded"]
    assert report.steps_done == ["validate", "metrics"]
    assert _dq(tiny, "score_metric_ratio_range") == []
    resumed = FakeJobContext(state=ctx.saved_states[-1])
    report = run_scoring(BUILD_ID, con=tiny, ctx=resumed)
    assert report.flags == []
    assert report.steps_done == ["validate", "metrics", "check"]
    assert set(report.duration_ms) == {"check"}


# --- UT04-113 / UT04-114 validate step --------------------------------------------------------


def test_ut04_113_no_fact_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT04-113 stage 400 did not run: SchemaViolation, nothing written."""
    from tests.support.metrics_tiny import build_metrics_tiny  # noqa: PLC0415 - local

    _apply(monkeypatch, patches(small_catalog()))
    con = build_metrics_tiny()
    try:
        with pytest.raises(SchemaViolation, match=r"^fact tables missing; stage 400 did not run$"):
            run_scoring(BUILD_ID, con=con)
        assert _count(con, "SELECT count(*) FROM meta.dq_result") == 0
    finally:
        con.close()


def test_ut04_113_catalog_errors(
    tiny: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT04-113 a catalog with `validate_catalog` errors is a ConfigError."""
    cat = shipped_catalog()
    metrics = [
        m.model_copy(update={"enabled": m.name != "reopen_rate"}) for m in cat.config.metrics
    ]
    from herness.metrics.catalog import MetricCatalog  # noqa: PLC0415 - local

    broken = MetricCatalog(cat.config.model_copy(update={"metrics": metrics}))
    monkeypatch.setattr(scoring, "catalog_from_config", lambda _cfg=None: broken)
    with pytest.raises(ConfigError, match=r"^metric catalog invalid: scoring\.org\.metrics\."):
        run_scoring(BUILD_ID, con=tiny)


def test_ut04_114_acknowledged_at_all_null(tiny: duckdb.DuckDBPyConnection) -> None:
    """UT04-114 `acknowledged_at` all NULL: `mtta_minutes` is skipped with one dq warn row;
    a rerun replaces the row (no duplicate)."""
    tiny.execute("UPDATE core.incident SET acknowledged_at = NULL")
    assert missing_required_columns(tiny, small_catalog()) == {
        "mtta_minutes": ["core.incident.acknowledged_at"]
    }
    with capture_logs() as logs:
        report = run_scoring(BUILD_ID, steps=["metrics"], con=tiny)
    run_scoring(BUILD_ID, steps=["metrics"], con=tiny)
    row = _dq(tiny, "score_metric_disabled")
    assert row == [
        (
            "warn",
            1.0,
            0.0,
            False,
            '{"metric":"mtta_minutes","columns":["core.incident.acknowledged_at"]}',
        )
    ]
    metrics = {
        r[0] for r in tiny.execute("SELECT DISTINCT metric FROM metrics.metric_value").fetchall()
    }
    assert "mtta_minutes" not in metrics
    assert "incident_count" in metrics
    assert report.row_counts["metrics.metric_value"] > 0
    disabled = [e for e in logs if e["event"] == "metrics.scoring.metric_disabled"]
    assert [(e["metric"], e["missing_count"], e["log_level"]) for e in disabled] == [
        ("mtta_minutes", 1, "warning")
    ]


def test_ut04_114_absent_column(tiny: duckdb.DuckDBPyConnection) -> None:
    """UT04-114 a required column absent from information_schema disables the metric; with
    every column filled nothing is disabled."""
    assert missing_required_columns(tiny, small_catalog()) == {}
    tiny.execute("ALTER TABLE core.incident DROP COLUMN acknowledged_at")
    assert missing_required_columns(tiny, small_catalog()) == {
        "mtta_minutes": ["core.incident.acknowledged_at"]
    }


# --- UT04-115 check step ----------------------------------------------------------------------


def test_ut04_115_corrupt_ratio_fails_check(tiny: duckdb.DuckDBPyConnection) -> None:
    """UT04-115 a ratio outside [0, 1]: its dq row fails, `run_scoring` raises SchemaViolation
    after committing the rows, and `check` is not checkpointed."""
    run_scoring(BUILD_ID, steps=["metrics"], con=tiny)
    tiny.execute(
        "UPDATE metrics.metric_value SET value = 1.5 WHERE unit = 'ratio'"
        " AND entity_type = 'team' AND period = 't12m'"
    )
    bad = _count(
        tiny,
        "SELECT count(*) FROM metrics.metric_value WHERE unit = 'ratio' AND value > 1",
    )
    assert bad > 0
    ctx = FakeJobContext(state={"scoring": _state(["metrics"])})
    with (
        capture_logs() as logs,
        pytest.raises(
            SchemaViolation, match=r"^scoring invariants failed: score_metric_ratio_range$"
        ),
    ):
        run_scoring(BUILD_ID, con=tiny, ctx=ctx)
    severity, value, threshold, passed, details = _dq(tiny, "score_metric_ratio_range")[0]
    assert (severity, value, threshold, passed) == ("error", float(bad), 0.0, False)
    assert json.loads(details)["n_bad"] == bad
    assert ctx.saved_states == []
    failed = [e for e in logs if e["event"] == "metrics.scoring.check_failed"]
    assert [(e["check_name"], e["value"], e["threshold"]) for e in failed] == [
        ("score_metric_ratio_range", float(bad), 0)
    ]


def test_ut04_115_each_check_detects_its_violation(tiny: duckdb.DuckDBPyConnection) -> None:
    """UT04-115 one planted violation per base check; `score_work_item_cycle` is a warn (a
    report warning, not a failed check); every check query is recorded (producer score)."""
    run_scoring(BUILD_ID, steps=["metrics"], con=tiny)
    tiny.execute("UPDATE metrics.incident_fact SET resolve_h = -1 WHERE resolve_h IS NOT NULL")
    tiny.execute(
        "INSERT INTO metrics.metric_value VALUES"
        " ('x_pct', 'service', 'S1', 'week', DATE '2026-01-05', 101, 1, 1, 1, 'pct', [], 'q_x1'),"
        " ('x_neg', 'service', 'S1', 'week', DATE '2026-01-05', 1, -1, 1, 1, 'count', [], 'q_x2'),"
        " ('incident_count', 'service', 'S9', 'week', DATE '2026-01-05', 1.5, 1, 1, 1, 'count',"
        " [], 'q_x3')"
    )
    tiny.execute("UPDATE metrics.work_item_closure SET depth = 10")
    tiny.execute("BEGIN TRANSACTION")
    result = run_check_step(tiny, _step_context(tiny))
    tiny.execute("COMMIT")
    assert result.failed_checks == [
        "score_fact_duration_negative",
        "score_metric_pct_range",
        "score_metric_negative",
        "score_metric_count_integral",
        "score_evidence_coverage",
    ]
    assert result.warnings == ["check score_work_item_cycle failed"]
    assert result.row_counts == {"meta.dq_result": len(CHECK_NAMES)}
    coverage = _dq(tiny, "score_evidence_coverage")[0]
    assert coverage[1] == 3.0
    checks = {
        json.loads(p)["template"]["name"]
        for (p,) in tiny.execute(
            "SELECT params FROM meta.evidence WHERE producer = 'score'"
        ).fetchall()
    }
    assert checks == {f"checks:{name}" for name in CHECK_NAMES}


def test_ut04_115_evidence_coverage_counts_distinct_ids(tiny: duckdb.DuckDBPyConnection) -> None:
    """UT04-115 `score_evidence_coverage` (metrics.* part): distinct query_ids of the fact and
    metric tables absent from meta.evidence; covered ids do not count (the passing case is the
    full run of UT04-110). A check's query_id is fixed per build, so its data is changed before
    the first check run: a different result under the same query_id is a hash conflict."""
    run_scoring(BUILD_ID, steps=["metrics"], con=tiny)
    tiny.execute("UPDATE metrics.org_closure SET query_id = 'q_missing00000001'")
    tiny.execute("UPDATE metrics.metric_value SET query_id = 'q_missing00000002'")
    with pytest.raises(SchemaViolation, match="score_evidence_coverage"):
        run_scoring(BUILD_ID, steps=["check"], con=tiny)
    assert _dq(tiny, "score_evidence_coverage")[0][1:4] == (2.0, 0.0, False)


# --- UT04-116 build status ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("setup", "status"),
    [
        ("UPDATE meta.build SET status = 'promoted'", "promoted"),
        ("DELETE FROM meta.build", "missing"),
    ],
)
def test_ut04_116_build_not_building(
    tiny: duckdb.DuckDBPyConnection, setup: str, status: str
) -> None:
    """UT04-116 a promoted (or absent) build is a ConfigError."""
    tiny.execute(setup)
    msg = rf"^build {BUILD_ID} is {status}; scoring runs only before promotion$"
    with pytest.raises(ConfigError, match=msg):
        run_scoring(BUILD_ID, con=tiny)


def test_ut04_116_build_without_start_time(tiny: duckdb.DuckDBPyConnection) -> None:
    """UT04-116 a building row without `started_at` is a SchemaViolation."""
    tiny.execute("UPDATE meta.build SET started_at = NULL")
    with pytest.raises(SchemaViolation, match="has no start time"):
        run_scoring(BUILD_ID, con=tiny)


# --- UT04-121 connection -------------------------------------------------------------------------


def test_ut04_121_read_only_connection(tmp_path: Path) -> None:
    """UT04-121 a read-only connection is a ConfigError."""
    path = tmp_path / "ro.duckdb"
    duckdb.connect(str(path)).close()
    con = duckdb.connect(str(path), read_only=True)
    try:
        msg = r"^run_scoring needs the build pipeline's writable connection$"
        with pytest.raises(ConfigError, match=msg):
            run_scoring(BUILD_ID, con=con)
    finally:
        con.close()


def test_ut04_121_con_is_required() -> None:
    """UT04-121 a call without `con` is a TypeError (required keyword)."""
    with pytest.raises(TypeError, match="con"):
        run_scoring(BUILD_ID)  # type: ignore[call-arg]


def test_ut04_121_no_rw_warehouse_import() -> None:
    """UT04-121 no module of herness/metrics imports herness.store._warehouse_rw (C12)."""
    offenders = []
    for path in sorted((ROOT / "herness" / "metrics").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                names = [base, *(f"{base}.{a.name}" for a in node.names)]
            if any(n.startswith("herness.store._warehouse_rw") for n in names):
                offenders.append(path.name)
    assert offenders == []


def test_ut04_121_step_rolls_back_unexpected_errors(
    tiny: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT04-121 a DuckDB error outside `run_recorded` becomes SchemaViolation("<step> failed:
    ..."); other errors propagate unchanged; both roll the step back."""

    def ddl_error(con: duckdb.DuckDBPyConnection, _sc: StepContext) -> None:
        con.execute("CREATE TABLE metrics.metric_value (x INTEGER)")
        con.execute("SELECT * FROM no_such_table")

    def config_error(_con: duckdb.DuckDBPyConnection, _sc: StepContext) -> None:
        msg = "bad template"
        raise ConfigError(msg)

    monkeypatch.setitem(scoring._STEP_FUNCS, "metrics", ddl_error)
    with capture_logs() as logs, pytest.raises(SchemaViolation, match=r"^metrics failed: Catalog"):
        run_scoring(BUILD_ID, steps=["metrics"], con=tiny)
    assert "metrics.metric_value" not in _tables(tiny)
    assert [e["step"] for e in logs if e["event"] == "metrics.scoring.step_failed"] == ["metrics"]
    monkeypatch.setitem(scoring._STEP_FUNCS, "metrics", config_error)
    with pytest.raises(ConfigError, match=r"^bad template$"):
        run_scoring(BUILD_ID, steps=["metrics"], con=tiny)


def test_ut04_121_validate_write_error_is_schema_violation(
    tiny: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT04-121 a failing dq write in the validate step is a SchemaViolation (U04-56 error
    contract) and its transaction is rolled back."""
    tiny.execute("UPDATE core.incident SET acknowledged_at = NULL")
    monkeypatch.setattr(scoring, "_DISABLED_ROW_SQL", "INSERT INTO meta.nope VALUES (?, ?, ?)")
    with capture_logs() as logs, pytest.raises(SchemaViolation, match=r"^validate failed: Catalog"):
        run_scoring(BUILD_ID, steps=["metrics"], con=tiny)
    assert [e["step"] for e in logs if e["event"] == "metrics.scoring.step_failed"] == ["validate"]
    tiny.execute("BEGIN TRANSACTION")  # no transaction left open by the failed step
    tiny.execute("ROLLBACK")


def test_ut04_121_validate_read_error_is_schema_violation(
    tiny: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT04-121 a DuckDB error while reading required columns is a SchemaViolation too."""
    monkeypatch.setattr(scoring, "_COLUMN_SQL", "SELECT count(*) FROM no_such_table")
    with pytest.raises(SchemaViolation, match=r"^validate failed: "):
        run_scoring(BUILD_ID, steps=["metrics"], con=tiny)


def test_ut04_121_rollback_failure_keeps_original_error(
    tiny: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT04-121 when ROLLBACK itself fails, the step's own error is raised, with the rollback
    failure logged and added as a note; the validate path uses the same helper."""

    def ends_txn_then_fails(con: duckdb.DuckDBPyConnection, _sc: StepContext) -> None:
        con.execute("ROLLBACK")  # the runner's ROLLBACK now has no transaction
        msg = "bad template"
        raise ConfigError(msg)

    monkeypatch.setitem(scoring._STEP_FUNCS, "metrics", ends_txn_then_fails)
    with capture_logs() as logs, pytest.raises(ConfigError, match=r"^bad template") as caught:
        run_scoring(BUILD_ID, steps=["metrics"], con=tiny)
    assert str(caught.value) == "bad template"
    assert caught.value.__notes__ == ["rollback after metrics failed: TransactionException"]
    failed = [e for e in logs if e["event"] == "metrics.scoring.rollback_failed"]
    assert [(e["step"], e["error_class"]) for e in failed] == [("metrics", "TransactionException")]
    err = RuntimeError("validate write")
    scoring._rollback(tiny, "validate", None, err)
    assert err.__notes__ == ["rollback after validate failed: TransactionException"]


def test_ut04_111_bare_string_steps_rejected(tiny: duckdb.DuckDBPyConnection) -> None:
    """UT04-111 `steps="metrics"` (a str, not a list of names) is a ConfigError, not a run of
    the steps named by its characters."""
    with pytest.raises(ConfigError, match=r"^unknown scoring step metrics$"):
        run_scoring(BUILD_ID, steps="metrics", con=tiny)
    assert "metrics.metric_value" not in _tables(tiny)


def test_ut04_115_same_build_retry_after_config_change(
    tiny: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT04-115 a check fails, the config is fixed and the job retried on the same build: the
    checkpoint resets, metrics rewrites, and the check records a new query_id (config_hash is
    part of its template) instead of a "nondeterministic result" conflict. Uses the real
    `load_config` and `config_hash`; only the catalog stays the small test catalog (the config
    hash covers the whole config, the catalog only decides which metrics run)."""
    from herness.core.config import config_hash, load_config  # noqa: PLC0415 - local

    first = load_config("local", config_dir=ROOT / "config", env={})
    fixed = load_config(
        "local",
        overrides=["weights.cost_per_engineer_hour.value=150"],
        config_dir=ROOT / "config",
        env={},
    )
    assert config_hash(first) != config_hash(fixed)
    for module in (scoring, _scoring_checks):
        monkeypatch.setattr(module, "config_hash", config_hash)
        monkeypatch.setattr(module, "get_config", lambda: first)
    ctx = FakeJobContext()
    run_scoring(BUILD_ID, steps=["metrics"], con=tiny, ctx=ctx)
    tiny.execute("UPDATE metrics.metric_value SET value = 2 WHERE unit = 'ratio'")
    with pytest.raises(SchemaViolation, match="score_metric_ratio_range"):
        run_scoring(BUILD_ID, con=tiny, ctx=FakeJobContext(state=ctx.saved_states[-1]))
    failed_qid = json.loads(_dq(tiny, "score_metric_ratio_range")[0][4])["query_id"]
    for module in (scoring, _scoring_checks):
        monkeypatch.setattr(module, "get_config", lambda: fixed)
    retry = FakeJobContext(state=ctx.saved_states[-1])
    report = run_scoring(BUILD_ID, con=tiny, ctx=retry)
    assert set(report.duration_ms) == {"metrics", "check"}  # stale checkpoint: metrics reran
    severity, value, _, passed, details = _dq(tiny, "score_metric_ratio_range")[0]
    assert (severity, value, passed) == ("error", 0.0, True)
    new_qid = json.loads(details)["query_id"]
    assert new_qid != failed_qid
    hashes = dict(
        tiny.execute(
            "SELECT query_id, json_extract_string(params, '$.template.config_hash')"
            " FROM meta.evidence WHERE query_id IN (?, ?)",
            [failed_qid, new_qid],
        ).fetchall()
    )
    assert hashes == {failed_qid: config_hash(first), new_qid: config_hash(fixed)}
    assert retry.saved_states[-1]["scoring"] == {
        "build_id": BUILD_ID,
        "config_hash": config_hash(fixed),
        "steps_done": ["metrics", "check"],
    }
