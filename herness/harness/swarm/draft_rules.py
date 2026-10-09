"""Pure draft assembly and gate 2 rules (impl 06 U06-116 to U06-120, U06-142; design 06 §5.8,
§5.9).

`fill_draft` turns the Writer output into a `ReportDraft` with the swarm-owned fields;
`check_recommendation_rules` and `check_extra_text` (private sibling `_draft_checks`, re-exported
here) find what `verify_draft` does not check; `apply_gate2_removals` drops what still fails
after the fix passes; `compute_coverage` is the publishability rule; `build_findings_only_draft`
is the draft of a run whose Writer is dead (R-49). Every function is pure.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel

from herness.core.errors import ConfigError
from herness.core.types import Banner, Coverage, Paragraph, ReportDraft, Section, VerificationResult
from herness.harness.swarm._draft_checks import (
    Failure,
    check_extra_text,
    check_recommendation_rules,
)

if TYPE_CHECKING:
    from herness.core.types import Finding
    from herness.harness.pipelines.base import Pipeline, PlanContext
    from herness.store.ops.runs import RunRow, TaskRow

__all__ = [
    "DEFAULT_TITLES",
    "FINDINGS_ONLY_MAX",
    "DraftExtras",
    "analysis_exhausted",
    "apply_gate2_removals",
    "build_findings_only_draft",
    "check_extra_text",
    "check_recommendation_rules",
    "compute_coverage",
    "empty_verification",
    "fill_draft",
]

DEFAULT_TITLES: Final[Mapping[str, str]] = MappingProxyType(
    {"funding_review": "Funding review", "org_review": "Org review"}
)
FINDINGS_ONLY_MAX: Final = 50
_EXHAUSTIBLE: Final = frozenset({"planner", "judge", "analyst", "skeptic"})
_PARAGRAPH: Final = re.compile(r"sections\[(\d+)\]\.paragraphs\[(\d+)\]")
_INDEXED: Final = re.compile(r"(recommendations|caveats)\[(\d+)\]")
_EMPTY_COVERAGE: Final = Coverage(
    planned_tasks=0, done_tasks=0, dead_tasks=0, must_cover_total=0, must_cover_done=0,
    verified_findings=0, rejected_findings=0, publishable=False,
)  # fmt: skip


@dataclass(frozen=True, slots=True)
class DraftExtras:
    """Swarm-owned draft inputs that are not in the Writer output (U06-116)."""

    contested: list[str] = field(default_factory=list)
    dead_tasks: list[dict[str, object]] = field(default_factory=list)
    flags_notes: list[str] = field(default_factory=list)
    hybrid_fallback: bool = False
    budget_exhausted: bool = False


def empty_verification(build_id: str, at: datetime) -> VerificationResult:
    """A passing result without items: the placeholder until `verify_draft` runs."""
    return VerificationResult(
        build_id=build_id,
        passed=True,
        items=[],
        n_numbers=0,
        n_failed=0,
        verified_at=at,
        duration_ms=0,
    )


# --- U06-116 fill_draft -------------------------------------------------------------------------


def _banners(plan_ctx: PlanContext, extras: DraftExtras) -> list[Banner]:
    """U06-116 step 4: the banners in their fixed order."""
    flags: list[tuple[Banner, bool]] = [
        ("dq_warnings", bool(plan_ctx.dq_warnings)),
        ("unconfirmed_weights", bool(plan_ctx.unconfirmed_weights)),
        ("hybrid_fallback", extras.hybrid_fallback),
        ("budget_exhausted", extras.budget_exhausted),
    ]
    return [name for name, on in flags if on]


def _base(run: RunRow, plan_ctx: PlanContext, extras: DraftExtras) -> dict[str, object]:
    """Swarm-owned fields shared by the full and the findings-only draft."""
    if run.build_id is None:
        msg = f"run {run.run_id} has no build_id"
        raise ConfigError(msg)
    return {
        "run_id": run.run_id, "kind": run.kind, "depth": run.depth, "profile": run.profile,
        "build_id": run.build_id, "portfolio_custom": plan_ctx.portfolio["custom"],
        "banners": _banners(plan_ctx, extras), "contested": list(extras.contested),
        "dead_tasks": list(extras.dead_tasks), "removed": [], "coverage": _EMPTY_COVERAGE,
        "flags": {"partial_coverage": [], "notes": list(extras.flags_notes)},
        "ranked_entities": [],
    }  # fmt: skip


def _finding_qids(ids: Collection[str], findings: Mapping[str, Finding]) -> set[str]:
    return {q for fid in ids if fid in findings for q in findings[fid].query_ids}


def _paragraph_qids(p: Paragraph | None, findings: Mapping[str, Finding]) -> set[str]:
    if p is None:
        return set()
    return {n.query_id for n in p.numbers} | _finding_qids(p.finding_ids, findings)


def fill_draft(  # noqa: PLR0913 - U06-116 signature
    out: BaseModel,
    *,
    run: RunRow,
    pipeline: Pipeline,
    plan_ctx: PlanContext,
    findings: Mapping[str, Finding],
    extras: DraftExtras,
    verification: VerificationResult,
) -> ReportDraft:
    """The `ReportDraft` of one Writer output with the swarm-owned fields (U06-116); pure.

    Recommendation ranks follow the Writer order and their `query_ids` are their numbers' and
    cited findings' queries; the draft `query_ids` are the union over every text object.
    """
    data = out.model_dump(mode="json")
    recs = [
        rec | {"rank": i + 1, "query_ids": sorted(
            {n["query_id"] for n in rec["numbers"]} | _finding_qids(rec["finding_ids"], findings)
        )}
        for i, rec in enumerate(data["recommendations"])
    ]  # fmt: skip
    keys = ("title", "sections", "caveats", "prior_outcomes_commentary")
    draft = ReportDraft.model_validate(
        _base(run, plan_ctx, extras)
        | {key: data[key] for key in keys}
        | {"mode": "full", "recommendations": recs, "query_ids": [],
           "verification": verification}
    )  # fmt: skip
    qids = _paragraph_qids(draft.prior_outcomes_commentary, findings)
    for section in draft.sections:
        for p in section.paragraphs:
            qids |= _paragraph_qids(p, findings)
    for rec in draft.recommendations:
        qids |= set(rec.query_ids)
    draft = draft.model_copy(update={"query_ids": sorted(qids)})
    return draft.model_copy(update={"ranked_entities": pipeline.ranked_entities(draft)})


# --- U06-119 removals ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Drops:
    paragraphs: set[tuple[int, int]] = field(default_factory=set)
    recommendations: set[int] = field(default_factory=set)
    caveats: set[int] = field(default_factory=set)
    commentary: bool = False
    title: bool = False

    def add(self, where: str) -> bool:
        """Record the removal of `where`; False when `where` names no removable item."""
        if (m := _PARAGRAPH.fullmatch(where)) is not None:
            self.paragraphs.add((int(m.group(1)), int(m.group(2))))
        elif (m := _INDEXED.fullmatch(where)) is not None:
            getattr(self, m.group(1)).add(int(m.group(2)))
        elif where == "prior_outcomes_commentary":
            self.commentary = True
        elif where == "title":
            self.title = True
        else:
            return False
        return True


def apply_gate2_removals(draft: ReportDraft, failures: Sequence[Failure]) -> ReportDraft:
    """Drop every item that still fails (U06-119, design 06 §5.8 Gate 2); pure.

    Indexes refer to `draft`; each failure on a removable item is appended once to `removed`
    and recommendation ranks are renumbered 1..n. A failure on another item (a section title)
    removes nothing and is not listed: the final `verify_draft` still fails on it.
    """
    drops, removed = _Drops(), list(draft.removed)
    for f in failures:
        entry = {"where": f["where"], "reason": f["reason"]}
        if drops.add(f["where"]) and entry not in removed:
            removed.append(entry)  # type: ignore[arg-type]  # the `_Removed` TypedDict shape
    sections = [
        Section(id=s.id, title=s.title, paragraphs=[
            p for j, p in enumerate(s.paragraphs) if (i, j) not in drops.paragraphs
        ])
        for i, s in enumerate(draft.sections)
    ]  # fmt: skip
    kept = [r for i, r in enumerate(draft.recommendations) if i not in drops.recommendations]
    update: dict[str, object] = {
        "sections": sections,
        "recommendations": [r.model_copy(update={"rank": k}) for k, r in enumerate(kept, 1)],
        "caveats": [c for i, c in enumerate(draft.caveats) if i not in drops.caveats],
        "removed": removed,
    }
    if drops.commentary:
        update["prior_outcomes_commentary"] = None
    if drops.title:
        update["title"] = DEFAULT_TITLES[draft.kind]
    return draft.model_copy(update=update)


# --- U06-120 coverage ---------------------------------------------------------------------------


def _keys(t: TaskRow) -> set[str]:
    scope = t.spec.scope
    return {f"{scope.entity_type}:{e}" for e in scope.entity_ids}


def _silent_done(t: TaskRow) -> bool:
    """`done`, not partial and posted no finding (U06-120 step 2)."""
    result = t.result or {}
    return t.status == "done" and result.get("partial") is False and not result.get("finding_ids")


def analysis_exhausted(tasks: Sequence[TaskRow]) -> bool:
    """Any planner, judge, analyst or skeptic task `dead` on `BudgetExceeded` (U06-120)."""
    return any(
        t.role in _EXHAUSTIBLE and t.status == "dead"
        and (t.last_error or {}).get("class") == "BudgetExceeded"
        for t in tasks
    )  # fmt: skip


def compute_coverage(  # noqa: PLR0913 - U06-120 signature
    tasks: Sequence[TaskRow],
    findings: Sequence[Finding],
    must_cover: set[str],
    *,
    gate2_passed: bool,
    n_recs: int,
    analysis_exhausted: bool,
    min_done_fraction: float,
) -> tuple[Coverage, list[str]]:
    """The `Coverage` and the sorted uncovered must-cover keys (U06-120, design 06 §5.9); pure."""
    planned = [
        t for t in tasks
        if t.role == "analyst" and t.spec.round == 0 and t.spec.specialty != "crosscheck"
    ]  # fmt: skip
    done = sum(1 for t in planned if t.status == "done")
    must_tasks = [t for t in tasks if t.spec.must_cover]
    seen = {f"{f.entity_type}:{f.entity_id}" for f in findings if f.status == "verified"}
    seen |= {k for t in must_tasks if _silent_done(t) for k in _keys(t)}
    covered = must_cover & seen
    must_done = sum(
        1 for k in covered if all(t.status == "done" for t in must_tasks if k in _keys(t))
    )
    publishable = (
        bool(planned) and done / len(planned) >= min_done_fraction
        and all(t.status == "done" for t in must_tasks) and covered == must_cover
        and gate2_passed and n_recs >= 1 and not analysis_exhausted
    )  # fmt: skip
    coverage = Coverage(
        planned_tasks=len(planned), done_tasks=done,
        dead_tasks=sum(1 for t in tasks if t.status == "dead"),
        must_cover_total=len(must_cover), must_cover_done=must_done,
        verified_findings=sum(1 for f in findings if f.status == "verified"),
        rejected_findings=sum(1 for f in findings if f.status == "rejected"),
        publishable=publishable,
    )  # fmt: skip
    return coverage, sorted(must_cover - covered)


# --- U06-142 findings-only draft ----------------------------------------------------------------


def build_findings_only_draft(  # noqa: PLR0913 - U06-142 signature
    run: RunRow,
    findings: Sequence[Finding],
    *,
    pipeline: Pipeline,
    plan_ctx: PlanContext,
    extras: DraftExtras,
    tasks: Sequence[TaskRow],
    verify: Callable[[ReportDraft], VerificationResult],
) -> ReportDraft:
    """The `draft.json` of a run whose Writer task is dead (U06-142, R-49, TH06-08).

    One `executive_summary` section with one paragraph per verified claim (gate 1 passed), in
    `challenge_priority` order, at most `FINDINGS_ONLY_MAX`; never publishable.
    """
    ordered = sorted(
        findings, key=lambda f: (-pipeline.challenge_priority(f, plan_ctx), f.finding_id)
    )[:FINDINGS_ONLY_MAX]
    paragraphs = [
        Paragraph(text=f.claim, numbers=f.numbers, finding_ids=[f.finding_id]) for f in ordered
    ]
    coverage, partial = compute_coverage(
        tasks, findings, pipeline.must_cover(plan_ctx), gate2_passed=False, n_recs=0,
        analysis_exhausted=analysis_exhausted(tasks),
        min_done_fraction=1.0,  # no effect: gate2_passed=False already makes it unpublishable
    )  # fmt: skip
    base = _base(run, plan_ctx, extras)
    banners = [*base["banners"], "partial_run"]  # type: ignore[misc]  # list of Banner
    draft = ReportDraft.model_validate(
        base | {
            "mode": "findings_only", "title": DEFAULT_TITLES[run.kind],
            "sections": [Section(id="executive_summary", title="Verified findings",
                                 paragraphs=paragraphs)],
            "recommendations": [], "caveats": [], "prior_outcomes_commentary": None,
            "banners": banners, "coverage": coverage,
            "flags": {"partial_coverage": partial, "notes": list(extras.flags_notes)},
            "query_ids": sorted({n.query_id for p in paragraphs for n in p.numbers}),
            "verification": empty_verification(str(base["build_id"]), run.started_at),
        }
    )  # fmt: skip
    return draft.model_copy(update={"verification": verify(draft)})
