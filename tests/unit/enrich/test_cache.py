"""Tests for herness.enrich.cache (U03-36 ... U03-38; T03-08)."""

from __future__ import annotations

import datetime
import errno
import os
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from herness.core.errors import ConfigError, FatalError, SchemaViolation, StoreBusy
from herness.core.types import Answer, DecisionOutput, Question, QuestionSet
from herness.enrich import cache
from herness.enrich.cache import CACHE_SCHEMA, CacheWriter, DecisionCache
from herness.enrich.layout import EnrichPaths
from herness.enrich.questions import question_fingerprint

pytestmark = pytest.mark.unit

QSV = "qs-2026-09-01"
LAYA_V = "laya-20260901-1"
FP_A, FP_B, FP_OLD = "aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb", "0000000000000000"
OJ_VERSION = "openjev-0.4.0/qwen:7b"
HASH1, HASH2, HASH3 = "1" * 32, "2" * 32, "3" * 32


def _question(qid: str, fingerprint: str) -> Question:
    return Question(
        id=qid, type="bool", instructions="Is this a thing?", threshold=0.7, fingerprint=fingerprint
    )


QS = QuestionSet(version=QSV, questions=(_question("q_a", FP_A), _question("q_b", FP_B)))


@pytest.fixture
def paths(tmp_path: Path) -> EnrichPaths:
    return EnrichPaths(data_root=tmp_path, embedding_path="data/e", laya_current_file="data/c")


@pytest.fixture
def fault_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(cache, "_fault_point", calls.append)
    return calls


def _answer(p: float = 0.8) -> Answer:
    return Answer(answer="yes", probability=p, distribution={"yes": p, "no": round(1 - p, 6)})


def _output(
    content_hash: str,
    answers: dict[str, Answer],
    *,
    decider: str = "laya",
    version: str = LAYA_V,
    error: str | None = None,
) -> DecisionOutput:
    return DecisionOutput(
        record_id="r-" + content_hash[:4],
        content_hash=content_hash,
        decider=decider,  # type: ignore[arg-type]
        decider_version=version,
        answers=answers,
        error=error,
    )


def _row(content_hash: str, question: str, fingerprint: str) -> dict[str, object]:
    return {
        "content_hash": content_hash,
        "question": question,
        "question_fingerprint": fingerprint,
        "answer": "yes",
        "probability": 0.8,
        "distribution": [("yes", 0.8), ("no", 0.2)],
        "backend_confidence": None,
        "samples": None,
        "decided_at": datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC),
    }


def _write_part(directory: Path, name: str, rows: list[dict[str, object]]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    pq.write_table(pa.Table.from_pylist(rows, schema=CACHE_SCHEMA), target)
    return target


def _parts(directory: Path) -> list[Path]:
    return sorted(directory.glob("part-*.parquet"))


def test_ut03_33_extra_column_part_rejected_on_register(paths: EnrichPaths) -> None:
    """UT03-33 a part with an extra column makes register raise SchemaViolation (TH03-18)."""
    part_dir = paths.cache_partition(QSV, "laya", LAYA_V)
    _write_part(part_dir, "part-01good.parquet", [_row(HASH1, "q_a", FP_A)])
    tampered = pa.Table.from_pylist([_row(HASH2, "q_a", FP_A)], schema=CACHE_SCHEMA)
    tampered = tampered.append_column("text", pa.array(["leaked ticket text"]))
    pq.write_table(tampered, part_dir / "part-02bad.parquet")
    con = duckdb.connect()
    with pytest.raises(SchemaViolation, match=r"^cache part schema mismatch: part-02bad\.parquet$"):
        DecisionCache(paths, QSV).register(con, "cache_v")


def test_ut03_33_schema_constant_and_empty_register(paths: EnrichPaths) -> None:
    """UT03-33 CACHE_SCHEMA is exact; register without parts gives an empty view."""
    assert CACHE_SCHEMA.names == [
        "content_hash", "question", "question_fingerprint", "answer", "probability",
        "distribution", "backend_confidence", "samples", "decided_at",
    ]  # fmt: skip
    assert CACHE_SCHEMA.field("samples").type == pa.int16()
    assert CACHE_SCHEMA.field("decided_at").type == pa.timestamp("us", tz="UTC")
    assert CACHE_SCHEMA.field("distribution").type == pa.map_(pa.string(), pa.float64())
    dc = DecisionCache(paths, QSV)
    assert dc.dataset() is None
    assert dc.existing_keys("laya", LAYA_V, QS) == set()
    con = duckdb.connect()
    dc.register(con, "cache_v")
    names = [row[0] for row in con.execute("DESCRIBE cache_v").fetchall()]
    assert names == [*CACHE_SCHEMA.names, "decider", "decider_version"]
    assert con.execute("SELECT count(*) FROM cache_v").fetchone() == (0,)


def test_ut03_33_register_reads_decoded_versions(paths: EnrichPaths) -> None:
    """UT03-33 register exposes partition columns with URL-decoded decider versions."""
    oj_dir = paths.cache_partition(QSV, "openjev", OJ_VERSION)
    _write_part(oj_dir, "part-01.parquet", [_row(HASH1, "q_a", FP_A)])
    con = duckdb.connect()
    DecisionCache(paths, QSV).register(con, "cache_v")
    got = con.execute("SELECT decider, decider_version, content_hash FROM cache_v").fetchall()
    assert got == [("openjev", OJ_VERSION, HASH1)]


def test_ut03_34_tmp_ignored_keys_filtered(paths: EnrichPaths) -> None:
    """UT03-34 parts in 2 partitions and a .tmp file: tmp ignored, keys filtered by fingerprint."""
    laya_dir = paths.cache_partition(QSV, "laya", LAYA_V)
    oj_dir = paths.cache_partition(QSV, "openjev", OJ_VERSION)
    laya_rows = [_row(HASH1, "q_a", FP_A), _row(HASH2, "q_b", FP_B), _row(HASH3, "q_a", FP_OLD)]
    _write_part(laya_dir, "part-01.parquet", laya_rows)
    _write_part(oj_dir, "part-02.parquet", [_row(HASH3, "q_b", FP_B)])
    (laya_dir / ".part-03.parquet.tmp").write_bytes(b"half written garbage")
    (oj_dir / "_ignored.parquet").write_bytes(b"not parquet")
    (paths.cache_dir(QSV) / "questions.json").write_text("{}", encoding="utf-8")
    dc = DecisionCache(paths, QSV)
    dataset = dc.dataset()
    assert dataset is not None
    assert dataset.count_rows() == 4
    assert dc.existing_keys("laya", LAYA_V, QS) == {(HASH1, "q_a"), (HASH2, "q_b")}
    assert dc.existing_keys("openjev", OJ_VERSION, QS) == {(HASH3, "q_b")}
    only_b = QuestionSet(version=QSV, questions=(_question("q_b", FP_B),))
    assert dc.existing_keys("laya", LAYA_V, only_b) == {(HASH2, "q_b")}
    assert dc.existing_keys("laya", "laya-other", QS) == set()


def test_ut03_34_only_tmp_means_no_dataset(paths: EnrichPaths) -> None:
    """UT03-34 a partition holding only a .tmp file has no dataset."""
    laya_dir = paths.cache_partition(QSV, "laya", LAYA_V)
    laya_dir.mkdir(parents=True)
    (laya_dir / ".part-01.parquet.tmp").write_bytes(b"x")
    assert DecisionCache(paths, QSV).dataset() is None


def test_ut03_35_add_flush_dedupes_and_skips(paths: EnrichPaths, fault_calls: list[str]) -> None:
    """UT03-35 duplicates, an error output and an unknown qid: one part, fault point called."""
    writer = DecisionCache(paths, QSV).writer("laya", LAYA_V, questions=QS, flush_rows=100)
    outputs = [
        _output(HASH1, {"q_a": _answer(), "q_b": _answer(0.6), "q_unknown": _answer()}),
        _output(HASH1, {"q_a": _answer(0.9)}),  # duplicate key
        _output(HASH2, {}, error="OutputValidationError"),
        _output(HASH2, {"q_b": _answer()}),
    ]
    assert writer.add(outputs, samples=3) == 3
    assert writer.add([_output(HASH2, {"q_b": _answer()})], samples=3) == 0
    part = writer.flush()
    assert part is not None
    assert part.name.startswith("part-")
    assert part.suffix == ".parquet"
    assert fault_calls == ["enrich.after_batch_write"]
    assert [p.name for p in part.parent.iterdir()] == [part.name]
    table = pq.read_table(part)
    assert table.schema.equals(CACHE_SCHEMA)
    rows = sorted(
        (r["content_hash"], r["question"], r["question_fingerprint"], r["probability"])
        for r in table.to_pylist()
    )
    assert rows == [(HASH1, "q_a", FP_A, 0.8), (HASH1, "q_b", FP_B, 0.6), (HASH2, "q_b", FP_B, 0.8)]
    assert set(table.column("samples").to_pylist()) == {3}
    assert writer.flush() is None
    assert fault_calls == ["enrich.after_batch_write"]
    keys = DecisionCache(paths, QSV).existing_keys("laya", LAYA_V, QS)
    assert keys == {(HASH1, "q_a"), (HASH1, "q_b"), (HASH2, "q_b")}


def _add_then_fail(writer: CacheWriter) -> None:
    with writer:
        writer.add([_output(HASH3, {"q_a": _answer()})], samples=None)
        raise RuntimeError


def test_ut03_35_auto_flush_and_context_manager(paths: EnrichPaths, fault_calls: list[str]) -> None:
    """UT03-35 add flushes at flush_rows; the context manager flushes on normal exit only."""
    dc = DecisionCache(paths, QSV)
    part_dir = paths.cache_partition(QSV, "laya", LAYA_V)
    with dc.writer("laya", LAYA_V, questions=QS, flush_rows=2) as writer:
        assert writer.add([_output(HASH1, {"q_a": _answer(), "q_b": _answer()})], samples=None) == 2
        assert len(_parts(part_dir)) == 1
        writer.add([_output(HASH2, {"q_a": _answer()})], samples=None)
    assert len(_parts(part_dir)) == 2
    assert fault_calls == ["enrich.after_batch_write"] * 2
    failing = dc.writer("laya", LAYA_V, questions=QS, flush_rows=10)
    with pytest.raises(RuntimeError):
        _add_then_fail(failing)
    assert len(_parts(part_dir)) == 2
    closing = dc.writer("laya", LAYA_V, questions=QS, flush_rows=10)
    closing.add([_output(HASH3, {"q_a": _answer()})], samples=None)
    closing.close()
    assert len(_parts(part_dir)) == 3


def test_ut03_35_decider_mismatch_raises(paths: EnrichPaths) -> None:
    """UT03-35 an output of another decider or version is a SchemaViolation."""
    writer = DecisionCache(paths, QSV).writer("laya", LAYA_V, questions=QS, flush_rows=10)
    with pytest.raises(SchemaViolation):
        writer.add([_output(HASH1, {"q_a": _answer()}, version="laya-20260901-2")], samples=None)
    other = _output(HASH1, {"q_a": _answer()}, decider="openjev")
    with pytest.raises(SchemaViolation):
        writer.add([other], samples=None)
    with pytest.raises(ConfigError, match="flush_rows"):
        DecisionCache(paths, QSV).writer("laya", LAYA_V, questions=QS, flush_rows=0)


def test_ut03_33_missing_column_part_rejected(paths: EnrichPaths) -> None:
    """UT03-33 a part lacking a column is rejected as well (TH03-18)."""
    part_dir = paths.cache_partition(QSV, "laya", LAYA_V)
    part_dir.mkdir(parents=True)
    short = pa.Table.from_pylist([_row(HASH1, "q_a", FP_A)], schema=CACHE_SCHEMA)
    pq.write_table(short.drop_columns(["samples"]), part_dir / "part-01.parquet")
    with pytest.raises(SchemaViolation, match=r"part-01\.parquet"):
        DecisionCache(paths, QSV).existing_keys("laya", LAYA_V, QS)


def test_ut03_35_missing_fingerprint_is_computed(
    paths: EnrichPaths, fault_calls: list[str]
) -> None:
    """UT03-35 a question without a stored fingerprint gets its computed fingerprint."""
    bare = _question("q_a", "")
    qs = QuestionSet(version=QSV, questions=(bare,))
    writer = DecisionCache(paths, QSV).writer("laya", LAYA_V, questions=qs, flush_rows=5)
    writer.add([_output(HASH1, {"q_a": _answer()})], samples=None)
    part = writer.flush()
    assert part is not None
    fingerprints = pq.read_table(part).column("question_fingerprint").to_pylist()
    assert fingerprints == [question_fingerprint(bare)]


@pytest.mark.parametrize(
    ("code", "expected"),
    [(errno.EACCES, StoreBusy), (errno.EBUSY, StoreBusy), (errno.ENOSPC, FatalError)],
)
def test_ut03_35_os_error_mapping(
    paths: EnrichPaths,
    fault_calls: list[str],
    monkeypatch: pytest.MonkeyPatch,
    code: int,
    expected: type[Exception],
) -> None:
    """UT03-35 OS errors on write map to StoreBusy (EACCES/EBUSY) or FatalError; tmp removed."""
    writer = DecisionCache(paths, QSV).writer("laya", LAYA_V, questions=QS, flush_rows=10)
    writer.add([_output(HASH1, {"q_a": _answer()})], samples=None)

    def _fail(src: object, dst: object) -> None:
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(cache.os, "replace", _fail)
    with pytest.raises(expected) as info:
        writer.flush()
    assert type(info.value) is expected
    assert list(paths.cache_partition(QSV, "laya", LAYA_V).iterdir()) == []
    assert fault_calls == []


@pytest.mark.parametrize(
    ("code", "expected"),
    [(errno.EACCES, StoreBusy), (errno.EBUSY, StoreBusy), (errno.ENOSPC, FatalError)],
)
def test_ut03_35_os_error_mapping_when_unlink_fails_too(
    paths: EnrichPaths,
    fault_calls: list[str],
    monkeypatch: pytest.MonkeyPatch,
    code: int,
    expected: type[Exception],
) -> None:
    """UT03-35 a locked tmp file that cannot be unlinked still maps to StoreBusy/FatalError."""
    writer = DecisionCache(paths, QSV).writer("laya", LAYA_V, questions=QS, flush_rows=10)
    writer.add([_output(HASH1, {"q_a": _answer()})], samples=None)

    def _fail(*args: object, **kwargs: object) -> None:
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(cache.os, "replace", _fail)
    monkeypatch.setattr(Path, "unlink", _fail)
    with pytest.raises(expected) as info:
        writer.flush()
    assert type(info.value) is expected
    assert fault_calls == []


@pytest.mark.parametrize("samples", [0, -1, 2**15])
def test_ut03_35_samples_out_of_int16_range_rejected(paths: EnrichPaths, samples: int) -> None:
    """UT03-35 samples outside 1 ... 32767 is a ConfigError, never a raw Arrow error."""
    writer = DecisionCache(paths, QSV).writer("laya", LAYA_V, questions=QS, flush_rows=10)
    with pytest.raises(ConfigError, match="samples out of range"):
        writer.add([_output(HASH1, {"q_a": _answer()})], samples=samples)
    assert writer.flush() is None


def test_ut03_35_samples_upper_bound_accepted(paths: EnrichPaths, fault_calls: list[str]) -> None:
    """UT03-35 samples = 32767 fits the int16 column."""
    writer = DecisionCache(paths, QSV).writer("laya", LAYA_V, questions=QS, flush_rows=10)
    writer.add([_output(HASH1, {"q_a": _answer()})], samples=2**15 - 1)
    part = writer.flush()
    assert part is not None
    assert pq.read_table(part).column("samples").to_pylist() == [2**15 - 1]
