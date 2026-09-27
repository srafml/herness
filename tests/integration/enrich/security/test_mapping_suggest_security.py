"""ST03-12 (TH03-10): a high-score mapping suggestion never changes mappings by itself.

Stage `suggest` over a real core build and a real ops store: the best-scoring pair becomes a
`pending` item only, and `core.service_map` is identical before the stage, after it and after
the next build (U03-114, T03-27).
"""

from __future__ import annotations

import pytest
from tests.integration.enrich._mapping_build import MAP_SQL, SVC, TEAM, rebuild, suggest, write_lake
from tests.support.build_harness import BuildHarness
from tests.support.ops_store import OpsStoreHandle

from herness.store.ops import approved_mapping_suggestions, list_review_items

pytestmark = pytest.mark.integration


@pytest.mark.usefixtures("redaction_on")
def test_st03_12_high_score_suggestion_stays_pending(
    ops_store: OpsStoreHandle, build_harness: BuildHarness
) -> None:
    """ST03-12 high-score mapping suggestion: only a `pending` item; `core.service_map`
    unchanged after the build."""
    write_lake(build_harness)
    before = rebuild(build_harness)

    suggest(build_harness)

    items = list_review_items(kind="mapping_suggestion", limit=100)
    top = next(it for it in items if it.payload["team_id"] == TEAM + "t2"
               and it.payload["service_id"] == SVC + "s2")  # fmt: skip
    assert float(str(top.payload["score"])) >= 0.8
    assert {it.status for it in items} == {"pending"}
    assert all(it.decided_at is None and it.decided_by is None for it in items)
    assert approved_mapping_suggestions() == []
    assert build_harness.query(MAP_SQL) == before
    assert rebuild(build_harness) == before
    assert not any(r[5] == "suggested_approved" for r in before)
