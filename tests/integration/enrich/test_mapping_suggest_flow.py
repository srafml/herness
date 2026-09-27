"""IT03-14: mapping suggestions through a real ops store and a real core rebuild (F03-12).

Stage `suggest` writes `pending` items only; one is approved through impl 02's
`decide_review_item`; the rebuild's `core.service_map` holds a `suggested_approved` row for
that item and for no other suggestion (U03-114, T03-27).
"""

from __future__ import annotations

import datetime

import pytest
from tests.integration.enrich._mapping_build import (
    MAP_SQL,
    SVC,
    TEAM,
    rebuild,
    suggest,
    write_lake,
)
from tests.support.build_harness import BuildHarness
from tests.support.ops_store import OpsStoreHandle

from herness.store.ops import decide_review_item, list_review_items

pytestmark = pytest.mark.integration

USER = "ab" * 16
NOW = datetime.datetime(2026, 9, 26, 10, tzinfo=datetime.UTC)


@pytest.mark.usefixtures("redaction_on")
def test_it03_14_only_approved_suggestion_reaches_service_map(
    ops_store: OpsStoreHandle, build_harness: BuildHarness
) -> None:
    """IT03-14 suggestions, one approved via ops, rebuild: no `core.service_map` row with
    `suggested_approved` without an approved item."""
    write_lake(build_harness)
    before = rebuild(build_harness)
    assert [r for r in before if r[5] == "suggested_approved"] == []

    report = suggest(build_harness)

    pending = list_review_items(kind="mapping_suggestion", status="pending", limit=100)
    assert report.rows == len(pending) >= 2
    assert all(it.status == "pending" for it in pending)
    pairs = {(it.payload["team_id"], it.payload["service_id"]) for it in pending}
    assert (TEAM + "t2", SVC + "s2") in pairs
    assert (TEAM + "t3", SVC + "s3") in pairs
    assert all(team != TEAM + "t1" for team, _ in pairs)  # t1 is mapped by the CMDB
    assert build_harness.query(MAP_SQL) == before  # the stage never writes core.service_map
    chosen = next(it for it in pending if it.payload["team_id"] == TEAM + "t2"
                  and it.payload["service_id"] == SVC + "s2")  # fmt: skip
    decide_review_item(chosen.item_id, "approved", decided_by=USER, now=NOW)

    after = rebuild(build_harness)

    suggested = [r for r in after if r[5] == "suggested_approved"]
    assert suggested == [(SVC + "s2", TEAM + "t2", None, None, "support", "suggested_approved")]
    others = pairs - {(TEAM + "t2", SVC + "s2")}
    assert all((svc, team) not in {(r[0], r[1]) for r in after} for team, svc in others)
    assert [r for r in after if r[5] != "suggested_approved"] == before

    again = suggest(build_harness)  # t2 is mapped now; nothing is re-emitted
    assert again.rows == 0
