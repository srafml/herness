"""UT06-77 to UT06-81, UT06-94: pure draft assembly, gate 2 rules, removals, coverage and the
findings-only draft (impl 06 T06-19, `herness.harness.swarm.draft_rules`)."""

from __future__ import annotations

import dataclasses
import re
from typing import Any

import pytest
from tests.unit.harness.swarm import _swarm_drafts as d
from tests.unit.harness.swarm._swarm_findings import fid, finding, number, qid, run_row

from herness.core.errors import ConfigError
from herness.core.types import ReportDraft
from herness.harness.swarm import draft_rules as dr
from herness.harness.swarm.draft_rules import DraftExtras

pytestmark = pytest.mark.unit

ALLOWED = (re.compile(r"\b(19|20)\d{2}\b"), re.compile(r"Q[1-4] \d{4}"))
_TABLES = {
    qid(11): frozenset({"score.portfolio"}),
    qid(12): frozenset({"score.funding"}),
    qid(21): frozenset({"score.action_lever"}),
}
_LEVERS = [{"entity_id": "t1", "metric": "mttr"}, {"entity_id": "t1", "metric": "reopen"}]


def _tables_of(query_id: str) -> frozenset[str]:
    return _TABLES.get(query_id, frozenset())


def _fill(out: Any = None, **kw: Any) -> ReportDraft:
    findings = kw.pop("findings", {fid(1): finding(1, numbers=[number(1, q=5)])})
    return dr.fill_draft(
        out if out is not None else d.writer_out(), run=kw.pop("run", run_row()),
        pipeline=d.StubPipeline(), plan_ctx=kw.pop("ctx", d.plan_ctx()), findings=findings,
        extras=kw.pop("extras", DraftExtras()), verification=d.vresult(),
    )  # fmt: skip


def _rules(draft: ReportDraft, statuses: dict[str, str] | None = None, **kw: Any) -> list[str]:
    out = dr.check_recommendation_rules(
        draft, statuses=statuses or {fid(1): "verified"}, tables_of=_tables_of,
        portfolio_query_ids=kw.get("portfolio", frozenset()), levers=kw.get("levers", _LEVERS),
    )  # fmt: skip
    return [f"{f['where']} {f['reason']}" for f in out]


# --- UT06-77


def test_ut06_77_fill_ranks_query_ids_and_fields() -> None:
    """UT06-77 ranks follow the Writer order; recommendation and draft query ids are unions."""
    out = d.writer_out(recommendations=[d.fund_rec("c1"), d.fund_rec("c2")])
    draft = _fill(out)
    assert [r.rank for r in draft.recommendations] == [1, 2]
    assert [r.target_id for r in draft.recommendations] == ["c1", "c2"]
    assert draft.recommendations[0].query_ids == sorted([qid(5), qid(11), qid(12)])
    assert draft.query_ids == sorted([qid(1), qid(5), qid(11), qid(12)])
    assert [e.entity_id for e in draft.ranked_entities] == ["c1", "c2"]
    assert draft.mode == "full"
    assert draft.portfolio_custom == [{"x": 1}]
    assert draft.flags == {"partial_coverage": [], "notes": []}
    assert draft.removed == []
    assert draft.coverage.publishable is False
    assert draft.coverage.planned_tasks == 0


def test_ut06_77_banners_fixed_order_and_extras() -> None:
    """UT06-77 banners in the fixed order dq, unconfirmed, hybrid_fallback, budget_exhausted."""
    extras = DraftExtras(
        contested=[fid(9)], dead_tasks=[{"task_id": "t", "role": "analyst", "objective": "o",
                                         "last_error": None}],
        flags_notes=["note"], hybrid_fallback=True, budget_exhausted=True,
    )  # fmt: skip
    draft = _fill(ctx=d.plan_ctx(dq=True, unconfirmed=True), extras=extras)
    assert draft.banners == ["dq_warnings", "unconfirmed_weights", "hybrid_fallback",
                             "budget_exhausted"]  # fmt: skip
    assert draft.contested == [fid(9)]
    assert draft.dead_tasks[0]["task_id"] == "t"
    assert draft.flags["notes"] == ["note"]
    assert _fill(extras=DraftExtras(budget_exhausted=True)).banners == ["budget_exhausted"]
    assert _fill().banners == []


def test_ut06_77_commentary_query_ids_and_no_build_id() -> None:
    """UT06-77 commentary numbers count in `query_ids`; a run without build id → ConfigError."""
    out = d.writer_out(prior_outcomes_commentary=d.para(q=7))
    assert qid(7) in _fill(out).query_ids
    with pytest.raises(ConfigError):
        _fill(run=dataclasses.replace(run_row(), build_id=None))


# --- UT06-78


def test_ut06_78_valid_recommendations_pass() -> None:
    """UT06-78 fund and org_action recommendations meeting every rule give no violation."""
    draft = _fill(d.writer_out(recommendations=[d.fund_rec(), d.org_rec()]))
    assert _rules(draft) == []


@pytest.mark.parametrize(
    ("over", "rule"),
    [
        ({"expected_usd_ref": None}, "expected_usd_source"),
        ({"effort_usd_ref": None}, "effort_source"),
        ({"confidence_ref": "n2"}, "confidence_source"),
        ({"finding_ids": [fid(2)]}, "unverified_finding"),
    ],
)
def test_ut06_78_fund_rule_named(over: dict[str, Any], rule: str) -> None:
    """UT06-78 each fund rule violated is named per recommendation."""
    draft = _fill(d.writer_out(recommendations=[d.org_rec(), d.fund_rec(**over)]))
    assert _rules(draft) == [f"recommendations[1] rule:{rule}"]


def test_ut06_78_fund_usd_sources() -> None:
    """UT06-78 `expected_usd_ref` from a portfolio query id or `addressable_pain_usd` passes;
    the same column from a table outside `score.portfolio` fails."""
    pain = d.nref(1, q=12, unit="usd", column="addressable_pain_usd")
    rec = d.fund_rec(numbers=[pain, *d.fund_rec()["numbers"][1:]])
    assert _rules(_fill(d.writer_out(recommendations=[rec]))) == []
    other = d.nref(1, q=30, unit="usd", column="expected_impact_usd")
    rec = d.fund_rec(numbers=[other, *d.fund_rec()["numbers"][1:]])
    draft = _fill(d.writer_out(recommendations=[rec]))
    assert _rules(draft) == ["recommendations[0] rule:expected_usd_source"]
    assert _rules(draft, portfolio=frozenset({qid(30)})) == []


@pytest.mark.parametrize(
    ("over", "rule"),
    [
        ({"expected_metric": "cost"}, "expected_metric"),
        ({"expected_metric": None}, "expected_metric"),
        ({"expected_usd_ref": "n2"}, "expected_usd_top_lever"),
    ],
)
def test_ut06_78_org_rule_named(over: dict[str, Any], rule: str) -> None:
    """UT06-78 each org_action rule violated is named per recommendation."""
    draft = _fill(d.writer_out(recommendations=[d.org_rec(**over)]))
    assert _rules(draft) == [f"recommendations[0] rule:{rule}"]


def test_ut06_78_org_lever_source_and_no_levers() -> None:
    """UT06-78 a lever ref outside `score.action_lever` breaks `lever_source`; without lever
    refs the top-lever rule fails too; lever rows of another team do not count."""
    nums = [d.nref(1, q=21, unit="usd", column="delta_usd", value="900.00"),
            d.nref(2, q=40, unit="usd", column="delta_usd", value="300.00")]  # fmt: skip
    draft = _fill(d.writer_out(recommendations=[d.org_rec(numbers=nums)]))
    assert _rules(draft) == ["recommendations[0] rule:lever_source"]
    bare = d.org_rec(action_levers=[], numbers=nums[:1])
    draft = _fill(d.writer_out(recommendations=[bare]))
    assert _rules(draft) == ["recommendations[0] rule:expected_usd_top_lever"]
    draft = _fill(d.writer_out(recommendations=[d.org_rec()]))
    assert _rules(draft, levers=[{"entity_id": "t2", "metric": "mttr"}]) == [
        "recommendations[0] rule:expected_metric"
    ]


# --- UT06-79


def _extra(**over: Any) -> list[str]:
    draft = _fill(d.writer_out(**over))
    return [f"{f['where']} {f['reason']}" for f in dr.check_extra_text(draft, ALLOWED)]


def test_ut06_79_clean_text_passes() -> None:
    """UT06-79 allowed numerals (`Q3 2026`) and spelled-out numbers raise nothing."""
    assert _extra() == []


def test_ut06_79_numerals_in_title_headline_caveat_listed() -> None:
    """UT06-79 numerals in the title, a headline and a caveat are listed; a caveat marker too."""
    recs = [
        d.fund_rec(headline="Fund 3 rewrites"), d.fund_rec("c2", headline="Save [[n7]]"),
        d.fund_rec("c3", headline="Save [[n1]] now"), d.fund_rec("c4", headline="[[bad]]"),
    ]  # fmt: skip
    got = _extra(
        title="Top 5 bets", recommendations=recs,
        caveats=["Covers 90 days", "See [[n1]]", "No numbers here", "Broken [[x]]"],
    )  # fmt: skip
    assert got == [
        "title uncited_numeral",
        "recommendations[0] headline",
        "recommendations[1] headline",
        "recommendations[3] headline",
        "caveats[0] uncited_numeral",
        "caveats[1] marker_in_caveat",
        "caveats[3] marker_in_caveat",
    ]
    assert _extra(title="Review [[n1]]") == ["title uncited_numeral"]


# --- UT06-80


def test_ut06_80_removals_ranks_and_default_title() -> None:
    """UT06-80 failures on a paragraph, a rec, a caveat, the commentary and the title drop
    them, fill `removed` once each and renumber the ranks; titles fall back to the default."""
    paras = [d.para("A [[n1]]"), d.para("B [[n1]]"), d.para("C [[n1]]")]
    sections = [{"id": "executive_summary", "title": "S", "paragraphs": paras}]
    out = d.writer_out(
        sections=sections, recommendations=[d.fund_rec("c1"), d.fund_rec("c2"), d.fund_rec("c3")],
        caveats=["one", "two"], prior_outcomes_commentary=d.para(),
    )  # fmt: skip
    draft = _fill(out)
    fails = [
        {"where": "sections[0].paragraphs[0]", "reason": "gate2"},
        {"where": "sections[0].paragraphs[2]", "reason": "gate2"},
        {"where": "recommendations[0]", "reason": "rule:effort_source"},
        {"where": "recommendations[0]", "reason": "rule:effort_source"},
        {"where": "recommendations[0]", "reason": "gate2"},
        {"where": "caveats[1]", "reason": "uncited_numeral"},
        {"where": "prior_outcomes_commentary", "reason": "gate2"},
        {"where": "title", "reason": "uncited_numeral"},
        {"where": "sections[9].paragraphs[x]", "reason": "gate2"},
    ]
    after = dr.apply_gate2_removals(draft, fails)
    assert [p.text for p in after.sections[0].paragraphs] == ["B [[n1]]"]
    assert [(r.rank, r.target_id) for r in after.recommendations] == [(1, "c2"), (2, "c3")]
    assert after.caveats == ["one"]
    assert after.prior_outcomes_commentary is None
    assert after.title == "Funding review"
    assert len(after.removed) == 7
    assert {"where": "sections[9].paragraphs[x]", "reason": "gate2"} not in after.removed
    org = draft.model_copy(update={"kind": "org_review"})
    assert dr.apply_gate2_removals(org, fails[-2:]).title == "Org review"
    assert dr.apply_gate2_removals(draft, []) == draft


def test_ut06_80_failing_section_title_replaced_and_listed() -> None:
    """UT06-80 (review round 1, TH06-08) a failing section title is replaced by the humanized
    section id and listed once in `removed`; other sections keep their titles."""
    sections = [
        {"id": "executive_summary", "title": "Cut costs 40%", "paragraphs": [d.para()]},
        {"id": "risks_and_caveats", "title": "Risks", "paragraphs": []},
    ]
    draft = _fill(d.writer_out(sections=sections))
    fails = [{"where": "sections[0].title", "reason": "gate2"}] * 2
    after = dr.apply_gate2_removals(draft, fails)
    assert [s.title for s in after.sections] == ["Executive summary", "Risks"]
    assert after.sections[0].paragraphs == draft.sections[0].paragraphs
    assert after.removed == [{"where": "sections[0].title", "reason": "gate2"}]
    assert dr.removable("sections[1].title")
    assert not dr.removable("sections[0].heading")
    assert dr.section_default_title("org_scorecards") == "Org scorecards"
    assert dr.section_default_title("q3_2026") == "Section"


# --- UT06-81


def _cov(tasks: list[Any], findings: list[Any] | None = None, **kw: Any) -> Any:
    args: dict[str, Any] = {"gate2_passed": True, "n_recs": 1, "analysis_exhausted": False,
                            "min_done_fraction": 0.8} | kw  # fmt: skip
    must = args.pop("must", {"candidate:c1"})
    if findings is None:
        findings = [
            finding(1, entity=("candidate", "c1")).model_copy(update={"status": "verified"})
        ]
    return dr.compute_coverage(tasks, findings, must, **args)


def _tasks(n_done: int = 5, n_dead: int = 0, **must_kw: Any) -> list[Any]:
    rows = [d.task_row(i, entity=("candidate", f"c{i + 10}")) for i in range(n_done)]
    rows += [d.task_row(50 + i, status="dead") for i in range(n_dead)]
    rows.append(d.task_row(99, must_cover=True, **must_kw))
    return rows


def test_ut06_81_publishable_baseline_and_counts() -> None:
    """UT06-81 the baseline is publishable; counts exclude crosscheck and round > 0 tasks."""
    tasks = [*_tasks(), d.task_row(70, specialty="crosscheck"), d.task_row(71, round_no=1),
             d.task_row(72, role="skeptic")]  # fmt: skip
    rejected = finding(2).model_copy(update={"status": "rejected"})
    verified = finding(1, entity=("candidate", "c1")).model_copy(update={"status": "verified"})
    cov, partial = _cov(tasks, [verified, rejected])
    assert cov.publishable is True
    assert partial == []
    assert (cov.planned_tasks, cov.done_tasks, cov.dead_tasks) == (6, 6, 0)
    assert (cov.must_cover_total, cov.must_cover_done) == (1, 1)
    assert (cov.verified_findings, cov.rejected_findings) == (1, 1)


@pytest.mark.parametrize(
    "flip",
    ["gate2", "n_recs", "exhausted", "fraction", "must_task", "uncovered", "no_planned"],
)
def test_ut06_81_each_rule_flips_the_verdict(flip: str) -> None:
    """UT06-81 each publishability rule alone flips the verdict to not publishable."""
    tasks, findings, kw = _tasks(), None, {}
    if flip == "gate2":
        kw = {"gate2_passed": False}
    elif flip == "n_recs":
        kw = {"n_recs": 0}
    elif flip == "exhausted":
        kw = {"analysis_exhausted": True}
    elif flip == "fraction":
        tasks = _tasks(n_done=1, n_dead=2)
    elif flip == "must_task":
        tasks = [*_tasks(), d.task_row(98, must_cover=True, status="pending",
                                       entity=("candidate", "c1"))]  # fmt: skip
    elif flip == "uncovered":
        findings = []
    else:
        tasks = [d.task_row(1, role="skeptic")]
    cov, partial = _cov(tasks, findings, **kw)
    assert cov.publishable is False
    assert partial == (["candidate:c1"] if flip == "uncovered" else [])


def test_ut06_81_silent_must_task_covers_and_must_done() -> None:
    """UT06-81 a must-cover task done, not partial and without findings covers its entity; a
    partial one does not; an entity with an open must-cover task is covered but not done."""
    silent = {"partial": False, "finding_ids": []}
    cov, partial = _cov(_tasks(result=silent), [])
    assert cov.publishable is True
    assert partial == []
    cov, partial = _cov(_tasks(result={"partial": True, "finding_ids": []}), [])
    assert partial == ["candidate:c1"]
    open_task = d.task_row(98, must_cover=True, status="running", entity=("candidate", "c1"))
    cov, _ = _cov([*_tasks(), open_task])
    assert (cov.must_cover_done, cov.publishable) == (0, False)


def test_ut06_81_analysis_exhausted_rule() -> None:
    """UT06-81 `analysis_exhausted`: a dead planner/judge/analyst/skeptic on BudgetExceeded."""
    budget = {"class": "BudgetExceeded"}
    assert dr.analysis_exhausted([d.task_row(1, status="dead", last_error=budget)])
    assert dr.analysis_exhausted([d.task_row(1, role="skeptic", status="dead", last_error=budget)])
    assert not dr.analysis_exhausted(
        [d.task_row(1, role="writer", status="dead", last_error=budget)]
    )
    assert not dr.analysis_exhausted([d.task_row(1, status="dead", last_error={"class": "X"})])
    assert not dr.analysis_exhausted([d.task_row(1, status="dead")])


# --- UT06-94


def test_ut06_94_findings_only_draft() -> None:
    """UT06-94 60 verified findings and a fake `verify`: findings_only mode, one executive
    summary of 50 claim paragraphs in priority order, no recommendations, banner partial_run,
    not publishable, the `verify` result stored."""
    findings = [
        finding(i, confidence=(i % 60) / 100, claim=f"Claim {i:02d} had [[n1]] incidents",
                numbers=[number(1, q=i + 1)]).model_copy(update={"status": "verified"})
        for i in range(60)
    ]  # fmt: skip
    seen: list[ReportDraft] = []

    def verify(draft: ReportDraft) -> Any:
        seen.append(draft)
        return d.vresult()

    draft = dr.build_findings_only_draft(
        run_row(), findings, pipeline=d.StubPipeline({"candidate:c1"}),
        plan_ctx=d.plan_ctx(dq=True), extras=DraftExtras(contested=[fid(99)]),
        tasks=[d.task_row(1)], verify=verify,
    )  # fmt: skip
    assert draft.mode == "findings_only"
    assert draft.title == "Funding review"
    assert [s.id for s in draft.sections] == ["executive_summary"]
    assert draft.sections[0].title == "Verified findings"
    paras = draft.sections[0].paragraphs
    assert len(paras) == 50
    assert [p.finding_ids[0] for p in paras] == [fid(i) for i in range(59, 9, -1)]
    assert paras[0].text == findings[59].claim
    assert paras[0].numbers == findings[59].numbers
    assert draft.recommendations == []
    assert draft.ranked_entities == []
    assert draft.caveats == []
    assert draft.prior_outcomes_commentary is None
    assert draft.banners == ["dq_warnings", "partial_run"]
    assert draft.coverage.publishable is False
    assert draft.coverage.verified_findings == 60
    assert draft.flags["partial_coverage"] == ["candidate:c1"]
    assert draft.query_ids == sorted(qid(i + 1) for i in range(10, 60))
    assert draft.contested == [fid(99)]
    assert len(seen) == 1
    assert seen[0].mode == "findings_only"
    assert draft.verification == d.vresult()
