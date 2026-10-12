"""Change linking and mapping-suggestion benchmarks BT03-09 and BT03-11 (impl 03 §10, T03-36).

Run (idle host only): `HERNESS_BENCH_FULL=1 HERNESS_SYNTH_FULL_ROOT=<seed 42 full root>
pytest -m "integration and slow" tests/bench/enrich/test_enrich_link_bench.py`.

Both rows are pure pipeline work (no model), so they run here on the `full` build:

* BT03-09 times the heuristic link of ALL incidents against ALL changes of the build:
  `link_candidates` (the window/CI/service SQL into `link_cand`) plus `run_link_stage` with no
  pair decider (the decider part is a model step, BT03-10's fakes cover it). The dataset is the
  lake's own: the incident and change counts are recorded as rows; the spec scale is
  5M x 0.5M and the controller reads the recorded counts against it.
* BT03-11 times the real `run_suggest_stage` (scores, ranking, `mapping_suggestion` review
  items into the ops store) on `MappingVectors` of exactly 2,000 subjects x 5,000 services.
  Subjects: the lake's active teams without a mapping, then synthetic Jira-component subjects;
  services: the lake's `core.service` rows (at most 5,000), topped up with synthetic names to
  5,000. The vectors are deterministic (seed 42), planted so that each synthetic subject has
  one near service (similar name, correlated vector) and therefore emits suggestions. No
  encoder runs: the embedding of the 7,000 texts belongs to BT03-02's rate.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Final

import duckdb
import numpy as np
import pytest
from tests.bench.enrich._common import (
    BT09_LIMIT_S,
    BT11_LIMIT_S,
    TIMEOUT_S,
    record,
    record_info,
)
from tests.bench.enrich._lake import LakeBuild, Report, activate_enrich
from tests.support._build_clock import pinned_clock
from tests.support.bench import BenchRecorder

from herness.enrich.cache import DecisionCache
from herness.enrich.calibrate import CalibrationStore
from herness.enrich.layout import EnrichPaths
from herness.enrich.link_changes import link_candidates, run_link_stage
from herness.enrich.mapping_suggest import (
    MAX_SERVICES,
    MAX_SUBJECTS,
    MappingVectors,
    Service,
    Subject,
    run_suggest_stage,
)
from herness.enrich.questions import load_question_set
from herness.store.vectors import EMBEDDING_DIM

pytestmark = [pytest.mark.integration, pytest.mark.slow]

_SEED: Final = 42
_NOISE: Final = 0.05


@pytest.mark.timeout(TIMEOUT_S)
def test_bt03_09_heuristic_change_linking_all_incidents_x_changes(
    lake: LakeBuild, wh: duckdb.DuckDBPyConnection, tmp_path: Path, bench_recorder: BenchRecorder
) -> None:
    """BT03-09 heuristic linking of every incident against every change of the `full`
    build (5M x 0.5M in the spec) takes under 3 min."""
    with pinned_clock():
        cfg = activate_enrich(lake, tmp_path / "data", "bt09")
        dcfg = cfg.decisions
        qs = load_question_set(dcfg)
        paths = EnrichPaths.from_config(cfg)
        wh.execute("DELETE FROM enrich.incident_change_link")
        report = Report()
        start = time.perf_counter()
        link_candidates(wh, cfg=dcfg)
        cache, calibration = DecisionCache(paths, qs.version), CalibrationStore(paths)
        run_link_stage(wh, cfg=dcfg, qs=qs, cache=cache, calibration=calibration,
                       pair_decider=None, report=report)  # fmt: skip
        elapsed = time.perf_counter() - start
    candidates = wh.execute("SELECT count(*) FROM link_cand").fetchone()
    wh.execute("DROP TABLE IF EXISTS link_cand")
    wh.execute("DELETE FROM enrich.incident_change_link")
    assert report.rows > 0
    assert report.decided == 0  # no pair decider: heuristic links only
    record_info(bench_recorder, "BT03-09", "incidents", lake.incidents, "rows", lake.scale)
    record_info(bench_recorder, "BT03-09", "changes", lake.changes, "rows", lake.scale)
    record_info(bench_recorder, "BT03-09", "candidate_pairs", candidates[0] if candidates else 0,
                "pairs", lake.scale)  # fmt: skip
    record_info(bench_recorder, "BT03-09", "links_written", report.rows, "rows", lake.scale)
    assert record(bench_recorder, "BT03-09", "link_s", elapsed, "s", BT09_LIMIT_S,
                  lake.scale).passed  # fmt: skip


def _unit(rows: np.ndarray) -> np.ndarray:
    out: np.ndarray = rows / np.linalg.norm(rows, axis=1, keepdims=True)
    return out.astype(np.float32)


def _mapping_vectors(wh: duckdb.DuckDBPyConnection) -> MappingVectors:
    """2,000 subjects x 5,000 services (see the module docstring for how they are derived)."""
    rng = np.random.default_rng(_SEED)
    svc_rows = wh.execute(
        "SELECT service_id, coalesce(name, service_id) FROM core.service"
        " WHERE service_id IS NOT NULL ORDER BY service_id LIMIT ?", [MAX_SERVICES]
    ).fetchall()  # fmt: skip
    services = [Service(str(s), str(n)) for s, n in svc_rows]
    services.extend(
        Service(f"bench:service:{i:05d}", f"Bench platform service {i} gateway")
        for i in range(len(services), MAX_SERVICES)
    )
    service_vecs = _unit(rng.standard_normal((MAX_SERVICES, EMBEDDING_DIM)))
    team_rows = wh.execute(
        "SELECT team_id, coalesce(name, team_id) FROM core.team AS t WHERE team_id IS NOT NULL"
        " AND coalesce(active, true) AND NOT EXISTS (SELECT 1 FROM core.service_map AS m"
        " WHERE m.team_id = t.team_id) ORDER BY team_id LIMIT ?", [MAX_SUBJECTS]
    ).fetchall()  # fmt: skip
    subjects = [Subject("team", None, None, str(t), str(n)) for t, n in team_rows]
    vecs = [_unit(rng.standard_normal((len(subjects), EMBEDDING_DIM)))] if subjects else []
    planted = rng.integers(0, MAX_SERVICES, size=MAX_SUBJECTS - len(subjects))
    for k, j in enumerate(planted):
        name = f"BP{k:04d} {services[j].name}"
        subjects.append(Subject("jira_component", f"BP{k:04d}", f"comp{k}", None, name,
                                work_items=10 + k % 90))  # fmt: skip
    noise = _NOISE * rng.standard_normal((len(planted), EMBEDDING_DIM))
    vecs.append(_unit(service_vecs[planted] + noise))
    assert len(subjects) == MAX_SUBJECTS
    return MappingVectors(tuple(subjects), tuple(services), np.concatenate(vecs), service_vecs)


@pytest.mark.timeout(TIMEOUT_S)
def test_bt03_11_mapping_suggestions_2k_subjects_x_5k_services(
    lake: LakeBuild, wh: duckdb.DuckDBPyConnection, tmp_path: Path, bench_recorder: BenchRecorder
) -> None:
    """BT03-11 mapping suggestions for 2k subjects x 5k services (scores, ranking and
    review items) take under 5 min."""
    with pinned_clock():
        cfg = activate_enrich(lake, tmp_path / "data", "bt11")
        vectors = _mapping_vectors(wh)
        report = Report()
        start = time.perf_counter()
        run_suggest_stage(wh, vectors=vectors, cfg=cfg.decisions, report=report)
        elapsed = time.perf_counter() - start
    assert (len(vectors.subjects), len(vectors.services)) == (MAX_SUBJECTS, MAX_SERVICES)
    assert report.status == "done"
    assert report.rows > 0  # the planted subjects emit pending suggestions
    record_info(bench_recorder, "BT03-11", "suggestions_created", report.rows, "items", lake.scale)
    assert record(bench_recorder, "BT03-11", "suggest_s", elapsed, "s", BT11_LIMIT_S,
                  lake.scale).passed  # fmt: skip
