"""Tests for herness.enrich.cache_maint (U03-39 ... U03-41; T03-09)."""

from __future__ import annotations

import datetime
import errno
import json
import os
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from herness.core.errors import ConfigError, FatalError, SchemaViolation, StoreBusy
from herness.core.types import Question, QuestionSet
from herness.enrich import cache
from herness.enrich.cache import CACHE_SCHEMA, DecisionCache
from herness.enrich.cache_maint import compact, migrate, purge_hashes
from herness.enrich.layout import EnrichPaths

pytestmark = pytest.mark.unit

OLD, NEW, OTHER = "qs-2026-09-01", "qs-2026-09-02", "qs-2026-08-01"
LAYA_V = "laya-20260901-1"
OJ_V = "openjev-0.4.0/qwen:7b"
FP_A, FP_B, FP_B2 = "aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb", "cccccccccccccccc"
H1, H2, H3 = "1" * 32, "2" * 32, "3" * 32
T0 = datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC)


def _question(qid: str, fingerprint: str) -> Question:
    return Question(
        id=qid, type="bool", instructions="Is this a thing?", threshold=0.7, fingerprint=fingerprint
    )


NEW_QS = QuestionSet(version=NEW, questions=(_question("q_a", FP_A), _question("q_b", FP_B2)))


@pytest.fixture
def paths(tmp_path: Path) -> EnrichPaths:
    return EnrichPaths(data_root=tmp_path, embedding_path="data/e", laya_current_file="data/c")


def _row(h: str, qid: str, fp: str, *, minute: int = 0, answer: str = "yes") -> dict[str, object]:
    return {
        "content_hash": h,
        "question": qid,
        "question_fingerprint": fp,
        "answer": answer,
        "probability": 0.8,
        "distribution": [("yes", 0.8), ("no", 0.2)],
        "backend_confidence": None,
        "samples": 3,
        "decided_at": T0 + datetime.timedelta(minutes=minute),
    }


def _write(directory: Path, name: str, rows: list[dict[str, object]]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    pq.write_table(pa.Table.from_pylist(rows, schema=CACHE_SCHEMA), target)
    return target


def _parts(directory: Path) -> list[Path]:
    return sorted(directory.glob("part-*.parquet"))


def _read_back(paths: EnrichPaths, qsv: str) -> list[dict[str, object]]:
    dataset = DecisionCache(paths, qsv).dataset()
    assert dataset is not None
    rows = dataset.to_table().to_pylist()
    return sorted(
        rows, key=lambda r: (str(r["decider"]), str(r["content_hash"]), str(r["question"]))
    )


def _assert_schema(parts: list[Path]) -> None:
    assert parts
    for part in parts:
        assert pq.read_schema(part).equals(CACHE_SCHEMA, check_metadata=False)


def test_ut03_36_migrate_copies_unchanged_once(paths: EnrichPaths) -> None:
    """UT03-36 migrate copies the unchanged question once; the second call is a no-op."""
    old_laya = paths.cache_partition(OLD, "laya", LAYA_V)
    _write(old_laya, "part-01.parquet", [_row(H1, "q_a", FP_A), _row(H1, "q_b", FP_B)])
    _write(old_laya, "part-02.parquet", [_row(H1, "q_a", FP_A, minute=5, answer="no")])
    _write(paths.cache_partition(OLD, "openjev", OJ_V), "part-01.parquet", [_row(H2, "q_b", FP_B)])

    assert migrate(paths, OLD, NEW_QS) == 1
    new_laya = paths.cache_partition(NEW, "laya", LAYA_V)
    assert len(_parts(new_laya)) == 1
    _assert_schema(_parts(new_laya))
    assert not paths.cache_partition(NEW, "openjev", OJ_V).exists()
    rows = _read_back(paths, NEW)
    assert [(r["content_hash"], r["question"], r["answer"]) for r in rows] == [(H1, "q_a", "no")]
    assert rows[0]["decider_version"] == LAYA_V
    marker = paths.cache_dir(NEW) / f"_migrated_from_{OLD}.json"
    data = json.loads(marker.read_text(encoding="utf-8"))
    assert data["rows"] == 1
    assert data["finished_at"].endswith("Z")

    assert migrate(paths, OLD, NEW_QS) == 1
    assert len(_parts(new_laya)) == 1


def test_ut03_36_migrate_url_quoted_version_and_empty_old(paths: EnrichPaths) -> None:
    """UT03-36 a URL-quoted decider version is carried to the matching new partition."""
    assert migrate(paths, OTHER, NEW_QS) == 0  # no old cache at all
    _write(paths.cache_partition(OLD, "openjev", OJ_V), "part-01.parquet", [_row(H2, "q_a", FP_A)])
    paths.cache_partition(OLD, "laya", LAYA_V).mkdir(parents=True)  # partition without parts
    assert migrate(paths, OLD, NEW_QS) == 1
    assert not paths.cache_partition(NEW, "laya", LAYA_V).exists()
    rows = _read_back(paths, NEW)
    assert [(r["decider"], r["decider_version"]) for r in rows] == [("openjev", OJ_V)]


def test_ut03_36_migrate_rejects_same_version_and_bad_marker(paths: EnrichPaths) -> None:
    """UT03-36 same old and new version is refused; an unreadable marker raises ConfigError."""
    with pytest.raises(ConfigError):
        migrate(paths, NEW, NEW_QS)
    marker = paths.cache_dir(NEW) / f"_migrated_from_{OLD}.json"
    marker.parent.mkdir(parents=True)
    marker.write_text('{"rows": "many"}', encoding="utf-8")
    with pytest.raises(ConfigError):
        migrate(paths, OLD, NEW_QS)
    marker.write_text("not json", encoding="utf-8")
    with pytest.raises(ConfigError):
        migrate(paths, OLD, NEW_QS)


def test_ut03_36_migrate_rejects_foreign_schema(paths: EnrichPaths) -> None:
    """UT03-36 an old part whose schema is not CACHE_SCHEMA raises SchemaViolation."""
    partition = paths.cache_partition(OLD, "laya", LAYA_V)
    partition.mkdir(parents=True)
    pq.write_table(pa.table({"content_hash": [H1]}), partition / "part-01.parquet")
    with pytest.raises(SchemaViolation):
        migrate(paths, OLD, NEW_QS)
    assert not (paths.cache_dir(NEW) / f"_migrated_from_{OLD}.json").exists()


def test_ut03_37_compact_merges_small_parts(paths: EnrichPaths) -> None:
    """UT03-37 five small parts with duplicate keys become one part keeping the latest row."""
    partition = paths.cache_partition(OLD, "laya", LAYA_V)
    for i in range(5):
        _write(
            partition,
            f"part-0{i}.parquet",
            [_row(H1, "q_a", FP_A, minute=i, answer=f"v{i}"), _row(H2, "q_a", FP_A, minute=i)],
        )
    _write(partition, ".part-99.parquet.tmp", [_row(H3, "q_a", FP_A)])  # ignored
    single = paths.cache_partition(OLD, "openjev", OJ_V)
    _write(single, "part-01.parquet", [_row(H3, "q_a", FP_A)])

    assert compact(paths, OLD) == 5
    parts = _parts(partition)
    assert len(parts) == 1
    _assert_schema(parts)
    assert (partition / ".part-99.parquet.tmp").exists()
    assert len(_parts(single)) == 1
    rows = [r for r in _read_back(paths, OLD) if r["decider"] == "laya"]
    assert [(r["content_hash"], r["answer"]) for r in rows] == [(H1, "v4"), (H2, "yes")]
    assert rows[0]["decided_at"] == T0 + datetime.timedelta(minutes=4)
    assert rows[1]["decided_at"] == T0 + datetime.timedelta(minutes=4)
    assert compact(paths, OLD) == 0


def test_ut03_37_compact_leaves_large_parts(paths: EnrichPaths) -> None:
    """UT03-37 parts at or above small_bytes are not merged; a bad threshold is refused."""
    partition = paths.cache_partition(OLD, "laya", LAYA_V)
    _write(partition, "part-01.parquet", [_row(H1, "q_a", FP_A)])
    _write(partition, "part-02.parquet", [_row(H2, "q_a", FP_A)])
    assert compact(paths, OLD, small_bytes=1) == 0
    assert len(_parts(partition)) == 2
    assert compact(paths, NEW) == 0  # no cache for this version
    with pytest.raises(ConfigError):
        compact(paths, OLD, small_bytes=0)


def test_ut03_37_compact_write_error_keeps_sources(
    paths: EnrichPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-37 a failed merged write raises StoreBusy and keeps every source part."""
    partition = paths.cache_partition(OLD, "laya", LAYA_V)
    _write(partition, "part-01.parquet", [_row(H1, "q_a", FP_A)])
    _write(partition, "part-02.parquet", [_row(H2, "q_a", FP_A)])

    def _fail(src: object, dst: object) -> None:
        raise OSError(errno.EACCES, os.strerror(errno.EACCES))

    monkeypatch.setattr(cache.os, "replace", _fail)
    with pytest.raises(StoreBusy):
        compact(paths, OLD)
    assert len(_parts(partition)) == 2


def test_ut03_38_purge_across_versions(paths: EnrichPaths) -> None:
    """UT03-38 target rows are removed in 2 versions, an emptied part is deleted, count is right."""
    old_laya = paths.cache_partition(OLD, "laya", LAYA_V)
    mixed = _write(old_laya, "part-01.parquet", [_row(H1, "q_a", FP_A), _row(H2, "q_a", FP_A)])
    untouched = _write(old_laya, "part-02.parquet", [_row(H3, "q_a", FP_A)])
    before = untouched.stat().st_mtime_ns
    only = _write(
        paths.cache_partition(NEW, "openjev", OJ_V),
        "part-01.parquet",
        [_row(H1, "q_a", FP_A), _row(H1, "q_b", FP_B2)],
    )
    (paths.cache_dir(NEW) / "questions.json").write_text("{}", encoding="utf-8")

    assert purge_hashes(paths, frozenset({H1})) == 3
    assert not only.exists()
    assert mixed.exists()
    _assert_schema(_parts(old_laya))
    assert untouched.stat().st_mtime_ns == before
    assert [r["content_hash"] for r in _read_back(paths, OLD)] == [H2, H3]
    assert DecisionCache(paths, NEW).dataset() is None
    assert purge_hashes(paths, frozenset({H1})) == 0


def test_ut03_38_purge_validates_hashes_and_empty_cache(paths: EnrichPaths) -> None:
    """UT03-38 malformed hashes are refused; an empty set or missing cache purges nothing."""
    with pytest.raises(ConfigError):
        purge_hashes(paths, frozenset({"ABC"}))
    assert purge_hashes(paths, frozenset({H1})) == 0
    assert purge_hashes(paths, frozenset()) == 0


@pytest.mark.parametrize(
    ("code", "expected"),
    [(errno.EACCES, StoreBusy), (errno.EBUSY, StoreBusy), (errno.ENOSPC, FatalError)],
)
def test_ut03_38_purge_os_error_mapping(
    paths: EnrichPaths, monkeypatch: pytest.MonkeyPatch, code: int, expected: type[Exception]
) -> None:
    """UT03-38 an OS error deleting an emptied part maps to StoreBusy or FatalError (U03-38)."""
    partition = paths.cache_partition(OLD, "laya", LAYA_V)
    part = _write(partition, "part-01.parquet", [_row(H1, "q_a", FP_A)])

    def _fail(*args: object, **kwargs: object) -> None:
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(Path, "unlink", _fail)
    with pytest.raises(expected) as info:
        purge_hashes(paths, frozenset({H1}))
    assert type(info.value) is expected
    assert part.exists()
