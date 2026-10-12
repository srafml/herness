"""Clustering benchmarks BT03-07 (full recluster, naming) and BT03-08 (nightly incremental).

Run (idle host only): `HERNESS_BENCH_FULL=1 HERNESS_SYNTH_FULL_ROOT=<seed 42 full root>
pytest -m "integration and slow" tests/bench/enrich/test_enrich_cluster_bench.py`; the naming
row also needs `HERNESS_BENCH_LLM_PROFILE` (see `_services.py`).

Classification (T03-36 ruling): the recluster and the incremental run are CPU/IO work on
deterministic vectors, so they run here at the spec scale (5M incidents); only the naming
calls need a real local LLM (V-12) and are env-gated.

State (module fixture, built once): `enrich.text_redacted` filled for every incident by SQL
(`fill_text_sql`, unredacted stand-in), planted 1024-d vectors for all of them in a LanceDB
store under the module's own data root (`_cluster_data`), then the real
`run_cluster_stage(force_full=True)` + `finalize_clusters` with auto names. BT03-07 is the
wall time of that run (excluding the LLM naming calls); the device is CUDA when torch sees
one, else the CPU. BT03-08 builds a slim previous warehouse (the full run's `enrich.cluster`,
`enrich.cluster_member`, and `enrich.text_redacted` minus 10,000 incidents chosen by
`sha256(record_id)`), so those 10,000 are the new incidents of the night, and times
`run_cluster_stage` (not forced), which must come out `incremental`.
"""

from __future__ import annotations

import dataclasses
import shutil
import time
from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest
from tests.bench.enrich._cluster_data import write_incident_vectors
from tests.bench.enrich._common import (
    BT07_NAMING_CALLS,
    BT07_NAMING_LIMIT_S,
    BT07_RECLUSTER_LIMIT_S,
    BT08_LIMIT_S,
    LLM_PROFILE_ENV,
    NIGHTLY_RECORDS,
    TIMEOUT_S,
    record,
    record_info,
    require_service,
)
from tests.bench.enrich._lake import LakeBuild, Report, activate_enrich, fill_text_sql
from tests.bench.enrich._services import llm_client
from tests.support._build_clock import pinned_clock
from tests.support.bench import BenchRecorder
from tests.support.build_harness import FakeJobContext

from herness.core.config import get_config
from herness.enrich import cluster_stage
from herness.enrich.cluster_describe import name_clusters
from herness.enrich.cluster_stage import ClusterStageResult, finalize_clusters, run_cluster_stage
from herness.enrich.layout import EnrichPaths
from herness.enrich.questions import load_question_set
from herness.store.vectors import VectorStore

pytestmark = [pytest.mark.integration, pytest.mark.slow]

_PROMPT = Path(cluster_stage.__file__).parent / "prompts" / "cluster_namer.md"
_BUILD_FULL = "bench-full-0001"
_BUILD_NIGHT = "bench-night-0002"


def _device() -> str:
    import torch  # noqa: PLC0415 - only when a clustering row runs

    return "cuda" if torch.cuda.is_available() else "cpu"


@dataclasses.dataclass
class ClusterState:
    """The finished full recluster the rows measure or build on."""

    con: duckdb.DuckDBPyConnection
    data: Path
    prepare_s: float
    full_s: float
    result: ClusterStageResult
    incidents: int
    clusters: int
    device: str


def _incident_ids(con: duckdb.DuckDBPyConnection, count: int) -> list[str]:
    rows = con.execute(
        "SELECT record_id FROM core.incident WHERE record_id IS NOT NULL"
        " ORDER BY sha256(record_id) LIMIT ?",
        [count],
    ).fetchall()
    return [r[0] for r in rows]


@pytest.fixture(scope="module")
def state(lake: LakeBuild, tmp_path_factory: pytest.TempPathFactory) -> Iterator[ClusterState]:
    """Text + planted vectors for every incident, then the timed full recluster."""
    data = tmp_path_factory.mktemp("bt03-cluster")
    con = lake.open()
    try:
        with pinned_clock():
            cfg = activate_enrich(lake, data, "cluster")
            con.execute("DELETE FROM enrich.text_redacted")
            start = time.perf_counter()
            con.execute(fill_text_sql()[0])  # incidents only
            paths = EnrichPaths.from_config(cfg)
            n = write_incident_vectors(con, VectorStore(paths.vectors_dir()))
            prepare_s = time.perf_counter() - start
            device = _device()
            start = time.perf_counter()
            result = run_cluster_stage(
                con, prev_warehouse=None, paths=paths, cfg=cfg.decisions, build_id=_BUILD_FULL,
                force_full=True, device=device, ctx=FakeJobContext(), report=Report(),
            )  # fmt: skip
            names = name_clusters(result.naming, client=None, root_cause_labels=None,
                                  max_calls=0, prompt_path=_PROMPT)  # fmt: skip
            qs = load_question_set(cfg.decisions)
            finalize_clusters(con, result=result, names=names, qs=qs, paths=paths)
            full_s = time.perf_counter() - start
        row = con.execute("SELECT count(*) FROM enrich.cluster").fetchone()
        clusters = int(row[0]) if row else 0
        yield ClusterState(con, data, prepare_s, full_s, result, n, clusters, device)
    finally:
        con.execute("DELETE FROM enrich.cluster_member")
        con.execute("DELETE FROM enrich.cluster")
        con.execute("DELETE FROM enrich.text_redacted")
        con.close()
        shutil.rmtree(data, ignore_errors=True)


@pytest.mark.timeout(TIMEOUT_S)
def test_bt03_07_full_recluster_excluding_naming(
    lake: LakeBuild, state: ClusterState, bench_recorder: BenchRecorder
) -> None:
    """BT03-07 full recluster of the `full` build's incidents (planted vectors), excluding
    the LLM naming calls, takes under 30 min."""
    assert state.result.kind == "full"
    assert state.incidents == lake.incidents
    assert state.clusters > 0
    record_info(bench_recorder, "BT03-07", "clustered_incidents", state.incidents, "rows",
                lake.scale)  # fmt: skip
    record_info(bench_recorder, "BT03-07", "clusters", state.clusters, "clusters", lake.scale)
    assert record(bench_recorder, "BT03-07", "recluster_s", state.full_s, "s",
                  BT07_RECLUSTER_LIMIT_S, lake.scale).passed  # fmt: skip


@pytest.mark.timeout(TIMEOUT_S)
def test_bt03_07_naming_500_calls(
    lake: LakeBuild, state: ClusterState, request: pytest.FixtureRequest,
    bench_recorder: BenchRecorder,
) -> None:  # fmt: skip
    """BT03-07 naming of up to 500 clusters with the real `cluster_namer` LLM takes under
    45 min (V-12: needs the local LLM; skipped otherwise)."""
    blocker = "V-12 (no local LLM service on this host)"
    env = require_service(request.config, "cluster naming (real LLM)", blocker, [LLM_PROFILE_ENV])
    with pinned_clock():
        activate_enrich(lake, state.data, "cluster-naming")
        client = llm_client(request.config, env[LLM_PROFILE_ENV], role="cluster_namer")[0]
        candidates = state.result.naming
        assert candidates, "the full recluster produced no naming candidates"
        qs = load_question_set(get_config().decisions)
        root = next((q for q in qs.questions if q.id == "root_cause"), None)
        labels = list(root.options or {}) if root is not None else None
        start = time.perf_counter()
        names = name_clusters(candidates, client=client, root_cause_labels=labels,
                              max_calls=BT07_NAMING_CALLS, prompt_path=_PROMPT)  # fmt: skip
        elapsed = time.perf_counter() - start
    assert any(n.source != "auto" for n in names.values()), "no cluster was named by the LLM"
    calls = min(len(candidates), BT07_NAMING_CALLS)
    record_info(bench_recorder, "BT03-07", "naming_calls", calls, "calls", lake.scale)
    assert record(bench_recorder, "BT03-07", "naming_s", elapsed, "s", BT07_NAMING_LIMIT_S,
                  lake.scale).passed  # fmt: skip


def _prepare_prev(con: duckdb.DuckDBPyConnection, prev: Path, new_ids: list[str]) -> None:
    """The previous warehouse: the full run's enrich tables without the night's new incidents."""
    con.execute(f"ATTACH '{prev.as_posix()}' AS mk")
    con.execute("CREATE SCHEMA mk.enrich")
    con.execute("CREATE TEMP TABLE new_ids (record_id VARCHAR)")
    con.executemany("INSERT INTO new_ids VALUES (?)", [[i] for i in new_ids])
    not_new = "t.record_id NOT IN (SELECT record_id FROM new_ids)"
    con.execute(
        "CREATE TABLE mk.enrich.text_redacted AS SELECT t.* FROM enrich.text_redacted AS t"  # noqa: S608
        f" WHERE t.entity = 'incident' AND {not_new}"
    )
    con.execute(
        "CREATE TABLE mk.enrich.cluster_member AS SELECT t.* FROM enrich.cluster_member AS t"  # noqa: S608
        f" WHERE {not_new}"
    )
    con.execute("CREATE TABLE mk.enrich.cluster AS SELECT * FROM enrich.cluster")
    con.execute("DETACH mk")
    con.execute("DROP TABLE new_ids")


@pytest.mark.timeout(TIMEOUT_S)
def test_bt03_08_nightly_incremental_clustering_10k(
    lake: LakeBuild, state: ClusterState, tmp_path: Path, bench_recorder: BenchRecorder
) -> None:
    """BT03-08 nightly incremental clustering of 10k new incidents against the previous
    night's snapshot takes under 2 min (and must stay incremental)."""
    con = state.con
    new_ids = _incident_ids(con, NIGHTLY_RECORDS)
    assert len(new_ids) == NIGHTLY_RECORDS
    with pinned_clock():
        cfg = activate_enrich(lake, state.data, "cluster-night")
        paths = EnrichPaths.from_config(cfg)
        prev = tmp_path / "prev.duckdb"
        _prepare_prev(con, prev, new_ids)
        report = Report()
        start = time.perf_counter()
        result = run_cluster_stage(
            con, prev_warehouse=prev, paths=paths, cfg=cfg.decisions, build_id=_BUILD_NIGHT,
            force_full=False, device=state.device, ctx=FakeJobContext(), report=report,
        )  # fmt: skip
        elapsed = time.perf_counter() - start
    assert result.kind == "incremental", "the night fell back to a full recluster"
    assert report.rows > 0
    assert record(bench_recorder, "BT03-08", "incremental_10k_s", elapsed, "s", BT08_LIMIT_S,
                  lake.scale).passed  # fmt: skip
