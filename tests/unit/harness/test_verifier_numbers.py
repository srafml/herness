"""Tests for herness.harness.verifier.Verifier (U05-63, U05-64, U05-67; T05-25).

Runs on the test-local stand-in build of `_verifier_standin` (spec 11 `tiny_build` does not
exist yet) with `FakeOps` and `RecordingTracer`.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest
from structlog.testing import capture_logs
from tests.support.harness_fakes import FakeOps, RecordingTracer
from tests.unit.harness import _verifier_standin as sd

from herness.core.errors import ConfigError
from herness.core.ids import query_id as compute_query_id
from herness.core.types import (
    ChatAnswer,
    Coverage,
    Evidence,
    Finding,
    NumberRef,
    Paragraph,
    RecommendationItem,
    ReportDraft,
    Section,
    VerificationResult,
)
from herness.harness import _verifier_items as vitems
from herness.harness import _verifier_rerun as rr
from herness.harness import verifier as vmod
from herness.harness.llm.settings import SqlSettings, VerifierSettings
from herness.harness.verifier import VERIFIER_CACHE_ROWS, Verifier
from herness.harness.warehouse import WarehousePool

pytestmark = pytest.mark.unit

FND_A = "fnd_" + "A" * 26
FND_B = "fnd_" + "B" * 26
RUN_ID = "run_" + "C" * 26
TASK_ID = "task_" + "D" * 26
PAY_WEEK = {"team": "payments", "week": "2026-09-21"}


@dataclass
class Env:
    tmp: Path
    pool: WarehousePool
    ops: FakeOps
    tracer: RecordingTracer
    verifier: Verifier

    def add(self, sql: str, params: Mapping[str, Any] | None = None) -> Evidence:
        ev = sd.record(self.pool, sql, params)
        self.ops.record_evidence(ev)
        return ev


def _verifier(
    pool: WarehousePool, ops: FakeOps, tracer: RecordingTracer | None, **sql: Any
) -> Verifier:
    return Verifier(
        ops,
        pool,
        VerifierSettings(),
        tracer,
        allowed_numeral_patterns=sd.PATTERNS,
        sql=SqlSettings(**sql),
    )


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    sd.make_build(tmp_path)
    pool = WarehousePool(tmp_path, SqlSettings())
    ops = FakeOps({FND_A: "verified", FND_B: "challenged"})
    tracer = RecordingTracer()
    try:
        yield Env(tmp_path, pool, ops, tracer, _verifier(pool, ops, tracer))
    finally:
        pool.close_all()


def _results(result: VerificationResult) -> list[str]:
    return [check.result for check in result.items[0].checks]


def _stable(result: VerificationResult) -> dict[str, Any]:
    return result.model_dump(exclude={"verified_at", "duration_ms"})


# --- UT05-117: each check result as planted -------------------------------------------------


def test_ut05_117_each_check_result_as_planted(env: Env) -> None:
    """UT05-117 match, mismatch, missing_query, wrong_build, query_failed, missing_column,
    row_not_found and row_ambiguous are each reported as planted."""
    team = env.add(sd.TEAM_SQL).query_id
    one = env.add(sd.ONE_ROW_SQL).query_id
    other_sql = "SELECT 1 AS x"
    other = Evidence(
        query_id=compute_query_id(other_sql, {}, sd.OTHER_BUILD_ID),
        run_id=None,
        build_id=sd.OTHER_BUILD_ID,
        sql=other_sql,
        params={},
        result_hash="0" * 64,
        row_count=1,
        result_sample=[{"x": 1}],
        executed_at=sd.EXECUTED_AT,
        duration_ms=1,
    )
    env.ops.record_evidence(other)
    bad_sql = "SELECT nope FROM metrics.no_such_table"
    bad = Evidence(
        query_id=compute_query_id(bad_sql, {}, sd.BUILD_ID),
        run_id=None,
        build_id=sd.BUILD_ID,
        sql=bad_sql,
        params={},
        result_hash="0" * 64,
        row_count=1,
        result_sample=[],
        executed_at=sd.EXECUTED_AT,
        duration_ms=1,
    )
    env.ops.record_evidence(bad)
    numbers = [
        sd.ref("n1", 12, team, "incidents", PAY_WEEK),
        sd.ref("n2", 13, team, "incidents", PAY_WEEK),
        sd.ref("n3", 1, "q_0123456789abcdef", "incidents", PAY_WEEK),
        sd.ref("n4", 1, other.query_id, "x", None),
        sd.ref("n5", 1, bad.query_id, "nope", None),
        sd.ref("n6", 12, team, "no_column", PAY_WEEK),
        sd.ref("n7", 12, team, "incidents", {"team": "nobody"}),
        sd.ref("n8", 12, team, "incidents", {"team": "payments"}),
        sd.ref("n9", 5, one, "n", None),
        sd.ref("n10", 5, team, "incidents", None),
    ]
    text = " ".join(f"[[n{i}]]" for i in range(1, 11))
    result = env.verifier.verify_numbers(sd.item(text, numbers), sd.BUILD_ID)
    assert _results(result) == [
        "match",
        "mismatch",
        "missing_query",
        "wrong_build",
        "query_failed",
        "missing_column",
        "row_not_found",
        "row_ambiguous",
        "match",
        "row_ambiguous",
    ]
    checks = result.items[0].checks
    assert checks[0].actual == 12
    assert checks[1].actual == 12
    assert checks[2].actual is None
    assert not result.passed
    assert (result.n_numbers, result.n_failed) == (10, 8)
    assert result.build_id == sd.BUILD_ID
    assert result.items[0].claim_support is None


def test_ut05_117_usd_and_float_values_json_safe(env: Env) -> None:
    """UT05-117 a USD claim on a DECIMAL column matches and `actual` is JSON-safe text."""
    team = env.add(sd.TEAM_SQL).query_id
    numbers = [
        sd.ref("n1", "1234.50", team, "cost_usd", PAY_WEEK, unit="usd"),
        sd.ref("n2", 7.4, team, "mttr_hours", PAY_WEEK, unit="hours"),
        sd.ref("n3", "1234.51", team, "cost_usd", PAY_WEEK, unit="usd"),
        sd.ref("n4", 1, team, "team", PAY_WEEK),
        sd.ref("n5", 1, team, "week", PAY_WEEK),
    ]
    result = env.verifier.verify_numbers(
        sd.item("[[n1]] [[n2]] [[n3]] [[n4]] [[n5]]", numbers), sd.BUILD_ID
    )
    checks = result.items[0].checks
    assert [c.result for c in checks] == ["match", "match", "mismatch", "mismatch", "mismatch"]
    assert checks[0].actual == "1234.50"
    assert checks[1].actual == pytest.approx(7.416666666666667)
    assert checks[3].actual == "payments"
    assert checks[4].actual == "2026-09-21"


def test_ut05_117_item_level_lists(env: Env) -> None:
    """UT05-117 uncited, unknown markers, bad refs and unverified findings as planted."""
    team = env.add(sd.TEAM_SQL).query_id
    numbers = [
        sd.ref("n1", 12, team, "incidents", PAY_WEEK),
        sd.ref("n1", 12, team, "incidents", PAY_WEEK),
        sd.ref("n2", "1234.50", team, "cost_usd", PAY_WEEK, unit="usd"),
        sd.ref("n3", 12, team, "incidents", PAY_WEEK),
    ]
    text = "[[n1]] rose by 42 in Q3 2026 (INC0012345, 2026-09-24) [[n9]] [[bad]] [[n2]]"
    planted = sd.item(
        text,
        numbers,
        finding_ids=[FND_A, FND_B, "fnd_" + "E" * 26],
        refs={"expected_usd_ref": "n1", "confidence_ref": "n7", "effort_usd_ref": "n2"},
    )
    with capture_logs() as logs:
        result = env.verifier.verify_numbers(planted, sd.BUILD_ID)
    got = result.items[0]
    assert [(u.text, u.start, u.end) for u in got.uncited] == [("42", 15, 17)]
    assert got.unknown_markers == ["n9", "bad"]
    assert got.bad_refs == ["duplicate:n1", "expected_usd_ref:not_usd", "confidence_ref:n7"]
    assert got.unverified_findings == [FND_B, "fnd_" + "E" * 26]
    assert all(c.result == "match" for c in got.checks)
    assert not got.passed
    unused = [e for e in logs if e["event"] == "harness.verifier.number_unused"]
    assert unused == [
        {
            "event": "harness.verifier.number_unused",
            "log_level": "warning",
            "where": "item",
            "ids": ["n3"],
            "component": "harness.verifier",
        },
    ]
    failed = [e for e in logs if e["event"] == "harness.verifier.item_failed"]
    assert failed[0]["n_uncited"] == 1
    assert failed[0]["build_id"] == sd.BUILD_ID


def test_ut05_117_clean_item_passes_and_traces(env: Env) -> None:
    """UT05-117 a clean item passes; one `verifier_verdict` event with the listed fields."""
    team = env.add(sd.TEAM_SQL).query_id
    numbers = [sd.ref("n1", 12, team, "incidents", PAY_WEEK)]
    ok = sd.item("Payments had [[n1]] incidents in Q3 2026.", numbers, finding_ids=[FND_A])
    result = env.verifier.verify_numbers(ok, sd.BUILD_ID)
    assert result.passed
    assert result.verified_at.tzinfo is UTC
    [(kind, _span, fields)] = env.tracer.events
    assert kind == "verifier_verdict"
    assert set(fields) == {
        "where",
        "passed",
        "n_numbers",
        "n_failed",
        "n_uncited",
        "duration_ms",
        "n_hash_equal",
        "n_hash_equivalent",
        "n_hash_values_only",
    }
    assert fields["passed"] is True
    assert (fields["n_numbers"], fields["n_hash_equal"]) == (1, 1)


def test_ut05_117_meta_evidence_fallback(env: Env) -> None:
    """UT05-117 a query_id missing in ops is read from the build's `meta.evidence`."""
    qid = sd.meta_query_id()
    numbers = [sd.ref("n1", 7, qid, "incidents", {"team": "payments"})]
    result = env.verifier.verify_numbers(sd.item("[[n1]]", numbers), sd.BUILD_ID)
    assert _results(result) == ["match"]


def test_ut05_117_meta_evidence_tampered(tmp_path: Path) -> None:
    """UT05-117 a `meta.evidence` row whose sql no longer recomputes its query_id is missing."""
    path = sd.make_build(tmp_path)
    con = duckdb.connect(str(path))
    con.execute(
        "UPDATE meta.evidence SET sql = 'SELECT team, 99 AS incidents FROM metrics.team_week'"
    )
    con.close()
    pool = WarehousePool(tmp_path, SqlSettings())
    try:
        verifier = _verifier(pool, FakeOps(), None)
        numbers = [sd.ref("n1", 7, sd.meta_query_id(), "incidents", {"team": "payments"})]
        result = verifier.verify_numbers(sd.item("[[n1]]", numbers), sd.BUILD_ID)
    finally:
        pool.close_all()
    assert _results(result) == ["missing_query"]


def test_ut05_117_invalid_build_id_raises(env: Env) -> None:
    """UT05-117 a build_id that does not match BUILD_ID_RE raises ConfigError."""
    with pytest.raises(ConfigError):
        env.verifier.verify_numbers(sd.item("no numbers", []), "../etc")


def test_ut05_117_bad_override_pattern_raises(env: Env) -> None:
    """UT05-117 an override pattern that does not compile raises ConfigError."""
    with pytest.raises(ConfigError):
        Verifier(
            env.ops, env.pool, VerifierSettings(), allowed_numeral_patterns=["("], sql=SqlSettings()
        )


def test_ut05_117_defaults_from_config(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT05-117 `None` overrides read `harness.sql` and the reports patterns from config."""

    class _Reports:
        allowed_numeral_patterns = (r"\b42\b",)

    class _App:
        reports = _Reports()

    class _Harness:
        sql = SqlSettings(scan_rows=1_000)

    class _Models:
        harness = _Harness()

    class _Cfg:
        app = _App()
        models = _Models()

    monkeypatch.setattr(vmod, "get_config", _Cfg)
    verifier = Verifier(env.ops, env.pool, VerifierSettings())
    result = verifier.verify_numbers(sd.item("42 and 43", []), sd.BUILD_ID)
    assert [u.text for u in result.items[0].uncited] == ["43"]

    class _BareApp:
        pass

    _Cfg.app = _BareApp()  # type: ignore[assignment]
    fallback = Verifier(env.ops, env.pool, VerifierSettings())
    result = fallback.verify_numbers(sd.item("INC0012345 in Q3 2026 and 7", []), sd.BUILD_ID)
    assert [u.text for u in result.items[0].uncited] == ["7"]


# --- UT05-118: re-run cache -----------------------------------------------------------------


class _SpyCursor:
    def __init__(self, real: duckdb.DuckDBPyConnection, calls: list[str]) -> None:
        self._real = real
        self._calls = calls

    def execute(self, sql: str, params: object = None) -> _SpyCursor:
        self._calls.append(sql)
        self._real.execute(sql, params)
        return self

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def _spy(env: Env, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    handle = env.pool.get(sd.BUILD_ID)
    real = handle.cursor
    monkeypatch.setattr(handle, "cursor", lambda: _SpyCursor(real(), calls))
    return calls


def test_ut05_118_query_cited_30_times_runs_once(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT05-118 an item citing one query 30 times executes it once (spy cursor)."""
    team = env.add(sd.TEAM_SQL).query_id
    calls = _spy(env, monkeypatch)
    keys = [
        dict(zip(("team", "week"), (r[0], r[1].isoformat()), strict=True)) for r in sd.TEAM_ROWS
    ]
    numbers = [
        sd.ref(f"n{i}", sd.TEAM_ROWS[i % 5][2], team, "incidents", keys[i % 5]) for i in range(30)
    ]
    text = " ".join(f"[[n{i}]]" for i in range(30))
    result = env.verifier.verify_numbers(sd.item(text, numbers), sd.BUILD_ID)
    assert result.passed
    assert len(calls) == 1
    again = env.verifier.verify_numbers(
        sd.item("[[n1]]", [sd.ref("n1", 5, team, "incidents", {"team": "search"})]), sd.BUILD_ID
    )
    assert again.passed
    assert len(calls) == 1
    assert VERIFIER_CACHE_ROWS == 10_000


def test_ut05_118_large_result_recomputes_new_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT05-118 rows are not kept above VERIFIER_CACHE_ROWS; a new row_key re-runs the query."""
    sd.make_build(tmp_path, extra_rows=VERIFIER_CACHE_ROWS + 5)
    pool = WarehousePool(tmp_path, SqlSettings())
    ops = FakeOps()
    try:
        ev = sd.record(pool, sd.TEAM_SQL)
        ops.record_evidence(ev)
        verifier = _verifier(pool, ops, None)
        calls: list[str] = []
        handle = pool.get(sd.BUILD_ID)
        real = handle.cursor
        monkeypatch.setattr(handle, "cursor", lambda: _SpyCursor(real(), calls))
        first = sd.item("[[n1]]", [sd.ref("n1", 12, ev.query_id, "incidents", PAY_WEEK)])
        second = sd.item("[[n1]]", [sd.ref("n1", 5, ev.query_id, "incidents", {"team": "search"})])
        assert verifier.verify_numbers(first, sd.BUILD_ID).passed
        assert verifier.verify_numbers(first, sd.BUILD_ID).passed
        assert len(calls) == 1
        assert verifier.verify_numbers(second, sd.BUILD_ID).passed
        assert len(calls) == 2
        cached = verifier._cache.peek(ev.query_id, sd.BUILD_ID)
        assert cached is not None
        assert cached.rows is None
        assert cached.row_count == VERIFIER_CACHE_ROWS + 5 + len(sd.TEAM_ROWS)
    finally:
        pool.close_all()


def test_ut05_118_too_large_and_timeout_fail(tmp_path: Path) -> None:
    """UT05-118 above `scan_rows` -> query_failed "too large"; a slow re-run is interrupted."""
    sd.make_build(tmp_path, extra_rows=1_500)
    pool = WarehousePool(tmp_path, SqlSettings())
    ops = FakeOps()
    try:
        ev = sd.record(pool, sd.TEAM_SQL)
        slow_sql = "SELECT sum(i * i) AS s FROM range(100000000000) t(i)"
        slow = Evidence(
            query_id=compute_query_id(slow_sql, {}, sd.BUILD_ID),
            run_id=None,
            build_id=sd.BUILD_ID,
            sql=slow_sql,
            params={},
            result_hash="0" * 64,
            row_count=1,
            result_sample=[],
            executed_at=sd.EXECUTED_AT,
            duration_ms=1,
        )
        ops.record_evidence(ev)
        ops.record_evidence(slow)
        verifier = Verifier(
            ops,
            pool,
            VerifierSettings(rerun_timeout_s=1),
            allowed_numeral_patterns=sd.PATTERNS,
            sql=SqlSettings(scan_rows=1_000),
        )
        numbers = [
            sd.ref("n1", 12, ev.query_id, "incidents", PAY_WEEK),
            sd.ref("n2", 1, slow.query_id, "s", None),
        ]
        result = verifier.verify_numbers(sd.item("[[n1]] [[n2]]", numbers), sd.BUILD_ID)
        assert _results(result) == ["query_failed", "query_failed"]
        too_large = verifier._cache.peek(ev.query_id, sd.BUILD_ID)
        interrupted = verifier._cache.peek(slow.query_id, sd.BUILD_ID)
        assert too_large is not None
        assert too_large.error == "too large"
        assert interrupted is not None
        assert interrupted.error is not None
        assert len(interrupted.error) <= 200
    finally:
        pool.close_all()


def test_ut05_118_guard_rejection_is_query_failed(env: Env) -> None:
    """UT05-118 ops evidence whose SQL the guard rejects -> query_failed, error `guard: <rule>`."""
    sql = "SELECT * FROM read_csv('x.csv')"
    ev = Evidence(
        query_id=compute_query_id(sql, {}, sd.BUILD_ID),
        run_id=None,
        build_id=sd.BUILD_ID,
        sql=sql,
        params={},
        result_hash="0" * 64,
        row_count=1,
        result_sample=[],
        executed_at=sd.EXECUTED_AT,
        duration_ms=1,
    )
    env.ops.record_evidence(ev)
    result = env.verifier.verify_numbers(
        sd.item("[[n1]]", [sd.ref("n1", 1, ev.query_id, "a", None)]), sd.BUILD_ID
    )
    assert _results(result) == ["query_failed"]
    cached = env.verifier._cache.peek(ev.query_id, sd.BUILD_ID)
    assert cached is not None
    assert cached.error is not None
    assert cached.error.startswith("guard: ")


# --- UT05-119 / UT05-120: wrappers ----------------------------------------------------------


def _paragraph(text: str, numbers: list[NumberRef], finding_ids: list[str]) -> Paragraph:
    return Paragraph(text=text, numbers=numbers, finding_ids=finding_ids)


def _draft(team: str) -> ReportDraft:
    n_inc = sd.ref("n1", 12, team, "incidents", PAY_WEEK)
    n_usd = sd.ref("n2", "1234.50", team, "cost_usd", PAY_WEEK, unit="usd")
    n_hours = sd.ref("n3", 7.4, team, "mttr_hours", PAY_WEEK, unit="hours")
    rec = RecommendationItem(
        kind="fund",
        target_type="service",
        target_id="payments",
        headline="Fund payments reliability",
        summary="Cost [[n2]] with [[n1]] incidents and MTTR [[n3]] hours.",
        numbers=[n_inc, n_usd, n_hours],
        expected_delta_ref="n1",
        expected_usd_ref="n2",
        confidence_ref=None,
        effort_usd_ref="n3",
        action_levers=[
            {
                "entity_type": "team",
                "entity_id": "payments",
                "metric": "mttr",
                "delta_usd_ref": "n2",
            },
        ],
        finding_ids=[FND_A],
        query_ids=[team],
        rank=1,
    )
    return ReportDraft(
        run_id=RUN_ID,
        kind="funding_review",
        depth="standard",
        profile="local",
        build_id=sd.BUILD_ID,
        title="Funding review Q3 2026",
        sections=[
            Section(
                id="executive_summary",
                title="Summary",
                paragraphs=[
                    _paragraph("Payments had [[n1]] incidents.", [n_inc], [FND_A]),
                    _paragraph("It cost [[n2]].", [n_usd], [FND_A]),
                ],
            ),
            Section(id="method", title="Method", paragraphs=[]),
        ],
        recommendations=[rec],
        ranked_entities=[],
        caveats=["Data up to 2026-09-24.", "Small sample of 3 teams."],
        prior_outcomes_commentary=_paragraph("MTTR was [[n3]] hours.", [n_hours], [FND_A]),
        banners=[],
        flags={},
        contested=[],
        removed=[],
        coverage=Coverage(
            planned_tasks=1,
            done_tasks=1,
            dead_tasks=0,
            must_cover_total=0,
            must_cover_done=0,
            verified_findings=1,
            rejected_findings=0,
            publishable=True,
        ),
        dead_tasks=[],
        query_ids=[team],
        verification=VerificationResult(
            build_id=sd.BUILD_ID,
            passed=True,
            items=[],
            n_numbers=0,
            n_failed=0,
            verified_at=sd.EXECUTED_AT,
            duration_ms=0,
        ),
    )


def test_ut05_119_draft_items_in_order_with_refs(env: Env) -> None:
    """UT05-119 `verify_draft` item `where` list in the specified order; refs incl. not_usd."""
    team = env.add(sd.TEAM_SQL).query_id
    result = env.verifier.verify_draft(_draft(team), sd.BUILD_ID)
    assert [i.where for i in result.items] == [
        "title",
        "sections[0].title",
        "sections[0].paragraphs[0]",
        "sections[0].paragraphs[1]",
        "sections[1].title",
        "recommendations[0]",
        "caveats[0]",
        "caveats[1]",
        "prior_outcomes_commentary",
    ]
    by_where = {i.where: i for i in result.items}
    rec = by_where["recommendations[0]"]
    assert rec.bad_refs == ["effort_usd_ref:not_usd"]
    assert all(c.result == "match" for c in rec.checks)
    assert not rec.passed
    assert by_where["caveats[1]"].uncited[0].text == "3"
    assert by_where["title"].passed
    assert by_where["sections[0].paragraphs[1]"].passed
    assert len(env.tracer.events) == len(result.items)


def test_ut05_119_findings_only_draft(env: Env) -> None:
    """UT05-119 a `findings_only` draft is verified with the same rules (R-49)."""
    team = env.add(sd.TEAM_SQL).query_id
    full = _draft(team)
    draft = full.model_copy(
        update={
            "mode": "findings_only",
            "recommendations": [],
            "sections": full.sections[:1],
            "caveats": [],
            "prior_outcomes_commentary": None,
        }
    )
    result = env.verifier.verify_draft(draft, sd.BUILD_ID)
    assert [i.where for i in result.items] == [
        "title",
        "sections[0].title",
        "sections[0].paragraphs[0]",
        "sections[0].paragraphs[1]",
    ]
    assert result.passed


def test_ut05_120_findings_and_answer_wrappers(env: Env) -> None:
    """UT05-120 one result per finding in input order; the answer is one `answer` item."""
    team = env.add(sd.TEAM_SQL).query_id
    good = sd.ref("n1", 12, team, "incidents", PAY_WEEK)
    bad = sd.ref("n1", 99, team, "incidents", PAY_WEEK)
    findings = [
        Finding(
            finding_id=fid,
            run_id=RUN_ID,
            task_id=TASK_ID,
            author_role="analyst",
            claim="Payments had [[n1]] incidents.",
            entity_type="team",
            entity_id="payments",
            numbers=[number],
            query_ids=[team],
            confidence=0.8,
            created_at=sd.EXECUTED_AT,
        )
        for fid, number in ((FND_A, good), (FND_B, bad))
    ]
    results = env.verifier.verify_findings(findings, sd.BUILD_ID)
    assert [r.items[0].where for r in results] == [f"finding:{FND_A}", f"finding:{FND_B}"]
    assert [r.passed for r in results] == [True, False]
    assert results[0].items[0].unverified_findings == []
    answer = ChatAnswer(text="Payments had [[n1]] incidents.", numbers=[good], query_ids=[team])
    got = env.verifier.verify_answer(answer, sd.BUILD_ID)
    assert [i.where for i in got.items] == ["answer"]
    assert got.passed


# --- UT05-122: determinism ------------------------------------------------------------------


def test_ut05_122_same_inputs_same_result(env: Env) -> None:
    """UT05-122 three runs give identical results except timestamps and durations."""
    team = env.add(sd.TEAM_SQL).query_id
    numbers = [
        sd.ref("n1", 12, team, "incidents", PAY_WEEK),
        sd.ref("n2", 13, team, "incidents", {"team": "search"}),
    ]
    planted = sd.item("[[n1]] and [[n2]] plus 5", numbers)
    runs = [_stable(env.verifier.verify_numbers(planted, sd.BUILD_ID)) for _ in range(3)]
    fresh = _verifier(env.pool, env.ops, None).verify_numbers(planted, sd.BUILD_ID)
    assert runs[0] == runs[1] == runs[2] == _stable(fresh)


# --- UT05-129: cell tolerance (R-15) --------------------------------------------------------


def _with(ev: Evidence, **changes: Any) -> Evidence:
    return Evidence.model_validate({**ev.model_dump(), **changes})


def test_ut05_129_cell_tolerance(env: Env) -> None:
    """UT05-129 float-order hash drift -> n_hash_equivalent; changed values -> query_failed
    (`result drift`); `row_count > 50` -> n_hash_values_only, judged by value comparison."""
    real = sd.record(env.pool, sd.TOTALS_SQL)
    # 1: the stored sample holds the same rows up to float-sum order; the hash differs.
    sample = [dict(row) for row in real.result_sample]
    for row in sample:
        row["mttr_hours"] = float(row["mttr_hours"]) * (1 + 1e-12)  # type: ignore[arg-type]
    equivalent = _with(real, result_hash="1" * 64, result_sample=sample)
    env.ops.record_evidence(equivalent)
    # 2: another query whose stored values changed.
    real_one = sd.record(env.pool, sd.ONE_ROW_SQL)
    drifted = _with(real_one, result_hash="2" * 64, result_sample=[{"n": 6, "total_usd": "1.00"}])
    env.ops.record_evidence(drifted)
    # 3: a large result: only the cited values can be checked.
    real_team = sd.record(env.pool, sd.TEAM_SQL)
    large = _with(real_team, result_hash="3" * 64, row_count=5_000)
    env.ops.record_evidence(large)

    first = env.verifier.verify_numbers(
        sd.item("[[n1]]", [sd.ref("n1", 19, real.query_id, "incidents", {"team": "payments"})]),
        sd.BUILD_ID,
    )
    assert first.passed
    assert env.tracer.events[-1][2]["n_hash_equivalent"] == 1
    second = env.verifier.verify_numbers(
        sd.item("[[n1]]", [sd.ref("n1", 5, drifted.query_id, "n", None)]), sd.BUILD_ID
    )
    assert _results(second) == ["query_failed"]
    cached = env.verifier._cache.peek(drifted.query_id, sd.BUILD_ID)
    assert cached is not None
    assert cached.error is None  # the re-run worked: the failure is the result drift
    third = env.verifier.verify_numbers(
        sd.item(
            "[[n1]] [[n2]]",
            [
                sd.ref("n1", 12, large.query_id, "incidents", PAY_WEEK),
                sd.ref("n2", 99, large.query_id, "incidents", PAY_WEEK),
            ],
        ),
        sd.BUILD_ID,
    )
    assert _results(third) == ["match", "mismatch"]
    fields = env.tracer.events[-1][2]
    assert (fields["n_hash_values_only"], fields["n_hash_equal"]) == (1, 0)


def test_ut05_129_hash_kind_edges() -> None:
    """UT05-129 hash_kind: dropped rows mean drift; unencodable cells mean drift."""
    stored = rr.StoredEvidence("SELECT 1", {}, "a" * 64, 2, [{"x": "1"}], True)
    no_rows = rr.Rerun(("x",), ("INTEGER",), None, "b" * 64, 2, None)
    assert rr.hash_kind(stored, no_rows) is None
    odd = rr.Rerun(("x",), ("INTEGER",), [("not-a-number",)], "b" * 64, 1, None)
    one = rr.StoredEvidence("SELECT 1", {}, "a" * 64, 1, [{"x": "zzz"}], True)
    assert rr.hash_kind(one, odd) is None


def test_ut05_129_json_safe_conversion() -> None:
    """UT05-129 the U05-35 step 6 stand-in: Decimal, date, datetime, bytes, nested values."""
    naive = datetime(2026, 9, 24, 1, 2, 3)  # noqa: DTZ001 - DuckDB TIMESTAMP is naive UTC
    assert rr.json_safe(Decimal("1.50")) == "1.50"
    assert rr.json_safe(naive) == "2026-09-24T01:02:03.000000Z"
    assert rr.json_safe(naive.date()) == "2026-09-24"
    assert rr.json_safe(naive.time()) == "01:02:03"
    assert rr.json_safe(b"x") is None
    assert rr.json_safe([Decimal(1), {"k": (1, 2)}]) == ["1", {"k": [1, 2]}]
    assert rr.json_safe(object.__new__(_Odd)) == "odd"
    rows = [(1, b"blob")] * 60
    assert rr.sample_rows(["a", "b"], rows) == [{"a": 1}] * 50
    stored = rr.from_ops(
        Evidence(
            query_id=compute_query_id("SELECT 1", {}, sd.BUILD_ID),
            run_id=None,
            build_id=sd.BUILD_ID,
            sql="SELECT 1",
            params={},
            result_hash="a" * 64,
            row_count=1,
            result_sample=[],
            executed_at=sd.EXECUTED_AT,
            duration_ms=0,
        )
    )
    assert stored.guard


class _Odd:
    def __str__(self) -> str:
        return "odd"


def test_ut05_129_meta_params_shapes() -> None:
    """UT05-129 impl 04 `{bind, template}` params bind their `bind` part; others bind as-is."""
    assert rr._bind_of({"bind": {"a": 1}, "template": {}}) == {"a": 1}
    assert rr._bind_of({"a": 1}) == {"a": 1}
    assert rr._bind_of(None) == {}


def test_ut05_129_actual_json_safe_cases() -> None:
    """UT05-129 NumberCheck.actual stays JSON-safe for bool, list and None cells."""
    assert vitems.actual_value(True) == "true"
    assert vitems.actual_value([1, 2]) == "[1, 2]"
    assert vitems.actual_value(None) is None
    assert vitems.actual_value(Decimal("2.50")) == "2.50"


# --- Fix round 1 (review M1, M2) --------------------------------------------------------


def _closed(*_args: object) -> Any:
    msg = "Connection already closed!"
    raise duckdb.ConnectionException(msg)


@pytest.mark.parametrize("method", ["schema", "cursor"])
def test_ut05_118_handle_closed_by_other_thread_is_query_failed(
    env: Env, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    """UT05-118 a handle closed by the pool mid-verify (schema or cursor) -> query_failed."""
    team = env.add(sd.TEAM_SQL).query_id
    monkeypatch.setattr(env.pool.get(sd.BUILD_ID), method, _closed)
    numbers = [sd.ref("n1", 12, team, "incidents", PAY_WEEK)]
    result = env.verifier.verify_numbers(sd.item("[[n1]]", numbers), sd.BUILD_ID)
    assert _results(result) == ["query_failed"]
    cached = env.verifier._cache.peek(team, sd.BUILD_ID)
    assert cached is not None
    assert cached.error == "build unavailable"


@pytest.mark.parametrize("column", ["row_count", "sql", "result_hash"])
def test_ut05_117_meta_evidence_null_fields_missing_query(tmp_path: Path, column: str) -> None:
    """UT05-117 a `meta.evidence` row with a NULL row_count, sql or hash -> missing_query."""
    path = sd.make_build(tmp_path)
    con = duckdb.connect(str(path))
    con.execute(f"UPDATE meta.evidence SET {column} = NULL")  # noqa: S608 - fixed column names
    con.close()
    pool = WarehousePool(tmp_path, SqlSettings())
    try:
        verifier = _verifier(pool, FakeOps(), None)
        numbers = [sd.ref("n1", 7, sd.meta_query_id(), "incidents", {"team": "payments"})]
        result = verifier.verify_numbers(sd.item("[[n1]]", numbers), sd.BUILD_ID)
    finally:
        pool.close_all()
    assert _results(result) == ["missing_query"]


def test_ut05_118_query_failed_logs_category_only(env: Env) -> None:
    """UT05-118 each query_failed logs `harness.verifier.query_failed` with query_id and the
    failure category; DuckDB message text (which may quote data) is not logged."""
    quoting_sql = "SELECT CAST('Jane Doe INC0099999' AS INTEGER) AS a FROM metrics.team_week"
    bad = sd.foreign_evidence(quoting_sql)
    env.ops.record_evidence(bad)
    rejected = sd.foreign_evidence(sd.REJECTED_SQL)
    env.ops.record_evidence(rejected)
    drifted = _with(sd.record(env.pool, sd.ONE_ROW_SQL), result_hash="2" * 64, result_sample=[])
    env.ops.record_evidence(drifted)
    numbers = [
        sd.ref("n1", 1, bad.query_id, "a", None),
        sd.ref("n2", 1, rejected.query_id, "a", None),
        sd.ref("n3", 5, drifted.query_id, "n", None),
    ]
    with capture_logs() as logs:
        result = env.verifier.verify_numbers(sd.item("[[n1]] [[n2]] [[n3]]", numbers), sd.BUILD_ID)
    assert _results(result) == ["query_failed"] * 3
    events = [e for e in logs if e["event"] == "harness.verifier.query_failed"]
    by_query = {e["query_id"]: e["reason"] for e in events}
    assert by_query[bad.query_id] == "duckdb: ConversionException"
    assert by_query[rejected.query_id].startswith("guard: ")
    assert by_query[drifted.query_id] == "result drift"
    assert all(e["log_level"] == "info" for e in events)
    assert "Jane" not in repr(events)
    cached = env.verifier._cache.peek(bad.query_id, sd.BUILD_ID)
    assert cached is not None
    assert cached.error is not None
    assert "Jane" in cached.error  # the cached error keeps the DuckDB text; the log does not
