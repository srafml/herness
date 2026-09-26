"""Security test for herness.store.vectors history purge (ST02-09, TH02-09)."""

from pathlib import Path

import pyarrow as pa
import pytest

from herness.store.vectors import EMBEDDING_DIM, MEMORY_EMBEDDING_SCHEMA, VectorStore

pytestmark = pytest.mark.integration


def memory_rows(ids: list[str], value: float) -> pa.Table:
    n = len(ids)
    vec_type = pa.list_(pa.float32(), EMBEDDING_DIM)
    return pa.table(
        {
            "memory_id": ids,
            "layer": ["team"] * n,
            "kind": ["fact"] * n,
            "status": ["active"] * n,
            "content_hash": [f"h-{i}" for i in ids],
            "model": ["bge-m3"] * n,
            "vector": pa.array([[value] * EMBEDDING_DIM] * n, vec_type),
        },
        schema=MEMORY_EMBEDDING_SCHEMA,
    )


def test_st02_09_deleted_vector_absent_in_every_version(tmp_path: Path) -> None:
    """ST02-09 delete, purge, then open every remaining version: deleted vector absent in all."""
    store = VectorStore(tmp_path / "vectors")
    store.ensure_tables()
    table = store.table("memory_embedding")
    table.add(memory_rows(["m1", "m2"], 0.1))
    table.add(memory_rows(["m3"], 0.2))
    assert store.delete_ids("memory_embedding", "memory_id", ["m2"]) == 1
    store.purge_history("memory_embedding")

    versions = store.table("memory_embedding").list_versions()
    assert versions
    for entry in versions:
        view = store.table("memory_embedding")
        view.checkout(entry["version"])
        ids = view.to_arrow().column("memory_id").to_pylist()
        assert "m2" not in ids
        assert sorted(ids) == ["m1", "m3"]
