"""Overflow and bounded-group tests for cache compaction (U03-39, U03-40; T03-39)."""

from __future__ import annotations

import datetime
import hashlib
import importlib
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from herness.enrich.cache import CACHE_SCHEMA
from herness.enrich.cache_maint import compact
from herness.enrich.layout import EnrichPaths

pytestmark = pytest.mark.unit

QSV = "qs-2026-10-01"
VERSION = "laya-20261001-1"
FP = "aaaaaaaaaaaaaaaa"
T0 = datetime.datetime(2026, 10, 1, tzinfo=datetime.UTC)
PARTS, ROWS_PER_PART, STALE_PER_PART = 24, 100, 5
ANSWER_BYTES = 2**20


@pytest.fixture
def paths(tmp_path: Path) -> EnrichPaths:
    return EnrichPaths(data_root=tmp_path, embedding_path="data/e", laya_current_file="data/c")


def _row(index: int, *, minute: int, answer: str) -> dict[str, object]:
    return {
        "content_hash": f"{index:032x}",
        "question": f"q_{index % 7}",
        "question_fingerprint": FP,
        "answer": answer,
        "probability": 0.5,
        "distribution": [("yes", 0.5), ("no", 0.5)],
        "backend_confidence": None,
        "samples": 1,
        "decided_at": T0 + datetime.timedelta(minutes=minute),
    }


def _digest(row: dict[str, object]) -> str:
    keys = ("content_hash", "question", "question_fingerprint", "decided_at")
    text = "|".join(str(row[name]) for name in keys).encode()
    return hashlib.sha256(text + hashlib.sha256(str(row["answer"]).encode()).digest()).hexdigest()


def _read_digests(partition: Path) -> list[str]:
    digests: list[str] = []
    for part in sorted(partition.glob("part-*.parquet")):
        with pq.ParquetFile(part) as handle:
            for batch in handle.iter_batches(batch_size=20):
                digests.extend(_digest(r) for r in batch.to_pylist())
    return sorted(digests)


def test_ut03_141_compact_survives_string_column_over_2gib(paths: EnrichPaths) -> None:
    """UT03-141 small parts whose answer column totals > 2^31 bytes compact without overflow."""
    partition = paths.cache_partition(QSV, "laya", VERSION)
    partition.mkdir(parents=True)
    block = ("0123456789abcdef" * (ANSWER_BYTES // 16))[: ANSWER_BYTES - 8]
    expected: dict[tuple[str, str], tuple[str, str]] = {}
    answer_bytes = 0
    for p in range(PARTS):
        rows = [
            _row(p * ROWS_PER_PART + i, minute=10, answer=f"{p * ROWS_PER_PART + i:08d}{block}")
            for i in range(ROWS_PER_PART)
        ]
        if p:  # stale older versions of the previous part's first keys: must lose
            rows += [
                _row((p - 1) * ROWS_PER_PART + i, minute=1, answer="stale")
                for i in range(STALE_PER_PART)
            ]
        answer_bytes += sum(len(str(r["answer"])) for r in rows)
        for r in rows:
            key = (str(r["content_hash"]), str(r["question"]))
            if key not in expected or str(r["decided_at"]) > expected[key][0]:
                expected[key] = (str(r["decided_at"]), _digest(r))
        pq.write_table(
            pa.Table.from_pylist(rows, schema=CACHE_SCHEMA),
            partition / f"part-{p:02d}.parquet",
            compression="zstd",
        )
    assert answer_bytes > 2**31
    assert all(f.stat().st_size < 64 * 2**20 for f in partition.glob("part-*.parquet"))
    want = sorted(d for _, d in expected.values())
    assert len(want) == PARTS * ROWS_PER_PART

    assert compact(paths, QSV) == PARTS

    got = _read_digests(partition)
    assert len(got) == len(want)
    assert got == want
    assert (
        hashlib.sha256("".join(got).encode()).hexdigest()
        == hashlib.sha256("".join(want).encode()).hexdigest()
    )


def test_ut03_141_bounded_groups_dedupe_across_groups(
    paths: EnrichPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-141 with two rows per output chunk a key in two parts keeps the latest only."""
    monkeypatch.setattr(importlib.import_module("herness.enrich._cache_compact"), "OUTPUT_ROWS", 2)
    partition = paths.cache_partition(QSV, "laya", VERSION)
    partition.mkdir(parents=True)
    layout = {
        "part-01.parquet": [_row(1, minute=5, answer="old"), _row(2, minute=1, answer="a")],
        "part-02.parquet": [_row(3, minute=1, answer="b"), _row(4, minute=1, answer="c")],
        "part-03.parquet": [_row(1, minute=9, answer="new"), _row(5, minute=1, answer="d")],
    }
    for name, rows in layout.items():
        pq.write_table(pa.Table.from_pylist(rows, schema=CACHE_SCHEMA), partition / name)

    assert compact(paths, QSV) == 3
    outputs = sorted(partition.glob("part-*.parquet"))
    assert len(outputs) == 3  # 5 winners, 2 rows per chunk
    rows = [r for p in outputs for r in pq.read_table(p).to_pylist()]
    got = sorted((r["content_hash"], r["answer"]) for r in rows)
    assert got == [
        (f"{1:032x}", "new"),
        (f"{2:032x}", "a"),
        (f"{3:032x}", "b"),
        (f"{4:032x}", "c"),
        (f"{5:032x}", "d"),
    ]
