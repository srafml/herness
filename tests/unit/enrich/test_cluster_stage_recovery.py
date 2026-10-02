"""Tests for herness.enrich.cluster_stage OOM, rerun and previous-warehouse handling
(U03-105; T03-25 review round 1). CPU: CUDA OOM is simulated by a fake projector."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pytest
import torch
from tests.support.build_harness import FakeJobContext
from tests.unit.enrich._cluster_support import (
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
    run_stage,
    store_vectors,
    unit,
    vector_store,
)

from herness.core.errors import FatalError
from herness.enrich import _cluster_io, cluster_stage
from herness.enrich.cluster import project as real_project
from herness.enrich.gpu import YieldRequested
from herness.enrich.layout import EnrichPaths
from herness.enrich.text import content_hash
from herness.store.vectors import VectorStore

pytestmark = pytest.mark.unit

_ULID_ID = re.compile(r"^cl_[0-9A-HJKMNP-TV-Z]{26}$")


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[VectorStore, list[Inc]]:
    """Frozen clock, 64-row chunks (floor 16) and 3 planted clusters of 40 plus 6 noise."""
    freeze_now(monkeypatch)
    monkeypatch.setattr(cluster_stage, "CHUNK", 64)
    monkeypatch.setattr(_cluster_io, "MIN_CHUNK", 16)
    store = vector_store(tmp_path)
    vectors, truth = planted(3, 40, noise=6, seed=11)
    incs = planted_incidents(vectors, truth)
    store_vectors(store, incs)
    return store, incs


def _first_build(tmp_path: Path, incs: list[Inc], paths: EnrichPaths) -> Path:
    path = tmp_path / "b1.duckdb"
    wh = cluster_warehouse(path, incs)
    result, _ = run_stage(wh, paths, "b-0001")
    finalize_auto(wh, result, paths)
    wh.close()
    return path


def _oom_projector(
    monkeypatch: pytest.MonkeyPatch, *, fail_at: int | None
) -> tuple[list[int], list[bool]]:
    """Patch the projector: OOM once at chunk `fail_at` (None: at every chunk). Returns
    (chunks seen, release_cuda calls)."""
    chunks: list[int] = []
    released: list[bool] = []

    def fake(vectors: np.ndarray, pca: Any, *, device: str, chunk: int) -> np.ndarray:
        chunks.append(chunk)
        if fail_at is None or (chunk == fail_at and chunks.count(chunk) == 1):
            raise torch.cuda.OutOfMemoryError
        return real_project(vectors, pca, device=device, chunk=chunk)

    monkeypatch.setattr(_cluster_io, "project", fake)
    monkeypatch.setattr(_cluster_io, "release_cuda", lambda: released.append(True))
    return chunks, released


def test_ut03_100_projection_oom_halves_chunk_with_same_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, world: tuple[VectorStore, list[Inc]]
) -> None:
    """UT03-100 CUDA OOM once while projecting -> released, retried at chunk // 2; same result."""
    _, incs = world
    runs = []
    chunks: list[int] = []
    released: list[bool] = []
    for n in (1, 2):
        fixed_ids(monkeypatch)
        if n == 2:
            chunks, released = _oom_projector(monkeypatch, fail_at=64)
        store_vectors(vector_store(tmp_path / f"r{n}"), incs)
        wh = cluster_warehouse(tmp_path / f"r{n}.duckdb", incs)
        result, _ = run_stage(wh, enrich_paths(tmp_path / f"r{n}"), "b-0001")
        runs.append((members_of(wh), clusters_of(wh), result.snapshot.prototypes))
    assert chunks[:2] == [64, 32]
    assert set(chunks[2:]) == {32}
    assert released == [True]
    assert runs[0][:2] == runs[1][:2]
    assert np.array_equal(runs[0][2], runs[1][2])


@pytest.mark.parametrize("path", ["full", "incremental", "rerun"])
def test_ut03_100_projection_oom_at_minimum_chunk_is_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, world: tuple[VectorStore, list[Inc]], path: str
) -> None:
    """UT03-100 CUDA OOM down to the minimum chunk -> FatalError on every projection path."""
    store, incs = world
    fixed_ids(monkeypatch)
    paths = enrich_paths(tmp_path)
    kw: dict[str, Any] = {}
    if path == "incremental":  # one new incident, so the incremental run projects
        kw["prev_warehouse"] = _first_build(tmp_path, incs, paths)
        incs = [*incs, near(incs[0], "NEW001", "new text", 7)]
        store_vectors(store, incs[-1:])
    wh = cluster_warehouse(tmp_path / "b2.duckdb", incs)
    if path == "rerun":
        with pytest.raises(YieldRequested):
            run_stage(wh, paths, "b-0002", ctx=FakeJobContext(yield_after=0))
    chunks, released = _oom_projector(monkeypatch, fail_at=None)
    with pytest.raises(FatalError, match="cuda out of memory") as info:
        run_stage(wh, paths, "b-0002", **kw)
    assert chunks == [64, 32, 16]
    assert len(released) == 3
    assert info.value.__cause__ is None
    assert wh.execute("SELECT count(*) FROM enrich.cluster_member").fetchone() == (0,)


def test_ut03_100_new_cluster_ids_are_ulids(
    tmp_path: Path, world: tuple[VectorStore, list[Inc]]
) -> None:
    """UT03-100 the real id factory: every new cluster id is "cl_" + a 26-char ULID."""
    _, incs = world
    wh = cluster_warehouse(tmp_path / "b1.duckdb", incs)
    run_stage(wh, enrich_paths(tmp_path), "b-0001")
    ids = set(members_of(wh).values())
    assert len(ids) == 3
    assert all(_ULID_ID.match(cid) for cid in ids)


def test_ut03_100_previous_warehouse_without_enrich_tables_runs_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, world: tuple[VectorStore, list[Inc]]
) -> None:
    """UT03-100 a previous warehouse that attaches but lacks the enrich tables -> full run."""
    _, incs = world
    fixed_ids(monkeypatch)
    paths = enrich_paths(tmp_path)
    _first_build(tmp_path, incs, paths)
    duckdb.connect(str(tmp_path / "empty.duckdb")).close()
    wh = cluster_warehouse(tmp_path / "b2.duckdb", incs)
    result, _ = run_stage(wh, paths, "b-0002", prev_warehouse=tmp_path / "empty.duckdb")
    assert result.kind == "full"
    assert set(members_of(wh).values()) == {"cl_T0000", "cl_T0001", "cl_T0002"}
    attached = {r[0] for r in wh.execute("SELECT database_name FROM duckdb_databases()").fetchall()}
    assert "cluster_prev" not in attached


def test_ut03_100_other_entity_row_does_not_mask_a_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, world: tuple[VectorStore, list[Inc]]
) -> None:
    """UT03-100 change detection reads only previous `incident` rows of text_redacted."""
    store, incs = world
    fixed_ids(monkeypatch)
    paths = enrich_paths(tmp_path)
    b1 = _first_build(tmp_path, incs, paths)
    rng = np.random.default_rng(9)
    edited = Inc(incs[0].number, "rewritten far away", unit(rng.standard_normal(1024)))
    with duckdb.connect(str(b1)) as prev:
        prev.execute(
            "INSERT INTO enrich.text_redacted VALUES (?, 'problem', 'x', ?)",
            [edited.record_id, content_hash(edited.text)],
        )
    store_vectors(store, [edited])
    wh = cluster_warehouse(tmp_path / "b2.duckdb", [edited, *incs[1:]])
    result, _ = run_stage(wh, paths, "b-0002", prev_warehouse=b1)
    assert result.kind == "incremental"
    assert edited.record_id not in members_of(wh)


def test_ut03_100_rerun_after_warehouse_write_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, world: tuple[VectorStore, list[Inc]]
) -> None:
    """UT03-100 the same build rerun after its warehouse write: no duplicate rows, no new ids."""
    _, incs = world
    fixed_ids(monkeypatch)
    paths = enrich_paths(tmp_path)
    wh = cluster_warehouse(tmp_path / "b1.duckdb", incs)
    run_stage(wh, paths, "b-0001")
    members, clusters = members_of(wh), clusters_of(wh)
    issued = fixed_ids(monkeypatch, prefix="cl_RE")
    result, _ = run_stage(wh, paths, "b-0001")
    assert (result.kind, issued) == ("full", [])
    assert (members_of(wh), clusters_of(wh)) == (members, clusters)
    counts = wh.execute(
        "SELECT (SELECT count(*) FROM enrich.cluster_member), (SELECT count(*) FROM enrich.cluster)"
    ).fetchone()
    assert counts == (len(members), len(clusters))
