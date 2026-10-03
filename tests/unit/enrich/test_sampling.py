"""Tests for herness.enrich.sampling (U03-120 ... U03-123; UT03-115 ... UT03-118, PT03-15)."""

from __future__ import annotations

import hashlib
from collections import Counter
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import duckdb
import numpy as np
import pyarrow as pa
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.unit.enrich._sampling_support import (
    T0,
    Rec,
    many,
    no_vectors,
    proto_reader,
    snapshot,
    text_hash,
    warehouse,
)

from herness.core.errors import SchemaViolation, StoreBusy
from herness.core.types import Question, QuestionSet
from herness.enrich import sampling
from herness.enrich.sampling import allocate, select_active, stratified_sample, stratum_of

pytestmark = pytest.mark.unit

QSV = "qs-2026-10-01"


def _qs(*applies: str) -> QuestionSet:
    q = Question.model_validate(
        {
            "id": "q_a",
            "type": "bool",
            "instructions": "Is this a thing?",
            "threshold": 0.7,
            "applies_to": applies or ("incident",),
        }
    )
    return QuestionSet(version=QSV, questions=(q,))


def _sample(wh: duckdb.DuckDBPyConnection, size: int, **kw: object) -> pa.Table:
    args: dict[str, object] = {
        "qs": _qs(),
        "size": size,
        "exclude_hashes": frozenset(),
        "salt": "train:v1",
        "snapshot": None,
        "vector_reader": no_vectors,
    } | kw
    return stratified_sample(wh, **args)  # type: ignore[arg-type]


# --- UT03-115: stratum key, Python vs SQL -----------------------------------------------------


def _edge_records() -> list[Rec]:
    """Band, length, quarter and service edges, plus 51 services so one falls out of the top 50."""
    edges: list[Rec] = []
    priorities = (1, 2, 3, 4, 5, None)
    lengths = (0, 1, 119, 120, 121, 599, 600, 601, 2000)
    stamps = (
        datetime(2025, 12, 31, 23, 59, 59, tzinfo=UTC),
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 3, 31, 23, 59, 59, tzinfo=UTC),
        datetime(2026, 4, 1, tzinfo=UTC),
        datetime(2026, 7, 1, 2, tzinfo=timezone(timedelta(hours=5))),  # 2026-06-30 21:00 UTC
        datetime(2026, 10, 1, tzinfo=UTC),
    )
    for i, (priority, length, stamp) in enumerate(
        (p, n, s) for p in priorities for n in lengths for s in stamps
    ):
        text = f"{i:04d}" + "y" * max(length - 4, 0) if length >= 4 else "z" * length
        service = (f"svc-{i % 52:02d}", None)[i % 9 == 0]
        edges.append(
            Rec(
                f"INC-{i:04d}",
                text[:length] if length < 4 else text,
                service,
                priority,
                stamp,
                content_hash=f"{i:032x}",
            )
        )
    problem = Rec("PRB-1", "problem root cause text", "svc-00", None, T0, entity="problem")
    return [*edges, problem]


def _top(records: list[Rec]) -> frozenset[str]:
    counts = Counter(r.service_id for r in records if r.entity == "incident" and r.service_id)
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return frozenset(service for service, _ in ranked[:50])


@pytest.mark.parametrize("tz", ["UTC", "America/New_York", "Asia/Kolkata"])
def test_ut03_115_python_and_sql_strata_agree(tz: str) -> None:
    """UT03-115: every edge record gets the same stratum key in Python and in SQL."""
    records = _edge_records()
    wh = warehouse(records, timezone=tz)
    table = _sample(wh, 100_000, qs=_qs("incident", "problem"))
    got = dict(
        zip(table.column("record_id").to_pylist(), table.column("stratum").to_pylist(), strict=True)
    )
    top = _top(records)
    unique = {r.hash: r for r in sorted(records, key=lambda r: r.record_id)}
    assert set(got) == {r.record_id for r in unique.values()}
    for rec in unique.values():
        assert rec.opened_at is not None
        expected = stratum_of(
            service_id=rec.service_id,
            top_services=top,
            priority=rec.priority if rec.entity == "incident" else None,
            opened_at=rec.opened_at,
            text_len=len(rec.text),
        )
        assert got[rec.record_id] == expected, rec.record_id
    assert len(top) == 50
    assert any(key.startswith("other|") for key in got.values())


def test_ut03_115_stratum_of_values() -> None:
    """UT03-115: bands, length edges, quarters and naive timestamps."""
    top = frozenset({"svc"})
    stamp = datetime(2026, 4, 1, tzinfo=UTC)
    base: dict[str, Any] = {
        "service_id": "svc",
        "top_services": top,
        "priority": 1,
        "opened_at": stamp,
        "text_len": 50,
    }

    def key(**kw: Any) -> str:
        return stratum_of(**(base | kw))

    assert key() == "svc|p12|2026Q2|s"
    assert key(priority=2, text_len=120) == "svc|p12|2026Q2|m"
    assert key(priority=3, text_len=600) == "svc|p3|2026Q2|m"
    assert key(priority=4, text_len=601) == "svc|p45|2026Q2|l"
    assert key(priority=None, service_id=None) == "other|p45|2026Q2|s"
    assert key(service_id="nope", priority=5) == "other|p45|2026Q2|s"
    assert key(opened_at=datetime(2026, 12, 31, 23, 0)) == "svc|p12|2026Q4|s"  # noqa: DTZ001


# --- UT03-116 / PT03-15: allocate --------------------------------------------------------------


def test_ut03_116_allocate_min_cap_sum() -> None:
    """UT03-116: {a: 100, b: 1, c: 10,000}, total 50 -> min 5 rule, cap at N, sum 50."""
    assert allocate({"a": 100, "b": 1, "c": 10_000}, 50) == {"a": 5, "b": 1, "c": 44}


def test_ut03_116_allocate_remainder_and_small_total() -> None:
    """UT03-116: remainder goes by largest fraction then key; total below the minimums."""
    assert allocate({"a": 4, "b": 4, "c": 4, "d": 4}, 10, min_per=2) == {
        "a": 3,
        "b": 3,
        "c": 2,
        "d": 2,
    }
    assert allocate({"a": 3, "b": 100}, 6) == {"a": 3, "b": 5}
    assert allocate({"a": 3, "b": 4}, 100) == {"a": 3, "b": 4}
    assert allocate({"a": 100, "b": 1, "c": 2}, 10) == {"a": 5, "b": 1, "c": 2}


@settings(max_examples=200, deadline=None)
@given(
    sizes=st.dictionaries(
        st.text("abcdefgh", min_size=1, max_size=3), st.integers(1, 2_000), min_size=1, max_size=25
    ),
    extra=st.integers(0, 20_000),
    min_per=st.integers(1, 8),
)
def test_pt03_15_allocate_sum_and_caps(sizes: dict[str, int], extra: int, min_per: int) -> None:
    """PT03-15: sum n_h = min(total, sum N_h) and n_h <= N_h (within the precondition)."""
    total = min_per * len(sizes) + extra
    n = allocate(sizes, total, min_per=min_per)
    assert sum(n.values()) == min(total, sum(sizes.values()))
    assert all(0 <= n[key] <= sizes[key] for key in sizes)
    assert all(n[key] >= min(min_per, sizes[key]) for key in sizes)
    assert n == allocate(dict(reversed(list(sizes.items()))), total, min_per=min_per)


# --- UT03-117: stratified_sample ---------------------------------------------------------------


def _pool() -> list[Rec]:
    return many(400)


def _proto_of(content_hash: str) -> int:
    """Two thirds of the hashes share prototype 0; the rest spread over the others."""
    value = int(content_hash, 16)
    return 0 if value % 3 else 1 + value % 15


def test_ut03_117_sample_excludes_gold_caps_prototypes_deterministic() -> None:
    """UT03-117: no gold hash; <= 2 % per prototype; deterministic for a salt."""
    records = _pool()
    wh = warehouse(records)
    gold = frozenset(r.hash for r in records[::5])

    def run(salt: str) -> pa.Table:
        return _sample(
            wh,
            200,
            exclude_hashes=gold,
            salt=salt,
            snapshot=snapshot(),
            vector_reader=proto_reader(_proto_of),
        )

    first = run("gold:q_a:" + "a" * 16)
    hashes = first.column("content_hash").to_pylist()
    assert hashes
    assert not set(hashes) & gold
    assert len(set(hashes)) == len(hashes)
    per_proto = Counter(_proto_of(h) for h in hashes)
    assert max(per_proto.values()) <= 0.02 * 200
    assert first.equals(run("gold:q_a:" + "a" * 16))
    assert run("train:v2").column("content_hash").to_pylist() != hashes
    assert first.schema.names == ["record_id", "entity", "content_hash", "text", "stratum"]


def test_ut03_117_sample_without_snapshot_dedupes_and_allocates() -> None:
    """UT03-117: without a snapshot the sample has exactly the allocated size; duplicate hashes
    keep the lowest record_id; problems join only when a question applies to them."""
    records = [
        *many(120),
        Rec("INC-99998", "dup body", content_hash="d" * 32),
        Rec("INC-99997", "dup body", content_hash="d" * 32),
        Rec("PRB-1", "problem body", entity="problem", service_id=None),
    ]
    wh = warehouse(records)
    table = _sample(wh, 1_000)
    ids = table.column("record_id").to_pylist()
    assert "INC-99997" in ids
    assert "INC-99998" not in ids
    assert "PRB-1" not in ids
    assert len(ids) == 121
    assert "PRB-1" in _sample(wh, 1_000, qs=_qs("problem")).column("record_id").to_pylist()
    assert _sample(wh, 0).num_rows == 0
    one_stratum = warehouse([Rec(f"INC-{i:03d}", f"body {i}") for i in range(120)])
    small = _sample(one_stratum, 60)
    assert small.num_rows == 60
    assert set(small.column("stratum").to_pylist()) == {"svc-a|p3|2026Q1|s"}


def test_ut03_117_sample_empty_and_missing_rows() -> None:
    """UT03-117: no text rows -> empty; records without opened_at are skipped."""
    assert _sample(warehouse([]), 10).num_rows == 0
    wh = warehouse([Rec("INC-1", "body one", opened_at=None), Rec("INC-2", "body two")])
    assert _sample(wh, 10).column("record_id").to_pylist() == ["INC-2"]


def test_ut03_117_duckdb_errors_map() -> None:
    """UT03-117: a missing table is SchemaViolation, an IO error StoreBusy; views released."""
    with pytest.raises(SchemaViolation, match="stratified_sample"):
        _sample(duckdb.connect(), 10)

    class _Busy:
        def __init__(self) -> None:
            self.views: set[str] = set()

        def register(self, name: str, _table: pa.Table) -> None:
            self.views.add(name)

        def unregister(self, name: str) -> None:
            self.views.discard(name)

        def execute(self, _sql: str, _params: object) -> None:
            msg = "Could not set lock on file"
            raise duckdb.IOException(msg)

    busy = _Busy()
    with pytest.raises(StoreBusy):
        _sample(busy, 10)  # type: ignore[arg-type]
    assert busy.views == set()
    wh = warehouse([Rec("INC-1", "body")])
    wh.execute("DROP TABLE enrich.text_redacted")
    with pytest.raises(SchemaViolation):
        sampling._ordered_pool(wh, qs=_qs(), exclude_hashes=frozenset(), salt="s")


def test_ut03_117_ordered_pool_hash_order() -> None:
    """UT03-117: the top-up pool is the deduplicated pool in sha256(salt || hash) order."""
    records = many(30)
    wh = warehouse(records)
    skip = frozenset({records[0].hash})
    pool = sampling._ordered_pool(wh, qs=_qs(), exclude_hashes=skip, salt="gold:x")

    hashes = pool.column("content_hash").to_pylist()
    assert hashes == sorted(
        (r.hash for r in records[1:]),
        key=lambda h: hashlib.sha256(f"gold:x{h}".encode()).hexdigest(),
    )
    assert text_hash(records[1].text) in hashes


def test_ut03_116_allocate_largest_fraction_first() -> None:
    """UT03-116: shares 3.33 / 6.67 -> the remainder unit goes to the larger fraction (b)."""
    assert allocate({"a": 100, "b": 400}, 10, min_per=1) == {"a": 3, "b": 7}


def test_ut03_117_capped_stratum_fills_from_three_fold_candidates() -> None:
    """UT03-117: when the prototype cap rejects early candidates, the stratum still fills
    from the 3 x n_h candidates (a 1 x pool would come up short)."""

    def half_on_zero(content_hash: str) -> int:
        value = int(content_hash, 16)
        return 0 if value % 2 else 1 + value % 15

    wh = warehouse([Rec(f"INC-{i:03d}", f"body {i}") for i in range(150)])
    table = _sample(
        wh, 50, snapshot=snapshot(), max_proto_share=0.2, vector_reader=proto_reader(half_on_zero)
    )
    assert table.num_rows == 50
    per_proto = Counter(half_on_zero(h) for h in table.column("content_hash").to_pylist())
    assert per_proto[0] == 10


# --- UT03-118: select_active -------------------------------------------------------------------


def test_ut03_118_select_active_prototype_cap() -> None:
    """UT03-118: ten top uncertainties on one prototype -> at most 5 of them are kept."""
    n = 30
    u = np.array([0.9] * 10 + [0.5] * 10 + [0.1] * 10)
    protos = np.array([7] * 10 + list(range(10)) + list(range(10)))
    hashes = [f"{i:032x}" for i in range(n)]
    picked = select_active(u, protos, hashes, candidates=25, per_prototype=5, per_round=12)
    assert len(picked) == 12
    assert Counter(int(protos[i]) for i in picked)[7] == 5
    assert picked[:5] == [0, 1, 2, 3, 4]  # ties by hash ascending
    assert all(i < 25 for i in picked)
    assert select_active(u, protos, hashes, candidates=8, per_prototype=5, per_round=50) == [
        0,
        1,
        2,
        3,
        4,
    ]
    assert (
        select_active(np.array([]), np.array([]), [], candidates=5, per_prototype=1, per_round=5)
        == []
    )


def test_ut03_118_select_active_tie_break_by_hash() -> None:
    """UT03-118: equal uncertainty sorts by hash ascending, not by index."""
    u = np.array([0.5, 0.5, 0.5])
    picked = select_active(
        u,
        np.array([0, 1, 2]),
        ["c" * 32, "a" * 32, "b" * 32],
        candidates=3,
        per_prototype=1,
        per_round=3,
    )
    assert picked == [1, 2, 0]
