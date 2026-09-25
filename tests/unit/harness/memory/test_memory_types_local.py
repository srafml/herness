"""Type-level tests for herness.harness.memory.types (impl 07 U07-11 … U07-17).

The behavioural tests named by those units (UT07-24, 32, 44, 52, 68, 78, 80, 81) belong to
later cards; these functions carry the same IDs and check only the type contracts.
"""

import pickle  # noqa: TID251 - proves multiprocessing transport, as UT00-08 does
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from herness.core import types as shared
from herness.core.errors import NotFound, RecoverableError
from herness.core.types import MemoryItem
from herness.harness.memory import types as mt

pytestmark = pytest.mark.unit

ULID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
NOW = datetime(2026, 9, 1, tzinfo=UTC)


def test_ut07_44_recall_filters_limits() -> None:
    """UT07-44 RecallFilters defaults and limits (type-level part of U07-11)."""
    filters = mt.RecallFilters()
    assert filters.kinds is None
    assert filters.entity_ids == []
    assert filters.min_confidence == 0.0
    assert mt.RecallFilters(kinds=["insight", "glossary"], include_pending_for="a" * 32).kinds
    bad_values: list[dict[str, Any]] = [
        {"kinds": ["insight", "insight"]},
        {"kinds": ["unknown"]},
        {"entity_type": "person"},
        {"entity_ids": ["x"] * 51},
        {"entity_ids": ["x" * 201]},
        {"min_confidence": 1.5},
        {"include_pending_for": "user"},
        {"extra": 1},
    ]
    for bad in bad_values:
        with pytest.raises(ValidationError):
            mt.RecallFilters(**bad)


def test_ut07_24_propose_result_merge_rule() -> None:
    """UT07-24 ProposeResult: merged_into implies memory_id == merged_into (U07-12)."""
    mem = f"mem_{ULID}"
    merged = mt.ProposeResult(
        memory_id=mem, status="active", review_item_id=None, merged_into=mem, flags=["conflict"]
    )
    assert merged.merged_into == mem
    with pytest.raises(ValidationError):
        mt.ProposeResult(
            memory_id=mem, status="active", review_item_id=None, merged_into="mem_x", flags=[]
        )
    with pytest.raises(ValidationError):
        mt.ProposeResult(
            memory_id=mem, status="active", review_item_id=None, merged_into=None, flags=["odd"]
        )


def test_ut07_81_session_context_and_chat_turn() -> None:
    """UT07-81 SessionContext holds ChatTurn messages (type-level part of U07-13)."""
    turn = mt.ChatTurn(message_id="m1", role="user", content="hi", created_at=NOW, query_ids=[])
    ctx = mt.SessionContext(session_id="s1", summary=None, messages=[turn], memory_ids=[])
    assert ctx.messages[0].role == "user"
    with pytest.raises(ValidationError):
        mt.ChatTurn(message_id="m1", role="system", content="x", created_at=NOW, query_ids=[])


def test_ut07_78_promotion_report_counts() -> None:
    """UT07-78 PromotionReport counts are non-negative (type-level part of U07-14)."""
    fields: dict[str, Any] = {
        "run_id": "run_1",
        "queries_seen": 3,
        "skipped_unparsable": 0,
        "templates_created": 1,
        "templates_updated": 0,
        "qa_pairs_created": 1,
        "promoted": [],
        "expired": [],
        "already_processed": False,
    }
    assert mt.PromotionReport(**fields).queries_seen == 3
    with pytest.raises(ValidationError):
        mt.PromotionReport(**{**fields, "templates_created": -1})


def test_ut07_80_export_report_counts() -> None:
    """UT07-80 ExportReport counts are non-negative (type-level part of U07-15)."""
    fields: dict[str, Any] = {
        "export_id": ULID,
        "out_dir": Path("exports"),
        "train_count": 10,
        "val_count": 2,
        "excluded_golden": 1,
        "excluded_low_pass_lb": 0,
        "templates": 4,
        "config_hash": "h",
        "manifest_sha256": "0" * 64,
    }
    assert mt.ExportReport(**fields).out_dir == Path("exports")
    with pytest.raises(ValidationError):
        mt.ExportReport(**{**fields, "val_count": -1})


def test_ut07_52_context_stats_ordering() -> None:
    """UT07-52 ContextStats requires 0 < target < soft < hard < budget (U07-16)."""
    stats = mt.ContextStats(tokens=10, exact=True, budget=100, soft=70, hard=85, target=50)
    assert stats.hard == 85
    bad_orders = [(100, 70, 85, 0), (100, 70, 85, 70), (100, 90, 85, 50), (85, 70, 85, 50)]
    for budget, soft, hard, target in bad_orders:
        with pytest.raises(ValidationError):
            mt.ContextStats(
                tokens=0, exact=False, budget=budget, soft=soft, hard=hard, target=target
            )


def test_ut07_32_memory_not_found_fields() -> None:
    """UT07-32 MemoryNotFound message, attributes, details and pickling (U07-17)."""
    exc = mt.MemoryNotFound("memory_item", f"mem_{ULID}")
    assert isinstance(exc, NotFound)
    assert isinstance(exc, RecoverableError)
    assert str(exc) == f"memory_item not found: mem_{ULID}"
    assert (exc.kind, exc.ident) == ("memory_item", f"mem_{ULID}")
    assert dict(exc.details) == {"kind": "memory_item", "ident": f"mem_{ULID}"}
    clone = pickle.loads(pickle.dumps(exc))  # noqa: S301 - multiprocessing transport
    assert (clone.kind, clone.ident, str(clone)) == (exc.kind, exc.ident, str(exc))


def test_ut07_68_id_patterns_and_reexports() -> None:
    """UT07-68 ID patterns match the shared types and the shared names are re-exported."""
    assert mt.MEMORY_ID_RE.fullmatch(f"mem_{ULID}")
    assert mt.REC_ID_RE.fullmatch(f"rec_{ULID}")
    assert mt.FINDING_ID_RE.fullmatch(f"fnd_{ULID}")
    assert mt.QUERY_ID_RE.fullmatch("q_0123456789abcdef")
    assert not mt.MEMORY_ID_RE.fullmatch(f"mem_{ULID[:-1]}I")
    field = MemoryItem.model_fields["memory_id"]
    assert any(getattr(m, "pattern", None) == mt.MEMORY_ID_RE.pattern for m in field.metadata)
    for name in ("Layer", "Kind", "Status", "KIND_LAYER", "Provenance", "MemoryItem",
                 "MemoryProposal", "RecallHit", "MemoryRunContext", "RecommendationDraft",
                 "PriorRecommendation", "PriorContext", "ConfidenceAdjustment",
                 "SimilarOutcome"):  # fmt: skip
        assert getattr(mt, name) is getattr(shared, name)
        assert name in mt.__all__
