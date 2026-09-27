"""Cluster centroids and stable cluster ids (impl 03 U03-96, U03-97; design 03 §5.3 step 6).

A centroid is the normalized mean of the members' 1024-d vectors, independent of the PCA
fit. New centroids inherit a previous active id through a one-to-one Hungarian assignment on
`1 - cos` (pairs with `cos >= match_cos`), then revive a recently retired id the same way
(`cos >= revive_cos`, retired within `revive_days`); the rest get `new_id()`. A split keeps
the id on the side with the higher similarity and a merge keeps the matched predecessor,
both as consequences of the one-to-one assignment. Pure numerics: no I/O, no logging.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timedelta
from typing import Final

import numpy as np
from scipy.optimize import linear_sum_assignment  # type: ignore[import-untyped]  # no types

__all__ = ["IdMatch", "compute_centroids", "match_cluster_ids"]

_DIM: Final = 1024
_NORM_EPS: Final = 1e-12


@dataclasses.dataclass(frozen=True)
class IdMatch:
    """Stable ids per new cluster (in input order) and the counts of how each was found."""

    ids: tuple[str, ...]
    inherited: int
    revived: int
    created: int
    retired: tuple[str, ...]


def compute_centroids(
    batches: Iterable[tuple[np.ndarray, np.ndarray]], *, n_clusters: int
) -> tuple[np.ndarray, np.ndarray]:
    """Unit-norm centroids (c, d) and member counts (c,) from streamed (vectors, cluster_idx)
    batches (U03-96). Noise rows (`cluster_idx < 0`) are skipped; an empty cluster keeps a
    zero centroid. The width is the vectors' (1024 when no batch arrives).
    """
    sums: np.ndarray | None = None
    sizes = np.zeros(n_clusters, dtype=np.int64)
    for vectors, idx in batches:
        keep = np.asarray(idx) >= 0
        rows = np.asarray(vectors, dtype=np.float64)[keep]
        labels = np.asarray(idx)[keep].astype(np.int64)
        if sums is None:
            sums = np.zeros((n_clusters, rows.shape[1]), dtype=np.float64)
        np.add.at(sums, labels, rows)
        np.add.at(sizes, labels, 1)
    if sums is None:
        sums = np.zeros((n_clusters, _DIM), dtype=np.float64)
    norms = np.linalg.norm(sums, axis=1, keepdims=True)
    return sums / np.maximum(norms, _NORM_EPS), sizes


def _pairs(sim: np.ndarray, threshold: float) -> list[tuple[int, int]]:
    """Hungarian assignment on `1 - sim`; the (row, col) pairs with `sim >= threshold`."""
    if sim.size == 0:
        return []
    rows, cols = linear_sum_assignment(1.0 - sim)
    return [(int(r), int(c)) for r, c in zip(rows, cols, strict=True) if sim[r, c] >= threshold]


def _by_id(ids: Sequence[str], vectors: np.ndarray, dim: int) -> tuple[list[str], np.ndarray]:
    """Ids and vector rows sorted by id, so the result does not depend on the row order."""
    order = sorted(range(len(ids)), key=lambda i: ids[i])
    rows = np.asarray(vectors, dtype=np.float64).reshape(-1, dim)
    return [ids[i] for i in order], rows[order]


def match_cluster_ids(  # noqa: PLR0913 - U03-97's keyword-only signature is binding (8 params)
    new_centroids: np.ndarray,
    *,
    prev_active: tuple[Sequence[str], np.ndarray],
    prev_retired: tuple[Sequence[str], np.ndarray, Sequence[datetime]],
    now: datetime,
    match_cos: float,
    revive_cos: float,
    revive_days: int,
    new_id: Callable[[], str],
) -> IdMatch:
    """Assign one stable id to every new cluster (U03-97).

    Inherited pairs have `cos >= match_cos`; revived ids have `cos >= revive_cos` and
    `retired_at >= now - revive_days`. New ids come only from ``new_id`` (called in cluster
    order). `retired` lists the unmatched previous active ids, sorted.
    """
    new = np.asarray(new_centroids, dtype=np.float64)
    dim = new.shape[1] if new.ndim == 2 else _DIM  # noqa: PLR2004 - a (c, d) matrix
    new = new.reshape(-1, dim)
    act_ids, act = _by_id(prev_active[0], prev_active[1], dim)
    cutoff = now - timedelta(days=revive_days)
    ret_ids_all, ret_vecs, ret_at = prev_retired
    recent = [i for i, at in enumerate(ret_at) if at >= cutoff]
    ret_ids, ret = _by_id(
        [ret_ids_all[i] for i in recent], np.asarray(ret_vecs).reshape(-1, dim)[recent], dim
    )

    ids: list[str | None] = [None] * len(new)
    for r, c in _pairs(new @ act.T, match_cos):
        ids[r] = act_ids[c]
    inherited = sum(i is not None for i in ids)

    open_rows = [r for r, cid in enumerate(ids) if cid is None]
    for r, c in _pairs(new[open_rows] @ ret.T, revive_cos):
        ids[open_rows[r]] = ret_ids[c]
    revived = sum(i is not None for i in ids) - inherited

    final = [cid if cid is not None else new_id() for cid in ids]
    matched = set(final)
    retired = tuple(cid for cid in act_ids if cid not in matched)
    return IdMatch(
        ids=tuple(final),
        inherited=inherited,
        revived=revived,
        created=len(final) - inherited - revived,
        retired=retired,
    )
