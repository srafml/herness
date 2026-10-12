"""Warehouse scale benchmarks BT02-01…BT02-04 (impl 02 §10.1, T02-23).

Run (idle host only): `HERNESS_BENCH_FULL=1 HERNESS_SYNTH_FULL_ROOT=<seed 42 full root>
pytest -m "integration and slow" tests/bench/test_store_warehouse_bench.py`. Without
`HERNESS_BENCH_FULL=1` the module skips (w36 ruling: full-scale benches are
[integration, slow] and opt-in). Dataset `synth_data.py --seed 42 --scale full`, reused from
the root named by HERNESS_SYNTH_FULL_ROOT (only the warehouse folder the build wrote is
removed afterwards) or generated into the test's temp folder.

ONE module fixture runs the "full pipeline" once for all four rows: the real spec 02
`build_pipeline` stage `build` (000-299), the truth enrichment, stage 400 and the default
scoring (`tests.support.metrics_synth.scored_build`), then `run_sql_range(900, 999)`
(the `dq` stage's SQL) on that build file, then CHECKPOINT. Each row is recorded through the
T11-18 recorder (`data/bench/`, gitignored) and writes one stderr line like the BT04 benches.

* BT02-01 reads `durations_ms["build"]` of the `build_pipeline` job result.
* BT02-02 times `herness.model.build.run_sql_range(con, files, 900, 999, ...)` with the
  same render context the stage builds.
* BT02-03 is the size in decimal GB (1e9 bytes) of the build file (plus any `.wal`)
  after the pipeline above.
* BT02-04 runs the 200 queries of `QUERIES` once each (after a short warm-up) on
  `warehouse.open_readonly` of the build; p95 over the 200 wall times.

Pinned as-of (T11-39): `SynthEnv` writes `metrics.yaml scoring.as_of` = the day after the
generated span (2026-09-01) and the build runs inside `_build_clock.pinned_clock()`
(`herness.core.time.now` frozen at 2026-09-01), so no row depends on the calendar day.

`QUERIES` is the stand-in for `tests/fixtures/sql_ok/` (impl 11 never built that corpus):
200 typical agent read queries on the real built schema (`core.*`, `metrics.*`, `score.*`).
Thresholds are the verbatim §10.1 values; a miss is never fixed by editing them.
"""

# ruff: noqa: S608 - the f-string SQL interpolates only module-level literals, never input

from __future__ import annotations

import dataclasses
import datetime
import os
import shutil
import statistics
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest
from tests.support._build_clock import pinned_clock
from tests.support.bench import BenchRecorder, BenchTarget, bench_flag, record_value
from tests.support.metrics_synth import SynthEnv, generate_root, remove_builds, scored_build

from herness.model.build import run_sql_range
from herness.model.lakeinfo import LakeInventory, scan_lake
from herness.model.refdata import register_prev_row_counts
from herness.model.render_context import build_render_context
from herness.model.sqlfiles import discover_sql_files
from herness.store import warehouse

pytestmark = [pytest.mark.integration, pytest.mark.slow]

SEED: Final = 42
FULL_ENV: Final = "HERNESS_BENCH_FULL"
FULL_ROOT_ENV: Final = "HERNESS_SYNTH_FULL_ROOT"
TIMEOUT_S: Final = 7200
OWNER: Final = "02"
BUILD_LIMIT_S: Final = 600.0  # BT02-01: build 000-299 < 600 s
DQ_LIMIT_S: Final = 60.0  # BT02-02: run_sql_range(900, 999) < 60 s
SIZE_LIMIT_GB: Final = 15.0  # BT02-03: warehouse file < 15 GB
QUERY_P95_LIMIT_S: Final = 2.0  # BT02-04: p95 of 200 agent queries < 2 s
QUERY_COUNT: Final = 200
BYTES_PER_GB: Final = 10**9  # decimal GB, the unit of the BT02-03 threshold
WARMUP: Final = 3
FULL_MIN_INCIDENTS: Final = 4_000_000


@dataclasses.dataclass(frozen=True)
class Pipeline:
    """The measurements of the one full pipeline run."""

    scale: str
    build_id: str
    env: SynthEnv
    build_s: float
    dq_s: float
    size_gb: float


def _run_dq(env: SynthEnv, build_id: str) -> float:
    """Wall seconds of `run_sql_range(900, 999)` on the finished build file."""
    cfg = env.activate()
    con = env.open_build(build_id)
    try:
        # the stage's own preparation (`build._prepare`): probe, scan with the extra entities
        probe = build_render_context(cfg, LakeInventory(root=env.layout.raw, entities={}), build_id)
        pairs = [(src, name) for src, names in probe.extra_entities.items() for name in names]
        context = dataclasses.replace(probe, lake=scan_lake(env.layout, extra_entities=pairs))
        files = discover_sql_files()
        register_prev_row_counts(con, None)  # as `stage_dq` does when CURRENT is this build
        start = time.perf_counter()
        run_sql_range(
            con,
            files,
            900,
            999,
            context=context,
            should_yield=lambda: False,
            heartbeat=lambda _message: None,
        )
        elapsed = time.perf_counter() - start
        con.execute("CHECKPOINT")
    finally:
        con.close()
    return elapsed


def _scale(env: SynthEnv, build_id: str) -> str:
    """`full` when the build holds the spec's 5M incidents (at least 4M), else `small`: the
    record's scale label follows the data, not the environment."""
    con = warehouse.open_readonly(build_id, layout=env.layout)
    try:
        row = con.execute("SELECT count(*) FROM core.incident").fetchone()
    finally:
        con.close()
    return "full" if row is not None and row[0] >= FULL_MIN_INCIDENTS else "small"


def _size_gb(env: SynthEnv, build_id: str) -> float:
    path = warehouse.build_path(build_id, layout=env.layout)
    total = path.stat().st_size
    wal = path.with_name(path.name + ".wal")
    if wal.exists():
        total += wal.stat().st_size
    return total / BYTES_PER_GB


@pytest.fixture(scope="module")
def pipeline(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Pipeline]:
    """Generate (or reuse) `full`, run build + facts + scoring + dq SQL once, measure."""
    if bench_flag(request.config, FULL_ENV) != "1":
        pytest.skip(f"full-scale warehouse bench: set {FULL_ENV}=1 on an idle host")
    base = tmp_path_factory.mktemp("bt02")
    cached = os.environ.get(FULL_ROOT_ENV)
    root = Path(cached) if cached else generate_root(SEED, "full", base / "synth", workers=8)
    env = SynthEnv.create(root, base / "work")
    try:
        with pinned_clock():
            built = scored_build(env)
            dq_s = _run_dq(env, built.build_id)
        yield Pipeline(
            _scale(env, built.build_id),
            built.build_id,
            env,
            built.build_durations_ms["build"] / 1000.0,
            dq_s,
            _size_gb(env, built.build_id),
        )
    finally:
        remove_builds(env)
        if not cached:
            shutil.rmtree(root, ignore_errors=True)


def _target(bench_id: str, metric: str, unit: str, threshold: float, scale: str) -> BenchTarget:
    return BenchTarget(bench_id, OWNER, metric, unit, threshold, scale)


def _line(bench_id: str, stat: str, value: float, unit: str, limit: float) -> None:
    sys.stderr.write(f"{bench_id} {stat} {value:.4f} {unit} (target < {limit:g} {unit})\n")


@pytest.mark.timeout(TIMEOUT_S)
def test_bt02_01_build_stage_duration(pipeline: Pipeline, bench_recorder: BenchRecorder) -> None:
    """BT02-01 the `build` stage (000-299) on `full` takes under 600 s."""
    target = _target("BT02-01", "build_stage_s", "s", BUILD_LIMIT_S, pipeline.scale)
    _line("BT02-01", target.metric, pipeline.build_s, "s", BUILD_LIMIT_S)
    assert record_value(bench_recorder, target, pipeline.build_s).passed


@pytest.mark.timeout(TIMEOUT_S)
def test_bt02_02_dq_checks_duration(pipeline: Pipeline, bench_recorder: BenchRecorder) -> None:
    """BT02-02 `run_sql_range(900, 999)` on `full` takes under 60 s."""
    target = _target("BT02-02", "dq_range_s", "s", DQ_LIMIT_S, pipeline.scale)
    _line("BT02-02", target.metric, pipeline.dq_s, "s", DQ_LIMIT_S)
    assert record_value(bench_recorder, target, pipeline.dq_s).passed


@pytest.mark.timeout(TIMEOUT_S)
def test_bt02_03_warehouse_size(pipeline: Pipeline, bench_recorder: BenchRecorder) -> None:
    """BT02-03 the warehouse file after the full pipeline is under 15 GB."""
    target = _target("BT02-03", "warehouse_gb", "GB", SIZE_LIMIT_GB, pipeline.scale)
    _line("BT02-03", target.metric, pipeline.size_gb, "GB", SIZE_LIMIT_GB)
    assert record_value(bench_recorder, target, pipeline.size_gb).passed


def _windows() -> list[tuple[str, str]]:
    """Twelve quarterly [lo, hi) date windows over 2024-2026."""
    out = []
    for year in (2024, 2025, 2026):
        for quarter in range(4):
            lo = datetime.date(year, 3 * quarter + 1, 1)
            hi = datetime.date(year + (quarter == 3), (3 * quarter + 3) % 12 + 1, 1)
            out.append((lo.isoformat(), hi.isoformat()))
    return out


def _per_window(lo: str, hi: str) -> list[str]:
    return [
        f"SELECT priority, count(*) AS n FROM core.incident WHERE opened_at >= DATE '{lo}'"
        f" AND opened_at < DATE '{hi}' GROUP BY priority ORDER BY priority",
        f"SELECT s.name, count(*) AS n FROM core.incident i JOIN core.service s"
        f" ON s.service_id = i.service_id WHERE i.opened_at >= DATE '{lo}'"
        f" AND i.opened_at < DATE '{hi}' GROUP BY s.name ORDER BY n DESC LIMIT 10",
        f"SELECT t.name, avg(f.resolve_h) AS mttr_h FROM metrics.incident_fact f"
        f" JOIN core.team t ON t.team_id = f.team_id WHERE f.opened_at >= DATE '{lo}'"
        f" AND f.opened_at < DATE '{hi}' AND NOT f.excluded GROUP BY t.name"
        f" ORDER BY mttr_h DESC LIMIT 20",
        f"SELECT date_trunc('week', opened_at) AS wk, count(*) AS n, sum(sla_breached::INT)"
        f" AS breached FROM core.incident WHERE opened_at >= DATE '{lo}'"
        f" AND opened_at < DATE '{hi}' GROUP BY wk ORDER BY wk",
        f"SELECT type, outcome, count(*) AS n FROM core.change WHERE opened_at >= DATE"
        f" '{lo}' AND opened_at < DATE '{hi}' GROUP BY type, outcome ORDER BY n DESC",
        f"SELECT severity, count(*) AS n, avg(duration_s) AS avg_s FROM core.event"
        f" WHERE ts >= DATE '{lo}' AND ts < DATE '{hi}' GROUP BY severity ORDER BY n DESC",
        f"SELECT record_id, number, priority, resolved_at - opened_at AS open_for FROM"
        f" core.incident WHERE opened_at >= DATE '{lo}' AND opened_at < DATE '{hi}'"
        f" AND resolved_at IS NOT NULL ORDER BY open_for DESC LIMIT 25",
        f"SELECT service_id, metric_name, avg(value) AS v FROM core.metric_daily"
        f" WHERE date >= DATE '{lo}' AND date < DATE '{hi}' GROUP BY service_id,"
        f" metric_name ORDER BY v DESC LIMIT 20",
    ]


def _per_priority(priority: int) -> list[str]:
    return [
        f"SELECT state, count(*) AS n FROM core.incident WHERE priority = {priority}"
        f" GROUP BY state ORDER BY n DESC",
        f"SELECT team_id, count(*) AS n, avg(reassignment_count) AS reassign FROM"
        f" core.incident WHERE priority = {priority} GROUP BY team_id ORDER BY n DESC"
        f" LIMIT 15",
        f"SELECT service_id, count(*) AS n, rank() OVER (ORDER BY count(*) DESC) AS r FROM"
        f" core.incident WHERE priority = {priority} GROUP BY service_id QUALIFY r <= 10",
        f"SELECT month, n, sum(n) OVER (ORDER BY month) AS running FROM (SELECT"
        f" date_trunc('month', opened_at) AS month, count(*) AS n FROM core.incident"
        f" WHERE priority = {priority} GROUP BY month) ORDER BY month",
        f"SELECT close_code, count(*) AS n FROM core.incident WHERE priority = {priority}"
        f" AND close_code IS NOT NULL GROUP BY close_code ORDER BY n DESC",
        f"SELECT record_id, short_description FROM core.incident WHERE priority ="
        f" {priority} AND reopen_count > 0 ORDER BY opened_at DESC LIMIT 20",
    ]


def _per_state(state: str) -> list[str]:
    return [
        f"SELECT count(*) FROM core.incident WHERE state = '{state}'",
        f"SELECT service_id, count(*) AS n FROM core.incident WHERE state = '{state}'"
        f" GROUP BY service_id ORDER BY n DESC LIMIT 10",
        f"SELECT record_id, opened_at FROM core.incident WHERE state = '{state}'"
        f" ORDER BY opened_at DESC LIMIT 50",
    ]


def _per_limit(limit: int) -> list[str]:
    return [
        f"SELECT s.name, s.criticality, count(*) AS n FROM core.incident i JOIN"
        f" core.service s USING (service_id) GROUP BY s.name, s.criticality ORDER BY n"
        f" DESC LIMIT {limit}",
        f"SELECT team_id, sum(total_usd) AS usd FROM metrics.incident_fact GROUP BY"
        f" team_id ORDER BY usd DESC NULLS LAST LIMIT {limit}",
        f"SELECT candidate_id, title, annual_pain_usd, rank FROM score.funding ORDER BY"
        f" rank LIMIT {limit}",
        f"SELECT entity_id, metric, value, z_score, rank FROM score.org WHERE"
        f" entity_type = 'team' ORDER BY rank NULLS LAST, entity_id LIMIT {limit}",
        f"SELECT entity_id, metric, delta_usd FROM score.action_lever ORDER BY delta_usd"
        f" DESC LIMIT {limit}",
        f"SELECT key, summary, story_points FROM core.work_item WHERE status_category ="
        f" 'done' ORDER BY resolved_at DESC LIMIT {limit}",
        f"SELECT record_id, linked_incident_count FROM metrics.change_fact WHERE failed"
        f" ORDER BY linked_incident_count DESC LIMIT {limit}",
        f"SELECT alert_name, count(*) AS n FROM core.event GROUP BY alert_name ORDER BY n"
        f" DESC LIMIT {limit}",
        f"SELECT incident_id, count(*) AS alerts FROM core.event WHERE incident_id IS NOT"
        f" NULL GROUP BY incident_id ORDER BY alerts DESC LIMIT {limit}",
    ]


def _per_metric(metric: str, entity: str) -> list[str]:
    return [
        f"SELECT period_start, value FROM metrics.metric_value WHERE metric ="
        f" '{metric}' AND entity_type = '{entity}' AND period = 'month' ORDER BY"
        f" period_start, entity_id LIMIT 200",
        f"SELECT entity_id, avg(value) AS v, sum(sample_size) AS n FROM"
        f" metrics.metric_value WHERE metric = '{metric}' AND entity_type ="
        f" '{entity}' GROUP BY entity_id ORDER BY v DESC NULLS LAST LIMIT 20",
    ]


_FIXED: Final = [
    "SELECT count(*) FROM core.incident",
    "SELECT count(*) FROM core.change",
    "SELECT count(*) FROM core.event",
    "SELECT count(*) FROM core.work_item",
    "SELECT t.name, o.name AS org FROM core.team t JOIN core.org o USING (org_id)"
    " WHERE t.active ORDER BY o.name, t.name",
    "SELECT cause.number AS change_no, count(*) AS incidents FROM core.incident i JOIN"
    " core.change cause ON cause.record_id = i.caused_by_change_id GROUP BY change_no"
    " ORDER BY incidents DESC LIMIT 20",
    "SELECT w.type, count(*) AS n, median(w.story_points) AS med FROM core.work_item w"
    " GROUP BY w.type ORDER BY n DESC",
    "SELECT scenario, sum(expected_impact_usd) FILTER (WHERE selected) AS impact FROM"
    " score.portfolio GROUP BY scenario ORDER BY scenario",
]


def _queries() -> tuple[str, ...]:
    """200 distinct typical agent read queries on the built schema (stand-in for
    `tests/fixtures/sql_ok/`): 96 windowed + 24 per priority + 12 per state + 36 top-N +
    24 metric reads + 8 fixed."""
    out: list[str] = []
    for lo, hi in _windows():
        out += _per_window(lo, hi)
    for priority in (1, 2, 3, 4):
        out += _per_priority(priority)
    for state in ("closed", "resolved", "in_progress", "new"):
        out += _per_state(state)
    for limit in (5, 10, 20, 50):
        out += _per_limit(limit)
    for metric in ("mttr_hours", "change_failure_rate", "incident_count", "alert_noise_ratio"):
        for entity in ("team", "service", "org"):
            out += _per_metric(metric, entity)
    return (*out, *_FIXED)


QUERIES: Final = _queries()


@pytest.mark.timeout(TIMEOUT_S)
def test_bt02_04_agent_query_p95(pipeline: Pipeline, bench_recorder: BenchRecorder) -> None:
    """BT02-04 200 typical agent queries on `open_readonly` of the full build: p95 < 2 s."""
    assert len(QUERIES) == QUERY_COUNT == len(set(QUERIES))
    con = warehouse.open_readonly(pipeline.build_id, layout=pipeline.env.layout)
    try:
        for sql in QUERIES[:WARMUP]:
            con.execute(sql).fetchall()  # the reader's first open and cold caches
        times: list[float] = []
        for sql in QUERIES:
            start = time.perf_counter()
            con.execute(sql).fetchall()
            times.append(time.perf_counter() - start)
    finally:
        con.close()
    p95 = statistics.quantiles(times, n=20, method="inclusive")[-1]
    target = _target("BT02-04", "agent_query_p95_s", "s", QUERY_P95_LIMIT_S, pipeline.scale)
    _line("BT02-04", target.metric, p95, "s", QUERY_P95_LIMIT_S)
    assert record_value(bench_recorder, target, p95).passed
