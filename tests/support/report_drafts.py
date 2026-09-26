"""Minimal report drafts for renderer tests (impl 09 T09-05) until T09-11 adds fixture files.

``draft_dict`` returns a JSON-ready ``ReportDraft`` dict that passes validation; tests override
fields with keyword arguments or bypass validation with ``model_copy(update=...)``.
"""

from __future__ import annotations

from typing import Any, Final

from herness.core.types import ReportDraft

ULID: Final = "01J9ZQ4Y8M6V3K2N1P0R5T7W9X"
RUN_ID: Final = f"run_{ULID}"
FINDING_ID: Final = f"fnd_{ULID}"
REC_ID: Final = f"rec_{ULID}"
Q1: Final = "q_0123456789abcdef"
Q2: Final = "q_fedcba9876543210"


def number_ref(
    ref_id: str = "n1", unit: str = "count", value: object = 3, query_id: str = Q1
) -> dict[str, Any]:
    """One ``NumberRef`` dict."""
    return {
        "id": ref_id, "value": value, "unit": unit, "query_id": query_id, "column": "n",
        "row_key": None,
    }  # fmt: skip


def paragraph(text: str = "Incidents rose to [[n1]]", **overrides: Any) -> dict[str, Any]:
    """One paragraph citing ``n1``."""
    return {"text": text, "numbers": [number_ref()], "finding_ids": [FINDING_ID]} | overrides


def recommendation(rank: int = 1, **overrides: Any) -> dict[str, Any]:
    """One recommendation with a usd ref, a confidence ref and one lever."""
    return {
        "rank": rank, "kind": "fund", "target_type": "epic", "target_id": f"epic-{rank}",
        "headline": "Fund the cache rewrite", "summary": "It saves [[n2]] a year",
        "numbers": [number_ref(), number_ref("n2", "usd", "1200.50", Q2)],
        "expected_usd_ref": "n2", "confidence_ref": "n1",
        "action_levers": [
            {"entity_type": "team", "entity_id": "t1", "metric": "mttr", "delta_usd_ref": "n2"}
        ],
        "finding_ids": [FINDING_ID], "query_ids": [Q1],
    } | overrides  # fmt: skip


def _verification() -> dict[str, Any]:
    return {
        "build_id": "b1", "passed": True, "items": [], "n_numbers": 0, "n_failed": 0,
        "verified_at": "2026-09-25T12:00:00Z", "duration_ms": 0,
    }  # fmt: skip


def draft_dict(**overrides: Any) -> dict[str, Any]:
    """A valid funding-review draft dict: one section, one recommendation, one caveat."""
    return {
        "run_id": RUN_ID, "kind": "funding_review", "depth": "standard", "profile": "local",
        "build_id": "b1", "title": "Funding review",
        "sections": [
            {"id": "executive_summary", "title": "Summary", "paragraphs": [paragraph()]},
        ],
        "recommendations": [recommendation(1)],
        "ranked_entities": [{"rank": 1, "entity_type": "candidate", "entity_id": "epic-1"}],
        "caveats": ["Data covers ninety days"], "prior_outcomes_commentary": None,
        "banners": [], "flags": {}, "contested": [], "removed": [],
        "coverage": {
            "planned_tasks": 1, "done_tasks": 1, "dead_tasks": 0, "must_cover_total": 0,
            "must_cover_done": 0, "verified_findings": 1, "rejected_findings": 0,
            "publishable": True,
        },
        "dead_tasks": [], "query_ids": [Q1], "verification": _verification(),
    } | overrides  # fmt: skip


def make_draft(**overrides: Any) -> ReportDraft:
    """``draft_dict(**overrides)`` validated into a ``ReportDraft``."""
    return ReportDraft.model_validate(draft_dict(**overrides))
