"""Tests for herness.enrich.cluster (U03-90 ... U03-95; T03-23). All numerics run on CPU."""

from __future__ import annotations

import hashlib

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from herness.core.errors import ConfigError
from herness.enrich import cluster
from herness.enrich.cluster import (
    KMeansResult,
    PcaModel,
    assign_members,
    fit_pca,
    hdbscan_prototypes,
    project,
    prune_clusters,
    spherical_kmeans,
)

pytestmark = pytest.mark.unit


def _unit(x: np.ndarray) -> np.ndarray:
    out: np.ndarray = (x / np.linalg.norm(x, axis=1, keepdims=True)).astype(np.float32)
    return out


def _blobs(
    per: int, seed: int, dims: int = 64, spread: float = 0.05
) -> tuple[np.ndarray, np.ndarray]:
    """Three well-separated unit-sphere blobs around orthogonal axes; returns (x, truth)."""
    rng = np.random.default_rng(seed)
    centers = np.eye(dims, dtype=np.float32)[:3]
    parts = [c + spread * rng.standard_normal((per, dims)).astype(np.float32) for c in centers]
    truth = np.repeat(np.arange(3), per)
    return _unit(np.concatenate(parts)), truth


def _same_partition(a: np.ndarray, b: np.ndarray) -> bool:
    pairs = set(zip(a.tolist(), b.tolist(), strict=True))
    return len(pairs) == len(set(a.tolist())) == len(set(b.tolist()))


# --- UT03-85: fit_pca / project -------------------------------------------------------------


def test_ut03_85_fit_pca_shapes_and_stable_fit_id() -> None:
    """UT03-85 fit_pca returns float32 (dims, 1024) components and a stable 12-char fit_id."""
    rng = np.random.default_rng(1)
    sample = rng.standard_normal((200, 1024)).astype(np.float32)
    a = fit_pca(sample, dims=16, seed=7)
    b = fit_pca(sample.copy(), dims=16, seed=7)
    assert isinstance(a, PcaModel)
    assert a.components.shape == (16, 1024)
    assert a.components.dtype == np.float32
    assert a.mean.shape == (1024,)
    assert a.mean.dtype == np.float32
    assert len(a.fit_id) == 12
    assert a.fit_id == b.fit_id
    assert np.array_equal(a.components, b.components)


def test_ut03_85_fit_id_is_sha256_prefix_of_components_and_mean() -> None:
    """UT03-85 fit_id = sha256(components.tobytes() + mean.tobytes())[:12]."""
    rng = np.random.default_rng(2)
    model = fit_pca(rng.standard_normal((80, 1024)).astype(np.float32), dims=8, seed=3)
    digest = hashlib.sha256(model.components.tobytes() + model.mean.tobytes()).hexdigest()
    assert model.fit_id == digest[:12]


def test_ut03_85_fit_pca_rejects_sample_smaller_than_dims() -> None:
    """UT03-85 m < dims raises ConfigError."""
    sample = np.zeros((10, 1024), dtype=np.float32)
    with pytest.raises(ConfigError):
        fit_pca(sample, dims=64, seed=0)


def test_ut03_85_project_returns_unit_rows_across_chunks() -> None:
    """UT03-85 project yields (n, dims) float32 unit rows, identical across chunk sizes."""
    rng = np.random.default_rng(4)
    model = fit_pca(rng.standard_normal((120, 1024)).astype(np.float32), dims=16, seed=5)
    vectors = rng.standard_normal((50, 1024)).astype(np.float32)
    whole = project(vectors, model, device="cpu")
    chunked = project(vectors, model, device="cpu", chunk=7)
    assert whole.shape == (50, 16)
    assert whole.dtype == np.float32
    np.testing.assert_allclose(np.linalg.norm(whole, axis=1), 1.0, atol=1e-5)
    np.testing.assert_allclose(whole, chunked, atol=1e-6)
    expected = _unit((vectors - model.mean) @ model.components.T)
    np.testing.assert_allclose(whole, expected, atol=1e-4)


def test_ut03_85_project_keeps_zero_rows_zero() -> None:
    """UT03-85 a row whose projection is zero stays zero (norm clipped at 1e-12)."""
    rng = np.random.default_rng(6)
    model = fit_pca(rng.standard_normal((40, 1024)).astype(np.float32), dims=4, seed=0)
    vectors = np.stack([model.mean, model.mean + model.components[0]])
    out = project(vectors, model, device="cpu")
    assert np.all(out[0] == 0.0)
    assert np.isclose(np.linalg.norm(out[1]), 1.0, atol=1e-5)


def test_ut03_85_project_empty_input() -> None:
    """UT03-85 zero vectors in, (0, dims) out."""
    rng = np.random.default_rng(8)
    model = fit_pca(rng.standard_normal((20, 1024)).astype(np.float32), dims=4, seed=0)
    out = project(np.zeros((0, 1024), dtype=np.float32), model, device="cpu")
    assert out.shape == (0, 4)


def test_ut03_85_project_accepts_float64_pca_model() -> None:
    """UT03-85 a float64 PcaModel (e.g. reloaded from a snapshot) still projects to float32."""
    rng = np.random.default_rng(9)
    model = fit_pca(rng.standard_normal((60, 1024)).astype(np.float32), dims=8, seed=1)
    wide = PcaModel(
        components=model.components.astype(np.float64),
        mean=model.mean.astype(np.float64),
        fit_id=model.fit_id,
    )
    vectors = rng.standard_normal((12, 1024)).astype(np.float32)
    out = project(vectors, wide, device="cpu", chunk=5)
    assert out.dtype == np.float32
    np.testing.assert_allclose(np.linalg.norm(out, axis=1), 1.0, atol=1e-5)
    np.testing.assert_allclose(out, project(vectors, model, device="cpu"), atol=1e-6)


# --- UT03-86: spherical_kmeans --------------------------------------------------------------


def test_ut03_86_kmeans_recovers_three_blobs() -> None:
    """UT03-86 k=3 on three well-separated blobs recovers them (cpu)."""
    x, truth = _blobs(per=40, seed=11)
    res = spherical_kmeans(x, k=3, seed=0, device="cpu", chunk=17)
    assert isinstance(res, KMeansResult)
    assert res.prototypes.shape == (3, 64)
    assert res.prototypes.dtype == np.float32
    assert res.assign.dtype == np.int32
    assert res.sim.dtype == np.float32
    assert res.counts.dtype == np.int64
    assert _same_partition(res.assign, truth)
    assert sorted(res.counts.tolist()) == [40, 40, 40]
    np.testing.assert_allclose(np.linalg.norm(res.prototypes, axis=1), 1.0, atol=1e-5)
    assert np.all(res.sim > 0.8)


def test_ut03_86_kmeans_is_deterministic_for_a_seed() -> None:
    """UT03-86 the same seed gives the same prototypes and assignment."""
    x, _ = _blobs(per=20, seed=12)
    a = spherical_kmeans(x, k=3, seed=5, device="cpu", init_sample=30)
    b = spherical_kmeans(x, k=3, seed=5, device="cpu", init_sample=30)
    assert np.array_equal(a.assign, b.assign)
    np.testing.assert_allclose(a.prototypes, b.prototypes)


def test_ut03_86_empty_prototype_is_reseeded(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT03-86 forced case: a duplicated initial prototype is empty and gets re-seeded."""
    x, truth = _blobs(per=30, seed=13)
    forced = np.stack([x[0], x[0], x[30]])  # blobs 0, 0, 1: prototype 1 empty, blob 2 unowned

    def _fake_init(*_args: object, **_kwargs: object) -> np.ndarray:
        return forced.copy()

    monkeypatch.setattr(cluster, "_kmeans_pp_init", _fake_init)
    res = spherical_kmeans(x, k=3, seed=0, device="cpu", iters=5)
    assert np.all(res.counts > 0)
    assert _same_partition(res.assign, truth)


def test_ut03_86_kmeans_rejects_k_out_of_range() -> None:
    """UT03-86 k > n (and k < 1) raises ConfigError."""
    x, _ = _blobs(per=2, seed=14)
    with pytest.raises(ConfigError):
        spherical_kmeans(x, k=7, seed=0, device="cpu")
    with pytest.raises(ConfigError):
        spherical_kmeans(x, k=0, seed=0, device="cpu")


def test_ut03_86_kmeans_handles_duplicate_points() -> None:
    """UT03-86 k equal to n with identical points still returns unit prototypes."""
    x = _unit(np.tile(np.eye(64, dtype=np.float32)[:1], (4, 1)))
    res = spherical_kmeans(x, k=4, seed=0, device="cpu", iters=2)
    np.testing.assert_allclose(np.linalg.norm(res.prototypes, axis=1), 1.0, atol=1e-5)
    assert int(res.counts.sum()) == 4


# --- PT03-11: assignment equals argmax similarity -------------------------------------------


@settings(max_examples=25, deadline=None)
@given(
    n=st.integers(min_value=1, max_value=40),
    k_frac=st.floats(min_value=0.0, max_value=1.0),
    seed=st.integers(min_value=0, max_value=2**31 - 1),
    iters=st.integers(min_value=0, max_value=3),
    chunk=st.integers(min_value=1, max_value=16),
)
def test_pt03_11_assignment_is_argmax_similarity(
    n: int, k_frac: float, seed: int, iters: int, chunk: int
) -> None:
    """PT03-11 assign[i] is the argmax similarity of point i to the returned prototypes."""
    k = max(1, round(k_frac * n))
    rng = np.random.default_rng(seed)
    x = _unit(rng.standard_normal((n, 8)).astype(np.float32))
    res = spherical_kmeans(x, k=k, seed=seed, iters=iters, chunk=chunk, device="cpu")
    sims = x.astype(np.float64) @ res.prototypes.astype(np.float64).T
    best = sims.max(axis=1)
    chosen = sims[np.arange(n), res.assign]
    assert np.all(chosen >= best - 1e-5)
    np.testing.assert_allclose(res.sim, best, atol=1e-5)
    assert np.array_equal(res.counts, np.bincount(res.assign, minlength=k))
    np.testing.assert_allclose(np.linalg.norm(res.prototypes, axis=1), 1.0, atol=1e-5)


# --- UT03-87: hdbscan_prototypes ------------------------------------------------------------


def test_ut03_87_hdbscan_finds_three_groups_and_noise() -> None:
    """UT03-87 60 prototypes in 3 groups plus outliers give 3 labels and noise."""
    x, truth = _blobs(per=20, seed=21, spread=0.02)
    rng = np.random.default_rng(22)
    outliers = _unit(rng.standard_normal((4, 64)).astype(np.float32))
    protos = np.concatenate([x, outliers])
    labels, probs = hdbscan_prototypes(protos, min_cluster_size=5, min_samples=3)
    assert labels.shape == (64,)
    assert probs.shape == (64,)
    assert set(labels[:60].tolist()) == {0, 1, 2}
    assert _same_partition(labels[:60], truth)
    assert np.all(labels[60:] == -1)
    assert np.all((probs >= 0.0) & (probs <= 1.0))
    assert np.all(probs[60:] == 0.0)


# --- UT03-88: assign_members ----------------------------------------------------------------


def test_ut03_88_assign_members_threshold_and_probability() -> None:
    """UT03-88 sims 0.55, 0.60, 0.725, 0.9 give noise, prob 0, 0.5·p, p."""
    assign = np.array([0, 0, 0, 0], dtype=np.int32)
    sim = np.array([0.55, 0.60, 0.725, 0.9], dtype=np.float32)
    labels = np.array([2])
    probs = np.array([0.8])
    idx, prob = assign_members(assign, sim, labels, probs, assign_min_sim=0.60, full_sim=0.85)
    assert idx.tolist() == [-1, 2, 2, 2]
    np.testing.assert_allclose(prob, [0.0, 0.0, 0.4, 0.8], atol=1e-6)


def test_ut03_88_noise_prototype_gives_noise() -> None:
    """UT03-88 a point whose prototype is noise is noise with probability 0."""
    assign = np.array([0, 1], dtype=np.int32)
    sim = np.array([0.99, 0.99], dtype=np.float32)
    idx, prob = assign_members(
        assign, sim, np.array([-1, 0]), np.array([0.0, 0.5]), assign_min_sim=0.6, full_sim=0.85
    )
    assert idx.tolist() == [-1, 0]
    np.testing.assert_allclose(prob, [0.0, 0.5])


def test_ut03_88_rejects_full_sim_not_above_min() -> None:
    """UT03-88 precondition full_sim > assign_min_sim is enforced with ConfigError."""
    one = np.array([0], dtype=np.int32)
    with pytest.raises(ConfigError):
        assign_members(one, np.ones(1), np.zeros(1), np.ones(1), assign_min_sim=0.6, full_sim=0.6)


# --- UT03-89: prune_clusters ----------------------------------------------------------------


def test_ut03_89_prune_small_cluster_and_renumber() -> None:
    """UT03-89 clusters of 30 and 10 with min 25: the second becomes noise; renumbered."""
    idx = np.concatenate([np.full(10, 1), np.full(30, 4), np.full(5, -1)])
    out = prune_clusters(idx, min_incidents=25)
    assert out.tolist() == [-1] * 10 + [0] * 30 + [-1] * 5


def test_ut03_89_renumbers_in_ascending_original_order() -> None:
    """UT03-89 surviving clusters are numbered 0..c'-1 by ascending original index."""
    idx = np.array([5, 2, 5, 9, 2, 7, 9, -1])
    out = prune_clusters(idx, min_incidents=2)
    assert out.tolist() == [1, 0, 1, 2, 0, -1, 2, -1]


def test_ut03_89_all_noise_and_empty_inputs() -> None:
    """UT03-89 all-noise and empty inputs pass through."""
    assert prune_clusters(np.array([-1, -1]), min_incidents=1).tolist() == [-1, -1]
    assert prune_clusters(np.array([], dtype=np.int64), min_incidents=1).tolist() == []
