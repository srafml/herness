"""Tests of the memory vector ANN index (impl 07 U07-49 `refresh_index`, T07-27; UT07-91, UT07-92).

The threshold is the module constant `_vector_ann.MIN_ROWS`, lowered by monkeypatch in UT07-91
so a few hundred rows stand for 10,000; UT07-92 runs the production threshold on 20,000
clustered corpus vectors (`tests.support.memory_bench_corpus`) and is marked `slow`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest
from structlog.testing import capture_logs
from tests.support.memory_bench_corpus import near_queries, seed_vectors

from herness.core.errors import ModelUnavailable
from herness.enrich import embed_stage
from herness.harness.memory import _vector_ann as ann
from herness.harness.memory.store import VectorIndex, VectorRow
from herness.store.vectors import EMBEDDING_DIM, VectorStore

pytestmark = pytest.mark.unit

SMALL = 300  # rows standing for MIN_ROWS (PQ training needs 256 rows)
ALL_LAYERS = ["semantic", "procedural", "episodic"]


def _mid(n: int) -> str:
    return f"mem_{n:026d}"


def _rows(start: int, count: int, *, layer: Any = "semantic", status: Any = "active",
          seed: int = 3) -> list[VectorRow]:  # fmt: skip
    vecs = np.random.default_rng(seed + start).standard_normal((count, EMBEDDING_DIM))
    kind = "run_summary" if layer == "episodic" else "glossary"
    return [VectorRow(_mid(start + i), layer, kind, status, "h", "bge-m3", vecs[i])
            for i in range(count)]  # fmt: skip


@pytest.fixture
def index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> VectorIndex:
    """A VectorIndex in tmp whose threshold is SMALL rows."""
    monkeypatch.setattr(ann, "MIN_ROWS", SMALL)
    return VectorIndex(lambda: VectorStore(tmp_path / "vectors"))


def _table(index: VectorIndex) -> Any:
    return index._tbl(latest=True)


def _indexed(index: VectorIndex) -> bool:
    return any("vector" in i.columns for i in _table(index).list_indices())


def _recorded(index: VectorIndex) -> bytes | None:
    metadata = _table(index).schema.field("vector").metadata or {}
    return metadata.get(ann.ROWS_KEY.encode())


# --- UT07-91 threshold, rebuild vs optimize ----------------------------------------------------


def test_ut07_91_policy_mirrors_spec03_constants() -> None:
    """UT07-91 the index policy constants equal spec 03's (one policy for LanceDB tables)."""
    assert ann.MIN_ROWS == embed_stage.INDEX_MIN_ROWS == 10_000
    assert ann.GROWTH == embed_stage.INDEX_GROWTH
    assert ann.SUB_VECTORS == embed_stage.INDEX_SUB_VECTORS == 64
    assert ann.ROWS_KEY == embed_stage.INDEX_ROWS_KEY


def test_ut07_91_below_threshold_skips_and_searches_flat(index: VectorIndex) -> None:
    """UT07-91 below MIN_ROWS no index is built; search answers from the flat scan."""
    rows = _rows(0, SMALL - 1)
    index.upsert(rows)
    assert index.refresh_index() == {"action": "skipped", "rows": SMALL - 1}
    assert not _indexed(index)
    assert index.search(rows[5].vector, ["semantic"], ["active"], 3)[0][0] == _mid(5)


def test_ut07_91_empty_table_skips(index: VectorIndex) -> None:
    """UT07-91 a fresh table is skipped (0 rows)."""
    assert index.refresh_index() == {"action": "skipped", "rows": 0}


def test_ut07_91_ensure_table_builds_nothing(index: VectorIndex) -> None:
    """UT07-91 ensure_table stays cheap: at the threshold it still builds no index."""
    index.upsert(_rows(0, SMALL))
    index.ensure_table()
    assert not _indexed(index)


def test_ut07_91_at_threshold_builds_and_logs(index: VectorIndex) -> None:
    """UT07-91 at MIN_ROWS the IVF_PQ cosine index is built, its row count recorded in the
    `vector` field metadata and `memory.vector.index_built` logged with rows and duration."""
    rows = _rows(0, SMALL)
    index.upsert(rows)
    with capture_logs() as logs:
        assert index.refresh_index() == {"action": "built", "rows": SMALL}
    (config,) = _table(index).list_indices()
    assert config.columns == ["vector"]
    assert config.index_type == "IvfPq"
    assert _recorded(index) == str(SMALL).encode()
    (event,) = [e for e in logs if e["event"] == "memory.vector.index_built"]
    assert (event["log_level"], event["action"], event["rows"]) == ("info", "built", SMALL)
    assert isinstance(event["duration_ms"], int)
    assert event["duration_ms"] >= 0
    assert index.search(rows[7].vector, ["semantic"], ["active"], 3)[0][0] == _mid(7)


def test_ut07_91_growth_within_bound_optimizes(index: VectorIndex) -> None:
    """UT07-91 up to 1.2x the recorded rows the index is optimized, not rebuilt; rows added
    after the build are answered before and after the optimize."""
    index.upsert(_rows(0, SMALL))
    index.refresh_index()
    extra = _rows(SMALL, SMALL // 5)  # exactly 1.2x
    index.upsert(extra)
    assert index.search(extra[0].vector, ["semantic"], ["active"], 1)[0][0] == extra[0].memory_id
    with capture_logs() as logs:
        assert index.refresh_index() == {"action": "optimized", "rows": SMALL + SMALL // 5}
    assert not [e for e in logs if e["event"] == "memory.vector.index_built"]
    assert _recorded(index) == str(SMALL).encode()
    assert index.search(extra[1].vector, ["semantic"], ["active"], 1)[0][0] == extra[1].memory_id


def test_ut07_91_growth_past_bound_rebuilds(index: VectorIndex) -> None:
    """UT07-91 past 1.2x the recorded rows the index is rebuilt and the new count recorded."""
    index.upsert(_rows(0, SMALL))
    index.refresh_index()
    index.upsert(_rows(SMALL, SMALL // 5 + 1))
    total = SMALL + SMALL // 5 + 1
    assert index.refresh_index() == {"action": "rebuilt", "rows": total}
    assert _recorded(index) == str(total).encode()
    assert len(list(_table(index).list_indices())) == 1


def test_ut07_91_index_without_recorded_rows_is_rebuilt(index: VectorIndex) -> None:
    """UT07-91 an index whose row count was never recorded (metadata lost) is rebuilt."""
    index.upsert(_rows(0, SMALL))
    index.refresh_index()
    _table(index).update_field_metadata({"path": "vector", "metadata": {ann.ROWS_KEY: "x"}})
    assert index.refresh_index() == {"action": "rebuilt", "rows": SMALL}


def test_ut07_91_prefilter_on_layer_and_status_with_index(index: VectorIndex) -> None:
    """UT07-91 with the index the layer/status prefilter still holds, and a narrow filter
    returns every matching row (probes widen until `limit` rows are found)."""
    index.upsert(_rows(0, SMALL))
    episodic = _rows(1000, 6, layer="episodic")
    index.upsert(episodic)
    index.refresh_index()
    index.set_status([_mid(3)], "rejected")
    query = _rows(0, SMALL)[3].vector
    assert _mid(3) not in {h[0] for h in index.search(query, ["semantic"], ["active"], 10)}
    assert index.search(query, ["semantic"], ["rejected"], 10)[0][0] == _mid(3)
    found = index.search(query, ["episodic"], ["active"], 10)
    assert sorted(h[0] for h in found) == sorted(r.memory_id for r in episodic)


def test_ut07_91_distances_are_exact_with_index(index: VectorIndex) -> None:
    """UT07-91 indexed search returns exact cosine distances (refine re-rank), as the
    near-duplicate thresholds need: the stored vector itself is at distance 0."""
    rows = _rows(0, SMALL)
    index.upsert(rows)
    index.refresh_index()
    hits = index.search(rows[11].vector, ALL_LAYERS, ["active"], 5)
    assert hits[0] == (_mid(11), pytest.approx(0.0, abs=1e-5))
    unit = rows[11].vector / np.linalg.norm(rows[11].vector)
    stored = index.vectors([hits[1][0]])[hits[1][0]]
    assert hits[1][1] == pytest.approx(1.0 - float(unit @ stored), abs=1e-5)


def test_ut07_91_lancedb_failure_is_model_unavailable(
    index: VectorIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-91 a LanceDB error during the refresh maps to ModelUnavailable (vector path down)."""
    index.upsert(_rows(0, SMALL))

    def broken(*_args: object, **_kwargs: object) -> None:
        msg = "lance io"
        raise OSError(msg)

    monkeypatch.setattr(type(_table(index)), "create_index", broken)
    with pytest.raises(ModelUnavailable, match="refresh_index"):
        index.refresh_index()


# --- UT07-92 recall guard -----------------------------------------------------------------------


@pytest.mark.slow  # 20,000 x 1,024 vectors and an index build: about 10 s
def test_ut07_92_recall_at_10_vs_flat(tmp_path: Path) -> None:
    """UT07-92 recall@10 of the indexed search against flat search is at least 0.90 on the
    bench corpus vectors (20,000 rows, production threshold and parameters)."""
    seed_vectors(tmp_path, 20_000)
    index = VectorIndex(lambda: VectorStore(tmp_path / "vectors"))
    assert index.refresh_index() == {"action": "built", "rows": 20_000}
    table = _table(index)
    where = "layer IN ('semantic', 'procedural', 'episodic') AND status IN ('active')"
    recalls = []
    for query in near_queries(40):
        flat = table.search(query).distance_type("cosine").bypass_vector_index()
        truth = set(flat.where(where, prefilter=True).limit(10).to_arrow()["memory_id"].to_pylist())
        found = {h[0] for h in index.search(query, ALL_LAYERS, ["active"], 10)}
        recalls.append(len(found & truth) / 10)
    assert float(np.mean(recalls)) >= 0.90
