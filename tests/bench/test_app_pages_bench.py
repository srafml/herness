"""Dashboard benchmarks BT09-01, BT09-02 and BT09-03 (impl 09 §10; T09-26).

Run: `pytest -m "integration and slow" tests/bench/test_app_pages_bench.py -s`. Dataset: a
real scored synthetic build (`tests.support.metrics_synth`: spec 02 build of the generated
lake, the truth enrichment planted, stage 400 and `run_scoring`, promoted to CURRENT), seed 42
at scale `small` by default. With `HERNESS_BENCH_FULL=1` (the phase gate and the idle-host
bench session) the scale is `full` (≈ 5M incidents), reused from `HERNESS_SYNTH_FULL_ROOT` when
that names a generated root, else generated into the test's temp folder (long; large disk).
BT09-03 uses its own migrated ops store with 50,000 `evidence` rows, 1,000,000 with
`HERNESS_BENCH_FULL=1`. Every record carries its scale; small-scale numbers are indicative only.

BT09-01: per page of `APP_PAGES` (all 12), the page's named queries (captured from a first
render through `app.common.wh.query`, then replayed through the same executor on a fresh
read-only connection for cold and on one warm connection) and a `streamlit.testing` render,
cold (warehouse cache, connection pool and ops cache cleared before each) and warm, 20
repetitions each; p95 cold < 2 s, warm < 0.5 s. BT09-02: `clusters.size_over_time` +
`clusters.members` for the 10 largest clusters, each pair on a fresh connection < 1 s.
BT09-03: `ui_get_evidence_rows` for 10 ids plus the evidence widget render (timed inside the
AppTest script; harness overhead excluded), cold (ops cache cleared), 20 repetitions; p95 <
300 ms. Each target writes stderr lines.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import sqlite3
import statistics
import sys
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final, cast

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest
from tests.support.app_env import (
    APP_PAGES,
    PageSpec,
    install_redaction_double,
    install_session,
    page_services,
    preserved_main,
)
from tests.support.bench import BenchRecorder, BenchTarget, bench_flag, record_value
from tests.support.metrics_synth import (
    SynthEnv,
    generate_root,
    release,
    remove_builds,
    scored_build,
)
from tests.support.ops_store import OpsStoreHandle

from app.common import data, wh
from herness.core.resilience import ProcessState
from herness.store.layout import DataLayout
from herness.store.ops import bind_core_backends
from herness.store.ops.ui_reads import ui_get_evidence_rows

pytestmark = [pytest.mark.integration, pytest.mark.slow]

SEED: Final = 42
REPS: Final = 20
FULL_ENV: Final = "HERNESS_BENCH_FULL"
FULL_ROOT_ENV: Final = "HERNESS_SYNTH_FULL_ROOT"
TIMEOUT_S: Final = 120
COLD_S: Final = 2.0
WARM_S: Final = 0.5
CLUSTER_S: Final = 1.0
EVIDENCE_S: Final = 0.3
EVIDENCE_ROWS: Final = {"small": 50_000, "full": 1_000_000}
SNAPSHOT: Final = {"workers": [], "running": []}


def _scale(config: pytest.Config) -> str:
    return "full" if bench_flag(config, FULL_ENV) == "1" else "small"


def p95(values: list[float]) -> float:
    """The 95th percentile (inclusive method) of at least two values."""
    return statistics.quantiles(values, n=20, method="inclusive")[18]


@dataclasses.dataclass(frozen=True)
class Built:
    """The scored, promoted build of the module and its scale."""

    env: SynthEnv
    build_id: str
    scale: str


@pytest.fixture(scope="module")
def built(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Built]:
    """Generate (or reuse, `full` only) the lake, build, plant, score and promote it."""
    scale = _scale(request.config)
    base = tmp_path_factory.mktemp("bt09")
    cached = os.environ.get(FULL_ROOT_ENV) if scale == "full" else None
    root = Path(cached) if cached else generate_root(SEED, scale, base / "synth")
    env = SynthEnv.create(root, base / "work")
    try:
        build = scored_build(env)
        sys.stderr.write(f"BT09 dataset seed {SEED} scale {scale} build {build.build_id}\n")
        yield Built(env, build.build_id, scale)
    finally:
        remove_builds(env)
        if not cached:
            shutil.rmtree(root, ignore_errors=True)


def _clear_caches() -> None:
    """Cold start: the warehouse and ops caches and the connection pool."""
    st.cache_data.clear()
    wh._pool().close_all()
    wh._pool.clear()


@pytest.fixture
def pages(
    built: Built, reset_process_state: ProcessState, monkeypatch: pytest.MonkeyPatch
) -> Iterator[SimpleNamespace]:
    """The build's config and ops store loaded, a viewer session over its data root."""
    del reset_process_state
    built.env.activate()
    bind_core_backends()
    reads = cast("dict[str, object]", data.OPS_READS)
    monkeypatch.setitem(reads, "status_snapshot", lambda: SNAPSHOT)
    services = page_services(built.env.data_root)
    install_session(monkeypatch, services)
    install_redaction_double(monkeypatch)  # titles pass a double (no redaction key loaded)
    _clear_caches()
    with preserved_main():
        yield services
    _clear_caches()
    release()


def _render(spec: PageSpec) -> float:
    started = time.perf_counter()
    at = AppTest.from_file(str(spec.path), default_timeout=TIMEOUT_S).run()
    seconds = time.perf_counter() - started
    assert not at.exception, [e.value for e in at.exception]
    assert not at.error, [e.value for e in at.error]
    return seconds


Frozen = tuple[tuple[str, object], ...]


def _capture(
    monkeypatch: pytest.MonkeyPatch, queries: Mapping[str, Any]
) -> list[tuple[str, Frozen]]:
    """Record every named query (bound parameters) the pages run."""
    seen: list[tuple[str, Frozen]] = []
    real = wh.query

    def spy(name: str, /, **params: object) -> wh.QueryResult:
        seen.append((name, wh._bind(queries[name], params)))
        return real(name, **params)

    monkeypatch.setattr(wh, "query", spy)
    return seen


def _replay(built: Built, calls: list[tuple[str, Frozen]], queries: Mapping[str, Any],
            *, cold: bool) -> list[float]:  # fmt: skip
    """REPS timings of all ``calls`` through the dashboard executor (no app cache)."""
    layout = DataLayout.from_root(built.env.data_root)
    times: list[float] = []
    warm = None if cold else wh._open(built.build_id, layout)
    try:
        for rep in range(REPS + (0 if cold else 1)):  # warm: one untimed warm-up pass
            started = time.perf_counter()
            conn = wh._open(built.build_id, layout) if warm is None else warm
            try:
                for name, frozen in calls:
                    wh._execute(built.build_id, name, frozen, conn, queries)
            finally:
                if warm is None:
                    conn.close()
            if cold or rep:
                times.append(time.perf_counter() - started)
    finally:
        if warm is not None:
            warm.close()
    return times


def _gate(recorder: BenchRecorder, target: BenchTarget, values: list[float]) -> bool:
    value = p95(values)
    sys.stderr.write(
        f"{target.target_id} {target.metric} p95 {value:.4f} s median "
        f"{statistics.median(values):.4f} s max {max(values):.4f} s (target < "
        f"{target.threshold:g}, scale {target.scale}, n={len(values)})\n"
    )
    return record_value(recorder, target, value).passed


PAGE_PARAMS: Final = [pytest.param(spec, id=spec.path.stem) for spec in APP_PAGES]
WARM_GAP_PAGE: Final = "06_Change_Health"  # warm render gated in its own non-strict xfail (T09-30)


@pytest.mark.parametrize("spec", PAGE_PARAMS)
def test_bt09_01_dashboard_page_first_load(
    pages: SimpleNamespace, built: Built, spec: PageSpec, bench_recorder: BenchRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    """BT09-01 a page's named queries and its ``streamlit.testing`` render, cold (caches
    cleared) and warm, 20 repetitions: p95 cold < 2 s, warm < 0.5 s."""
    queries = cast("Mapping[str, Any]", pages.queries)
    calls = _capture(monkeypatch, queries)
    _render(spec)  # captures the page's named queries
    replay = list(dict.fromkeys(calls))
    page = spec.path.stem.lower()

    def target(metric: str, threshold: float) -> BenchTarget:
        return BenchTarget("BT09-01", "09", f"{page}_{metric}_p95", "s", threshold, built.scale)

    cold: list[float] = []
    for _ in range(REPS):
        _clear_caches()
        cold.append(_render(spec))
    passed = [_gate(bench_recorder, target("render_cold", COLD_S), cold)]
    if spec.path.stem != WARM_GAP_PAGE:
        _render(spec)  # warm-up
        warm = [_render(spec) for _ in range(REPS)]
        passed.append(_gate(bench_recorder, target("render_warm", WARM_S), warm))
    if replay:
        passed += [
            _gate(bench_recorder, target("queries_cold", COLD_S),
                  _replay(built, replay, queries, cold=True)),
            _gate(bench_recorder, target("queries_warm", WARM_S),
                  _replay(built, replay, queries, cold=False)),
        ]  # fmt: skip
    assert spec.needs_warehouse is False or replay, "a warehouse page ran no named query"
    assert all(passed), f"BT09-01 {page}: a p95 is over its threshold (see stderr)"


# Non-strict by controller ruling: the page 06 warm p95 depends on host load (0.479 s idle,
# 0.74-0.78 s under load), so a strict xfail would fail whenever it passes. Owner: T09-30.
@pytest.mark.xfail(strict=False, reason="product gap T09-30 (page 06 warm render chart cost)")
def test_bt09_01_page_06_warm_render(
    pages: SimpleNamespace, built: Built, bench_recorder: BenchRecorder
) -> None:
    """BT09-01 page 06 warm render, 20 repetitions: p95 < 0.5 s (cold is gated above).

    Measured 0.479 s idle and 0.74-0.78 s under host load; xfail (not strict) until T09-30.
    """
    spec = next(s for s in APP_PAGES if s.path.stem == WARM_GAP_PAGE)
    _render(spec)  # warm-up
    warm = [_render(spec) for _ in range(REPS)]
    target = BenchTarget(
        "BT09-01", "09", f"{spec.path.stem.lower()}_render_warm_p95", "s", WARM_S, built.scale
    )
    assert _gate(bench_recorder, target, warm)


def test_bt09_02_cluster_detail_ten_largest(
    pages: SimpleNamespace, built: Built, bench_recorder: BenchRecorder
) -> None:
    """BT09-02 ``clusters.size_over_time`` + ``clusters.members`` for each of the 10 largest
    clusters on a fresh read-only connection: each pair < 1 s."""
    queries = cast("Mapping[str, Any]", pages.queries)
    layout = DataLayout.from_root(built.env.data_root)
    conn = wh._open(built.build_id, layout)
    try:
        rows = conn.execute(
            "SELECT cluster_id FROM enrich.cluster ORDER BY size DESC, cluster_id LIMIT 10"
        ).fetchall()
    finally:
        conn.close()
    assert rows, "the build has no cluster"
    times: list[float] = []
    for (cluster_id,) in rows:
        started = time.perf_counter()
        conn = wh._open(built.build_id, layout)
        try:
            for name in ("clusters.size_over_time", "clusters.members"):
                frozen = wh._bind(queries[name], {"cluster_id": cluster_id})
                table, _ = wh._execute(built.build_id, name, frozen, conn, queries)
                assert table.num_rows > 0, (name, cluster_id)
        finally:
            conn.close()
        times.append(time.perf_counter() - started)
    worst = max(times)
    each = ", ".join(f"{t:.4f}" for t in times)
    sys.stderr.write(f"BT09-02 cluster_detail_s max {worst:.4f} s (target < 1, scale "
                     f"{built.scale}, clusters={len(times)}) each=[{each}]\n")  # fmt: skip
    target = BenchTarget("BT09-02", "09", "cluster_detail_max_s", "s", CLUSTER_S, built.scale)
    assert record_value(bench_recorder, target, worst).passed


# --- BT09-03 ------------------------------------------------------------------------------------

EXECUTED: Final = "2026-09-26T09:00:00.000000Z"
SAMPLE: Final = json.dumps([{"day": f"2026-01-{d:02d}", "n": d} for d in range(1, 21)])


def _evidence_ids(count: int) -> list[str]:
    """Ten ids spread over the table (query ids are ``q_`` plus 16 hex digits)."""
    step = count // 10
    return [f"q_{k * step + 7:016x}" for k in range(10)]


def _fill_evidence(db: Path, count: int) -> None:
    con = sqlite3.connect(db)
    try:
        with con:
            for start in range(0, count, 50_000):
                con.executemany(
                    "INSERT INTO evidence (query_id, run_id, build_id, sql, result_hash, params,"
                    " result_sample, row_count, duration_ms, executed_at)"
                    " VALUES (?, NULL, 'b1', 'SELECT n FROM metrics.x WHERE d = $d', ?, ?, ?,"
                    " 20, 12, ?)",
                    [(f"q_{i:016x}", "0" * 64, json.dumps({"d": i}), SAMPLE, EXECUTED)
                     for i in range(start, min(count, start + 50_000))],
                )  # fmt: skip
    finally:
        con.close()


RENDER_KEY: Final = "bt09_03_widget_render_s"  # spelled out in evidence_script (run as source)


def evidence_script(ids: list[str]) -> None:
    """The AppTest script: the real evidence widget, its render timed inside the script."""
    import time  # noqa: PLC0415 - runs inside the AppTest script

    import streamlit as st  # noqa: PLC0415

    from app.common.widgets import evidence  # noqa: PLC0415

    started = time.perf_counter()
    evidence(ids)
    st.session_state["bt09_03_widget_render_s"] = time.perf_counter() - started


@pytest.fixture
def evidence_store(
    request: pytest.FixtureRequest, ops_store: OpsStoreHandle, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[list[str], str, int]]:
    """A migrated ops store with N ``evidence`` rows, a viewer session over it."""
    scale = _scale(request.config)
    count = EVIDENCE_ROWS[scale]
    started = time.perf_counter()
    _fill_evidence(ops_store.db_path, count)
    sys.stderr.write(f"BT09-03 inserted {count} evidence rows in "
                     f"{time.perf_counter() - started:.1f} s\n")  # fmt: skip
    bind_core_backends()
    install_session(monkeypatch, page_services(ops_store.data_root))
    _clear_caches()
    with preserved_main():
        yield _evidence_ids(count), scale, count
    _clear_caches()


def test_bt09_03_evidence_expander_open(
    evidence_store: tuple[list[str], str, int], bench_recorder: BenchRecorder
) -> None:
    """BT09-03 ``ui_get_evidence_rows`` for 10 ids plus the evidence widget render, ops
    cache cleared before each of 20 repetitions: p95 < 300 ms.

    Timed span (the spec row's): the direct ``ui_get_evidence_rows(ids)`` call plus the
    ``evidence(ids)`` widget call, the latter timed with ``perf_counter`` inside the AppTest
    script and read back from session state. The widget's own cached ``ops_read`` of the same
    ids also runs cold (cache cleared), so the query is counted twice (conservative). The
    ``streamlit.testing`` harness (AppTest construction, script compile, script thread; a
    ~0.3 s floor on this host) is excluded; the AppTest run still backs the assertions.
    """
    ids, scale, _count = evidence_store
    times: list[float] = []
    for _ in range(REPS + 1):  # the first (untimed) run loads the page modules
        st.cache_data.clear()
        started = time.perf_counter()
        rows = ui_get_evidence_rows(ids)
        query_s = time.perf_counter() - started
        at = AppTest.from_function(evidence_script, args=(ids,), default_timeout=TIMEOUT_S).run()
        assert not at.exception, [e.value for e in at.exception]
        assert sorted(rows) == sorted(ids)
        assert len(at.dataframe) == len(ids)
        render_s = at.session_state[RENDER_KEY]
        assert isinstance(render_s, float), render_s
        times.append(query_s + render_s)
    target = BenchTarget("BT09-03", "09", "evidence_open_p95", "s", EVIDENCE_S, scale)
    assert _gate(bench_recorder, target, times[1:])
