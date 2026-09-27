"""Tests for herness.enrich.cluster_stage (U03-103 ... U03-106; T03-25). CPU only."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import numpy as np
import pyarrow as pa
import pytest
from tests.unit.enrich._cluster_support import T_NOW, enrich_paths, tiny_snapshot

from herness.core.errors import ConfigError
from herness.enrich.cluster_stage import (
    ALGORITHM_BASE,
    DRIFT_MIN_NEW,
    ClusterSnapshot,
    SnapshotMeta,
    is_full_recluster_due,
)

pytestmark = pytest.mark.unit


# --- UT03-98: ClusterSnapshot IO ----------------------------------------------------------


def _assert_same(a: ClusterSnapshot, b: ClusterSnapshot) -> None:
    assert (a.algorithm_version, a.snapshot_id, a.meta) == (
        b.algorithm_version,
        b.snapshot_id,
        b.meta,
    )
    assert np.array_equal(a.pca.components, b.pca.components)
    assert np.array_equal(a.pca.mean, b.pca.mean)
    assert a.pca.fit_id == b.pca.fit_id
    assert np.array_equal(a.prototypes, b.prototypes)
    assert a.proto_cluster.equals(b.proto_cluster)
    assert a.centroids.equals(b.centroids)


def test_ut03_98_save_load_round_trip(tmp_path: Path) -> None:
    """UT03-98 save then load returns an equal snapshot; no CURRENT is written by save."""
    paths = enrich_paths(tmp_path)
    snap = tiny_snapshot()
    snap.save(paths)
    folder = paths.cluster_snapshot(snap.algorithm_version, snap.snapshot_id)
    names = {p.name for p in folder.iterdir()}
    assert names == {
        "pca.npz", "prototypes.npy", "proto_cluster.parquet", "centroids.parquet",
        "snapshot.json",
    }  # fmt: skip
    assert not (tmp_path / "data/models/clusters/CURRENT").exists()
    assert ClusterSnapshot.load_current(paths) is None
    _assert_same(ClusterSnapshot.load(paths, snap.algorithm_version, snap.snapshot_id), snap)


def test_ut03_98_mark_final_publishes_current(tmp_path: Path) -> None:
    """UT03-98 mark_final writes status final and CURRENT `<version>/<id>` (read back)."""
    paths = enrich_paths(tmp_path)
    snap = tiny_snapshot(status="assigned")
    snap.save(paths)
    snap.mark_final(paths)
    current = tmp_path / "data/models/clusters/CURRENT"
    assert current.read_text("utf-8") == f"{snap.algorithm_version}/{snap.snapshot_id}"
    loaded = ClusterSnapshot.load_current(paths)
    assert loaded is not None
    assert loaded.meta.status == "final"
    assert loaded.meta.full_at == snap.meta.full_at
    assert not list(current.parent.glob("**/.*.tmp"))


def test_ut03_98_save_centroids_and_members(tmp_path: Path) -> None:
    """UT03-98 save_centroids replaces centroids.parquet; members round-trip."""
    paths = enrich_paths(tmp_path)
    snap = tiny_snapshot()
    snap.save(paths)
    table = snap.centroids.slice(0, 2)
    snap.save_centroids(paths, table)
    loaded = ClusterSnapshot.load(paths, snap.algorithm_version, snap.snapshot_id)
    assert loaded.centroids.equals(table)
    members = pa.table({"record_id": ["a:b:c"], "cluster_id": ["cl_A"], "membership_prob": [0.5]})
    snap.save_members(paths, members)
    assert snap.load_members(paths).equals(members)


def test_ut03_98_pickled_npz_is_refused(tmp_path: Path) -> None:
    """UT03-98 a pca.npz holding a pickled object array -> ConfigError naming the file."""
    paths = enrich_paths(tmp_path)
    snap = tiny_snapshot()
    snap.save(paths)
    folder = paths.cluster_snapshot(snap.algorithm_version, snap.snapshot_id)
    np.savez(
        folder / "pca.npz",
        components=np.array([{"x": 1}], dtype=object),
        mean=snap.pca.mean,
        fit_id=np.array("abcdef012345"),
    )
    with pytest.raises(ConfigError, match=r"pca\.npz"):
        ClusterSnapshot.load(paths, snap.algorithm_version, snap.snapshot_id)


@pytest.mark.parametrize(
    "name", ["snapshot.json", "pca.npz", "prototypes.npy", "centroids.parquet"]
)
def test_ut03_98_missing_or_malformed_file_names_it(tmp_path: Path, name: str) -> None:
    """UT03-98 a missing or corrupt file -> ConfigError naming that file."""
    paths = enrich_paths(tmp_path)
    snap = tiny_snapshot()
    snap.save(paths)
    folder = paths.cluster_snapshot(snap.algorithm_version, snap.snapshot_id)
    (folder / name).unlink()
    with pytest.raises(ConfigError, match=name.replace(".", r"\.")):
        ClusterSnapshot.load(paths, snap.algorithm_version, snap.snapshot_id)
    (folder / name).write_bytes(b"{not valid")
    with pytest.raises(ConfigError, match=name.replace(".", r"\.")):
        ClusterSnapshot.load(paths, snap.algorithm_version, snap.snapshot_id)


@pytest.mark.parametrize("text", ["", "no-slash", "a/b/c", "../x/y", "v/../../etc"])
def test_ut03_98_malformed_current_raises(tmp_path: Path, text: str) -> None:
    """UT03-98 CURRENT that is not `<version>/<id>` (U03-13 rules) -> ConfigError."""
    paths = enrich_paths(tmp_path)
    current = tmp_path / "data/models/clusters/CURRENT"
    current.parent.mkdir(parents=True)
    current.write_text(text, "utf-8")
    with pytest.raises(ConfigError):
        ClusterSnapshot.load_current(paths)


def test_ut03_98_find_assigned_by_snapshot_id(tmp_path: Path) -> None:
    """UT03-98 load_assigned finds an `assigned` snapshot by id; a final one is not returned."""
    paths = enrich_paths(tmp_path)
    assert ClusterSnapshot.load_assigned(paths, "b-0002") is None
    tiny_snapshot(snapshot_id="b-0002", status="assigned").save(paths)
    tiny_snapshot(snapshot_id="b-0003", status="final").save(paths)
    found = ClusterSnapshot.load_assigned(paths, "b-0002")
    assert found is not None
    assert found.snapshot_id == "b-0002"
    assert ClusterSnapshot.load_assigned(paths, "b-0003") is None


def test_ut03_98_snapshot_meta_json_round_trip() -> None:
    """UT03-98 SnapshotMeta JSON round-trips; a naive timestamp or bad status is refused."""
    meta = SnapshotMeta("full", "assigned", T_NOW, T_NOW, 10, 3)
    assert SnapshotMeta.from_json(meta.to_json()) == meta
    with pytest.raises(ValueError, match="status"):
        SnapshotMeta.from_json(meta.to_json().replace('"assigned"', '"done"'))
    with pytest.raises(ValueError, match="timezone"):
        SnapshotMeta.from_json(meta.to_json().replace("+00:00", ""))


# --- UT03-99: is_full_recluster_due --------------------------------------------------------

_V = f"{ALGORITHM_BASE}-abcdef012345"
_PREV = SnapshotMeta("full", "final", T_NOW - timedelta(days=2), T_NOW, 100, 10)
_BASE: dict[str, object] = {
    "prev_algorithm_version": _V,
    "algorithm_base": ALGORITHM_BASE,
    "now": T_NOW,
    "full_every_days": 7,
    "forced": False,
    "drift_share": 0.05,
    "drift_threshold": 0.10,
    "n_new": DRIFT_MIN_NEW,
}
_OLD = SnapshotMeta("full", "final", T_NOW - timedelta(days=7), T_NOW, 100, 10)


@pytest.mark.parametrize(
    ("prev", "overrides", "expected"),
    [
        (_PREV, {}, (False, "none")),
        (_PREV, {"forced": True}, (True, "forced")),
        (None, {"forced": True}, (True, "forced")),
        (None, {}, (True, "no_snapshot")),
        (None, {"prev_algorithm_version": "other-v2"}, (True, "no_snapshot")),
        (_PREV, {"prev_algorithm_version": "kmeans-v0-pca32-x"}, (True, "algorithm_changed")),
        (_PREV, {"prev_algorithm_version": None}, (True, "algorithm_changed")),
        (_OLD, {"prev_algorithm_version": "x", "drift_share": 0.9}, (True, "algorithm_changed")),
        (_OLD, {}, (True, "age")),
        (_OLD, {"drift_share": 0.9}, (True, "age")),
        (_PREV, {"drift_share": 0.11}, (True, "drift")),
        (_PREV, {"drift_share": 0.10}, (False, "none")),
        (_PREV, {"drift_share": 0.9, "n_new": DRIFT_MIN_NEW - 1}, (False, "none")),
        (_PREV, {"drift_share": None, "n_new": 10_000}, (False, "none")),
    ],
)
def test_ut03_99_reasons_in_order(
    prev: SnapshotMeta | None, overrides: dict[str, object], expected: tuple[bool, str]
) -> None:
    """UT03-99 first match wins: forced, no_snapshot, algorithm_changed, age, drift."""
    kwargs = {**_BASE, **overrides}
    assert is_full_recluster_due(prev, **kwargs) == expected  # type: ignore[arg-type]


def test_ut03_99_drift_min_new_is_open_item_value() -> None:
    """UT03-99 DRIFT_MIN_NEW is 500 (OI-09); ALGORITHM_BASE is the spec's constant."""
    assert DRIFT_MIN_NEW == 500
    assert ALGORITHM_BASE == "proto-hdbscan-v1-pca64"
