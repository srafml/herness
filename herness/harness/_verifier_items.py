"""Item-level machinery of the Verifier: draft items, marker and ref checks, number checks.

Private sibling of ``herness.harness.verifier`` (impl 05 U05-64 steps 1-2 and 7, U05-67),
split out so ``verifier.py`` stays within its 400-line module budget; only the Verifier
imports it. Every public name here is an implementation detail of ``Verifier``.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass

from herness.core.logging import get_logger
from herness.core.numbers import parse_markers
from herness.core.types import (
    NumberCheck,
    NumberRef,
    Paragraph,
    RecommendationItem,
    ReportDraft,
    VerifiableItem,
)
from herness.harness._verifier_rerun import json_safe

_log = get_logger("harness.verifier")


def actual_value(value: object) -> float | int | str | None:
    """``NumberCheck.actual`` as a JSON-safe scalar (``Decimal`` -> ``str``, step 7)."""
    safe = json_safe(value)
    if isinstance(safe, bool):
        return "true" if safe else "false"
    if safe is None or isinstance(safe, int | float | str):
        return safe
    return json.dumps(safe, sort_keys=True)


def make_check(ref: NumberRef, result: str, actual: object = None) -> NumberCheck:
    """One ``NumberCheck`` for ``ref`` with a JSON-safe ``actual``."""
    return NumberCheck.model_validate(
        {
            "number_id": ref.id,
            "query_id": ref.query_id,
            "column": ref.column,
            "row_key": ref.row_key,
            "claimed": ref.value,
            "actual": actual_value(actual),
            "result": result,
        }
    )


@dataclass(slots=True)
class HashCounts:
    """Per-item counts of the three cell-tolerance outcomes (design 04 §4.4, R-15)."""

    equal: int = 0
    equivalent: int = 0
    values_only: int = 0


def _paragraph_item(where: str, paragraph: Paragraph) -> VerifiableItem:
    return VerifiableItem(
        where=where,
        text=paragraph.text,
        numbers=paragraph.numbers,
        finding_ids=paragraph.finding_ids,
    )


def _recommendation_refs(rec: RecommendationItem) -> dict[str, str]:
    named = {
        "expected_delta_ref": rec.expected_delta_ref,
        "expected_usd_ref": rec.expected_usd_ref,
        "confidence_ref": rec.confidence_ref,
        "effort_usd_ref": rec.effort_usd_ref,
    }
    for m, lever in enumerate(rec.action_levers):
        named[f"action_levers[{m}].delta_usd_ref"] = lever["delta_usd_ref"]
    return {name: ref for name, ref in named.items() if ref is not None}


def draft_items(draft: ReportDraft) -> list[VerifiableItem]:
    """U05-67 item order: title, sections, recommendations, caveats, commentary (D05-21)."""
    items = [VerifiableItem(where="title", text=draft.title, numbers=[])]
    for i, section in enumerate(draft.sections):
        items.append(VerifiableItem(where=f"sections[{i}].title", text=section.title, numbers=[]))
        items.extend(
            _paragraph_item(f"sections[{i}].paragraphs[{j}]", paragraph)
            for j, paragraph in enumerate(section.paragraphs)
        )
    items.extend(
        VerifiableItem(
            where=f"recommendations[{k}]",
            text=f"{rec.headline}\n{rec.summary}",
            numbers=rec.numbers,
            finding_ids=rec.finding_ids,
            refs=_recommendation_refs(rec),
        )
        for k, rec in enumerate(draft.recommendations)
    )
    items.extend(
        VerifiableItem(where=f"caveats[{c}]", text=caveat, numbers=[])
        for c, caveat in enumerate(draft.caveats)
    )
    if draft.prior_outcomes_commentary is not None:
        items.append(_paragraph_item("prior_outcomes_commentary", draft.prior_outcomes_commentary))
    return items


def _named_ref_problems(item: VerifiableItem, by_id: dict[str, NumberRef]) -> list[str]:
    problems: list[str] = []
    for name, ref_id in item.refs.items():
        target = by_id.get(ref_id)
        if target is None:
            problems.append(f"{name}:{ref_id}")
        elif name.endswith("usd_ref") and target.unit != "usd":
            problems.append(f"{name}:not_usd")
    return problems


def marker_problems(item: VerifiableItem) -> tuple[list[str], list[str]]:
    """Steps 1-2: unknown markers, duplicate ids and named refs; logs unused numbers.

    Returns ``(unknown_markers, bad_refs)``; unknown markers are in text order, first
    occurrence only, and include malformed ``[[...]]`` tokens.
    """
    scan = parse_markers(item.text)
    by_id: dict[str, NumberRef] = {}
    bad_refs: list[str] = []
    for number in item.numbers:
        if number.id in by_id and f"duplicate:{number.id}" not in bad_refs:
            bad_refs.append(f"duplicate:{number.id}")
        by_id.setdefault(number.id, number)
    tokens = [(m.start, m.id) for m in scan.markers if m.id not in by_id]
    tokens += [(m.start, m.text) for m in scan.malformed]
    unknown = list(dict.fromkeys(text for _, text in sorted(tokens)))
    bad_refs += _named_ref_problems(item, by_id)
    cited = set(scan.ids)
    unused = [number_id for number_id in by_id if number_id not in cited]
    if unused:
        _log.warning("harness.verifier.number_unused", where=item.where, ids=unused)
    return unknown, bad_refs


def group_by_query(numbers: Sequence[NumberRef]) -> dict[str, list[int]]:
    """Step 5: indexes of ``numbers`` per cited ``query_id``, in first-citation order."""
    groups: dict[str, list[int]] = {}
    for index, number in enumerate(numbers):
        groups.setdefault(number.query_id, []).append(index)
    return groups
