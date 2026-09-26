"""Tests for herness.store.vectors (U02-63 … U02-70): LanceDB tables, deletes, purge, health."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import lancedb
import pyarrow as pa
import pytest

from herness.core.errors import ConfigError, SchemaViolation, StoreBusy
from herness.store import vectors
from herness.store.errors import NotFoundError
from herness.store.layout import DataLayout
from herness.store.vectors import (
    EMBEDDING_DIM,
    MEMORY_EMBEDDING_SCHEMA,
    TICKET_EMBEDDING_SCHEMA,
    VectorStore,
)

pytestmark = pytest.mark.unit

VEC = pa.list_(pa.float32(), EMBEDDING_DIM)
INJECTED = "x' OR '1'='1"


def ticket_rows(ids: list[str]) -> pa.Table:
    n = len(ids)
    return pa.table(
        {
            "record_id": ids,
            "entity": ["issue"] * n,
            "service_id": [None] * n,
            "opened_at": pa.array([None] * n, pa.timestamp("us", tz="UTC")),
            "content_hash": [f"h{i}" for i in range(n)],
            "model": ["bge-m3"] * n,
            "vector": pa.array([[0.5] * EMBEDDING_DIM] * n, VEC),
        },
        schema=TICKET_EMBEDDING_SCHEMA,
    )


@pytest.fixture
def store(tmp_path: Path) -> VectorStore:
    vs = VectorStore(tmp_path / "vectors")
    vs.ensure_tables()
    return vs


def test_ut02_49_schema_constants() -> None:
    """UT02-49 schema constants carry the declared names, types and nullability."""
    assert EMBEDDING_DIM == 1024
    ticket = TICKET_EMBEDDING_SCHEMA
    assert ticket.names == [
        "record_id",
        "entity",
        "service_id",
        "opened_at",
        "content_hash",
        "model",
        "vector",
    ]
    assert ticket.field("opened_at").type == pa.timestamp("us", tz="UTC")
    assert ticket.field("vector").type == VEC
    assert {f.name for f in ticket if f.nullable} == {"service_id", "opened_at"}
    memory = MEMORY_EMBEDDING_SCHEMA
    assert memory.names == [
        "memory_id",
        "layer",
        "kind",
        "status",
        "content_hash",
        "model",
        "vector",
    ]
    assert all(not f.nullable for f in memory)
    assert all(f.type == pa.string() for f in memory if f.name != "vector")
    assert memory.field("vector").type == VEC


def test_ut02_49_ensure_twice_table_and_count(tmp_path: Path) -> None:
    """UT02-49 ensure twice is idempotent; tables open with their schemas; count."""
    vs = VectorStore(tmp_path / "vectors")
    assert (tmp_path / "vectors").is_dir()
    vs.ensure_tables()
    vs.ensure_tables()
    for name, schema in (
        ("ticket_embedding", TICKET_EMBEDDING_SCHEMA),
        ("memory_embedding", MEMORY_EMBEDDING_SCHEMA),
    ):
        table = vs.table(name)
        assert table.schema.names == schema.names
        assert [f.type for f in table.schema] == [f.type for f in schema]
        assert vs.count(name) == 0
    vs.table("ticket_embedding").add(ticket_rows(["a", "b"]))
    assert vs.count("ticket_embedding") == 2


def test_ut02_49_unknown_name_is_config_error(store: VectorStore) -> None:
    """UT02-49 an unknown table name raises ConfigError on every entry point."""
    bad: Any = "users"
    with pytest.raises(ConfigError):
        store.table(bad)
    with pytest.raises(ConfigError):
        store.count(bad)
    with pytest.raises(ConfigError):
        store.delete_ids(bad, "record_id", ["a"])
    with pytest.raises(ConfigError):
        store.purge_history(bad)


def test_ut02_49_missing_table_not_found(tmp_path: Path) -> None:
    """UT02-49 table and count on an absent table raise NotFoundError."""
    vs = VectorStore(tmp_path / "vectors")
    with pytest.raises(NotFoundError) as info:
        vs.count("memory_embedding")
    assert (info.value.kind, info.value.key) == ("vector_table", "memory_embedding")


def test_ut02_49_schema_missing_field(tmp_path: Path) -> None:
    """UT02-49 an existing table missing a field raises SchemaViolation naming it."""
    path = tmp_path / "vectors"
    wrong = pa.schema([pa.field("memory_id", pa.string(), nullable=False)])
    lancedb.connect(str(path)).create_table("memory_embedding", schema=wrong)
    with pytest.raises(SchemaViolation, match="memory_embedding schema mismatch: layer"):
        VectorStore(path).ensure_tables()


def test_ut02_49_schema_type_mismatch(tmp_path: Path) -> None:
    """UT02-49 a field with the right name but the wrong type is a mismatch."""
    path = tmp_path / "vectors"
    fields = [
        pa.field(f.name, pa.int64() if f.name == "kind" else f.type)
        for f in MEMORY_EMBEDDING_SCHEMA
    ]
    lancedb.connect(str(path)).create_table("memory_embedding", schema=pa.schema(fields))
    with pytest.raises(SchemaViolation, match="schema mismatch: kind"):
        VectorStore(path).ensure_tables()


def test_ut02_49_schema_extra_field(tmp_path: Path) -> None:
    """UT02-49 an extra field in an existing table is a mismatch."""
    path = tmp_path / "vectors"
    schema = MEMORY_EMBEDDING_SCHEMA.append(pa.field("extra", pa.string()))
    lancedb.connect(str(path)).create_table("memory_embedding", schema=schema)
    with pytest.raises(SchemaViolation, match="schema mismatch: extra"):
        VectorStore(path).ensure_tables()


def test_ut02_49_concurrent_create_tolerated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT02-49 an "already exists" error from a racing creator is tolerated by re-listing."""
    path = tmp_path / "vectors"
    vs = VectorStore(path)
    other = lancedb.connect(str(path))
    real_names = vs._table_names
    calls = {"n": 0}

    def stale_names() -> set[str]:
        calls["n"] += 1
        if calls["n"] == 1:
            other.create_table("ticket_embedding", schema=TICKET_EMBEDDING_SCHEMA)
            other.create_table("memory_embedding", schema=MEMORY_EMBEDDING_SCHEMA)
            return set()
        return real_names()

    monkeypatch.setattr(vs, "_table_names", stale_names)
    vs.ensure_tables()
    assert vs.count("ticket_embedding") == 0


def test_ut02_49_create_failure_is_schema_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT02-49 a create error other than "already exists" becomes SchemaViolation."""
    vs = VectorStore(tmp_path / "vectors")

    def boom(*_a: object, **_k: object) -> None:
        msg = "disk exploded"
        raise ValueError(msg)

    monkeypatch.setattr(vs._db, "create_table", boom)
    with pytest.raises(SchemaViolation, match="cannot create vector table ticket_embedding"):
        vs.ensure_tables()


def test_ut02_49_connect_failure(tmp_path: Path) -> None:
    """UT02-49 a path that cannot become a directory raises SchemaViolation."""
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(SchemaViolation, match="cannot open vector store"):
        VectorStore(blocker / "vectors")


def test_ut02_49_default_path_from_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT02-49 path None resolves data_layout().vectors."""
    monkeypatch.setattr(vectors, "data_layout", lambda: DataLayout.from_root(tmp_path))
    VectorStore()
    assert (tmp_path / "vectors").is_dir()


def test_ut02_50_delete_two_ids(store: VectorStore) -> None:
    """UT02-50 three rows, delete two IDs: returns 2, one row left."""
    ids = ["jira:issue:1", "jira:issue:2", "jira:issue:3"]
    store.table("ticket_embedding").add(ticket_rows(ids))
    assert store.delete_ids("ticket_embedding", "record_id", [ids[0], ids[2]]) == 2
    assert store.count("ticket_embedding") == 1
    remaining = store.table("ticket_embedding").to_arrow().column("record_id").to_pylist()
    assert remaining == ["jira:issue:2"]


def test_ut02_50_delete_by_content_hash_chunked(store: VectorStore) -> None:
    """UT02-50 deletes run in chunks of 500 ids; unknown ids count zero."""
    store.table("ticket_embedding").add(ticket_rows([f"r{i}" for i in range(1203)]))
    hashes = [f"h{i}" for i in range(1203)] + ["missing"]
    assert store.delete_ids("ticket_embedding", "content_hash", hashes) == 1203
    assert store.count("ticket_embedding") == 0


def test_ut02_50_delete_nothing_matching(store: VectorStore) -> None:
    """UT02-50 ids that match nothing return 0 and create no version."""
    before = store.table("memory_embedding").version
    assert store.delete_ids("memory_embedding", "memory_id", ["m1"]) == 0
    assert store.table("memory_embedding").version == before


@pytest.mark.parametrize(
    ("name", "column", "ids"),
    [
        ("ticket_embedding", "memory_id", ["a"]),
        ("memory_embedding", "record_id", ["a"]),
        ("ticket_embedding", "vector", ["a"]),
        ("ticket_embedding", "record_id", []),
        ("ticket_embedding", "record_id", "abc"),
        ("ticket_embedding", "record_id", ["a b"]),
        ("ticket_embedding", "record_id", [7]),
        ("ticket_embedding", "record_id", ["x" * 257]),
        ("ticket_embedding", "record_id", ["a"] * 100_001),
    ],
)
def test_ut02_50_delete_rejects_bad_input(
    store: VectorStore, name: Any, column: str, ids: Any
) -> None:
    """UT02-50 non-allowlisted column, bad or wrong-count ids raise ConfigError."""
    with pytest.raises(ConfigError):
        store.delete_ids(name, column, ids)


def test_st02_08_filter_injection_rejected(store: VectorStore) -> None:
    """ST02-08 an injected id raises ConfigError and leaves the row count unchanged."""
    store.table("ticket_embedding").add(ticket_rows(["a", "b", "c"]))
    with pytest.raises(ConfigError) as info:
        store.delete_ids("ticket_embedding", "record_id", ["a", INJECTED])
    assert INJECTED not in str(info.value)
    assert store.count("ticket_embedding") == 3


def test_ut02_50_commit_conflict_is_store_busy(
    store: VectorStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT02-50 a LanceDB commit conflict on delete is mapped to StoreBusy."""
    store.table("ticket_embedding").add(ticket_rows(["a"]))
    real_open = store._db.open_table

    class Conflicting:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def count_rows(self, *args: Any) -> int:
            return int(self._inner.count_rows(*args))

        def delete(self, _where: str) -> None:
            msg = "Retryable commit conflict for version 3"
            raise RuntimeError(msg)

    monkeypatch.setattr(store._db, "open_table", lambda n: Conflicting(real_open(n)))
    with pytest.raises(StoreBusy):
        store.delete_ids("ticket_embedding", "record_id", ["a"])


def test_ut02_51_purge_removes_old_versions(store: VectorStore) -> None:
    """UT02-51 delete then purge: previous versions cannot be checked out (OI-12 API check)."""
    table = store.table("ticket_embedding")
    # OI-12: the pinned lancedb exposes optimize(cleanup_older_than=, delete_unverified=);
    # compact_files() needs the uninstalled pylance package and is deprecated.
    assert hasattr(table, "optimize")
    table.add(ticket_rows(["a", "b", "c"]))
    added_version = table.version
    store.delete_ids("ticket_embedding", "record_id", ["b"])
    store.purge_history("ticket_embedding")
    fresh = store.table("ticket_embedding")
    assert len(fresh.list_versions()) == 1
    with pytest.raises(ValueError, match="no longer exists"):
        fresh.checkout(added_version)
    assert sorted(fresh.to_arrow().column("record_id").to_pylist()) == ["a", "c"]


@pytest.mark.parametrize(
    ("message", "expected"),
    [("commit conflict on manifest", StoreBusy), ("io failure", SchemaViolation)],
)
def test_ut02_51_purge_error_mapping(
    store: VectorStore,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
    expected: type[Exception],
) -> None:
    """UT02-51 purge errors map to StoreBusy (conflict) or SchemaViolation (other)."""

    class Failing:
        def optimize(self, **_kwargs: object) -> None:
            raise OSError(message)

    monkeypatch.setattr(store._db, "open_table", lambda _n: Failing())
    with pytest.raises(expected):
        store.purge_history("memory_embedding")


def test_ut02_52_health_degraded_when_table_missing(tmp_path: Path) -> None:
    """UT02-52 a missing table makes health degraded."""
    status, reason = VectorStore(tmp_path / "vectors").health()
    assert status == "degraded"
    assert "memory_embedding" in reason


def test_ut02_52_health_ok_and_down(store: VectorStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT02-52 ok with both tables; down with the class name when listing fails."""
    assert store.health()[0] == "ok"

    def broken() -> set[str]:
        msg = "gone"
        raise OSError(msg)

    monkeypatch.setattr(store, "_table_names", broken)
    assert store.health() == ("down", "OSError")


def test_ut02_49_table_listing_follows_pages(
    store: VectorStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT02-49 table listing follows list_tables page tokens."""
    pages = {
        None: SimpleNamespace(tables=["ticket_embedding"], page_token="p2"),  # noqa: S106 - page token, not a secret
        "p2": SimpleNamespace(tables=["memory_embedding"], page_token=None),
    }
    monkeypatch.setattr(store._db, "list_tables", lambda page_token=None: pages[page_token])
    assert store.health()[0] == "ok"


def test_ut02_49_open_failure_other_than_missing(
    store: VectorStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT02-49 an open error that is not "not found" becomes SchemaViolation."""

    def corrupt(_name: str) -> None:
        msg = "corrupt manifest"
        raise ValueError(msg)

    monkeypatch.setattr(store._db, "open_table", corrupt)
    with pytest.raises(SchemaViolation, match="vector open failed on ticket_embedding"):
        store.table("ticket_embedding")
