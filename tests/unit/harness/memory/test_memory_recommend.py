"""Tests for herness.harness.memory.recommend (impl 07 U07-78 … U07-80, T07-15)."""

from __future__ import annotations

import math
import random
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from structlog.testing import capture_logs
from tests.support.ops_store import OpsStoreHandle
from tests.unit.harness.memory._write_env import (
    NOW,
    PLANTED_EMAIL,
    PLANTED_NAME,
    RUN_ID,
    TASK_ID,
    Env,
    make_writer,
    memory_rows,
    seed_evidence,
    seed_finding,
)

from herness.core import time as clock
from herness.core.errors import ModelUnavailable, ReportContractError
from herness.core.ids import new_ulid
from herness.core.types import ConfidenceAdjustment, NumberRef, RecommendationDraft
from herness.harness.memory.policy import keyed_hash
from herness.harness.memory.recommend import (
    RecommendDeps,
    SimInput,
    outcome_adjustment,
    recommendation_similarity,
    write_recommendations,
)
from herness.harness.memory.settings import FeedbackConfig
from herness.harness.memory.types import MemoryNotFound
from herness.store.ops import SimilarityRow, core

pytestmark = pytest.mark.unit

QID = "q_" + "a" * 16
CFG = FeedbackConfig()

# ---------------------------------------------------------------- U07-78 helpers


def _run(meta: dict[str, Any] | None = None, run_id: str = RUN_ID) -> None:
    meta = (
        {"request": {"question": f"Why is {PLANTED_NAME} slow? {PLANTED_EMAIL}"}}
        if (meta is None)
        else meta
    )
    core.run_write(
        lambda c: c.execute(
            "INSERT INTO run (run_id, kind, depth, profile, config_hash, status, started_at,"
            " meta) VALUES (?, 'org_review', 'standard', 'p', 'h', 'recording', ?, ?)",
            (run_id, clock.format_utc(NOW), core.dump_json(meta, field="meta")),
        ),
        op="test_seed",
    )


def _dead_task() -> None:
    core.run_write(
        lambda c: c.execute(
            "INSERT INTO task (task_id, run_id, role, spec, status, created_at, updated_at)"
            " VALUES (?, ?, 'analyst', '{}', 'dead', ?, ?)",
            ("task_" + new_ulid(), RUN_ID, clock.format_utc(NOW), clock.format_utc(NOW)),
        ),
        op="test_seed",
    )


def _num(ref: str = "n1", value: Any = 1.5, unit: str = "pct", qid: str = QID) -> NumberRef:
    return NumberRef(id=ref, value=value, unit=unit, query_id=qid, column="c", row_key=None)  # type: ignore[arg-type]


def _draft(rank: int, findings: list[str], **fields: Any) -> RecommendationDraft:
    base: dict[str, Any] = {
        "rank": rank, "kind": "fund", "target_type": "service", "target_id": f"svc_{rank}",
        "summary": "Fund the platform team to lift [[n1]] and save [[n2]].",
        "numbers": [_num(), _num("n2", "1234.567", "usd")], "expected_metric": "mttr",
        "expected_delta_ref": "n1", "expected_usd_ref": "n2", "finding_ids": findings,
    }  # fmt: skip
    return RecommendationDraft(**(base | fields))


def _keep(r: RecommendationDraft, base: float) -> ConfidenceAdjustment:
    return ConfidenceAdjustment(confidence=min(0.95, max(0.05, base)), base=base, delta=0.0,
                                similar=[])  # fmt: skip


def _deps(env: Env, adjust: Any = _keep) -> RecommendDeps:
    return RecommendDeps(conn_factory=core.connection, writer=env.writer, adjust=adjust,
                         redactor=env.redactor, allowed=())  # fmt: skip


def _recs() -> list[dict[str, Any]]:
    rows = core.read_all("SELECT * FROM recommendation ORDER BY rec_id")
    out = []
    for row in rows:
        rec = dict(row)
        for name in ("numbers", "confidence_basis", "finding_ids"):
            rec[name] = core.load_json(rec[name], field=name)
        out.append(rec)
    return out


@pytest.fixture
def env(ops_store: OpsStoreHandle, tmp_path: Path) -> Env:
    _run()
    seed_evidence(QID)
    return make_writer(tmp_path)


def _contract(env: Env, recs: list[RecommendationDraft], match: str) -> None:
    with pytest.raises(ReportContractError, match=match):
        write_recommendations(RUN_ID, recs, deps=_deps(env), now=NOW)
    assert _recs() == []
    assert memory_rows() == []


# ---------------------------------------------------------------- UT07-63 validation


def test_ut07_63_unverified_finding(env: Env) -> None:
    """UT07-63 a finding that is not verified fails the draft; nothing is written."""
    ok, bad = seed_finding(), seed_finding(status="proposed")
    recs = [_draft(1, [ok]), _draft(2, [ok, bad])]
    _contract(env, recs, r"^recommendation rank 2: findings$")


def test_ut07_63_unknown_or_foreign_finding(env: Env) -> None:
    """UT07-63 an unknown finding or one of another run fails the draft."""
    _contract(env, [_draft(1, ["fnd_" + new_ulid()])], "rank 1: findings")
    other = "run_" + new_ulid()
    _run(run_id=other)
    with pytest.raises(ReportContractError, match="rank 1: findings"):
        write_recommendations(other, [_draft(1, [seed_finding()])], deps=_deps(env), now=NOW)
    assert _recs() == []
    assert memory_rows() == []


def test_ut07_63_bad_marker(env: Env) -> None:
    """UT07-63 a marker without a NumberRef fails `check_markers`."""
    f = seed_finding()
    _contract(env, [_draft(1, [f], summary="Lift [[n9]] now.")], "rank 1: markers")
    _contract(env, [_draft(1, [f], summary="Lift [[x1]] now.")], "rank 1: markers")


def test_ut07_63_uncited_numeral(env: Env) -> None:
    """UT07-63 a numeral outside a marker fails the draft."""
    summary = "Cut 5 servers to lift [[n1]] and save [[n2]]."
    _contract(env, [_draft(1, [seed_finding()], summary=summary)], "rank 1: numerals")


def test_ut07_63_non_usd_ref(env: Env) -> None:
    """UT07-63 `expected_usd_ref` naming a non-usd number fails the draft."""
    f = seed_finding()
    _contract(env, [_draft(1, [f], expected_usd_ref="n1")], "rank 1: expected_usd_unit")


def test_ut07_63_unresolved_refs(env: Env) -> None:
    """UT07-63 refs that are not ids in `numbers` fail the draft."""
    f = seed_finding()
    _contract(env, [_draft(1, [f], expected_delta_ref="n7")], "rank 1: expected_delta_ref")
    _contract(env, [_draft(1, [f], expected_usd_ref="n7")], "rank 1: expected_usd_ref")


def test_ut07_63_number_without_evidence(env: Env) -> None:
    """UT07-63 a NumberRef whose query_id is not in evidence fails the draft."""
    nums = [_num(), _num("n2", "10.00", "usd", qid="q_" + "b" * 16)]
    _contract(env, [_draft(1, [seed_finding()], numbers=nums)], "rank 1: evidence")


def test_ut07_63_duplicate_rank_and_count(env: Env) -> None:
    """UT07-63 duplicate ranks or more than 50 drafts raise ReportContractError."""
    f = seed_finding()
    _contract(env, [_draft(1, [f]), _draft(1, [f], target_id="svc_x")], "^duplicate rank$")
    many = [_draft(i, [f]) for i in range(1, 52)]
    _contract(env, many, "recommendation count")


def test_ut07_63_unknown_run_and_empty(env: Env) -> None:
    """UT07-63 an unknown run raises MemoryNotFound; no drafts return [] and write nothing."""
    missing = "run_" + new_ulid()
    with pytest.raises(MemoryNotFound) as info:
        write_recommendations(missing, [_draft(1, [seed_finding()])], deps=_deps(env), now=NOW)
    assert (info.value.kind, info.value.ident) == ("run", missing)
    assert write_recommendations(missing, [], deps=_deps(env), now=NOW) == []
    assert _recs() == []
    assert memory_rows() == []


def test_ut07_63_writes_rows_and_run_summary(env: Env) -> None:
    """UT07-63 a valid set: rows, ids in input order, one numeral-free run_summary."""
    f1, f2 = seed_finding(confidence=0.6), seed_finding(confidence=0.8)
    _dead_task()
    recs = [_draft(3, [f2]), _draft(1, [f1, f2], expected_usd_ref=None, kind="org_action",
                                     summary="Merge the on-call rotas to lift [[n1]].",
                                     numbers=[_num()])]  # fmt: skip
    seen: list[tuple[int, float]] = []

    def adjust(r: RecommendationDraft, base: float) -> ConfidenceAdjustment:
        seen.append((r.rank, base))
        return ConfidenceAdjustment(confidence=0.5, base=base, delta=-0.1, similar=[])

    with capture_logs() as events:
        ids = write_recommendations(RUN_ID, recs, deps=_deps(env, adjust), now=NOW)
    assert [r for r, _ in seen] == [1, 3]
    assert [b for _, b in seen] == pytest.approx([0.7, 0.8])
    rows = {r["rec_id"]: r for r in _recs()}
    assert len(ids) == 2
    assert list(rows) == sorted(ids)
    third, first = rows[ids[0]], rows[ids[1]]
    assert (third["target_id"], first["target_id"]) == ("svc_3", "svc_1")
    assert third["expected_usd"] == "1234.57"
    assert (third["expected_delta"], first["expected_usd"]) == (1.5, None)
    assert third["confidence"] == 0.5
    assert third["created_at"] == clock.format_utc(NOW)
    assert third["confidence_basis"] == {
        "base": pytest.approx(0.8), "delta": -0.1, "expected_delta_ref": "n1",
        "expected_usd_ref": "n2", "rank": 3, "similar": [],
    }  # fmt: skip
    assert (first["finding_ids"], first["kind"]) == ([f1, f2], "org_action")
    (item,) = memory_rows()
    assert item["kind"] == "run_summary"
    assert item["content"] == (
        f"Run {RUN_ID} (org_review) recorded recommendations for service:svc_1, service:svc_3."
    )
    data = item["data"]
    assert data["content_hash"] == keyed_hash("run_summary:" + RUN_ID)
    assert (data["run_kind"], data["rec_ids"]) == ("org_review", [ids[1], ids[0]])
    assert data["top_finding_ids"] == [f1, f2]
    assert data["dead_task_count"] == 1
    assert PLANTED_EMAIL not in data["question"]
    assert PLANTED_NAME not in data["question"]
    assert data["embedding_pending"] is False
    assert item["provenance"]["via"] == "pipeline"
    assert item["provenance"]["run_id"] == RUN_ID
    written = [e for e in events if e["event"] == "memory.recommendations.written"]
    assert written == [{
        "event": "memory.recommendations.written", "log_level": "info", "component": "memory",
        "run_id": RUN_ID, "n": 2, "reused": False,
    }]  # fmt: skip


def test_ut07_63_question_bounds(env: Env) -> None:
    """UT07-63 the question is cut to 500 chars; a missing request stores null."""
    long_run = "run_" + new_ulid()
    _run({"request": {"question": "why " * 300}}, run_id=long_run)
    _run({}, run_id=(bare := "run_" + new_ulid()))
    for run_id in (long_run, bare):
        f = "fnd_" + new_ulid()
        sql = (
            "INSERT INTO finding (finding_id, run_id, task_id, author_role, claim, query_ids,"
            " confidence, status, created_at) VALUES (?, ?, ?, 'analyst', 'c', '[]', 0.5,"
            " 'verified', ?)"
        )
        params = (f, run_id, TASK_ID, clock.format_utc(NOW))

        def seed(c: sqlite3.Connection, q: str = sql, a: tuple[str, ...] = params) -> None:
            c.execute(q, a)

        core.run_write(seed, op="test_seed")
        write_recommendations(run_id, [_draft(1, [f])], deps=_deps(env), now=NOW)
    questions = [r["data"]["question"] for r in memory_rows()]
    assert sorted(questions, key=str) == sorted([("why " * 300)[:500], None], key=str)


def test_ut07_63_resume_returns_same_ids(env: Env) -> None:
    """UT07-63 a second call with the same targets writes nothing and returns the same ids."""
    f = seed_finding()
    recs = [_draft(2, [f]), _draft(1, [f])]
    first = write_recommendations(RUN_ID, recs, deps=_deps(env), now=NOW)
    before = (_recs(), memory_rows())
    with capture_logs() as events:
        again = write_recommendations(RUN_ID, recs, deps=_deps(env), now=NOW + timedelta(1))
    assert again == first
    assert (_recs(), memory_rows()) == before
    written = [e for e in events if e["event"] == "memory.recommendations.written"]
    assert written[0]["reused"] is True


# ---------------------------------------------------------------- UT07-64 resume mismatch


def test_ut07_64_changed_targets_raise(env: Env) -> None:
    """UT07-64 existing rows differing from the drafts: ReportContractError, nothing written."""
    f = seed_finding()
    write_recommendations(RUN_ID, [_draft(1, [f]), _draft(2, [f])], deps=_deps(env), now=NOW)
    before = (_recs(), memory_rows())
    changed = [
        [_draft(1, [f]), _draft(2, [f], target_id="svc_9")],
        [_draft(1, [f]), _draft(2, [f], kind="org_action")],
        [_draft(1, [f])],
        [_draft(1, [f]), _draft(2, [f]), _draft(3, [f])],
    ]
    for recs in changed:
        with capture_logs() as events, pytest.raises(ReportContractError) as info:
            write_recommendations(RUN_ID, recs, deps=_deps(env), now=NOW)
        assert str(info.value) == "recommendations for run changed on resume"
        conflict = [e for e in events if e["event"] == "memory.recommendations.conflict"]
        assert conflict == [{"event": "memory.recommendations.conflict", "log_level": "error",
                             "component": "memory", "run_id": RUN_ID}]  # fmt: skip
        assert (_recs(), memory_rows()) == before


# ---------------------------------------------------------------- UT07-65 similarity

_R = SimInput("fund", "mttr", "service", "svc_a")


@pytest.mark.parametrize(
    ("p", "related", "s_target"),
    [
        (SimInput("fund", "mttr", "service", "svc_a"), False, 1.0),
        (SimInput("fund", "mttr", "team", "svc_a"), True, 1.0),
        (SimInput("fund", "mttr", "team", "team_x"), True, 0.5),
        (SimInput("fund", "mttr", "service", "svc_b"), True, 0.5),
        (SimInput("fund", "mttr", "service", "svc_b"), False, 0.2),
        (SimInput("fund", "mttr", "team", "team_x"), False, 0.0),
    ],
)
def test_ut07_65_target_cases(p: SimInput, related: bool, s_target: float) -> None:
    """UT07-65 s_target is 1 / 0.5 / 0.2 / 0 and sim = 0.4 s_kind + 0.35 s_target + 0.25 s_text."""
    s_kind, got, s_text, sim = recommendation_similarity(_R, p, s_text=0.5, related=related)
    assert (s_kind, got, s_text) == (1.0, s_target, 0.5)
    assert sim == pytest.approx(0.4 + 0.35 * s_target + 0.125)


def test_ut07_65_kind_and_text() -> None:
    """UT07-65 s_kind needs kind and metric; s_text is clamped to [0, 1]."""
    other_metric = SimInput("fund", "cost", "service", "svc_a")
    other_kind = SimInput("org_action", "mttr", "service", "svc_a")
    got = recommendation_similarity(_R, other_metric, s_text=-0.4, related=False)
    assert got == pytest.approx((0.0, 1.0, 0.0, 0.35))
    assert recommendation_similarity(_R, other_kind, s_text=1.0, related=False)[0] == 0.0
    assert recommendation_similarity(_R, _R, s_text=2.0, related=False)[2:] == (1.0, 1.0)
    assert recommendation_similarity(_R, _R, s_text=math.nan, related=False)[2] == 0.0


# ---------------------------------------------------------------- UT07-66 outcome adjustment


def _prior(verdict: str, *, days: float = 0.0, **fields: Any) -> SimilarityRow:
    row: dict[str, Any] = {
        "rec_id": "rec_" + new_ulid(), "kind": "fund", "target_type": "service",
        "target_id": "svc_a", "expected_metric": "mttr", "summary": "Fund [[n1]] the team.",
        "verdict": verdict, "measured_at": clock.format_utc(NOW - timedelta(days=days)),
        "query_id": "q_" + "c" * 16,
    }  # fmt: skip
    out: SimilarityRow = row | fields  # type: ignore[assignment]
    return out


def _same(_text: str) -> np.ndarray:
    return np.array([1.0, 0.0])


def _never(_a: str, _b: str) -> bool:
    return False


def _adj(base: float, priors: list[SimilarityRow], **kw: Any) -> ConfidenceAdjustment:
    draft = _draft(1, ["fnd_" + new_ulid()], target_id="svc_a")
    args: dict[str, Any] = {"embed": _same, "related": _never, "cfg": CFG, "now": NOW} | kw
    return outcome_adjustment(draft, base, priors=priors, **args)


def test_ut07_66_no_prior_gives_zero_delta() -> None:
    """UT07-66 no qualifying prior: delta 0 and confidence = clamp(base)."""
    unrelated = _prior("worse", kind="org_action", target_id="svc_z")
    for base, conf in ((0.5, 0.5), (0.99, 0.95), (0.0, 0.05)):
        for priors in ([], [unrelated]):
            adj = _adj(base, priors)
            assert (adj.delta, adj.confidence, adj.base, adj.similar) == (0.0, conf, base, [])


def test_ut07_66_mixed_priors_bounded() -> None:
    """UT07-66 paid_off raises, worse lowers, both stay inside the delta bounds."""
    up = _adj(0.6, [_prior("paid_off") for _ in range(40)])
    assert up.delta == pytest.approx(0.15)
    assert up.confidence == pytest.approx(0.69)
    assert _adj(0.6, [_prior("paid_off")]).delta == 0.15  # 0.5 * 1 / (1 + 1) clamped
    down = _adj(0.6, [_prior("worse") for _ in range(40)])
    assert down.delta == -0.25
    assert down.confidence == pytest.approx(0.45)
    flat = _adj(0.6, [_prior("inconclusive"), _prior("no_effect")])
    assert flat.delta == pytest.approx(0.5 * (-0.5) / (2 + 1.0))
    assert len(up.similar) == 20


def test_ut07_66_formula_and_decay() -> None:
    """UT07-66 delta = alpha * sum(sim*v*decay) / (sum(sim*decay) + k0); 365 days halves."""
    old = _prior("no_effect", days=365)
    adj = _adj(0.5, [old])
    assert adj.delta == pytest.approx(0.5 * (1.0 * -0.5 * 0.5) / (0.5 + 1.0))
    assert adj.confidence == pytest.approx(0.5 * (1 + adj.delta))
    (entry,) = adj.similar
    assert (entry.rec_id, entry.sim, entry.verdict) == (old["rec_id"], 1.0, "no_effect")
    assert entry.outcome_query_id == "q_" + "c" * 16
    future = _adj(0.5, [_prior("no_effect", days=-30, query_id=None)])
    assert future.delta == pytest.approx(0.5 * -0.5 / 2.0)
    assert future.similar[0].outcome_query_id == ""


def test_ut07_66_filters_and_threshold() -> None:
    """UT07-66 priors below sim_threshold or matching neither kind nor target are dropped."""

    def orth(text: str) -> np.ndarray:
        return np.array([1.0, 0.0]) if "team" in text else np.array([0.0, 1.0])

    other_target = _prior("worse", target_id="svc_b")  # 0.4 + 0.07 + 0.25 s_text
    other_kind = _prior("worse", kind="org_action", summary="Other plan.")  # 0.35 + 0.0
    adj = _adj(0.5, [other_target, other_kind], embed=orth)
    assert adj.similar[0].rec_id == other_target["rec_id"]
    assert len(adj.similar) == 1
    assert adj.similar[0].sim == pytest.approx(0.72)
    related = _adj(0.5, [_prior("worse", target_id="team_x", target_type="team",
                                summary="Other plan.")],
                   embed=orth, related=lambda a, b: (a, b) == ("svc_a", "team_x"))  # fmt: skip
    assert related.similar == []  # 0.4 + 0.175 + 0 < 0.6
    loose = _adj(0.5, [_prior("worse", target_id="team_x", target_type="team")],
                 related=lambda a, b: True, cfg=FeedbackConfig(sim_threshold=0.9))  # fmt: skip
    assert loose.similar == []


def test_ut07_66_markers_stripped_before_embedding() -> None:
    """UT07-66 summaries are embedded with markers removed and whitespace collapsed."""
    texts: list[str] = []

    def record(text: str) -> np.ndarray:
        texts.append(text)
        return np.array([1.0, 0.0])

    _adj(0.5, [_prior("paid_off", summary="Fund  [[n1]]\n the [[bad]] team.")], embed=record)
    assert texts == ["Fund the platform team to lift and save .", "Fund the team."]


def test_ut07_66_embedding_down_degrades() -> None:
    """UT07-66 ModelUnavailable: s_text = 0 for all priors and memory.feedback.degraded."""

    def down(_text: str) -> np.ndarray:
        msg = "embedding down"
        raise ModelUnavailable(msg)

    with capture_logs() as events:
        adj = _adj(0.5, [_prior("paid_off"), _prior("worse", target_id="svc_b")], embed=down)
    assert [s.sim for s in adj.similar] == [0.75]
    degraded = [e for e in events if e["event"] == "memory.feedback.degraded"]
    assert degraded == [{"event": "memory.feedback.degraded", "log_level": "warning",
                         "component": "memory", "priors": 2}]  # fmt: skip


def test_ut07_66_deterministic_order() -> None:
    """UT07-66 similar is sorted by sim desc then rec_id; input order does not matter."""
    priors = [_prior(v, target_id=t) for v in ("paid_off", "worse") for t in ("svc_a", "svc_b")]
    priors += [_prior("no_effect") for _ in range(25)]
    first = _adj(0.5, priors)
    shuffled = list(priors)
    random.Random(7).shuffle(shuffled)
    assert _adj(0.5, shuffled) == first
    keys = [(-s.sim, s.rec_id) for s in first.similar]
    assert keys == sorted(keys)
    assert len(keys) == 20


def test_ut07_66_feedback_bounds_from_config() -> None:
    """UT07-66 narrower config bounds clamp delta and confidence."""
    cfg = FeedbackConfig(delta_bounds=(-0.1, 0.05), confidence_bounds=(0.2, 0.6))
    assert _adj(0.9, [_prior("paid_off")] * 10, cfg=cfg).confidence == 0.6
    worse = _adj(0.1, [_prior("worse")] * 10, cfg=cfg)
    assert (worse.delta, worse.confidence) == (-0.1, 0.2)
