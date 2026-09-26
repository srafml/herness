"""Unit tests for herness.enrich.review_items (U03-147 … U03-149; UT03-136 … UT03-138).

Uses the real migrated `ops_store` fixture (T11-40) and impl 02's `create_review_item` /
`decide_review_item` to seed rows, so paging, idempotent creation and open counts are
exercised against the actual `review_item` table, never a fake.
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path
from typing import Any

import pytest
from tests.support.ops_store import OpsStoreHandle

from herness.core.errors import ConfigError
from herness.enrich import review_items
from herness.store.ops import ReviewItem, create_review_item, decide_review_item
from herness.store.ops import shared as ops_shared

pytestmark = pytest.mark.unit

T0 = datetime.datetime(2026, 9, 26, 10, 0, tzinfo=datetime.UTC)
USER = "ab" * 16  # a 32-hex user_ref (spec 09)
_ENRICH_ROOT = Path(__file__).resolve().parents[3] / "herness" / "enrich"
_SQL_RE = re.compile(r"\b(FROM|INTO|UPDATE)\s+review_item\b", re.IGNORECASE)


@pytest.fixture(autouse=True)
def _no_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    """`decide_review_item` audits under the caller's config; these tests need no audit trail."""

    def fake(event: str, actor: str, **fields: object) -> None:
        return None

    monkeypatch.setattr(ops_shared, "audit", fake)


def _t(seconds: int) -> datetime.datetime:
    return T0 + datetime.timedelta(seconds=seconds)


def test_ut03_136_iter_review_items_pages_and_orders(
    ops_store: OpsStoreHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-136: 1,203 pending items page in 3 calls, in created_at/item_id order."""
    expected = [create_review_item("label_check", {"i": i}, now=_t(i)) for i in range(1_203)]
    for i in range(5):
        item_id = create_review_item("label_check", {"i": "approved" + str(i)}, now=_t(2_000 + i))
        decide_review_item(item_id, "approved", decided_by=USER, now=_t(3_000 + i))

    calls: list[int] = []
    real_list_review_items = review_items.list_review_items

    def spy(**kwargs: Any) -> list[ReviewItem]:
        calls.append(1)
        return real_list_review_items(**kwargs)

    monkeypatch.setattr(review_items, "list_review_items", spy)

    got = list(review_items.iter_review_items("label_check", "pending", page_size=500))

    assert [it.item_id for it in got] == expected
    assert len(calls) == 3


def test_ut03_136_page_size_precondition(ops_store: OpsStoreHandle) -> None:
    """UT03-136: page_size outside 1..5,000 raises ConfigError, never touching the store."""
    with pytest.raises(ConfigError):
        next(iter(review_items.iter_review_items("label_check", "pending", page_size=0)))
    with pytest.raises(ConfigError):
        next(iter(review_items.iter_review_items("label_check", "pending", page_size=5_001)))


def test_ut03_136_no_review_item_sql_in_enrich() -> None:
    """UT03-136: no module under herness/enrich issues SQL against review_item directly."""
    hits = [
        f"{path}:{lineno}"
        for path in _ENRICH_ROOT.rglob("*.py")
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if _SQL_RE.search(line)
    ]
    assert hits == []


def test_ut03_137_create_if_absent_idempotent(ops_store: OpsStoreHandle) -> None:
    """UT03-137: idempotent creation with scope; a rerun creates nothing more."""
    create_review_item("label_check", {"key": "K1", "question_set_version": "A"}, now=_t(0))
    k2 = create_review_item("label_check", {"key": "K2", "question_set_version": "A"}, now=_t(1))
    decide_review_item(k2, "rejected", decided_by=USER, now=_t(2))
    create_review_item("label_check", {"key": "K3", "question_set_version": "B"}, now=_t(3))

    payloads: list[dict[str, object]] = [
        {"key": "K1", "question_set_version": "A"},
        {"key": "K2", "question_set_version": "A"},
        {"key": "K3", "question_set_version": "A"},
        {"key": "K4", "question_set_version": "A"},
        {"key": "K4", "question_set_version": "A"},
    ]

    created, suppressed = review_items.create_if_absent(
        "label_check",
        payloads,
        match_keys=("key",),
        blocking_statuses=("pending", "rejected"),
        scope={"question_set_version": "A"},
        now=_t(10),
    )
    assert (created, suppressed) == (2, 3)

    pending_a = list(
        review_items.iter_review_items(
            "label_check", "pending", payload_match={"question_set_version": "A"}
        )
    )
    assert {item.payload["key"] for item in pending_a} == {"K1", "K3", "K4"}
    assert all(item.status == "pending" for item in pending_a)

    created2, suppressed2 = review_items.create_if_absent(
        "label_check",
        payloads,
        match_keys=("key",),
        blocking_statuses=("pending", "rejected"),
        scope={"question_set_version": "A"},
        now=_t(20),
    )
    assert created2 == 0
    assert suppressed2 == 5


def test_ut03_137_empty_match_keys(ops_store: OpsStoreHandle) -> None:
    """UT03-137: an empty match_keys raises ConfigError before any write."""
    with pytest.raises(ConfigError):
        review_items.create_if_absent(
            "label_check", [{"key": "K1"}], match_keys=(), blocking_statuses=("pending",), now=T0
        )


def test_ut03_137_empty_blocking_statuses(ops_store: OpsStoreHandle) -> None:
    """UT03-137: empty blocking_statuses raises ConfigError before any write."""
    with pytest.raises(ConfigError):
        review_items.create_if_absent(
            "label_check", [{"key": "K1"}], match_keys=("key",), blocking_statuses=(), now=T0
        )


def test_ut03_137_too_many_keys(ops_store: OpsStoreHandle) -> None:
    """UT03-137: more than 8 match_keys and scope keys together raises ConfigError."""
    match_keys = tuple(f"k{i}" for i in range(6))
    scope = {f"s{i}": "v" for i in range(3)}
    payload: dict[str, object] = {**dict.fromkeys(match_keys, "x"), **scope}
    with pytest.raises(ConfigError):
        review_items.create_if_absent(
            "label_check",
            [payload],
            match_keys=match_keys,
            blocking_statuses=("pending",),
            scope=scope,
            now=T0,
        )


def test_ut03_137_missing_match_key_field(ops_store: OpsStoreHandle) -> None:
    """UT03-137: a payload missing a match_keys field raises ConfigError, no value leaked."""
    with pytest.raises(ConfigError) as exc:
        review_items.create_if_absent(
            "label_check",
            [{"other": "x"}],
            match_keys=("key",),
            blocking_statuses=("pending",),
            now=T0,
        )
    assert "x" not in str(exc.value)


def test_ut03_137_scope_mismatch(ops_store: OpsStoreHandle) -> None:
    """UT03-137: a payload whose scope field disagrees with scope raises ConfigError."""
    with pytest.raises(ConfigError):
        review_items.create_if_absent(
            "label_check",
            [{"key": "K1", "question_set_version": "B"}],
            match_keys=("key",),
            blocking_statuses=("pending",),
            scope={"question_set_version": "A"},
            now=T0,
        )


def test_ut03_137_none_match_value(ops_store: OpsStoreHandle) -> None:
    """UT03-137: a None match_keys value is accepted and matched consistently across calls."""
    payloads: list[dict[str, object]] = [{"key": "K1", "subject": None}]
    created, suppressed = review_items.create_if_absent(
        "label_check",
        payloads,
        match_keys=("key", "subject"),
        blocking_statuses=("pending",),
        now=_t(0),
    )
    assert (created, suppressed) == (1, 0)
    created2, suppressed2 = review_items.create_if_absent(
        "label_check",
        payloads,
        match_keys=("key", "subject"),
        blocking_statuses=("pending",),
        now=_t(1),
    )
    assert (created2, suppressed2) == (0, 1)


def test_ut03_138_open_label_counts(ops_store: OpsStoreHandle) -> None:
    """UT03-138: counts pending label_check items by question, for qsv and purposes."""
    for i in range(3):
        create_review_item(
            "label_check",
            {"question": "q1", "question_set_version": "A", "purpose": "spot_check"},
            now=_t(i),
        )
    create_review_item(
        "label_check",
        {"question": "q1", "question_set_version": "A", "purpose": "gold"},
        now=_t(10),
    )
    for i in range(2):
        create_review_item(
            "label_check",
            {"question": "q2", "question_set_version": "B", "purpose": "spot_check"},
            now=_t(20 + i),
        )

    counts = review_items.open_label_counts(qsv="A", purposes=frozenset({"spot_check"}))
    assert counts == {"q1": 3}


def test_ut03_138_open_label_counts_absent_default(ops_store: OpsStoreHandle) -> None:
    """UT03-138: a question with no pending item is absent from the result (default 0)."""
    counts = review_items.open_label_counts(qsv="Z", purposes=frozenset({"spot_check"}))
    assert counts == {}
    assert counts.get("q1", 0) == 0
