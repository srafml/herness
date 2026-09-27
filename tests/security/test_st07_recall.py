"""ST07-06, ST07-08, ST07-13: recall visibility and FTS5 injection (TH07-06, TH07-08, TH07-13).

Runs on a fresh migrated ops store with real FTS5; the vector index is a local fake that can
lie about statuses (stale LanceDB rows) or ignore its prefilter entirely.
"""

import hashlib
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pytest

from herness.core import time as clock
from herness.core.redact import RedactionResult
from herness.harness.memory.recall import MemoryRecaller, RecallResult, fts_query_string
from herness.harness.memory.settings import MemoryConfig
from herness.harness.memory.types import RecallFilters
from herness.store import ops
from herness.store.ops import core
from herness.store.vectors import EMBEDDING_DIM

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
USER_A = "a" * 32
USER_B = "b" * 32
ZWSP = chr(0x200B)


def _vec(text: str) -> np.ndarray:
    seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big")
    raw = np.random.default_rng(seed).standard_normal(EMBEDDING_DIM)
    return (raw / np.linalg.norm(raw)).astype(np.float32)


class _Embedder:
    def embed(self, text: str) -> np.ndarray:
        return _vec("q")  # every query is maximally similar to every indexed row


class _LyingVectors:
    """Returns every indexed id for any search: statuses and layers are not trusted."""

    def __init__(self) -> None:
        self.ids: list[str] = []

    def search(
        self, vector: np.ndarray, layers: Sequence[str], statuses: Sequence[str], limit: int
    ) -> list[tuple[str, float]]:
        return [(i, 0.0) for i in self.ids][:limit]

    def vectors(self, memory_ids: Sequence[str]) -> dict[str, np.ndarray]:
        return {i: _vec("q") for i in memory_ids if i in self.ids}


class _Redactor:
    def redact(self, text: str | None) -> RedactionResult | None:
        return None if text is None else RedactionResult(text, {})


@pytest.fixture
def vectors(ops_store: Any) -> _LyingVectors:
    return _LyingVectors()


def _recaller(vectors: _LyingVectors) -> MemoryRecaller:
    return MemoryRecaller(
        MemoryConfig(), conn_factory=core.connection, vectors=vectors,  # type: ignore[arg-type]
        embedder=_Embedder(), redactor=_Redactor(),  # type: ignore[arg-type]
    )  # fmt: skip


def _put(vectors: _LyingVectors | None, n: int, content: str, **over: Any) -> str:
    memory_id = f"mem_{n:026d}"
    row: dict[str, Any] = {
        "memory_id": memory_id, "layer": "semantic", "kind": "insight", "content": content,
        "data": {}, "confidence": 0.9, "status": "active",
        "provenance": {"author_type": "system", "author_role": None, "author_ref": None,
                       "run_id": None, "task_id": None, "via": "cli"},
        "created_at": clock.format_utc(NOW - timedelta(days=1)), "expires_at": None,
        "last_used_at": None, "use_count": 0,
    }  # fmt: skip
    row.update(over)
    ops.insert_memory_item(row)  # type: ignore[arg-type]
    if vectors is not None:
        vectors.ids.append(memory_id)
    return memory_id


def _ids(result: RecallResult) -> set[str]:
    return {hit.item.memory_id for hit in result.hits}


def _pending(user: str) -> dict[str, Any]:
    return {"status": "pending_approval",
            "provenance": {"author_type": "human", "author_role": None, "author_ref": user,
                           "run_id": None, "task_id": None, "via": "chat"}}  # fmt: skip


def test_st07_06_other_users_pending_items_absent(vectors: _LyingVectors) -> None:
    """ST07-06 A's pending item is absent for user B's chat and for review pipelines."""
    a_item = _put(vectors, 1, "churn correction from user a", **_pending(USER_A))
    shared = _put(vectors, 2, "churn shared fact")
    rec = _recaller(vectors)
    b_chat = rec.recall("churn", filters=RecallFilters(include_pending_for=USER_B), now=NOW)
    review = rec.recall("churn", now=NOW)
    for result in (b_chat, review):
        assert a_item not in _ids(result)
        assert shared in _ids(result)
    a_chat = rec.recall("churn", filters=RecallFilters(include_pending_for=USER_A), now=NOW)
    assert a_item in _ids(a_chat)
    assert all(h.unconfirmed == (h.item.memory_id == a_item) for h in a_chat.hits)


def test_st07_06_agent_pending_item_needs_matching_author_ref(vectors: _LyingVectors) -> None:
    """ST07-06 a pending item with no author_ref is never shown, whoever asks."""
    prov = {"author_type": "agent", "author_role": "analyst", "author_ref": None,
            "run_id": "run_" + "0" * 26, "task_id": None, "via": "tool"}  # fmt: skip
    item = _put(vectors, 1, "churn agent proposal", status="pending_approval", provenance=prov)
    rec = _recaller(vectors)
    for user in (None, USER_A, USER_B):
        flt = RecallFilters(include_pending_for=user)
        assert item not in _ids(rec.recall("churn", filters=flt, now=NOW))


ATTACKS = [
    "churn OR *",
    "*",
    '"',
    'churn" OR "x',
    "NEAR(churn apple, 5)",
    "content:apple",
    "{content} : apple",
    "churn AND NOT banana",
    "^churn",
    "-banana",
    "churn*",
    "apple + banana",
    "(((",
    "churn\x00 OR apple",
    "c" + chr(0x308) + "hurn " + chr(0x430) + "pple",  # combining mark, Cyrillic a
    "".join(chr(ord(c) + 0xFEE0) for c in "apple"),  # fullwidth apple -> NFKC apple
    ZWSP + "churn" + ZWSP,
    "' OR 1=1 --",
    "memory_fts MATCH '*'",
]


@pytest.mark.parametrize("attack", ATTACKS)
def test_st07_08_fts_operators_no_error_no_widening(ops_store: Any, attack: str) -> None:
    """ST07-08 FTS operators, *, NEAR, column filters and quotes: no error, no widening."""
    rows = {
        _put(None, 1, "churn rose sharply"): {"churn", "rose", "sharply"},
        _put(None, 2, "apple orchard metrics"): {"apple", "orchard", "metrics"},
        _put(None, 3, "banana supply note"): {"banana", "supply", "note"},
    }
    match = fts_query_string(attack)
    assert match is None or re.fullmatch(r'"\w+"( OR "\w+")*', match)
    tokens = set(re.findall(r'"(\w+)"', match or ""))
    if match is not None:
        found = ops.fts_candidates(match, layers=["semantic"], statuses=["active"], limit=50)
        assert {m for m, _ in found} <= {m for m, words in rows.items() if words & tokens}
    result = _recaller(_LyingVectors()).recall(attack, now=NOW)
    assert result.degraded is False
    assert _ids(result) <= {m for m, words in rows.items() if words & tokens}


def test_st07_08_tokenless_query_searches_nothing(ops_store: Any) -> None:
    """ST07-08 a query of operators only builds no MATCH expression and returns no hits."""
    _put(None, 1, "churn rose sharply")
    assert fts_query_string('* "" () : ^ -') is None
    assert _recaller(_LyingVectors()).recall('* "" () : ^ -', now=NOW).hits == []


@pytest.mark.parametrize(
    ("label", "over"),
    [
        ("rejected", {"status": "rejected"}),
        ("expired_status", {"status": "expired"}),
        ("candidate", {"status": "candidate"}),
        ("past_expiry", {"expires_at": clock.format_utc(NOW - timedelta(minutes=1))}),
        ("wrong_layer", {"layer": "episodic", "kind": "outcome_summary"}),
    ],
)
def test_st07_13_vector_active_sqlite_not_visible(
    vectors: _LyingVectors, label: str, over: dict[str, Any]
) -> None:
    """ST07-13 the vector index says active, SQLite says otherwise: not recalled."""
    stale = _put(vectors, 1, "churn stale row", **over)
    live = _put(vectors, 2, "churn live row")
    result = _recaller(vectors).recall("churn", ["semantic"], k=50, now=NOW)
    assert stale not in _ids(result), label
    assert live in _ids(result)
    assert result.n_candidates == 2
