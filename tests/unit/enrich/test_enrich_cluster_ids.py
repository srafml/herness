"""Tests for herness.enrich.cluster_ids (U03-96, U03-97; T03-24).

Centroids are small-dimensional here: the functions do not depend on the 1024-d width.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from herness.core.ids import new_ulid
from herness.enrich.cluster_ids import IdMatch, compute_centroids, match_cluster_ids

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 9, 26, tzinfo=UTC)
_DIM = 16


def _unit(x: np.ndarray) -> np.ndarray:
    out: np.ndarray = x / np.linalg.norm(x, axis=-1, keepdims=True)
    return out


def _counter(prefix: str = "cl_new") -> Callable[[], str]:
    numbers = itertools.count(1)
    return lambda: f"{prefix}{next(numbers):03d}"


def _empty_retired() -> tuple[list[str], np.ndarray, list[datetime]]:
    return [], np.zeros((0, _DIM)), []


def _match(
    new: np.ndarray,
    prev_ids: list[str],
    prev: np.ndarray,
    retired: tuple[list[str], np.ndarray, list[datetime]] | None = None,
    new_id: Callable[[], str] | None = None,
) -> IdMatch:
    return match_cluster_ids(
        new,
        prev_active=(prev_ids, prev),
        prev_retired=retired if retired is not None else _empty_retired(),
        now=_NOW,
        match_cos=0.85,
        revive_cos=0.90,
        revive_days=90,
        new_id=new_id or _counter(),
    )


# --- UT03-90 -------------------------------------------------------------------------------


def test_ut03_90_centroids_are_normalized_means_over_batches() -> None:
    """UT03-90 streamed batches give the normalized mean per cluster; noise is skipped."""
    rng = np.random.default_rng(7)
    vectors = rng.normal(size=(50, _DIM))
    idx = rng.integers(-1, 3, size=50)
    batches = [(vectors[i : i + 7], idx[i : i + 7]) for i in range(0, 50, 7)]
    centroids, sizes = compute_centroids(iter(batches), n_clusters=4)
    assert centroids.shape == (4, _DIM)
    for c in range(3):
        members = vectors[idx == c]
        assert sizes[c] == len(members)
        np.testing.assert_allclose(centroids[c], _unit(members.mean(axis=0)), atol=1e-12)
        assert np.linalg.norm(centroids[c]) == pytest.approx(1.0)
    assert sizes[3] == 0
    assert not centroids[3].any()  # an empty cluster has a zero centroid, not NaN
    assert int(sizes.sum()) == int((idx >= 0).sum())


def test_ut03_90_no_batches_gives_zero_sizes() -> None:
    """UT03-90 an empty stream gives zero sizes and zero centroids of width 1024."""
    centroids, sizes = compute_centroids(iter(()), n_clusters=2)
    assert centroids.shape == (2, 1024)
    assert sizes.tolist() == [0, 0]


# --- UT03-91 -------------------------------------------------------------------------------


def _basis(i: int) -> np.ndarray:
    v = np.zeros(_DIM)
    v[i] = 1.0
    return v


def _near(base: np.ndarray, cos: float, other: int) -> np.ndarray:
    """A unit vector with the given cosine to the unit ``base`` (tilted towards axis ``other``)."""
    out: np.ndarray = cos * base + np.sqrt(1 - cos**2) * _basis(other)
    return out


def test_ut03_91_split_merge_revival_and_new() -> None:
    """UT03-91 a split keeps the id on the closer side; a merge keeps the matched
    predecessor and retires the other; a revival within 90 days reuses the retired id, one
    beyond 90 days does not; the rest get new ids."""
    prev_ids = ["cl_a", "cl_b", "cl_c"]
    prev = np.stack([_basis(0), _basis(1), _near(_basis(1), 0.9, 2)])
    new = np.stack(
        [
            _near(_basis(0), 0.97, 10),  # split of cl_a: the closer side
            _near(_basis(0), 0.90, 11),  # split of cl_a: the farther side -> new id
            _near(_basis(1), 0.96, 12),  # merge of cl_b and cl_c: cl_b is closer
            _near(_basis(5), 0.95, 13),  # revives cl_r1 (retired 30 days ago)
            _near(_basis(6), 0.95, 14),  # like cl_r2, retired 120 days ago -> new id
        ]
    )
    retired = (
        ["cl_r1", "cl_r2"],
        np.stack([_basis(5), _basis(6)]),
        [_NOW - timedelta(days=30), _NOW - timedelta(days=120)],
    )
    got = _match(new, prev_ids, prev, retired)
    assert got.ids == ("cl_a", "cl_new001", "cl_b", "cl_r1", "cl_new002")
    assert (got.inherited, got.revived, got.created) == (2, 1, 2)
    assert got.retired == ("cl_c",)


def test_ut03_91_ineligible_pair_does_not_displace_the_closer_split_side() -> None:
    """UT03-91 regression: A has cos 0.90 to P1 and 0.80 to P2, B has 0.86 to P1 and 0 to
    P2 (match_cos 0.85); A inherits P1, B is new and P2 retires (ineligible pairs are masked
    before the assignment)."""
    p1 = _basis(0)
    angle_a, angle_b = np.arccos(0.90), -np.arccos(0.86)  # A and B on opposite sides of P1
    a = np.cos(angle_a) * p1 + np.sin(angle_a) * _basis(1)
    b = np.cos(angle_b) * p1 + np.sin(angle_b) * _basis(1)
    in_plane = np.cos(angle_b + np.pi / 2) * p1 + np.sin(angle_b + np.pi / 2) * _basis(1)
    tilt = 0.80 / float(in_plane @ a)  # P2 is orthogonal to B with cos 0.80 to A
    p2 = tilt * in_plane + np.sqrt(1 - tilt**2) * _basis(2)
    new = np.stack([a, b])
    cos = new @ np.stack([p1, p2]).T
    np.testing.assert_allclose(cos, [[0.90, 0.80], [0.86, 0.0]], atol=1e-12)
    got = _match(new, ["cl_p1", "cl_p2"], np.stack([p1, p2]))
    assert got.ids == ("cl_p1", "cl_new001")
    assert got.retired == ("cl_p2",)
    assert (got.inherited, got.revived, got.created) == (1, 0, 1)


def test_ut03_91_below_threshold_is_not_inherited() -> None:
    """UT03-91 a best pair below match_cos (0.85) is not inherited; the old id retires."""
    got = _match(np.stack([_near(_basis(0), 0.84, 1)]), ["cl_a"], np.stack([_basis(0)]))
    assert got.ids == ("cl_new001",)
    assert got.retired == ("cl_a",)
    assert (got.inherited, got.revived, got.created) == (0, 0, 1)


def test_ut03_91_revival_needs_revive_cos() -> None:
    """UT03-91 a retired id within the window with cos 0.89 < 0.90 is not revived; the
    boundary day (exactly 90 days ago) still revives."""
    retired = (
        ["cl_r1", "cl_r2"],
        np.stack([_basis(0), _basis(3)]),
        [_NOW - timedelta(days=1), _NOW - timedelta(days=90)],
    )
    new = np.stack([_near(_basis(0), 0.89, 1), _near(_basis(3), 0.91, 2)])
    got = _match(new, [], np.zeros((0, _DIM)), retired)
    assert got.ids == ("cl_new001", "cl_r2")
    assert got.retired == ()


def test_ut03_91_first_run_and_empty_new() -> None:
    """UT03-91 with no previous snapshot every cluster is new (ids from `new_id`, as the
    stage passes); with no new clusters every active id retires, sorted."""
    got = _match(
        np.stack([_basis(0), _basis(1)]), [], np.zeros((0, _DIM)), new_id=lambda: "cl_" + new_ulid()
    )
    assert len(set(got.ids)) == 2
    assert all(i.startswith("cl_") and len(i) == 29 for i in got.ids)
    gone = _match(np.zeros((0, _DIM)), ["cl_b", "cl_a"], np.stack([_basis(0), _basis(1)]))
    assert gone.ids == ()
    assert gone.retired == ("cl_a", "cl_b")


# --- PT03-12 -------------------------------------------------------------------------------


def _scenario(seed: int, n_prev: int, n_ret: int, n_new: int) -> tuple[np.ndarray, ...]:
    rng = np.random.default_rng(seed)
    prev = _unit(rng.normal(size=(n_prev, _DIM))) if n_prev else np.zeros((0, _DIM))
    ret = _unit(rng.normal(size=(n_ret, _DIM))) if n_ret else np.zeros((0, _DIM))
    pool = np.concatenate([prev, ret, _unit(rng.normal(size=(max(n_new, 1), _DIM)))])
    base = pool[rng.integers(0, len(pool), size=n_new)]
    noise = rng.normal(scale=rng.uniform(0.05, 0.6), size=(n_new, _DIM))
    new = _unit(base + noise) if n_new else np.zeros((0, _DIM))
    return prev, ret, new


@settings(max_examples=60, deadline=None)
@given(
    seed=st.integers(0, 2**32 - 1),
    n_prev=st.integers(0, 12),
    n_ret=st.integers(0, 6),
    n_new=st.integers(0, 12),
    ages=st.lists(st.integers(0, 200), min_size=6, max_size=6),
)
def test_pt03_12_ids_unique_and_pairs_meet_thresholds(
    seed: int, n_prev: int, n_ret: int, n_new: int, ages: list[int]
) -> None:
    """PT03-12 ids are unique; inherited pairs have cos >= match_cos, revived ones cos >=
    revive_cos within revive_days; the result is invariant to the order of prev_active rows
    and deterministic across runs."""
    prev, ret, new = _scenario(seed, n_prev, n_ret, n_new)
    prev_ids = [f"cl_p{i:02d}" for i in range(n_prev)]
    retired = (
        [f"cl_r{i:02d}" for i in range(n_ret)],
        ret,
        [_NOW - timedelta(days=a) for a in ages[:n_ret]],
    )
    got = _match(new, prev_ids, prev, retired)
    assert len(got.ids) == n_new
    assert len(set(got.ids)) == n_new
    assert not set(got.ids) & set(got.retired)
    prev_of = dict(zip(prev_ids, prev, strict=True))
    ret_of = {rid: (vec, at) for rid, vec, at in zip(*retired, strict=True)}
    kinds = {"inherited": 0, "revived": 0, "created": 0}
    for cid, vec in zip(got.ids, new, strict=True):
        if cid in prev_of:
            kinds["inherited"] += 1
            assert float(vec @ prev_of[cid]) >= 0.85 - 1e-9
        elif cid in ret_of:
            kinds["revived"] += 1
            old, at = ret_of[cid]
            assert float(vec @ old) >= 0.90 - 1e-9
            assert at >= _NOW - timedelta(days=90)
        else:
            kinds["created"] += 1
    assert (got.inherited, got.revived, got.created) == tuple(kinds.values())
    assert set(got.retired) == set(prev_ids) - set(got.ids)
    unmatched_new = [v for cid, v in zip(got.ids, new, strict=True) if cid not in prev_of]
    for old_id in got.retired:  # maximal: no eligible pair is left between the unmatched
        assert all(float(v @ prev_of[old_id]) < 0.85 for v in unmatched_new)
    order = np.random.default_rng(seed + 1).permutation(n_prev)
    shuffled = _match(new, [prev_ids[i] for i in order], prev[order], retired)
    assert shuffled == got
    assert _match(new, prev_ids, prev, retired) == got
