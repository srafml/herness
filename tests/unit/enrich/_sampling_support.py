"""Hand-built warehouses, snapshots and vectors for the sampling and gold tests (T03-29).

The core DDL holds only the columns `herness.enrich.sampling` reads; `enrich.text_redacted`
comes from the real ``herness/model/sql/000_settings.sql``.
"""

from __future__ import annotations

import dataclasses
import hashlib
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pyarrow as pa

from herness.enrich._cluster_io import ClusterSnapshot, SnapshotMeta
from herness.enrich.cluster import PcaModel

SETTINGS_SQL = Path(__file__).resolve().parents[3] / "herness/model/sql/000_settings.sql"
T0 = datetime(2026, 1, 15, tzinfo=UTC)
DIM = 16  # vector and prototype dimension of the fake snapshot

_CORE_DDL = (
    "CREATE SCHEMA IF NOT EXISTS core",
    "CREATE TABLE core.incident (record_id VARCHAR, service_id VARCHAR, priority SMALLINT, "
    "opened_at TIMESTAMPTZ)",
    "CREATE TABLE core.problem (record_id VARCHAR, service_id VARCHAR, opened_at TIMESTAMPTZ)",
)


@dataclasses.dataclass(frozen=True)
class Rec:
    """One record with its redacted text; ``content_hash`` defaults to a hash of the text."""

    record_id: str
    text: str
    service_id: str | None = "svc-a"
    priority: int | None = 3
    opened_at: datetime | None = T0
    entity: str = "incident"
    content_hash: str | None = None

    @property
    def hash(self) -> str:
        return self.content_hash or text_hash(self.text)


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:32]


def warehouse(records: Iterable[Rec], *, timezone: str = "UTC") -> duckdb.DuckDBPyConnection:
    """An in-memory warehouse holding ``records`` (session time zone ``timezone``)."""
    wh = duckdb.connect()
    wh.execute(f"SET TimeZone = '{timezone}'")
    wh.execute(SETTINGS_SQL.read_text("utf-8"))
    for ddl in _CORE_DDL:
        wh.execute(ddl)
    for rec in records:
        if rec.entity == "problem":
            wh.execute(
                "INSERT INTO core.problem VALUES (?, ?, ?)",
                [rec.record_id, rec.service_id, rec.opened_at],
            )
        else:
            wh.execute(
                "INSERT INTO core.incident VALUES (?, ?, ?, ?)",
                [rec.record_id, rec.service_id, rec.priority, rec.opened_at],
            )
        wh.execute(
            "INSERT INTO enrich.text_redacted VALUES (?, ?, ?, ?)",
            [rec.record_id, rec.entity, rec.text, rec.hash],
        )
    return wh


def many(n: int, *, prefix: str = "INC", start: int = 0, **fields: object) -> list[Rec]:
    """``n`` incidents with distinct texts spread over services, priorities and quarters."""
    return [
        Rec(
            record_id=f"{prefix}-{i:05d}",
            text=f"{prefix} ticket body number {i} " + "x" * (i % 7 * 100),
            service_id=f"svc-{i % 3}",
            priority=1 + i % 5,
            opened_at=T0 + timedelta(days=(i % 4) * 95),
            **fields,  # type: ignore[arg-type]
        )
        for i in range(start, start + n)
    ]


def snapshot(k: int = DIM) -> ClusterSnapshot:
    """A snapshot whose PCA is the identity on ``DIM`` dims and prototype ``j`` is ``e_j``."""
    pca = PcaModel(
        components=np.eye(DIM, dtype=np.float32), mean=np.zeros(DIM, np.float32), fit_id="f" * 12
    )
    meta = SnapshotMeta(kind="full", status="final", full_at=T0, created_at=T0, n=0, k=k)
    return ClusterSnapshot(
        algorithm_version="clu-test",
        snapshot_id="snap-1",
        meta=meta,
        pca=pca,
        prototypes=np.eye(k, DIM, dtype=np.float32),
        proto_cluster=pa.table({"prototype": pa.array([], pa.int32())}),
        centroids=pa.table({"cluster_id": pa.array([], pa.string())}),
    )


def proto_reader(proto_of: Callable[[str], int]) -> Callable[[Sequence[str]], np.ndarray]:
    """A `vector_reader` returning ``e_{proto_of(hash)}`` (plus a little noise-free offset)."""

    def read(hashes: Sequence[str]) -> np.ndarray:
        out = np.zeros((len(hashes), DIM), dtype=np.float32)
        for row, content_hash in enumerate(hashes):
            out[row, proto_of(content_hash) % DIM] = 1.0
        return out

    return read


def no_vectors(hashes: Sequence[str]) -> np.ndarray:
    msg = "vector_reader must not be called without a snapshot"
    raise AssertionError(msg)
