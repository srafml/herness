"""UT06-70 … UT06-76, PT06-04: pure adversarial-layer rules (U06-104 … U06-107, U06-110 … 112)."""

from __future__ import annotations

import itertools
from collections.abc import Callable
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.unit.harness.swarm._swarm_findings import (
    RUN_ID,
    fid,
    finding,
    knobs,
    number,
    run_row,
    tid,
)

from herness.core.types import SKEPTIC_CHECKS, Challenge, EntityScope, Finding, TaskSpec
from herness.harness.findings import compute_dedup_key
from herness.harness.swarm import adversarial as adv
from herness.harness.swarm.routing import default_tools, role_budget

pytestmark = pytest.mark.unit


def _ids() -> Callable[[], str]:
    counter = itertools.count(100)
    return lambda: tid(next(counter))


# --- UT06-70 select_for_challenge -------------------------------------------------------------


def test_ut06_70_selection_is_union_in_score_order() -> None:
    """UT06-70 top_n | first of each must-cover entity | retrospective findings, ordered."""
    fs = [
        finding(1, entity=("team", "t1")),
        finding(2, entity=("team", "t2")),
        finding(3, entity=("team", "t3")),
        finding(4, entity=("team", "t3")),
        finding(5, entity=("team", "t4"), task=9),  # retrospective task
        finding(6, entity=("team", "t5"), task=8),  # crosscheck task: excluded
    ]
    scores = {fid(1): 0.9, fid(2): 0.8, fid(3): 0.1, fid(4): 0.3, fid(5): 0.0, fid(6): 1.0}
    out = adv.select_for_challenge(
        fs,
        score=lambda f: scores[f.finding_id],
        top_n=1,
        must_cover={"team:t3", "team:t9"},
        retrospective_task_ids={tid(9)},
        excluded_task_ids={tid(8)},
    )
    assert [(f.finding_id, s) for f, s in out] == [(fid(1), 0.9), (fid(4), 0.3), (fid(5), 0.0)]


def test_ut06_70_equal_scores_order_by_id() -> None:
    """UT06-70 ties on score are ordered by finding id; top_n 0 selects nothing extra."""
    fs = [finding(2), finding(1)]
    out = adv.select_for_challenge(
        fs, score=lambda f: 0.5, top_n=2, must_cover=set(), retrospective_task_ids=set(),
        excluded_task_ids=set(),
    )  # fmt: skip
    assert [f.finding_id for f, _ in out] == [fid(1), fid(2)]
    none = adv.select_for_challenge(
        fs, score=lambda f: 0.5, top_n=0, must_cover=set(), retrospective_task_ids=set(),
        excluded_task_ids=set(),
    )  # fmt: skip
    assert none == []


# --- UT06-71 normalize_verdict ----------------------------------------------------------------


def _challenge(verdict: str, results: dict[str, tuple[str, list[str]]], **kw: Any) -> Challenge:
    checks = [
        {
            "check": c,
            "result": results.get(c, ("pass", []))[0],
            "note": "",
            "query_ids": results.get(c, ("pass", []))[1],
        }
        for c in SKEPTIC_CHECKS
    ]
    return Challenge.model_validate(
        {"finding_id": fid(1), "checks": checks, "verdict": verdict} | kw
    )


_Q = "q_" + "a" * 16


def test_ut06_71_reject_without_evidence_becomes_revise() -> None:
    """UT06-71 reject without a fail check carrying query_ids → revise with check names.

    `CheckResult` already requires a query for `concern` and `fail` (U06-09), so an
    unevidenced reject is one whose checks hold concerns at most.
    """
    ch = _challenge("reject", {"confounding": ("concern", [_Q]), "small_sample": ("concern", [_Q])})
    out = adv.normalize_verdict(ch)
    assert out.verdict == "revise"
    assert out.required_actions == ["Address these checks: confounding, small_sample"]
    assert out.checks == ch.checks
    assert ch.verdict == "reject"  # input unchanged


def test_ut06_71_fail_without_query_is_not_evidence() -> None:
    """UT06-71 a `fail` check whose query_ids are empty (unvalidated input) is no evidence."""
    base = _challenge("reject", {"confounding": ("fail", [_Q])})
    bare = base.checks[0].model_copy(update={"query_ids": []})
    ch = base.model_copy(update={"checks": [bare, *base.checks[1:]]})
    out = adv.normalize_verdict(ch)
    assert out.verdict == "revise"
    assert out.required_actions == ["Address these checks: confounding"]


def test_ut06_71_reject_without_concerns_gets_default_action() -> None:
    """UT06-71 no concern/fail checks → the independent-query action."""
    out = adv.normalize_verdict(_challenge("reject", {}))
    assert out.verdict == "revise"
    assert out.required_actions == ["Re-examine the claim with an independent query."]


def test_ut06_71_existing_actions_are_kept() -> None:
    """UT06-71 a downgraded verdict keeps the Skeptic's own required actions."""
    out = adv.normalize_verdict(_challenge("reject", {}, required_actions=["check q"]))
    assert (out.verdict, out.required_actions) == ("revise", ["check q"])


@pytest.mark.parametrize("verdict", ["reject", "uphold", "revise"])
def test_ut06_71_evidenced_reject_and_others_unchanged(verdict: str) -> None:
    """UT06-71 reject with a fail check and a query stays; uphold and revise are untouched."""
    ch = _challenge(verdict, {"mis_mapping": ("fail", [_Q])}, required_actions=["x"])
    assert adv.normalize_verdict(ch) is ch


# --- UT06-72, PT06-04 majority_verdict --------------------------------------------------------


def test_ut06_72_tie_goes_to_stricter() -> None:
    """UT06-72 tie reject/uphold → reject; tallies only contain seen verdicts."""
    assert adv.majority_verdict(["uphold", "reject"]) == ("reject", {"uphold": 1, "reject": 1})
    assert adv.majority_verdict(["uphold", "revise"]) == ("revise", {"uphold": 1, "revise": 1})
    assert adv.majority_verdict(["uphold", "uphold", "reject"])[0] == "uphold"


def test_ut06_72_empty_votes_raise() -> None:
    """UT06-72 the vote needs at least one verdict."""
    with pytest.raises(ValueError, match="at least one"):
        adv.majority_verdict([])


_VERDICTS = st.lists(st.sampled_from(["uphold", "revise", "reject"]), min_size=1, max_size=9)


@given(votes=_VERDICTS, data=st.data())
def test_pt06_04_vote_invariant_under_permutation(votes: list[Any], data: st.DataObject) -> None:
    """PT06-04 the result is invariant under any permutation of the votes."""
    shuffled = data.draw(st.permutations(votes))
    assert adv.majority_verdict(shuffled) == adv.majority_verdict(votes)


# --- UT06-73 skeptic and revision tasks -------------------------------------------------------


@pytest.mark.parametrize(
    ("depth", "profile", "final", "role"),
    [
        ("deep", "local", True, "skeptic_final"),
        ("standard", "hybrid", True, "skeptic_final"),
        ("standard", "local", True, "skeptic"),
        ("deep", "local", False, "skeptic"),
    ],
)
def test_ut06_73_skeptic_task_fields(depth: str, profile: str, final: bool, role: str) -> None:
    """UT06-73 final round deep or hybrid → skeptic_final; other fields per U06-107."""
    f = finding(1, numbers=[number(1), number(2, q=2)])
    k = knobs("deep" if depth == "deep" else "standard")
    spec = adv.build_skeptic_task(
        f, run=run_row(depth, profile), round_no=2, final_round=final, score=0.7, knobs=k,
        new_task_id=_ids(),
    )  # fmt: skip
    objective = (
        f"Challenge finding {f.finding_id} (round 2): run the six checks and return a verdict."
    )
    scope = EntityScope(entity_type="team", entity_ids=["t1"])
    assert spec.model_role == role
    assert (spec.task_id, spec.run_id, spec.role, spec.round) == (tid(100), RUN_ID, "skeptic", 2)
    assert spec.scope == scope
    assert spec.inputs.finding_ids == [f.finding_id]
    assert spec.inputs.query_ids == f.query_ids
    assert spec.objective == objective
    assert spec.tools == default_tools("skeptic", "general", "standard", child_depth=0, knobs=k)
    assert spec.budget == role_budget("skeptic", k)
    assert (spec.priority, spec.k_samples) == (0.7, k.k_samples.default)
    assert spec.dedup_key == compute_dedup_key("skeptic", "general", scope, objective)


def _author() -> TaskSpec:
    k = knobs("standard")
    scope = EntityScope(entity_type="team", entity_ids=["t1", "t2"])
    return TaskSpec(
        task_id=tid(50), run_id=RUN_ID, role="analyst", specialty="ops", objective="o",
        scope=scope, tools=["run_sql"], budget=k.analyst_budget, model_role="analyst",
        dedup_key=compute_dedup_key("analyst", "ops", scope, "o"),
    )  # fmt: skip


def test_ut06_73_revision_task_fields() -> None:
    """UT06-73 revision: analyst of the author's specialty and scope, round + 1, depth 0."""
    f = finding(1, claim="x" * 1_500)
    k = knobs("standard")
    skeptic = adv.build_skeptic_task(
        f, run=run_row(), round_no=1, final_round=False, score=0.4, knobs=k, new_task_id=_ids()
    )
    actions = ["a" * 300, "b" * 300]
    ch = _challenge("revise", {}, required_actions=actions)
    author = _author()
    rev = adv.build_revision_task(f, ch, skeptic=skeptic, author=author, knobs=k,
                                  new_task_id=lambda: tid(77))  # fmt: skip
    objective = (
        f"Revise finding {f.finding_id}: " + f.claim + " Required actions: " + "; ".join(actions)
    )[:2000]
    assert (rev.task_id, rev.role, rev.specialty, rev.scope) == (tid(77), "analyst", "ops",
                                                                  author.scope)  # fmt: skip
    assert (rev.revision_of, rev.round, rev.depth) == (f.finding_id, 2, 0)
    assert rev.parent_task_id == skeptic.task_id
    assert rev.objective == objective
    assert len(rev.objective) == 2_000
    assert rev.inputs.finding_ids == [f.finding_id]
    assert rev.inputs.query_ids == f.query_ids
    assert rev.tools == default_tools("analyst", "ops", "standard",
                                      child_depth=k.max_spawn_depth, knobs=k)  # fmt: skip
    assert "request_subtask" not in rev.tools
    assert (rev.budget, rev.priority, rev.model_role) == (k.analyst_budget, 0.4, "analyst")
    assert rev.dedup_key == compute_dedup_key("analyst", "ops", author.scope, objective)


# --- UT06-74 plan_crosschecks -----------------------------------------------------------------


def _plan(fs: list[Finding], depth: str, **kw: Any) -> tuple[list[TaskSpec], list[str]]:
    params: dict[str, Any] = {
        "run": run_row(depth),
        "knobs": knobs("deep" if depth == "deep" else "standard"),
        "must_cover": {"team:t1"},
        "selected_ids": set(),
        "task_count": 0,
        "new_task_id": _ids(),
    } | kw
    return adv.plan_crosschecks(fs, **params)


def _usd_findings() -> list[Finding]:
    return [
        finding(2, entity=("team", "t2"), numbers=[number(1, unit="usd", column="cost")]),
        finding(1, numbers=[number(10, unit="usd", column="cost", row_key={"week": 3, "a": "x"}),
                            number(2, unit="count"), number(3, unit="usd", column="spend")]),
    ]  # fmt: skip


def test_ut06_74_standard_takes_usd_of_must_cover() -> None:
    """UT06-74 standard: usd numbers of must-cover findings; ways - 1 = 1 task each."""
    tasks, skipped = _plan(_usd_findings(), "standard")
    assert skipped == []
    assert [t.inputs.notes for t in tasks] == ["target_number=n3", "target_number=n10"]
    t = tasks[0]
    assert (t.role, t.specialty, t.priority, t.model_role) == ("analyst", "crosscheck", 0.0,
                                                                "analyst")  # fmt: skip
    assert t.inputs.finding_ids == [fid(1)]
    assert t.inputs.query_ids == []
    assert t.scope == EntityScope(entity_type="team", entity_ids=["t1"])
    k = knobs("standard")
    assert t.tools == default_tools("analyst", "crosscheck", "standard", child_depth=0, knobs=k)
    assert t.budget == k.analyst_budget
    assert t.objective == (
        "Compute independently: spend for team t1, unit usd, period the pipeline window, "
        "row single row. Use a different table or aggregation path. Post one finding whose "
        "first number is this value (way 1)."
    )
    assert "row a=x, week=3." in tasks[1].objective
    assert t.dedup_key == compute_dedup_key("analyst", "crosscheck", t.scope, t.objective)


def test_ut06_74_deep_adds_selected_numbers_and_ways() -> None:
    """UT06-74 deep: every usd number plus every number of selected findings; 2 ways each."""
    tasks, skipped = _plan(_usd_findings(), "deep", must_cover=set(), selected_ids={fid(1)})
    assert skipped == []
    got = [(t.inputs.finding_ids[0], t.inputs.notes, t.objective[-8:]) for t in tasks]
    assert got == [
        (fid(1), "target_number=n2", "(way 1)."),
        (fid(1), "target_number=n2", "(way 2)."),
        (fid(1), "target_number=n3", "(way 1)."),
        (fid(1), "target_number=n3", "(way 2)."),
        (fid(1), "target_number=n10", "(way 1)."),
        (fid(1), "target_number=n10", "(way 2)."),
        (fid(2), "target_number=n1", "(way 1)."),
        (fid(2), "target_number=n1", "(way 2)."),
    ]
    assert len({t.task_id for t in tasks}) == len({t.dedup_key for t in tasks}) == 8


def test_ut06_74_task_cap_returns_skip_list() -> None:
    """UT06-74 the task cap stops planning; the remaining finding ids are skipped."""
    tasks, skipped = _plan(_usd_findings(), "deep", must_cover=set(), task_count=595)
    assert len(tasks) == 5
    assert skipped == [fid(2)]
    tasks, skipped = _plan(_usd_findings(), "deep", must_cover=set(), task_count=597)
    assert len(tasks) == 3
    assert skipped == [fid(1), fid(2)]


def test_ut06_74_single_way_plans_nothing() -> None:
    """UT06-74 crosscheck_ways <= 1 → no tasks and no skips."""
    k = knobs("fast")
    assert _plan(_usd_findings(), "fast", knobs=k) == ([], [])


# --- UT06-75 is_independent_sql ---------------------------------------------------------------

_BASE = "SELECT sum(cost) AS c FROM core.incident WHERE team_id = 't1' GROUP BY team_id"


@pytest.mark.parametrize(
    ("other", "independent"),
    [
        (_BASE, False),
        ("select SUM(cost) as total from core.incident where team_id='t1' group by team_id", False),
        ("SELECT sum(cost) FROM core.change WHERE team_id = 't1' GROUP BY team_id", True),
        ("SELECT avg(cost) FROM core.incident WHERE team_id = 't1' GROUP BY team_id", True),
        ("SELECT sum(cost) FROM core.incident WHERE team_id = 't1' GROUP BY service_id", True),
        ("SELECT sum(amount) FROM core.incident WHERE team_id = 't1' GROUP BY team_id", True),
        (
            "WITH incident AS (SELECT * FROM core.incident) "
            "SELECT sum(cost) FROM incident WHERE team_id = 't1' GROUP BY team_id",
            False,
        ),
        ("SELECT FROM WHERE (((", False),
        ("", False),
    ],
)
def test_ut06_75_independence(other: str, independent: bool) -> None:
    """UT06-75 same path False; other table/aggregation/grouping True; unparsable False."""
    assert adv.is_independent_sql(_BASE, other) is independent
    assert adv.is_independent_sql(other, _BASE) is independent


# --- UT06-76 crosscheck_agree -----------------------------------------------------------------

_TOL = {"rel_tol": 0.005, "abs_tol_count": 0.5, "abs_tol_ratio": 0.001}


@pytest.mark.parametrize(
    ("a", "b", "unit", "agree"),
    [
        (10, 10.5, "count", True),
        (10, 10.6, "count", False),
        (1_000_000, 1_004_999, "usd", True),
        (1_000_000, 1_005_001, "usd", False),
        (0.25, 0.2509, "pct", True),
        (0.25, 0.2511, "pct", False),
        (0.5, 0.5009, "ratio", True),
        (0.5, 0.5011, "ratio", False),
        (1_000, 1_004, "hours", True),
    ],
)
def test_ut06_76_tolerance_per_unit(a: float, b: float, unit: str, agree: bool) -> None:
    """UT06-76 ratio/pct use the absolute ratio tolerance; others max(rel * |a|, abs count)."""
    assert adv.crosscheck_agree(a, b, unit, **_TOL) is agree
