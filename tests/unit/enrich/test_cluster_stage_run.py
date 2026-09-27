"""Tests for herness.enrich.cluster_stage run and finalize (U03-105, U03-106; T03-25). CPU.

A hand-built warehouse and a tmp LanceDB store stand in for spec 11's `tiny_build`.
"""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pyarrow.parquet as pq
import pytest
import torch
from tests.support.build_harness import FakeJobContext
from tests.unit.enrich._cluster_support import (
    T_NOW,
    Inc,
    cluster_warehouse,
    clusters_of,
    enrich_paths,
    finalize_auto,
    fixed_ids,
    freeze_now,
    members_of,
    near,
    planted,
    planted_incidents,
    question_set,
    run_stage,
    settings,
    store_vectors,
    unit,
    vector_store,
)

from herness.core.errors import ConfigError, FatalError, SchemaViolation, StoreBusy
from herness.enrich import _cluster_full, cluster, cluster_stage
from herness.enrich.cluster_describe import NameResult
from herness.enrich.cluster_stage import ClusterSnapshot, finalize_clusters
from herness.enrich.gpu import YieldRequested
from herness.enrich.layout import EnrichPaths
from herness.store.vectors import VectorStore

pytestmark = pytest.mark.unit

CURRENT = "data/models/clusters/CURRENT"


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[VectorStore, list[Inc]]:
    """Frozen clock, fixed ids, 64-row chunks and 3 planted clusters of 40 plus 6 noise."""
    freeze_now(monkeypatch)
    fixed_ids(monkeypatch)
    monkeypatch.setattr(cluster_stage, "CHUNK", 64)
    store = vector_store(tmp_path)
    vectors, truth = planted(3, 40, noise=6, seed=11)
    incs = planted_incidents(vectors, truth)
    store_vectors(store, incs)
    return store, incs


def _first_build(tmp_path: Path, incs: list[Inc], paths: EnrichPaths) -> Path:
    path = tmp_path / "b1.duckdb"
    wh = cluster_warehouse(path, incs)
    result, _ = run_stage(wh, paths, "b-0001")
    assert result.kind == "full"
    finalize_auto(wh, result, paths)
    wh.close()
    return path


def _read(path: Path) -> tuple[dict[str, str], dict[str, tuple[Any, ...]]]:
    wh = duckdb.connect(str(path), read_only=True)
    try:
        return members_of(wh), clusters_of(wh)
    finally:
        wh.close()


# --- UT03-100: run_cluster_stage ---------------------------------------------------------------


def test_ut03_100_incremental_copies_memberships_and_keeps_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, world: tuple[VectorStore, list[Inc]]
) -> None:
    """UT03-100 prev snapshot + 10 changed incidents: incremental; memberships copied, IDs kept."""
    store, incs = world
    paths = enrich_paths(tmp_path)
    b1 = _first_build(tmp_path, incs, paths)
    before, clusters_before = _read(b1)
    current = (tmp_path / CURRENT).read_text("utf-8")
    issued = fixed_ids(monkeypatch, prefix="cl_NEW")
    origin = {}
    edited = [near(incs[i], incs[i].number, f"edited text {i}", i) for i in (0, 41, 82, 1, 42)]
    fresh = [near(incs[i], f"NEW{i:03d}", f"new text {i}", 100 + i) for i in (2, 43, 84, 3, 44)]
    for inc, i in zip(edited + fresh, (0, 41, 82, 1, 42, 2, 43, 84, 3, 44), strict=True):
        origin[inc.record_id] = incs[i].record_id
    store_vectors(store, edited + fresh)
    kept = [inc for inc in incs if inc.record_id not in origin]
    wh = cluster_warehouse(tmp_path / "b2.duckdb", [*edited, *kept, *fresh])
    ctx = FakeJobContext()
    result, report = run_stage(wh, paths, "b-0002", prev_warehouse=b1, ctx=ctx)
    assert (result.kind, result.naming, issued) == ("incremental", [], [])
    after = members_of(wh)
    unchanged = {r: c for r, c in before.items() if r not in origin and r in after}
    assert len(unchanged) >= 100
    assert {r: after[r] for r in unchanged} == unchanged
    for record_id, source in origin.items():
        assert after.get(record_id) == before.get(source)
    assert {c: v[0] for c, v in clusters_of(wh).items()} == {
        c: v[0] for c, v in clusters_before.items()
    }
    assert sum(v[2] for v in clusters_of(wh).values()) == len(after)
    assert report.rows == len(after)
    assert (tmp_path / CURRENT).read_text("utf-8") == current
    assert ctx.heartbeats
    assert set(ctx.heartbeats) == {"cluster"}
    assert not list((tmp_path / "data/models/clusters").glob("*/b-0002"))


def test_ut03_100_full_run_writes_assigned_snapshot_and_tables(
    tmp_path: Path, world: tuple[VectorStore, list[Inc]]
) -> None:
    """UT03-100 first run is full: assigned snapshot + members.parquet; noise has no row."""
    _, incs = world
    paths = enrich_paths(tmp_path)
    wh = cluster_warehouse(tmp_path / "b1.duckdb", incs)
    ctx = FakeJobContext()
    result, report = run_stage(wh, paths, "b-0001", ctx=ctx)
    snap = result.snapshot
    assert (result.kind, snap.snapshot_id, snap.meta.status) == ("full", "b-0001", "assigned")
    assert snap.algorithm_version.startswith(cluster_stage.ALGORITHM_BASE + "-")
    assert not (tmp_path / CURRENT).exists()
    members = members_of(wh)
    assert set(members.values()) == {"cl_T0000", "cl_T0001", "cl_T0002"}
    assert all(r not in members for r in (i.record_id for i in incs[120:]))
    stored = pq.read_table(snap.folder(paths) / "members.parquet")
    assert (
        dict(zip(*stored.select(["record_id", "cluster_id"]).to_pydict().values(), strict=True))
        == members
    )
    rows = wh.execute("SELECT label, top_terms, algorithm_version FROM enrich.cluster").fetchall()
    assert all(r[0] is None and r[1] and r[2] == snap.algorithm_version for r in rows)
    assert {c.cluster_id for c in result.naming} == set(members.values())
    assert all(c.examples and len(c.examples) <= 20 for c in result.naming)
    assert report.rows == len(members)
    assert len(ctx.heartbeats) >= 6


def test_ut03_100_rerun_resumes_assigned_snapshot_after_yield(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, world: tuple[VectorStore, list[Inc]]
) -> None:
    """UT03-100 yield after the assigned snapshot; the rerun resumes it without new ids."""
    _, incs = world
    paths = enrich_paths(tmp_path)
    wh = cluster_warehouse(tmp_path / "b1.duckdb", incs)
    with pytest.raises(YieldRequested):
        run_stage(wh, paths, "b-0001", ctx=FakeJobContext(yield_after=0))
    assert wh.execute("SELECT count(*) FROM enrich.cluster_member").fetchone() == (0,)
    issued = fixed_ids(monkeypatch, prefix="cl_RE")
    result, _ = run_stage(wh, paths, "b-0001")
    assert (result.kind, issued) == ("full", [])
    assert set(members_of(wh).values()) == {"cl_T0000", "cl_T0001", "cl_T0002"}
    assert {c.cluster_id for c in result.naming} == set(members_of(wh).values())


def test_ut03_100_full_recluster_inherits_ids_and_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, world: tuple[VectorStore, list[Inc]]
) -> None:
    """UT03-100 forced full run: ids and labels inherited, nothing needs naming, then final."""
    _, incs = world
    paths = enrich_paths(tmp_path)
    b1 = _first_build(tmp_path, incs, paths)
    before, clusters_before = _read(b1)
    issued = fixed_ids(monkeypatch, prefix="cl_NEW")
    wh = cluster_warehouse(tmp_path / "b2.duckdb", incs)
    result, _ = run_stage(wh, paths, "b-0002", prev_warehouse=b1, force_full=True)
    assert (result.kind, issued, result.naming) == ("full", [], [])
    assert members_of(wh) == before
    assert clusters_of(wh) == clusters_before
    finalize_auto(wh, result, paths)
    loaded = ClusterSnapshot.load_current(paths)
    assert loaded is not None
    assert (loaded.snapshot_id, loaded.meta.status) == ("b-0002", "final")


@pytest.mark.parametrize("prev_warehouse", [None, "missing.duckdb", "bad\x01name"])
def test_ut03_100_without_previous_warehouse_runs_full(
    tmp_path: Path, world: tuple[VectorStore, list[Inc]], prev_warehouse: str | None
) -> None:
    """UT03-100 prev snapshot but no usable previous warehouse: full run, ids inherited."""
    _, incs = world
    paths = enrich_paths(tmp_path)
    _first_build(tmp_path, incs, paths)
    wh = cluster_warehouse(tmp_path / "b2.duckdb", incs)
    prev = None if prev_warehouse is None else tmp_path / prev_warehouse
    result, _ = run_stage(wh, paths, "b-0002", prev_warehouse=prev)
    assert result.kind == "full"
    assert set(members_of(wh).values()) == {"cl_T0000", "cl_T0001", "cl_T0002"}


def test_ut03_100_corrupt_current_snapshot_runs_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, world: tuple[VectorStore, list[Inc]]
) -> None:
    """UT03-100 corrupt CURRENT snapshot -> treated as absent: full run with new ids."""
    _, incs = world
    paths = enrich_paths(tmp_path)
    b1 = _first_build(tmp_path, incs, paths)
    (tmp_path / CURRENT).write_text("../escape/x", "utf-8")
    issued = fixed_ids(monkeypatch, prefix="cl_NEW")
    wh = cluster_warehouse(tmp_path / "b2.duckdb", incs)
    result, _ = run_stage(wh, paths, "b-0002", prev_warehouse=b1)
    assert result.kind == "full"
    assert len(issued) == 3


def test_ut03_100_drift_triggers_full_recluster_in_same_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, world: tuple[VectorStore, list[Inc]]
) -> None:
    """UT03-100 changed incidents far from every prototype -> drift -> full in the same run."""
    store, incs = world
    paths = enrich_paths(tmp_path)
    b1 = _first_build(tmp_path, incs, paths)
    monkeypatch.setattr(cluster_stage, "DRIFT_MIN_NEW", 5)
    rng = np.random.default_rng(3)
    odd = [Inc(f"ODD{i:03d}", f"strange {i}", unit(rng.standard_normal(1024))) for i in range(12)]
    store_vectors(store, odd)
    wh = cluster_warehouse(tmp_path / "b2.duckdb", [*incs, *odd])
    strict = settings(assign_min_sim=0.95, full_sim=0.99)
    result, _ = run_stage(wh, paths, "b-0002", prev_warehouse=b1, cfg=strict)
    assert result.kind == "full"
    assert result.snapshot.snapshot_id == "b-0002"


def test_ut03_100_age_triggers_full_recluster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, world: tuple[VectorStore, list[Inc]]
) -> None:
    """UT03-100 a snapshot older than full_every_days -> full recluster, ids kept."""
    _, incs = world
    paths = enrich_paths(tmp_path)
    b1 = _first_build(tmp_path, incs, paths)
    freeze_now(monkeypatch, T_NOW + timedelta(days=8))
    wh = cluster_warehouse(tmp_path / "b2.duckdb", incs)
    result, _ = run_stage(wh, paths, "b-0002", prev_warehouse=b1)
    assert result.kind == "full"
    assert set(members_of(wh).values()) == {"cl_T0000", "cl_T0001", "cl_T0002"}


def test_ut03_100_non_allowlisted_changed_id_is_skipped(
    tmp_path: Path, world: tuple[VectorStore, list[Inc]]
) -> None:
    """UT03-100 a changed incident whose id fails the U03-33 allowlist is never read."""
    _, incs = world
    paths = enrich_paths(tmp_path)
    b1 = _first_build(tmp_path, incs, paths)
    odd = Inc("BAD'ID", "weird text", incs[0].vector)
    wh = cluster_warehouse(tmp_path / "b2.duckdb", [*incs, odd])
    result, _ = run_stage(wh, paths, "b-0002", prev_warehouse=b1)
    assert result.kind == "incremental"
    assert odd.record_id not in members_of(wh)


def test_ut03_100_too_few_vectors_is_config_error(tmp_path: Path, world: Any) -> None:
    """UT03-100 an almost empty window -> ConfigError before any snapshot is written."""
    _, incs = world
    paths = enrich_paths(tmp_path)
    wh = cluster_warehouse(tmp_path / "b1.duckdb", incs[:20])
    with pytest.raises(ConfigError, match="too few"):
        run_stage(wh, paths, "b-0001", cfg=settings(pca_dims=8, k_min=1, proto_per=10))
    assert not (tmp_path / "data/models/clusters").exists()


def test_ut03_100_cuda_oom_is_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, world: Any
) -> None:
    """UT03-100 CUDA OOM during k-means -> FatalError (CUDA released first)."""
    _, incs = world
    released: list[bool] = []

    def oom(*_a: object, **_k: object) -> None:
        raise torch.cuda.OutOfMemoryError

    monkeypatch.setattr(cluster, "spherical_kmeans", oom)
    monkeypatch.setattr(_cluster_full, "release_cuda", lambda: released.append(True))
    wh = cluster_warehouse(tmp_path / "b1.duckdb", incs)
    with pytest.raises(FatalError, match="out of memory"):
        run_stage(wh, enrich_paths(tmp_path), "b-0001")
    assert released == [True]


def test_ut03_100_lancedb_conflict_is_store_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, world: Any
) -> None:
    """UT03-100 a LanceDB commit conflict while streaming -> StoreBusy."""
    _, incs = world

    def busy(*_a: object, **_k: object) -> None:
        msg = "commit conflict"
        raise OSError(msg)

    monkeypatch.setattr("lancedb.query.LanceEmptyQueryBuilder.to_batches", lambda *_a, **_k: busy())
    wh = cluster_warehouse(tmp_path / "b1.duckdb", incs)
    with pytest.raises(StoreBusy):
        run_stage(wh, enrich_paths(tmp_path), "b-0001")


def test_ut03_100_write_failure_is_schema_violation(tmp_path: Path, world: Any) -> None:
    """UT03-100 a warehouse write error -> SchemaViolation, transaction rolled back."""
    _, incs = world
    wh = cluster_warehouse(tmp_path / "b1.duckdb", incs)
    wh.execute("DROP TABLE enrich.cluster_member")
    wh.execute("CREATE TABLE enrich.cluster_member (record_id INTEGER)")
    with pytest.raises(SchemaViolation, match="cluster stage write"):
        run_stage(wh, enrich_paths(tmp_path), "b-0001")
    assert wh.execute("SELECT count(*) FROM enrich.cluster").fetchone() == (0,)


def test_ut03_100_deterministic_for_same_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, world: Any
) -> None:
    """UT03-100 the same input and seed give the same memberships and snapshot arrays."""
    _, incs = world
    runs = []
    for n in (1, 2):
        fixed_ids(monkeypatch)
        paths = enrich_paths(tmp_path / f"r{n}")
        store_vectors(vector_store(tmp_path / f"r{n}"), incs)
        wh = cluster_warehouse(tmp_path / f"r{n}.duckdb", incs)
        result, _ = run_stage(wh, paths, "b-0001")
        runs.append((members_of(wh), result.snapshot.prototypes, result.snapshot.pca.fit_id))
    assert runs[0][0] == runs[1][0]
    assert np.array_equal(runs[0][1], runs[1][1])
    assert runs[0][2] == runs[1][2]


# --- UT03-101: finalize_clusters ---------------------------------------------------------------


def _decide(wh: duckdb.DuckDBPyConnection, answers: dict[str, str]) -> None:
    for record_id, answer in answers.items():
        wh.execute(
            "INSERT INTO enrich.decision (record_id, question, answer) VALUES (?, 'root_cause', ?)",
            [record_id, answer],
        )


def test_ut03_101_majority_tie_takes_label_ascending_and_current_last(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, world: tuple[VectorStore, list[Inc]]
) -> None:
    """UT03-101 tied root_cause answers -> label ascending; CURRENT replaced last."""
    _, incs = world
    paths = enrich_paths(tmp_path)
    wh = cluster_warehouse(tmp_path / "b1.duckdb", incs)
    result, _ = run_stage(wh, paths, "b-0001")
    members = members_of(wh)
    by_cluster: dict[str, list[str]] = {}
    for record_id, cid in sorted(members.items()):
        by_cluster.setdefault(cid, []).append(record_id)
    tie, llm, other = sorted(by_cluster)
    _decide(wh, dict.fromkeys(by_cluster[tie][:3], "network"))
    _decide(wh, dict.fromkeys(by_cluster[tie][3:6], "hardware"))
    _decide(wh, dict.fromkeys(by_cluster[llm][:5], "software"))
    names = {
        tie: NameResult("auto: disk / disks", None, "auto"),
        llm: NameResult("VPN outages", "network", "llm"),
        other: NameResult("auto: vpn", "stale", "auto"),
    }
    replaced: list[str] = []
    real_replace = os.replace

    def spy(src: Any, dst: Any) -> None:
        replaced.append(Path(dst).name)
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    finalize_clusters(wh, result=result, names=names, qs=question_set(), paths=paths)
    rows = clusters_of(wh)
    assert rows[tie][:2] == ("auto: disk / disks", "hardware")
    assert rows[llm][:2] == ("VPN outages", "network")
    assert rows[other][:2] == ("auto: vpn", None)
    assert replaced == ["centroids.parquet", "snapshot.json", "CURRENT"]
    loaded = ClusterSnapshot.load_current(paths)
    assert loaded is not None
    assert loaded.meta.status == "final"
    cent = loaded.centroids.to_pydict()
    for i, cid in enumerate(cent["cluster_id"]):
        assert cent["named_size"][i] == cent["size"][i]
        assert cent["named_centroid"][i] == cent["centroid"][i]
        assert cent["label"][i] == rows[cid][0]


def test_ut03_101_without_root_cause_question_category_is_null(
    tmp_path: Path, world: tuple[VectorStore, list[Inc]]
) -> None:
    """UT03-101 no `root_cause` question -> auto clusters get NULL; unnamed rows get a label."""
    _, incs = world
    paths = enrich_paths(tmp_path)
    wh = cluster_warehouse(tmp_path / "b1.duckdb", incs)
    result, _ = run_stage(wh, paths, "b-0001")
    _decide(wh, dict.fromkeys(members_of(wh), "hardware"))
    finalize_clusters(wh, result=result, names={}, qs=question_set(root_cause=False), paths=paths)
    rows = clusters_of(wh)
    assert all(label.startswith("auto: ") and rc is None for label, rc, _ in rows.values())


def test_ut03_101_incremental_finalize_keeps_current(
    tmp_path: Path, world: tuple[VectorStore, list[Inc]]
) -> None:
    """UT03-101 finalizing an incremental run changes no snapshot file or CURRENT."""
    _, incs = world
    paths = enrich_paths(tmp_path)
    b1 = _first_build(tmp_path, incs, paths)
    current = (tmp_path / CURRENT).read_text("utf-8")
    wh = cluster_warehouse(tmp_path / "b2.duckdb", incs)
    result, _ = run_stage(wh, paths, "b-0002", prev_warehouse=b1)
    assert result.kind == "incremental"
    _decide(wh, dict.fromkeys(members_of(wh), "network"))
    finalize_clusters(wh, result=result, names={}, qs=question_set(), paths=paths)
    assert (tmp_path / CURRENT).read_text("utf-8") == current
    assert all(rc == "network" for _, rc, _ in clusters_of(wh).values())


def test_ut03_101_missing_decision_table_is_schema_violation(
    tmp_path: Path, world: tuple[VectorStore, list[Inc]]
) -> None:
    """UT03-101 a warehouse error during finalize -> SchemaViolation; CURRENT unchanged."""
    _, incs = world
    paths = enrich_paths(tmp_path)
    wh = cluster_warehouse(tmp_path / "b1.duckdb", incs)
    result, _ = run_stage(wh, paths, "b-0001")
    wh.execute("DROP TABLE enrich.decision")
    with pytest.raises(SchemaViolation, match="cluster finalize"):
        finalize_clusters(wh, result=result, names={}, qs=question_set(), paths=paths)
    assert not (tmp_path / CURRENT).exists()
