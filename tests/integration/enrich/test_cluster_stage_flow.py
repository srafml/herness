"""IT03-10 ... IT03-12: the cluster stage over several builds (impl 03 F03-09, F03-10; T03-25).

`small_build` and the spec 11 T2/T2c planted-cluster truths are not in the tree yet: a
hand-built DuckDB warehouse per build, a LanceDB store under `tmp_path` and a seeded local
planted-cluster generator (`tests/unit/enrich/_cluster_support.py`) stand in for them
(carry-over: re-point to spec 11 synthetic data / small_build). CPU only.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import numpy as np
import pytest
from sklearn.metrics import adjusted_rand_score  # type: ignore[import-untyped]  # no types
from tests.unit.enrich._cluster_support import (
    Inc,
    cluster_warehouse,
    enrich_paths,
    finalize_auto,
    fixed_ids,
    freeze_now,
    members_of,
    near,
    planted,
    planted_incidents,
    run_stage,
    settings,
    store_vectors,
    vector_store,
)

from herness.enrich import cluster_stage
from herness.enrich.layout import EnrichPaths

pytestmark = pytest.mark.integration

_CFG = {"proto_per": 5, "min_incidents": 15}


def _build(
    tmp_path: Path, name: str, incs: list[Inc], paths: EnrichPaths, **kw: object
) -> tuple[Path, dict[str, str], str]:
    """One build: warehouse, cluster stage, finalize; (warehouse path, members, kind)."""
    path = tmp_path / f"{name}.duckdb"
    wh = cluster_warehouse(path, incs)
    result, _ = run_stage(wh, paths, name, cfg=settings(**_CFG), **kw)
    finalize_auto(wh, result, paths)
    members = members_of(wh)
    wh.close()
    return path, members, result.kind


@pytest.fixture
def corpus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[list[Inc], np.ndarray]:
    """12 planted clusters of 50 incidents plus 60 noise incidents, vectors stored."""
    freeze_now(monkeypatch)
    fixed_ids(monkeypatch)
    monkeypatch.setattr(cluster_stage, "CHUNK", 128)
    vectors, truth = planted(12, 50, noise=60, seed=2026)
    incs = planted_incidents(vectors, truth)
    store_vectors(vector_store(tmp_path), incs)
    return incs, truth


def test_it03_10_nightly_incremental_after_full_changes_no_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corpus: tuple[list[Inc], np.ndarray]
) -> None:
    """IT03-10 full run then a nightly incremental run: no cluster id changes."""
    incs, _ = corpus
    paths = enrich_paths(tmp_path)
    b1, first, kind = _build(tmp_path, "b-0001", incs, paths)
    assert kind == "full"
    issued = fixed_ids(monkeypatch, prefix="cl_NEW")
    fresh = [near(incs[i], f"NEW{i:04d}", f"new text {i}", i) for i in range(0, 600, 50)]
    store_vectors(vector_store(tmp_path), fresh)
    _, second, kind = _build(tmp_path, "b-0002", [*incs, *fresh], paths, prev_warehouse=b1)
    assert (kind, issued) == ("incremental", [])
    assert {r: second[r] for r in first} == first
    assert set(second.values()) == set(first.values())
    assert all(second[inc.record_id] == first[incs[int(inc.number[3:])].record_id] for inc in fresh)


def test_it03_11_full_recluster_keeps_ids_for_95_percent_of_mass(
    tmp_path: Path, corpus: tuple[list[Inc], np.ndarray]
) -> None:
    """IT03-11 full recluster on the data + 1 % new incidents: ids kept for >= 95 % of mass."""
    incs, _ = corpus
    paths = enrich_paths(tmp_path)
    b1, first, _ = _build(tmp_path, "b-0001", incs, paths)
    fresh = [near(incs[i], f"NEW{i:04d}", f"new text {i}", i) for i in range(3, 600, 90)]
    assert len(fresh) >= len(incs) // 100
    store_vectors(vector_store(tmp_path), fresh)
    _, second, kind = _build(
        tmp_path, "b-0002", [*incs, *fresh], paths, prev_warehouse=b1, force_full=True
    )
    assert kind == "full"
    both = [r for r in first if r in second]
    kept = sum(first[r] == second[r] for r in both)
    assert kept / len(first) >= 0.95
    assert sum(c in set(first.values()) for c in second.values()) / len(second) >= 0.95


def test_it03_12_planted_clusters_recovered_with_ari_at_least_0_80(
    tmp_path: Path, corpus: tuple[list[Inc], np.ndarray]
) -> None:
    """IT03-12 synthetic planted clusters, full recluster: ARI >= 0.80 on planted members."""
    incs, truth = corpus
    paths = enrich_paths(tmp_path)
    wh_path, members, kind = _build(tmp_path, "b-0001", incs, paths)
    assert kind == "full"
    planted_ids = [i for i, label in enumerate(truth) if label >= 0]
    predicted = [members.get(incs[i].record_id, f"noise-{i}") for i in planted_ids]
    ari = adjusted_rand_score(truth[planted_ids], predicted)
    assert ari >= 0.80
    wh = duckdb.connect(str(wh_path), read_only=True)
    try:
        labels = wh.execute("SELECT label FROM enrich.cluster").fetchall()
    finally:
        wh.close()
    assert labels
    assert all(label is not None for (label,) in labels)
