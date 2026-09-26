"""Tests for herness.connectors.rows RowBatcher and tombstone_batch (U01-25, U01-26, U01-19)."""

import datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from herness.connectors import rows
from herness.connectors.base import METADATA_SCHEMA
from herness.core.errors import ConfigError, SchemaViolation
from herness.store.lake import LakeWriter

pytestmark = pytest.mark.unit

UTC = datetime.UTC
T0 = datetime.datetime(2024, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)
FETCHED = datetime.datetime(2024, 1, 3, tzinfo=UTC)


def _batcher(**kwargs: object) -> rows.RowBatcher:
    return rows.RowBatcher("servicenow", "incident", clock=lambda: FETCHED, **kwargs)  # type: ignore[arg-type]


def _feed(batcher: rows.RowBatcher) -> list[pa.RecordBatch]:
    out: list[pa.RecordBatch] = []
    for i in range(7):
        fields = {"number": f"INC{i}", "state": None}
        if i >= 4:
            fields["extra"] = f"x{i}"
        batch = batcher.add(f"k{i}", T0 + datetime.timedelta(seconds=i), '{"i":1}', fields)
        if batch is not None:
            out.append(batch)
    last = batcher.flush()
    assert last is not None
    return [*out, last]


def test_ut01_18_row_batcher_batches_and_schema() -> None:
    """UT01-18 batch_rows 3 over 7 rows gives 3/3/1; new column on row 5 is null-filled later."""
    batcher = _batcher(batch_rows=3, columns=("state",))
    batches = _feed(batcher)
    assert [b.num_rows for b in batches] == [3, 3, 1]
    assert batcher.rows_emitted == 7
    assert batcher.flush() is None
    field_cols = [pa.field(n, pa.string()) for n in ("state", "number")]
    assert batches[0].schema == pa.schema([*METADATA_SCHEMA, *field_cols])
    extended = pa.schema([*METADATA_SCHEMA, *field_cols, pa.field("extra", pa.string())])
    assert batches[1].schema == extended
    assert batches[2].schema == extended
    assert batches[1].column("extra").to_pylist() == [None, "x4", "x5"]
    first = batches[0].to_pylist()[0]
    assert first["_record_id"] == "servicenow:incident:k0"
    assert (first["_source"], first["_entity"], first["_source_key"]) == (
        "servicenow",
        "incident",
        "k0",
    )
    assert first["_source_updated_at"] == T0
    assert first["_fetched_at"] == FETCHED
    assert first["_deleted"] is False
    assert first["number"] == "INC0"


def test_ut01_18_row_batcher_batch_accepted_by_lake_writer(tmp_path: Path) -> None:
    """UT01-18 a RowBatcher batch with a tombstone is accepted by a real LakeWriter."""
    batcher = _batcher(batch_rows=10)
    batcher.add("k1", T0, '{"a":1}', {"number": "INC1"})
    batcher.add_tombstone("k2", T0)
    batch = batcher.flush()
    assert batch is not None
    writer = LakeWriter("servicenow", "incident", root=tmp_path)
    writer.write(batch)
    files = writer.commit()
    assert files.rows == 2
    assert files.max_source_updated_at == T0
    table = pq.read_table(files.files[0])
    assert table.schema.names == [*METADATA_SCHEMA.names, "number"]
    assert table.column("_deleted").to_pylist() == [False, True]
    assert table.column("number").to_pylist() == ["INC1", None]


def test_ut01_18_row_batcher_tombstone_and_default_clock() -> None:
    """UT01-18 tombstones have null payload and fields; the default clock is time.now."""
    batcher = rows.RowBatcher("jira", "issue", batch_rows=1)
    batch = batcher.add_tombstone(
        "ABC-1", T0.astimezone(datetime.timezone(datetime.timedelta(hours=5)))
    )
    assert batch is not None
    row = batch.to_pylist()[0]
    assert (row["_deleted"], row["_payload"]) == (True, None)
    assert row["_source_updated_at"] == T0
    assert row["_fetched_at"] > T0


@pytest.mark.parametrize("batch_rows", [0, -1, True, 1.5])
def test_ut01_18_row_batcher_bad_batch_rows(batch_rows: object) -> None:
    """UT01-18 batch_rows below 1 or not an integer raises ConfigError."""
    with pytest.raises(ConfigError):
        _batcher(batch_rows=batch_rows)


def test_ut01_18_row_batcher_errors() -> None:
    """UT01-18 naive times, bad keys and metadata-named fields raise SchemaViolation."""
    batcher = _batcher(batch_rows=5)
    with pytest.raises(SchemaViolation, match="naive"):
        batcher.add("k", T0.replace(tzinfo=None), "{}", {})
    with pytest.raises(SchemaViolation, match="naive"):
        batcher.add_tombstone("k", T0.replace(tzinfo=None))
    with pytest.raises(SchemaViolation, match="invalid record key"):
        batcher.add("", T0, "{}", {})
    with pytest.raises(SchemaViolation, match="column collision"):
        batcher.add("k", T0, "{}", {"_payload": "x"})
    with pytest.raises(SchemaViolation, match="column collision"):
        _batcher(batch_rows=5, columns=("_deleted",))
    naive_clock = rows.RowBatcher(
        "servicenow", "incident", batch_rows=1, clock=lambda: FETCHED.replace(tzinfo=None)
    )
    with pytest.raises(SchemaViolation, match="naive"):
        naive_clock.add("k", T0, "{}", {})


def test_ut01_19_tombstone_batch() -> None:
    """UT01-19 three keys give deleted rows with null payload and correct record ids."""
    keys = pa.array(["a", "b:c", "d"], pa.string())
    batch = rows.tombstone_batch("mongodb", "orders", keys, deleted_at=T0, fetched_at=FETCHED)
    assert batch.schema == METADATA_SCHEMA
    assert batch.column("_record_id").to_pylist() == [
        "mongodb:orders:a",
        "mongodb:orders:b:c",
        "mongodb:orders:d",
    ]
    assert batch.column("_deleted").to_pylist() == [True] * 3
    assert batch.column("_payload").to_pylist() == [None] * 3
    assert batch.column("_source").to_pylist() == ["mongodb"] * 3
    assert batch.column("_entity").to_pylist() == ["orders"] * 3
    assert batch.column("_source_updated_at").to_pylist() == [T0] * 3
    assert batch.column("_fetched_at").to_pylist() == [FETCHED] * 3


@pytest.mark.parametrize(
    ("source", "keys", "deleted_at"),
    [
        ("mongodb", pa.array([], pa.string()), T0),
        ("mongodb", pa.array(["a", None], pa.string()), T0),
        ("mongodb", pa.array(["a", ""], pa.string()), T0),
        ("mongodb", pa.array(["a", "k" * 513], pa.string()), T0),
        ("mongodb", pa.array(["a", "b\nc"], pa.string()), T0),
        ("mongodb", pa.array([1, 2]), T0),
        ("mongodb", pa.array(["a"], pa.large_string()), T0),
        ("mongodb", ["a"], T0),
        ("MongoDB", pa.array(["a"], pa.string()), T0),
        ("mongodb", pa.array(["a"], pa.string()), T0.replace(tzinfo=None)),
    ],
)
def test_ut01_19_tombstone_batch_errors(
    source: str, keys: pa.Array, deleted_at: datetime.datetime
) -> None:
    """UT01-19 violated preconditions raise SchemaViolation."""
    with pytest.raises(SchemaViolation):
        rows.tombstone_batch(source, "orders", keys, deleted_at=deleted_at, fetched_at=FETCHED)


def test_ut01_19_tombstone_batch_limit() -> None:
    """UT01-19 more than 100,000 keys raise SchemaViolation."""
    keys = pa.array(["k"] * 100_001, pa.string())
    with pytest.raises(SchemaViolation):
        rows.tombstone_batch("mongodb", "orders", keys, deleted_at=T0, fetched_at=FETCHED)
