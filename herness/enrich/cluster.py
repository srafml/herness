"""Clustering numerics: PCA, projection, spherical k-means, HDBSCAN, assignment, pruning.

Pure numerics of design 03 §5.3 steps 1-5 (impl 03 U03-90 ... U03-95). The cluster stage
(U03-96 onwards) owns I/O, snapshots, stable IDs and naming; this module only computes.

`torch` is imported lazily inside functions so importing this module never loads it. GPU
work runs in fp32 on the caller's `device`; a CUDA out-of-memory error propagates so the
caller can halve `chunk` (U03-22 `run_batches_with_oom_backoff`).
"""

from __future__ import annotations

import dataclasses
import hashlib
from typing import TYPE_CHECKING, Final

import numpy as np
from sklearn.cluster import HDBSCAN  # type: ignore[import-untyped]  # sklearn ships no types
from sklearn.decomposition import PCA  # type: ignore[import-untyped]  # sklearn ships no types

from herness.core.errors import ConfigError

if TYPE_CHECKING:
    import torch

__all__ = [
    "KMeansResult",
    "PcaModel",
    "assign_members",
    "fit_pca",
    "hdbscan_prototypes",
    "project",
    "prune_clusters",
    "spherical_kmeans",
]

_NORM_EPS: Final = 1e-12


@dataclasses.dataclass(frozen=True, eq=False)
class PcaModel:
    """A stored PCA fit (U03-90): `components` (dims, 1024) and `mean` (1024,), both float32.

    `fit_id` = first 12 hex chars of sha256(components bytes + mean bytes); it names the fit
    in `algorithm_version` (design 03 §5.3). `eq=False`: arrays have no scalar equality.
    """

    components: np.ndarray
    mean: np.ndarray
    fit_id: str


@dataclasses.dataclass(frozen=True, eq=False)
class KMeansResult:
    """Spherical k-means output (U03-92).

    `prototypes` (k, d) float32 unit rows; `assign` (n,) int32 argmax prototype per point;
    `sim` (n,) float32 that similarity; `counts` (k,) int64 member counts (weight `w_p`).
    """

    prototypes: np.ndarray
    assign: np.ndarray
    sim: np.ndarray
    counts: np.ndarray


# --- U03-90 / U03-91: PCA ---------------------------------------------------------------------


def fit_pca(sample: np.ndarray, *, dims: int = 64, seed: int) -> PcaModel:
    """Fit a randomized PCA `sample.shape[1]` → `dims` on a uniform sample (U03-90).

    Raises `ConfigError` when the sample has fewer rows than `dims`.
    """
    m = int(sample.shape[0])
    if m < dims:
        msg = f"PCA sample has {m} rows, fewer than dims={dims}"
        raise ConfigError(msg)
    pca = PCA(n_components=dims, svd_solver="randomized", random_state=seed).fit(sample)
    components = np.ascontiguousarray(pca.components_, dtype=np.float32)
    mean = np.ascontiguousarray(pca.mean_, dtype=np.float32)
    fit_id = hashlib.sha256(components.tobytes() + mean.tobytes()).hexdigest()[:12]
    return PcaModel(components=components, mean=mean, fit_id=fit_id)


def project(vectors: np.ndarray, pca: PcaModel, *, device: str, chunk: int = 262_144) -> np.ndarray:
    """Project `vectors` with a stored PCA and L2-normalize each row (U03-91).

    Per chunk `(x - mean) @ components.T` in fp32 on `device`, divided by the row norm
    clipped at 1e-12, so zero rows stay zero. CUDA OOM propagates (caller halves `chunk`).
    The model arrays are cast to float32, so a float64 model (e.g. reloaded) also works.
    """
    n = int(vectors.shape[0])
    dims = int(pca.components.shape[0])
    out = np.empty((n, dims), dtype=np.float32)
    comp_t = _to_device(pca.components, device).T
    mean = _to_device(pca.mean, device)
    for start in range(0, n, chunk):
        xc = _to_device(vectors[start : start + chunk], device)
        y = (xc - mean) @ comp_t
        out[start : start + chunk] = _normalize(y).cpu().numpy()
    return out


# --- U03-92: spherical k-means ----------------------------------------------------------------


def spherical_kmeans(  # noqa: PLR0913 - keyword-only signature fixed by spec 03 U03-92
    x: np.ndarray,
    *,
    k: int,
    iters: int = 15,
    init_sample: int = 1_000_000,
    chunk: int = 32_768,
    seed: int,
    device: str,
) -> KMeansResult:
    """Prototype k-means on the unit sphere (U03-92, design 03 §5.3 step 2).

    k-means++ (distance 1 - cos) on a uniform sample of `min(init_sample, n)` points (at
    least k), then `iters` Lloyd passes over all points in chunks. Empty prototypes are
    re-seeded from the lowest-similarity points of the pass. A final pass after the last
    update gives `assign`, `sim` and `counts`, so `assign` matches `prototypes`.
    Raises `ConfigError` unless `1 ≤ k ≤ n`; CUDA OOM propagates.
    """
    import torch  # noqa: PLC0415 - lazy: importing this module must never load torch

    n = int(x.shape[0])
    if not 1 <= k <= n:
        msg = f"k-means needs 1 <= k <= n; got k={k}, n={n}"
        raise ConfigError(msg)
    rng = np.random.default_rng(seed)
    init = _kmeans_pp_init(x, k=k, init_sample=init_sample, rng=rng, device=device)
    protos = _normalize(torch.from_numpy(np.ascontiguousarray(init, dtype=np.float32)).to(device))
    for _ in range(iters):
        assign, sim, sums = _lloyd_pass(x, protos, chunk=chunk, device=device)
        protos = _update_prototypes(x, sums, assign, sim)
    assign, sim, _ = _lloyd_pass(x, protos, chunk=chunk, device=device)
    return KMeansResult(
        prototypes=protos.cpu().numpy().astype(np.float32),
        assign=assign.astype(np.int32),
        sim=sim,
        counts=np.bincount(assign, minlength=k).astype(np.int64),
    )


def _kmeans_pp_init(
    x: np.ndarray, *, k: int, init_sample: int, rng: np.random.Generator, device: str
) -> np.ndarray:
    """k-means++ seeding with distance `1 - cos` on a uniform sample; returns (k, d) rows.

    Each next seed is drawn with probability ∝ D², D the distance to the nearest seed so
    far; when every sampled point coincides with a seed (all D = 0) the draw is uniform.
    """
    import torch  # noqa: PLC0415 - lazy: importing this module must never load torch

    n = int(x.shape[0])
    m = max(min(init_sample, n), k)
    rows = np.sort(rng.choice(n, size=m, replace=False)) if m < n else np.arange(n)
    s = _to_device(x[rows], device)
    chosen = [int(rng.integers(m))]
    d2 = _sq_distance(s, s[chosen[0]])
    for _ in range(1, k):
        total = float(d2.sum().item())
        if total > 0.0:
            cum = torch.cumsum(d2, dim=0)
            target = torch.tensor([rng.random() * total], dtype=cum.dtype, device=cum.device)
            j = min(int(torch.searchsorted(cum, target).item()), m - 1)
        else:
            j = int(rng.integers(m))
        chosen.append(j)
        d2 = torch.minimum(d2, _sq_distance(s, s[j]))
    out: np.ndarray = s[chosen].cpu().numpy()
    return out


def _sq_distance(s: torch.Tensor, c: torch.Tensor) -> torch.Tensor:
    """Squared cosine distance (1 - cos)² of every row of `s` to `c`, in float64."""
    return (1.0 - (s @ c)).clamp_min(0.0).double() ** 2


def _lloyd_pass(
    x: np.ndarray, protos: torch.Tensor, *, chunk: int, device: str
) -> tuple[np.ndarray, np.ndarray, torch.Tensor]:
    """One pass: nearest prototype and its similarity per point, and per-prototype sums."""
    import torch  # noqa: PLC0415 - lazy: importing this module must never load torch

    n = int(x.shape[0])
    assign = np.empty(n, dtype=np.int64)
    sim = np.empty(n, dtype=np.float32)
    sums = torch.zeros_like(protos)
    for start in range(0, n, chunk):
        xc = _to_device(x[start : start + chunk], device)
        best, idx = (xc @ protos.T).max(dim=1)
        sums.index_add_(0, idx, xc)
        assign[start : start + chunk] = idx.cpu().numpy()
        sim[start : start + chunk] = best.cpu().numpy()
    return assign, sim, sums


def _update_prototypes(
    x: np.ndarray, sums: torch.Tensor, assign: np.ndarray, sim: np.ndarray
) -> torch.Tensor:
    """New prototypes = normalized member sums; empty ones re-seeded from the worst points.

    A prototype is empty when it has no members (or its member sum vanishes). Empties take
    the distinct points with the lowest `sim` of this pass, lowest first.
    """
    import torch  # noqa: PLC0415 - lazy: importing this module must never load torch

    k = int(sums.shape[0])
    counts = torch.from_numpy(np.bincount(assign, minlength=k)).to(sums.device)
    norms = sums.norm(dim=1)
    empty = torch.nonzero((counts == 0) | (norms <= _NORM_EPS)).flatten()
    protos: torch.Tensor = sums / norms.clamp_min(_NORM_EPS).unsqueeze(1)
    if empty.numel() > 0:
        worst = np.argsort(sim, kind="stable")[: int(empty.numel())]
        protos[empty] = _normalize(_to_device(x[worst], str(sums.device)))
    return protos


# --- U03-93 ... U03-95 ------------------------------------------------------------------------


def hdbscan_prototypes(
    prototypes: np.ndarray, *, min_cluster_size: int, min_samples: int
) -> tuple[np.ndarray, np.ndarray]:
    """HDBSCAN (euclidean, eom) on the prototypes (U03-93, design 03 §5.3 step 3).

    Returns `(labels, probabilities)`: labels 0..c-1 or -1 for noise. Prototype weights are
    not passed (sklearn has no sample weights); they enter later through cluster sizes.
    """
    model = HDBSCAN(
        min_cluster_size=min_cluster_size,
        min_samples=min_samples,
        metric="euclidean",
        cluster_selection_method="eom",
        copy=True,  # never mutate the caller's prototypes (explicit: sklearn 1.10 default)
    ).fit(prototypes)
    return np.asarray(model.labels_).copy(), np.asarray(model.probabilities_).copy()


def assign_members(
    assign: np.ndarray,
    sim: np.ndarray,
    proto_labels: np.ndarray,
    proto_probs: np.ndarray,
    *,
    assign_min_sim: float,
    full_sim: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Incident → cluster and membership probability (U03-94, design 03 §5.3 step 4).

    Point i joins `proto_labels[assign[i]]` when that label is ≥ 0 and `sim[i] ≥
    assign_min_sim`, else -1. Members get `proto_probs[p] * min(1, (s - assign_min_sim) /
    (full_sim - assign_min_sim))`; noise gets 0. Raises `ConfigError` unless `full_sim >
    assign_min_sim` (the precondition; it guards the division).
    """
    if not full_sim > assign_min_sim:
        msg = "full_sim must be greater than assign_min_sim"
        raise ConfigError(msg)
    labels = np.asarray(proto_labels)[assign]
    s = np.asarray(sim, dtype=np.float64)
    member = (labels >= 0) & (s >= assign_min_sim)
    ramp = np.minimum(1.0, (s - assign_min_sim) / (full_sim - assign_min_sim))
    prob = np.where(member, np.asarray(proto_probs, dtype=np.float64)[assign] * ramp, 0.0)
    return np.where(member, labels, -1), prob


def prune_clusters(cluster_idx: np.ndarray, *, min_incidents: int) -> np.ndarray:
    """Clusters with fewer than `min_incidents` members become noise (U03-95, §5.3 step 5).

    Survivors are renumbered 0..c'-1 in ascending order of their original index.
    """
    idx = np.asarray(cluster_idx, dtype=np.int64)
    members = idx >= 0
    if not members.any():
        return np.full(idx.shape, -1, dtype=np.int64)
    counts = np.bincount(idx[members])
    keep = counts >= min_incidents
    lookup = np.full(counts.shape[0], -1, dtype=np.int64)
    lookup[keep] = np.arange(int(keep.sum()), dtype=np.int64)
    return np.where(members, lookup[np.where(members, idx, 0)], -1)


# --- helpers ----------------------------------------------------------------------------------


def _to_device(a: np.ndarray, device: str) -> torch.Tensor:
    """A float32 contiguous copy of `a` on `device`."""
    import torch  # noqa: PLC0415 - lazy: importing this module must never load torch

    return torch.from_numpy(np.ascontiguousarray(a, dtype=np.float32)).to(device)


def _normalize(t: torch.Tensor) -> torch.Tensor:
    """Rows divided by their L2 norm clipped at 1e-12 (zero rows stay zero)."""
    out: torch.Tensor = t / t.norm(dim=1, keepdim=True).clamp_min(_NORM_EPS)
    return out
