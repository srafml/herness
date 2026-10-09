"""Recommendation rules and extra-text checks before gate 2 (impl 06 U06-117, U06-118).

Private sibling of `herness.harness.swarm.draft_rules`, which re-exports
`check_recommendation_rules` and `check_extra_text` (T06-19 split). Both are pure; stray
numerals come from the shared scanner (R-16).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from herness.core.numbers import find_uncited, parse_markers

if TYPE_CHECKING:
    from herness.core.types import NumberRef, RecommendationItem, ReportDraft

__all__ = ["Failure", "check_extra_text", "check_recommendation_rules"]

type Failure = dict[str, str]


# --- U06-117 recommendation rules ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Refs:
    """Number lookup of one recommendation and the evidence tables of its queries."""

    by_id: Mapping[str, NumberRef]
    tables_of: Callable[[str], frozenset[str]]

    def get(self, ref_id: str | None) -> NumberRef | None:
        return None if ref_id is None else self.by_id.get(ref_id)

    def cites(self, ref_id: str | None, column: str, table: str | None) -> bool:
        n = self.get(ref_id)
        if n is None or n.column != column:
            return False
        return table is None or table in self.tables_of(n.query_id)


def _fund_rules(r: RecommendationItem, refs: _Refs, portfolio_qids: frozenset[str]) -> list[str]:
    exp = refs.get(r.expected_usd_ref)
    usd_ok = exp is not None and (
        refs.cites(r.expected_usd_ref, "expected_impact_usd", "score.portfolio")
        or exp.query_id in portfolio_qids
        or refs.cites(r.expected_usd_ref, "addressable_pain_usd", "score.funding")
    )
    checks = [
        ("expected_usd_source", usd_ok),
        ("effort_source", refs.cites(r.effort_usd_ref, "effort_cost_usd", "score.funding")),
        ("confidence_source", refs.cites(r.confidence_ref, "confidence", "score.funding")),
    ]
    return [name for name, ok in checks if not ok]


def _org_rules(
    r: RecommendationItem, refs: _Refs, levers: Sequence[Mapping[str, object]]
) -> list[str]:
    metrics = {row.get("metric") for row in levers if row.get("entity_id") == r.target_id}
    lever_refs = [lever["delta_usd_ref"] for lever in r.action_levers]
    sourced = all(refs.cites(ref, "delta_usd", "score.action_lever") for ref in lever_refs)
    values = [Decimal(str(n.value)) for ref in lever_refs if (n := refs.get(ref)) is not None]
    exp = refs.get(r.expected_usd_ref)
    top = (
        exp is not None and bool(values) and refs.cites(r.expected_usd_ref, "delta_usd", None)
        and Decimal(str(exp.value)) >= max(values)
    )  # fmt: skip
    checks = [
        ("expected_metric", r.expected_metric is not None and r.expected_metric in metrics),
        ("lever_source", sourced),
        ("expected_usd_top_lever", top),
    ]
    return [name for name, ok in checks if not ok]


def check_recommendation_rules(
    draft: ReportDraft,
    *,
    statuses: Mapping[str, str],
    tables_of: Callable[[str], frozenset[str]],
    portfolio_query_ids: frozenset[str],
    levers: Sequence[Mapping[str, object]],
) -> list[Failure]:
    """Recommendation rule violations before gate 2 (U06-117, TH06-08); pure.

    One `{"where": "recommendations[i]", "reason": "rule:<name>"}` per broken rule.
    """
    out: list[Failure] = []
    for i, r in enumerate(draft.recommendations):
        refs = _Refs({n.id: n for n in r.numbers}, tables_of)
        if r.kind == "fund":
            names = _fund_rules(r, refs, portfolio_query_ids)
        else:
            names = _org_rules(r, refs, levers)
        if any(statuses.get(fid) != "verified" for fid in r.finding_ids):
            names.append("unverified_finding")
        out += [{"where": f"recommendations[{i}]", "reason": f"rule:{name}"} for name in names]
    return out


# --- U06-118 extra text -------------------------------------------------------------------------


def _marker_ids(text: str) -> tuple[set[str], bool]:
    """Valid marker ids of `text` and whether it holds a malformed marker."""
    scan = parse_markers(text)
    return set(scan.ids), bool(scan.malformed)


def check_extra_text(draft: ReportDraft, allowed: Sequence[re.Pattern[str]]) -> list[Failure]:
    """Markers and stray numerals in the title, headlines and caveats (U06-118); pure."""
    out: list[Failure] = []
    ids, bad = _marker_ids(draft.title)
    if ids or bad or find_uncited(draft.title, allowed):
        out.append({"where": "title", "reason": "uncited_numeral"})
    for i, r in enumerate(draft.recommendations):
        ids, bad = _marker_ids(r.headline)
        unknown = ids - {n.id for n in r.numbers}
        if unknown or bad or find_uncited(r.headline, allowed):
            out.append({"where": f"recommendations[{i}]", "reason": "headline"})
    for i, caveat in enumerate(draft.caveats):
        ids, bad = _marker_ids(caveat)
        if ids or bad:
            out.append({"where": f"caveats[{i}]", "reason": "marker_in_caveat"})
        elif find_uncited(caveat, allowed):
            out.append({"where": f"caveats[{i}]", "reason": "uncited_numeral"})
    return out
