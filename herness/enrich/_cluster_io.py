"""Cluster snapshot IO and vector streaming (impl 03 §4.3, U03-103, U03-105): a private
sibling of `cluster_stage`, split off for its 390-line budget (T03-25 spec note).

A snapshot lives in `models/clusters/<algorithm_version>/<snapshot_id>/`; every file is
written to a dot-temp name and then replaced, `snapshot.json` last, and
`models/clusters/CURRENT` only by `mark_final`. Nothing is ever unpickled (TH03-16): arrays
load with `np.load(allow_pickle=False)` and a `*.pkl`, `*.bin` or `*.pt` file in the
snapshot or version directory refuses the load. Errors name the file only, never its path.
`VectorSource` streams in-window incident vectors from LanceDB in bounded batches.
"""

from __future__ import annotations

import dataclasses
import json
import os
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Final, Literal, get_args

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from lancedb.table import Table

from herness.core.errors import ConfigError, FatalError
from herness.enrich.cluster import PcaModel, project
from herness.enrich.gpu import release_cuda
from herness.enrich.layout import EnrichPaths
from herness.store.vectors import EMBEDDING_DIM
from herness.store.vectors import _store_error as store_error

__all__ = [
    "ClusterSnapshot",
    "SnapshotMeta",
    "VectorSource",
    "centroid_dim",
    "empty_centroids",
    "fixed_list",
    "matrix",
    "named_centroids",
]

type SnapshotKind = Literal["full", "incremental"]
type SnapshotStatus = Literal["assigned", "final"]

META: Final = "snapshot.json"
PCA_FILE: Final = "pca.npz"
PROTOTYPES: Final = "prototypes.npy"
PROTO_CLUSTER: Final = "proto_cluster.parquet"
CENTROIDS: Final = "centroids.parquet"
MEMBERS: Final = "members.parquet"
CURRENT: Final = "CURRENT"
_FORBIDDEN: Final = frozenset({".pkl", ".bin", ".pt"})
_VECTORS: Final = ("centroid", "named_centroid")
_READ_ERRORS: Final = (OSError, ValueError, KeyError, TypeError, pa.ArrowException)
_LANCE_ERRORS: Final = (OSError, RuntimeError, ValueError)
_TABLE: Final = "ticket_embedding"
_COLUMNS: Final = ["record_id", "content_hash", "vector"]
MIN_CHUNK = 4_096  # projection chunk floor on CUDA OOM (U03-105 Errors); a value tests lower
_TS: Final = pa.timestamp("us", tz="UTC")


def clusters_dir(paths: EnrichPaths) -> Path:
    """`<data>/models/clusters`, the parent of every version directory and of `CURRENT`."""
    return paths.data_root / "models" / "clusters"


@dataclasses.dataclass(frozen=True)
class SnapshotMeta:
    """`snapshot.json` (delta DD-10): kind, status, last full run, creation time, n and k."""

    kind: SnapshotKind
    status: SnapshotStatus
    full_at: datetime
    created_at: datetime
    n: int
    k: int

    def to_json(self) -> str:
        data = dataclasses.asdict(self)
        data.update(full_at=self.full_at.isoformat(), created_at=self.created_at.isoformat())
        return json.dumps(data, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> SnapshotMeta:
        """Parse and check `snapshot.json`; ValueError (or KeyError) on any bad field."""
        data = json.loads(text)
        if data.get("kind") not in get_args(SnapshotKind.__value__):
            msg = "snapshot kind invalid"
            raise ValueError(msg)
        if data.get("status") not in get_args(SnapshotStatus.__value__):
            msg = "snapshot status invalid"
            raise ValueError(msg)
        stamps = [datetime.fromisoformat(data[key]) for key in ("full_at", "created_at")]
        if any(stamp.tzinfo is None for stamp in stamps):
            msg = "snapshot timestamps need a timezone"
            raise ValueError(msg)
        counts = [data["n"], data["k"]]
        if not all(type(v) is int and v >= 0 for v in counts):
            msg = "snapshot counts invalid"
            raise ValueError(msg)
        return cls(data["kind"], data["status"], stamps[0], stamps[1], counts[0], counts[1])


def _atomic(path: Path, write: Callable[[Path], object]) -> None:
    """Write through `write(tmp)` to a dot-temp sibling, then replace `path` with it."""
    tmp = path.with_name(f".{path.name}.tmp")
    write(tmp)
    os.replace(tmp, path)


def _write_npy(tmp: Path, array: np.ndarray) -> None:
    with tmp.open("wb") as fh:
        np.save(fh, array, allow_pickle=False)


def _write_pca(tmp: Path, pca: PcaModel) -> None:
    with tmp.open("wb") as fh:  # a file object: np.savez would append ".npz" to a name
        np.savez(fh, components=pca.components, mean=pca.mean, fit_id=np.array(pca.fit_id))


def _write_parquet(path: Path, table: pa.Table) -> None:
    _atomic(path, lambda tmp: pq.write_table(table, tmp))


def fixed_list(rows: np.ndarray) -> pa.FixedSizeListArray:
    """A (c, d) matrix as a `FLOAT[d]` column."""
    flat = pa.array(np.ascontiguousarray(rows, dtype=np.float32).reshape(-1))
    return pa.FixedSizeListArray.from_arrays(flat, int(rows.shape[1]))


def matrix(column: pa.Array | pa.ChunkedArray, dim: int) -> np.ndarray:
    """A writable (rows, dim) float32 copy of a non-null list column."""
    flat = column.combine_chunks() if isinstance(column, pa.ChunkedArray) else column
    values: np.ndarray = flat.flatten().to_numpy(zero_copy_only=False)
    return np.array(values, dtype=np.float32).reshape(-1, dim)  # writable: torch needs it


def centroid_dim(table: pa.Table) -> int:
    """The width of the `centroid` column (1024 for an empty table)."""
    kind = table.schema.field("centroid").type
    return int(kind.list_size) if pa.types.is_fixed_size_list(kind) else EMBEDDING_DIM


def empty_centroids(dim: int = EMBEDDING_DIM) -> pa.Table:
    """A `centroids.parquet` table without rows."""
    vec = pa.list_(pa.float32(), dim)
    fields = [("cluster_id", pa.string()), ("centroid", vec), ("size", pa.int64()),
              ("named_centroid", vec), ("named_size", pa.int64()), ("label", pa.string()),
              ("root_cause_category", pa.string()), ("retired_at", _TS)]  # fmt: skip
    return pa.schema(fields).empty_table()


def _vector_columns(table: pa.Table, dim: int | None) -> pa.Table:
    """Centroid columns as variable lists (dim None) or as `FLOAT[dim]`: pyarrow 25 cannot
    read back a null fixed-size list from Parquet, so they are stored as variable lists."""
    for name in _VECTORS:
        kind = pa.list_(pa.float32()) if dim is None else pa.list_(pa.float32(), dim)
        index = table.schema.get_field_index(name)
        table = table.set_column(index, name, table.column(name).cast(kind))
    return table


def _read_centroids(path: Path) -> pa.Table:
    table = pq.read_table(path)
    first = table.column("centroid")[0].values if table.num_rows else None
    return _vector_columns(table, EMBEDDING_DIM if first is None else len(first))


def _read[T](name: str, read: Callable[[], T]) -> T:
    """Run `read`; any IO or format error -> ConfigError naming the file (not its path)."""
    try:
        return read()
    except _READ_ERRORS:
        msg = f"cluster snapshot file {name} is missing or malformed"
        raise ConfigError(msg, file=name) from None  # the cause's text holds the full path


def _read_pca(path: Path) -> PcaModel:
    with np.load(path, allow_pickle=False) as z:  # TH03-16: object arrays raise ValueError
        components, mean, fit_id = z["components"], z["mean"], z["fit_id"]
    shape_ok = components.ndim == 2 and mean.shape == components.shape[1:]  # noqa: PLR2004
    if not (shape_ok and fit_id.shape == () and fit_id.dtype.kind == "U"):
        msg = "pca arrays have the wrong shape or type"
        raise ValueError(msg)
    return PcaModel(components=components, mean=mean, fit_id=str(fit_id[()]))


def _read_npy(path: Path) -> np.ndarray:
    array: np.ndarray = np.load(path, allow_pickle=False)  # TH03-16: pickles raise ValueError
    return array


def _refuse_pickles(*folders: Path) -> None:
    """ConfigError naming the first `*.pkl`/`*.bin`/`*.pt` file found in `folders`."""
    for folder in folders:
        found = sorted(p.name for p in folder.glob("*") if p.suffix.lower() in _FORBIDDEN)
        if found:
            msg = f"cluster snapshot file {found[0]} refused: pickle-capable format"
            raise ConfigError(msg, file=found[0])


@dataclasses.dataclass(frozen=True, eq=False)
class ClusterSnapshot:
    """One snapshot directory (U03-103); `eq=False`: arrays have no scalar equality."""

    algorithm_version: str
    snapshot_id: str
    meta: SnapshotMeta
    pca: PcaModel
    prototypes: np.ndarray
    proto_cluster: pa.Table
    centroids: pa.Table

    @classmethod
    def load_current(cls, paths: EnrichPaths) -> ClusterSnapshot | None:
        """The snapshot `CURRENT` names (`<algorithm_version>/<snapshot_id>`); None without
        CURRENT. ConfigError when CURRENT or any snapshot file is malformed."""
        current = clusters_dir(paths) / CURRENT
        if not current.exists():
            return None
        version, _, snapshot_id = _read(CURRENT, lambda: current.read_text("utf-8")).partition("/")
        return cls.load(paths, version.strip(), snapshot_id.strip())

    @classmethod
    def load(cls, paths: EnrichPaths, algorithm_version: str, snapshot_id: str) -> ClusterSnapshot:
        """Read a snapshot directory; identifiers checked by U03-13's rules (ConfigError)."""
        folder = paths.cluster_snapshot(algorithm_version, snapshot_id)
        _refuse_pickles(folder.parent, folder)
        meta = _read(META, lambda: SnapshotMeta.from_json((folder / META).read_text("utf-8")))
        return cls(
            algorithm_version=algorithm_version,
            snapshot_id=snapshot_id,
            meta=meta,
            pca=_read(PCA_FILE, lambda: _read_pca(folder / PCA_FILE)),
            prototypes=_read(PROTOTYPES, lambda: _read_npy(folder / PROTOTYPES)),
            proto_cluster=_read(PROTO_CLUSTER, lambda: pq.read_table(folder / PROTO_CLUSTER)),
            centroids=_read(CENTROIDS, lambda: _read_centroids(folder / CENTROIDS)),
        )

    @classmethod
    def load_assigned(cls, paths: EnrichPaths, snapshot_id: str) -> ClusterSnapshot | None:
        """The `assigned` (not yet final) snapshot `snapshot_id` of any algorithm version,
        for a crash rerun of that build; None when there is none."""
        root = clusters_dir(paths)
        paths.cluster_snapshot("v", snapshot_id)  # U03-13 check before any path probing
        versions = sorted(p.name for p in root.glob("*") if (p / snapshot_id / META).is_file())
        for version in versions:
            snap = cls.load(paths, version, snapshot_id)
            if snap.meta.status == "assigned":
                return snap
        return None

    def folder(self, paths: EnrichPaths) -> Path:
        return paths.cluster_snapshot(self.algorithm_version, self.snapshot_id)

    def save(self, paths: EnrichPaths) -> None:
        """Write every file except CURRENT (and `members.parquet`), `snapshot.json` last."""
        folder = self.folder(paths)
        folder.mkdir(parents=True, exist_ok=True)
        _atomic(folder / PCA_FILE, lambda tmp: _write_pca(tmp, self.pca))
        _atomic(folder / PROTOTYPES, lambda tmp: _write_npy(tmp, self.prototypes))
        _write_parquet(folder / PROTO_CLUSTER, self.proto_cluster)
        self.save_centroids(paths, self.centroids)
        self._write_meta(folder, self.meta)

    def save_centroids(self, paths: EnrichPaths, table: pa.Table) -> None:
        _write_parquet(self.folder(paths) / CENTROIDS, _vector_columns(table, None))

    def save_members(self, paths: EnrichPaths, table: pa.Table) -> None:
        """`members.parquet` (record_id, cluster_id, membership_prob) of a full run."""
        folder = self.folder(paths)
        folder.mkdir(parents=True, exist_ok=True)
        _write_parquet(folder / MEMBERS, table)

    def load_members(self, paths: EnrichPaths) -> pa.Table:
        return _read(MEMBERS, lambda: pq.read_table(self.folder(paths) / MEMBERS))

    def mark_final(self, paths: EnrichPaths) -> None:
        """`snapshot.json` with status final, then CURRENT replaced with this snapshot."""
        self._write_meta(self.folder(paths), dataclasses.replace(self.meta, status="final"))
        text = f"{self.algorithm_version}/{self.snapshot_id}"
        _atomic(clusters_dir(paths) / CURRENT, lambda tmp: tmp.write_text(text, "utf-8"))

    @staticmethod
    def _write_meta(folder: Path, meta: SnapshotMeta) -> None:
        _atomic(folder / META, lambda tmp: tmp.write_text(meta.to_json(), "utf-8"))


class VectorSource:
    """In-window incident vectors of `ticket_embedding`, streamed in batches (U03-105).

    A row counts only when its `record_id` is in the window and its `content_hash` is the
    window's (a stale vector is skipped). Batches carry window positions, so results do
    not depend on LanceDB's scan order. LanceDB conflicts and locks -> StoreBusy.
    """

    def __init__(self, table: Table, window: pa.Table, chunk: int) -> None:
        self.table, self.chunk = table, chunk
        self.ids = window.column("record_id").combine_chunks()
        self.hashes = window.column("content_hash").combine_chunks()

    def batches(self, where: Sequence[str]) -> Iterator[tuple[np.ndarray, np.ndarray]]:
        """(window positions, (b, 1024) vectors) per streamed batch of each filter."""
        for clause in where:
            query = self.table.search().where(clause).select(_COLUMNS).limit(None)
            try:
                for batch in query.to_batches(self.chunk):
                    pos = pc.index_in(batch.column("record_id"), value_set=self.ids)
                    same = pc.equal(batch.column("content_hash"), pc.take(self.hashes, pos))
                    keep = pc.fill_null(same, fill_value=False)
                    vectors = matrix(batch.column("vector").filter(keep), EMBEDDING_DIM)
                    yield pc.filter(pos, keep).to_numpy(zero_copy_only=False), vectors
            except _LANCE_ERRORS as exc:
                raise store_error(exc, _TABLE, "read") from exc

    def project(
        self, pca: PcaModel, where: Sequence[str], device: str
    ) -> tuple[np.ndarray, np.ndarray]:
        """(sorted window positions with a vector, their unit projections). CUDA OOM:
        release CUDA, retry at chunk // 2; FatalError below `MIN_CHUNK`."""
        import torch  # noqa: PLC0415 - lazy: importing this module must never load torch

        parts, chunk = [], self.chunk
        for pos, vectors in self.batches(where):
            while True:
                try:
                    parts.append((pos, project(vectors, pca, device=device, chunk=chunk)))
                    break
                except torch.cuda.OutOfMemoryError:
                    release_cuda()
                    chunk //= 2
                if chunk < MIN_CHUNK:
                    msg = "cluster stage: cuda out of memory"
                    raise FatalError(msg) from None
        dims = int(pca.components.shape[0])
        pos = np.concatenate([p for p, _ in parts]) if parts else np.zeros(0, np.int64)
        x = np.concatenate([x for _, x in parts]) if parts else np.zeros((0, dims), np.float32)
        order = np.argsort(pos, kind="stable")
        return pos[order].astype(np.int64), x[order]

    def sample(self, rank: np.ndarray, size: int, where: Sequence[str]) -> np.ndarray:
        """Vectors of the positions whose `rank` (0-based) is below `size`, in rank order."""
        rows = np.zeros((size, EMBEDDING_DIM), dtype=np.float32)
        got = np.zeros(size, dtype=bool)
        for pos, vectors in self.batches(where):
            picked = rank[pos] < size
            rows[rank[pos][picked]] = vectors[picked]
            got[rank[pos][picked]] = True
        return rows[got]


def named_centroids(
    cent: pa.Table, renamed: Collection[str], final: Mapping[str, tuple[str | None, str | None]]
) -> pa.Table:
    """`centroids.parquet` after naming (U03-106): renamed clusters get `named_centroid =
    centroid` and `named_size = size`; clusters in `final` its (label, root_cause_category)."""
    ids = cent.column("cluster_id").to_pylist()
    labels, causes = (
        cent.column("label").to_pylist(),
        cent.column("root_cause_category").to_pylist(),
    )
    old = zip(labels, causes, strict=True)
    pairs = [final.get(cid, pair) for cid, pair in zip(ids, old, strict=True)]
    mask = pa.array([cid in renamed for cid in ids], pa.bool_())
    updates = {
        "named_centroid": pc.if_else(mask, cent.column("centroid"), cent.column("named_centroid")),
        "named_size": pc.if_else(mask, cent.column("size"), cent.column("named_size")),
        "label": pa.array([p[0] for p in pairs], pa.string()),
        "root_cause_category": pa.array([p[1] for p in pairs], pa.string()),
    }
    for name, column in updates.items():
        cent = cent.set_column(cent.schema.get_field_index(name), name, column)
    return cent
