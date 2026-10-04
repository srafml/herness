"""Seeded memory corpus for the recall and write benchmarks (BT07-01..BT07-03, T07-25).

Not a test module. `seed_corpus` bulk-inserts `n` active `memory_item` rows (one transaction
per chunk, the FTS triggers fire) into the migrated ops store selected by the caller and the
same number of random unit vectors into a LanceDB `memory_embedding` table at `root/vectors`;
seeding is never timed. Item text is drawn from a fixed synthetic vocabulary so keyword
queries built from it find candidates. `open_store` builds the `MemoryStore` facade on those
files with a caller-chosen embedding function.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

import numpy as np
import pyarrow as pa
from tests.unit.harness.memory._write_env import AUTHOR, PATTERNS, PLANTED_NAME

from herness.core.redact import Redactor
from herness.core.redact_directory import NameDirectory
from herness.core.settings import RedactionConfig
from herness.harness.memory import MemoryStore
from herness.harness.memory.settings import MemoryConfig
from herness.harness.memory.store import Embedder, VectorIndex
from herness.store.ops import core
from herness.store.vectors import EMBEDDING_DIM, MEMORY_EMBEDDING_SCHEMA, VectorStore

__all__ = [
    "ALLOWED",
    "VOCAB",
    "make_redactor",
    "mem_id",
    "open_store",
    "plant_matches",
    "query_text",
    "seed_corpus",
]

VOCAB: Final = tuple(f"term{i:04d}" for i in range(3000))
_KINDS: Final = (
    ("glossary", "semantic"), ("business_rule", "semantic"), ("analysis_recipe", "procedural"),
    ("run_summary", "episodic"),
)  # fmt: skip
_CHUNK: Final = 20_000
ALLOWED: Final = (r"\b(19|20)\d{2}\b", r"(INC|CHG|PRB)\d+")
_MODEL: Final = "bge-m3"
_STAMP: Final = "2026-08-01T00:00:00.000000Z"
_INSERT: Final = (
    "INSERT INTO memory_item (memory_id, layer, kind, content, data, provenance, confidence,"
    " status, created_at, expires_at, last_used_at, use_count)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, NULL, NULL, 0)"
)
_PROVENANCE: Final = json.dumps(
    {
        "author_type": "human",
        "author_ref": AUTHOR,
        "via": "cli",
        "run_id": None,
        "task_id": None,
        "author_role": None,
    }
)


def mem_id(n: int) -> str:
    """A valid memory id for ordinal `n` (decimal digits are Crockford digits)."""
    return f"mem_{n:026d}"


def query_text(rng: np.random.Generator, words: int = 6) -> str:
    """A keyword query of `words` vocabulary terms."""
    return " ".join(VOCAB[i] for i in rng.integers(0, len(VOCAB), size=words))


def make_redactor() -> Redactor:
    """The real `Redactor` with a one-name directory (as the unit writer env)."""
    directory = NameDirectory.from_files(None, (PLANTED_NAME,), None)
    return Redactor(RedactionConfig(directory_file=None), bytes(range(32)), directory)


def _rows(start: int, count: int, rng: np.random.Generator) -> list[tuple[object, ...]]:
    picks = rng.integers(0, len(VOCAB), size=(count, 12))
    out: list[tuple[object, ...]] = []
    for offset in range(count):
        n = start + offset
        kind, layer = _KINDS[n % len(_KINDS)]
        content = " ".join(VOCAB[i] for i in picks[offset])
        data = json.dumps({"content_hash": f"{n:064x}"})
        out.append((mem_id(n), layer, kind, content, data, _PROVENANCE, 0.8, _STAMP))
    return out


def _vectors(start: int, count: int, rng: np.random.Generator) -> pa.Table:
    vecs = rng.standard_normal((count, EMBEDDING_DIM), dtype=np.float32)
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
    kinds = [_KINDS[(start + i) % len(_KINDS)] for i in range(count)]
    return pa.table(
        {
            "memory_id": [mem_id(start + i) for i in range(count)],
            "layer": [layer for _, layer in kinds], "kind": [kind for kind, _ in kinds],
            "status": ["active"] * count,
            "content_hash": [f"{start + i:064x}" for i in range(count)],
            "model": [_MODEL] * count,
            "vector": pa.FixedSizeListArray.from_arrays(vecs.reshape(-1), EMBEDDING_DIM),
        },
        schema=MEMORY_EMBEDDING_SCHEMA,
    )  # fmt: skip


def seed_corpus(root: Path, n: int, *, seed: int = 7) -> None:
    """`n` active items and vectors; the ops store must be migrated and selected already."""
    rng = np.random.default_rng(seed)
    store = VectorStore(root / "vectors")
    store.ensure_tables()
    table = store.table("memory_embedding")
    for start in range(0, n, _CHUNK):
        count = min(_CHUNK, n - start)

        def insert(conn: sqlite3.Connection, s: int = start, c: int = count) -> None:
            conn.executemany(_INSERT, _rows(s, c, rng))

        core.run_write(insert, op="bench_seed")
        table.add(_vectors(start, count, rng))


def plant_matches(root: Path, first: int, texts: Sequence[str], vectors: np.ndarray) -> None:
    """Append one active glossary item per text, ordinals from `first`, with the given unit
    vectors (a true match for the query of the same text); the ops store must be selected."""
    stamp, count = _STAMP, len(texts)
    rows = [
        (mem_id(first + i), "semantic", "glossary", texts[i],
         json.dumps({"content_hash": f"{first + i:064x}"}), _PROVENANCE, 0.8, stamp)
        for i in range(count)
    ]  # fmt: skip
    core.run_write(lambda conn: conn.executemany(_INSERT, rows), op="bench_seed")
    table = VectorStore(root / "vectors").table("memory_embedding")
    flat = pa.array(np.asarray(vectors, dtype=np.float32).reshape(-1))
    table.add(
        pa.table(
            {
                "memory_id": [mem_id(first + i) for i in range(count)],
                "layer": ["semantic"] * count, "kind": ["glossary"] * count,
                "status": ["active"] * count,
                "content_hash": [f"{first + i:064x}" for i in range(count)],
                "model": [_MODEL] * count,
                "vector": pa.FixedSizeListArray.from_arrays(flat, EMBEDDING_DIM),
            },
            schema=MEMORY_EMBEDDING_SCHEMA,
        )
    )  # fmt: skip


def open_store(
    root: Path, embed: Callable[[str], np.ndarray], *, allowed: Sequence[str] = ALLOWED
) -> MemoryStore:
    """The facade on `root`'s vectors and the selected ops store; `embed` embeds texts."""
    vectors = VectorIndex(lambda: VectorStore(root / "vectors"))
    return MemoryStore(
        MemoryConfig(injection_patterns=PATTERNS),
        conn_factory=core.connection, vectors=vectors,
        embedder=Embedder(embed, model_name=_MODEL), redactor=make_redactor(), llms=None,
        allowed_numeral_patterns=allowed, data_root=root / "data",
    )  # fmt: skip
