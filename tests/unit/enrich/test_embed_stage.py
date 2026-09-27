"""Tests for herness.enrich.embed_stage (U03-33 ... U03-35; T03-07).

The stage runs against a hand-built DuckDB warehouse and a LanceDB store under `tmp_path`
with a fake encoder (vectors derived from the text); the real tiny-st encoder runs in the
integration flow IT03-03.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pytest
from lancedb.table import LanceTable
from structlog.testing import capture_logs
from tests.support.build_harness import FakeJobContext
from tests.unit.enrich._embed_support import (
    MODEL_ID,
    T0,
    FakeEncoder,
    Rec,
    Report,
    add_rows,
    embed_warehouse,
    stored,
    unit_vector,
    use_batch_size,
    use_store,
)

from herness.core.errors import SchemaViolation, StoreBusy
from herness.enrich import embed_stage
from herness.enrich.embed_stage import (
    INDEX_ROWS_KEY,
    lance_filter_in,
    maintain_index,
    run_embed_stage,
)
from herness.enrich.gpu import YieldRequested
from herness.enrich.text import content_hash
from herness.store.vectors import TICKET_EMBEDDING_SCHEMA, VectorStore

pytestmark = pytest.mark.unit

HASH = "0123456789abcdef" * 2  # built at runtime: no detect-secrets baseline entry
T1 = datetime(2026, 9, 2, tzinfo=UTC)


# --- UT03-30: lance_filter_in -------------------------------------------------------------------


def test_ut03_30_valid_values_give_quoted_in_list() -> None:
    """UT03-30 valid record ids and hashes -> `<column> IN ('v1', 'v2', ...)`."""
    ids = ["servicenow:incident:INC0001", "jira:issue:OPS-12.a_b"]
    assert lance_filter_in("record_id", ids) == (
        "record_id IN ('servicenow:incident:INC0001', 'jira:issue:OPS-12.a_b')"
    )
    assert lance_filter_in("content_hash", [HASH]) == f"content_hash IN ('{HASH}')"
    assert lance_filter_in("record_id", [f"s:e:{i}" for i in range(1_000)]).count("'") == 2_000


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("record_id", "servicenow:incident:INC'1"),
        ("record_id", "Servicenow:incident:INC1"),
        ("record_id", "servicenow:INC1"),
        ("record_id", "servicenow:incident:INC1\n"),
        ("record_id", "a" * 33 + ":incident:INC1"),
        ("content_hash", HASH.upper()),
        ("content_hash", HASH[:31]),
        ("content_hash", HASH + "'"),
    ],
)
def test_ut03_30_invalid_value_is_schema_violation(column: Any, value: str) -> None:
    """UT03-30 id with `'` (and other allowlist misses) -> SchemaViolation, value not echoed."""
    with pytest.raises(SchemaViolation, match=f"invalid {column} for vector filter") as info:
        lance_filter_in(column, ["servicenow:incident:INC0001", value])
    assert value.strip() not in str(info.value)
    assert value.strip() not in repr((info.value.context, info.value.details, info.value.hint))


@pytest.mark.parametrize("count", [0, 1_001])
def test_ut03_30_size_outside_1_to_1000_is_schema_violation(count: int) -> None:
    """UT03-30 1,001 values (or none) -> SchemaViolation."""
    with pytest.raises(SchemaViolation, match="invalid record_id for vector filter"):
        lance_filter_in("record_id", [f"s:e:{i}" for i in range(count)])


def test_ut03_30_unknown_column_or_bare_string_is_schema_violation() -> None:
    """UT03-30 a column outside the allowlist or a str as values -> SchemaViolation."""
    with pytest.raises(SchemaViolation, match="invalid column for vector filter"):
        lance_filter_in("vector", [HASH])  # type: ignore[arg-type]
    with pytest.raises(SchemaViolation, match="invalid content_hash for vector filter"):
        lance_filter_in("content_hash", HASH)


# --- UT03-31: run_embed_stage -------------------------------------------------------------------


def _run(
    wh: duckdb.DuckDBPyConnection,
    encoder: FakeEncoder,
    ctx: FakeJobContext | None = None,
) -> Report:
    report = Report()
    run_embed_stage(wh, encoder=encoder, ctx=ctx or FakeJobContext(), report=report)  # type: ignore[arg-type]
    return report


def test_ut03_31_three_encodes_reuse_and_orphan_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-31 5 records, 2 sharing a hash, 1 existing hash, 1 orphan: 3 encodes, reuse, delete."""
    store = use_store(tmp_path, monkeypatch)
    add_rows(store, [("servicenow:incident:GONE1", "printer jam again", MODEL_ID)])
    recs = [
        Rec("incident", "INC1", "disk full on db01"),
        Rec("incident", "INC2", "disk full on db01"),  # shares INC1's hash
        Rec("change", "CHG1", "patch the vpn", opened_at=None, planned_start=T1),
        Rec("problem", "PRB1", "memory leak in queue", service_id=None),
        Rec("incident", "INC3", "printer jam again"),  # hash stored for the orphan
    ]
    wh = embed_warehouse(tmp_path / "wh.duckdb", recs)
    encoder = FakeEncoder()
    ctx = FakeJobContext()
    report = _run(wh, encoder, ctx)

    assert sorted(encoder.inputs) == ["disk full on db01", "memory leak in queue", "patch the vpn"]
    assert (report.embedded, report.cache_hits, report.rows) == (3, 1, 5)
    rows = stored(store)
    assert set(rows) == {rec.record_id for rec in recs}  # the orphan is gone
    for rec in recs:
        row = rows[rec.record_id]
        assert rec.text is not None
        assert row["content_hash"] == content_hash(rec.text)
        assert row["model"] == MODEL_ID
        assert row["entity"] == rec.entity
        assert np.allclose(row["vector"], unit_vector(rec.text))
    assert rows["servicenow:problem:PRB1"]["service_id"] is None
    assert rows["servicenow:incident:INC1"]["service_id"] == "servicenow:cmdb_ci:SVC1"
    assert rows["servicenow:change:CHG1"]["opened_at"] == T1  # coalesce(opened_at, planned_start)
    assert rows["servicenow:incident:INC1"]["opened_at"] == T0
    assert ctx.heartbeats == []  # fewer than 20 batches: no checkpoint beat


def test_ut03_31_current_rows_untouched_other_model_reencoded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-31 a current row is not re-upserted; a row of another model is re-encoded."""
    store = use_store(tmp_path, monkeypatch)
    add_rows(
        store,
        [
            ("servicenow:incident:INC1", "disk full", MODEL_ID),
            ("servicenow:incident:INC2", "vpn down", "old/model@x"),
            ("servicenow:incident:INC3", "stale text", MODEL_ID),
        ],
    )
    recs = [
        Rec("incident", "INC1", "disk full"),
        Rec("incident", "INC2", "vpn down"),
        Rec("incident", "INC3", "fresh text"),
        Rec("incident", "INC4", None),  # no redacted text: nothing to embed, not an orphan
    ]
    wh = embed_warehouse(tmp_path / "wh.duckdb", recs)
    encoder = FakeEncoder()
    version = store.table("ticket_embedding").version
    report = _run(wh, encoder)
    assert sorted(encoder.inputs) == ["fresh text", "vpn down"]
    assert (report.embedded, report.cache_hits, report.rows) == (2, 0, 2)
    rows = stored(store)
    assert set(rows) == {r.record_id for r in recs[:3]}
    assert {row["model"] for row in rows.values()} == {MODEL_ID}
    assert rows["servicenow:incident:INC3"]["content_hash"] == content_hash("fresh text")
    assert store.table("ticket_embedding").version > version


def test_ut03_31_nothing_to_do_encodes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-31 an empty warehouse: no encode, no upsert, zero counts."""
    store = use_store(tmp_path, monkeypatch)
    wh = embed_warehouse(tmp_path / "wh.duckdb", [])
    encoder = FakeEncoder()
    with capture_logs() as logs:
        report = _run(wh, encoder)
    assert encoder.calls == []
    assert (report.embedded, report.cache_hits, report.rows) == (0, 0, 0)
    assert store.count("ticket_embedding") == 0
    done = [e for e in logs if e["event"] == "enrich.embed.completed"]
    assert len(done) == 1
    assert (done[0]["embedded"], done[0]["rows"], done[0]["deleted"]) == (0, 0, 0)


def _many(count: int, *, prefix: str = "ticket") -> list[Rec]:
    return [Rec("incident", f"INC{i:05d}", f"{prefix} {i:05d}") for i in range(count)]


def test_ut03_31_flush_every_20_batches_with_heartbeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-31 batch 8, 170 new hashes: flush after batch 20 (160 rows) + heartbeat, then 10."""
    store = use_store(tmp_path, monkeypatch)
    use_batch_size(monkeypatch, 8)
    wh = embed_warehouse(tmp_path / "wh.duckdb", _many(170))
    encoder = FakeEncoder()
    ctx = FakeJobContext()
    with capture_logs() as logs:
        report = _run(wh, encoder, ctx)
    flushed = [e["rows"] for e in logs if e["event"] == "enrich.embed.batch_flushed"]
    assert flushed == [160, 10]
    assert ctx.heartbeats == ["embed"]
    assert ctx.yield_checks == 1
    assert len(encoder.calls) == 22
    assert all(len(call) <= 8 for call in encoder.calls)
    assert (report.embedded, report.rows) == (170, 170)
    assert store.count("ticket_embedding") == 170


def test_ut03_31_shared_hash_buffer_is_bounded_in_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-31 one hash shared by 200 records: buffer flushes at 20 x batch rows (160)."""
    store = use_store(tmp_path, monkeypatch)
    use_batch_size(monkeypatch, 8)
    recs = [Rec("incident", f"INC{i:04d}", "same text") for i in range(200)]
    wh = embed_warehouse(tmp_path / "wh.duckdb", recs)
    encoder = FakeEncoder()
    with capture_logs() as logs:
        report = _run(wh, encoder)
    flushed = [e["rows"] for e in logs if e["event"] == "enrich.embed.batch_flushed"]
    assert flushed == [160, 40]
    assert encoder.inputs == ["same text"]
    assert (report.embedded, report.cache_hits, report.rows) == (1, 0, 200)
    assert store.count("ticket_embedding") == 200


def test_ut03_31_yield_after_flush(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT03-31 should_yield at the 20-batch checkpoint: flush first, then YieldRequested."""
    store = use_store(tmp_path, monkeypatch)
    use_batch_size(monkeypatch, 8)
    wh = embed_warehouse(tmp_path / "wh.duckdb", _many(170))
    ctx = FakeJobContext(yield_after=0)
    report = Report()
    with pytest.raises(YieldRequested) as info:
        run_embed_stage(wh, encoder=FakeEncoder(), ctx=ctx, report=report)  # type: ignore[arg-type]
    assert info.value.stage == "embed"
    assert store.count("ticket_embedding") == 160
    assert (report.embedded, report.rows) == (160, 160)
    assert ctx.heartbeats == ["embed"]


def test_ut03_31_wrong_vector_width_is_schema_violation_before_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-31 (M2) encoder rows of width 512 -> SchemaViolation, no row written, no text."""
    store = use_store(tmp_path, monkeypatch)
    use_batch_size(monkeypatch, 8)
    wh = embed_warehouse(tmp_path / "wh.duckdb", _many(30, prefix="secret"))
    with pytest.raises(SchemaViolation, match="embedding shape") as info:
        _run(wh, FakeEncoder(width=512))
    assert "secret" not in str(info.value)
    assert store.count("ticket_embedding") == 0


def test_ut03_31_non_float_or_too_many_rows_is_schema_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-31 (M2) integer rows or more rows than texts -> SchemaViolation, nothing written."""
    store = use_store(tmp_path, monkeypatch)
    wh = embed_warehouse(tmp_path / "wh.duckdb", _many(3))

    class IntEncoder(FakeEncoder):
        def encode(self, texts: Any, *, batch_size: int) -> np.ndarray:
            return np.ones((len(texts), 1024), dtype=np.int32)

    class ExtraEncoder(FakeEncoder):
        def encode(self, texts: Any, *, batch_size: int) -> np.ndarray:
            return np.stack([unit_vector("x")] * (len(texts) + 1))

    for encoder in (IntEncoder(), ExtraEncoder()):
        with pytest.raises(SchemaViolation, match="embedding shape"):
            _run(wh, encoder)
    assert store.count("ticket_embedding") == 0


def _patch_lance(monkeypatch: pytest.MonkeyPatch, name: str, message: str) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(message)

    monkeypatch.setattr(LanceTable, name, boom)


@pytest.mark.parametrize(
    ("method", "message", "error"),
    [
        ("merge_insert", "Commit conflict for version 3", StoreBusy),
        ("merge_insert", "io failure", SchemaViolation),
        ("delete", "table lock held", StoreBusy),
    ],
)
def test_ut03_31_lance_errors_map_to_store_busy_or_schema_violation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    message: str,
    error: type[Exception],
) -> None:
    """UT03-31 LanceDB conflict or lock -> StoreBusy; any other failure -> SchemaViolation."""
    store = use_store(tmp_path, monkeypatch)
    add_rows(store, [("servicenow:incident:GONE1", "old", MODEL_ID)])
    wh = embed_warehouse(tmp_path / "wh.duckdb", [Rec("incident", "INC1", "disk full")])
    _patch_lance(monkeypatch, method, message)
    with pytest.raises(error, match="ticket_embedding"):
        _run(wh, FakeEncoder())


def test_ut03_31_warehouse_error_is_schema_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-31 a missing core table -> SchemaViolation naming only the error class."""
    use_store(tmp_path, monkeypatch)
    wh = embed_warehouse(tmp_path / "wh.duckdb", [Rec("incident", "INC1", "disk full")])
    wh.execute("DROP TABLE core.problem")
    with pytest.raises(SchemaViolation, match="embed stage: CatalogException"):
        _run(wh, FakeEncoder())


def test_ut03_31_default_store_path_and_config_batch_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-31 the stage opens `VectorStore()` and reads `embedding.batch_size` from config."""
    opened: list[Path] = []
    path = tmp_path / "vectors"

    def store() -> VectorStore:
        opened.append(path)
        return VectorStore(path)

    monkeypatch.setattr(embed_stage, "VectorStore", store)
    wh = embed_warehouse(tmp_path / "wh.duckdb", _many(3))
    encoder = FakeEncoder()
    _run(wh, encoder)
    assert opened == [path]
    assert encoder.calls == [[r.text for r in _many(3)]]  # one batch of up to 128


# --- UT03-32: maintain_index --------------------------------------------------------------------


class _FakeTable:
    """count_rows / create_index / optimize / field metadata of a LanceDB table, recorded."""

    def __init__(self, rows: int) -> None:
        self.rows = rows
        self.meta: dict[bytes, bytes] | None = None
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def count_rows(self) -> int:
        return self.rows

    @property
    def schema(self) -> Any:
        return TICKET_EMBEDDING_SCHEMA.set(
            TICKET_EMBEDDING_SCHEMA.get_field_index("vector"),
            TICKET_EMBEDDING_SCHEMA.field("vector").with_metadata(self.meta),
        )

    def create_index(self, column: str, **kwargs: Any) -> None:
        self.calls.append(("create_index", {"column": column, **kwargs}))

    def update_field_metadata(self, *updates: dict[str, Any]) -> None:
        for update in updates:
            assert update["path"] == "vector"
            self.meta = {k.encode(): v.encode() for k, v in update["metadata"].items()}

    def optimize(self) -> None:
        self.calls.append(("optimize", {}))


def test_ut03_32_skipped_rebuilt_optimized_rebuilt() -> None:
    """UT03-32 5k skipped, 20k rebuilt, 23k optimized, 25k rebuilt (> 1.2 x 20k)."""
    table = _FakeTable(5_000)
    with capture_logs() as logs:
        assert maintain_index(table) == "skipped"  # type: ignore[arg-type]
        table.rows = 20_000
        assert maintain_index(table) == "rebuilt"  # type: ignore[arg-type]
        table.rows = 23_000
        assert maintain_index(table) == "optimized"  # type: ignore[arg-type]
        table.rows = 25_000
        assert maintain_index(table) == "rebuilt"  # type: ignore[arg-type]
    assert [name for name, _ in table.calls] == ["create_index", "optimize", "create_index"]
    config = table.calls[0][1]["config"]
    assert table.calls[0][1]["column"] == "vector"
    assert table.calls[0][1]["replace"] is True
    assert config.distance_type == "cosine"
    assert config.num_partitions == round(math.sqrt(20_000))
    assert config.num_sub_vectors == 64
    assert table.calls[2][1]["config"].num_partitions == round(math.sqrt(25_000))
    assert table.meta == {INDEX_ROWS_KEY.encode(): b"25000"}
    events = [(e["event"], e["log_level"], e["action"], e["rows"]) for e in logs]
    assert events == [
        ("enrich.embed.index_maintained", "info", "skipped", 5_000),
        ("enrich.embed.index_maintained", "info", "rebuilt", 20_000),
        ("enrich.embed.index_maintained", "info", "optimized", 23_000),
        ("enrich.embed.index_maintained", "info", "rebuilt", 25_000),
    ]


def test_ut03_32_unreadable_index_rows_counts_as_never_built() -> None:
    """UT03-32 a non-integer `herness.index_rows` value -> rebuilt."""
    table = _FakeTable(12_000)
    table.meta = {INDEX_ROWS_KEY.encode(): b"many"}
    assert maintain_index(table) == "rebuilt"  # type: ignore[arg-type]
    assert table.meta == {INDEX_ROWS_KEY.encode(): b"12000"}


@pytest.mark.parametrize(
    ("message", "error"), [("dataset lock busy", StoreBusy), ("bad input", SchemaViolation)]
)
def test_ut03_32_lance_error_mapping(message: str, error: type[Exception]) -> None:
    """UT03-32 a LanceDB error -> StoreBusy for lock/conflict, else SchemaViolation."""
    table = _FakeTable(20_000)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise ValueError(message)

    table.create_index = boom  # type: ignore[method-assign]
    with pytest.raises(error, match="ticket_embedding"):
        maintain_index(table)  # type: ignore[arg-type]


def _real_table(tmp_path: Path, rows: int) -> Callable[[], Any]:
    import pyarrow as pa  # noqa: PLC0415 - only this helper builds bulk rows

    store = VectorStore(tmp_path / "vectors")
    store.ensure_tables()
    vectors = np.random.default_rng(7).standard_normal((rows, 1024)).astype(np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    table = pa.table(
        {
            "record_id": [f"s:incident:I{i}" for i in range(rows)],
            "entity": ["incident"] * rows,
            "service_id": pa.nulls(rows, pa.string()),
            "opened_at": pa.nulls(rows, pa.timestamp("us", tz="UTC")),
            "content_hash": [f"{i:032x}" for i in range(rows)],
            "model": [MODEL_ID] * rows,
            "vector": pa.FixedSizeListArray.from_arrays(pa.array(vectors.ravel()), 1024),
        },
        schema=TICKET_EMBEDDING_SCHEMA,
    )
    store.table("ticket_embedding").add(table)
    return lambda: VectorStore(tmp_path / "vectors").table("ticket_embedding")


def test_ut03_32_real_lancedb_index_and_metadata_round_trip(tmp_path: Path) -> None:
    """UT03-32 real table of 10k rows: rebuilt, `herness.index_rows` persists, then optimized."""
    reopen = _real_table(tmp_path, 10_000)
    assert maintain_index(reopen()) == "rebuilt"
    table = reopen()
    assert table.schema.field("vector").metadata == {INDEX_ROWS_KEY.encode(): b"10000"}
    assert len(table.list_indices()) == 1
    assert maintain_index(table) == "optimized"
    assert reopen().schema.field("vector").metadata == {INDEX_ROWS_KEY.encode(): b"10000"}
