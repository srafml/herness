"""BT11-05: the planted truths hold after the full pipeline (impl 11 §10; design 11 §8; T11-36).

`measure_truths` is the one set of checks, read from a scored build connection by the
`full` test (seed 42, `HERNESS_BENCH_FULL=1` only: the lake is too large for a shared host)
and by the `small` tests (seed 42 `small`, the build fixture of T11-34, run in every slow
session). Thresholds are the ones of the BT11-05 row, unchanged at both scales.

Fixture builds run enrichment for real (stub decider, stub LLM, `HashEncoder`) but keep the
T02-22 fake scoring hook (T11-34 carry-over), so `score.*` is empty in them. Both variants
therefore score a copy of the build file with the real `run_scoring` (the copy is set back to
`building`, which scoring requires), with `scoring.funding.window_days` 1095 and `as_of` the
day after the span, as IT04-08 does (spec note SN-1 / CO-T04-22a: the generator's E2 links lie
outside the shipped 365-day window). `alert_noise_ratio` is the product's `compute_metric`
at service grain, pooled over the periods of the span.

Run (small, ~4 min on a loaded host): pytest tests/bench/test_truth_full.py -m slow -k small
Full (idle host): HERNESS_BENCH_FULL=1 pytest tests/bench/test_truth_full.py -k full
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
import shutil
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Final, cast

import duckdb
import pyarrow as pa
import pytest
from tests.support._build_cache import Scale
from tests.support.bench import (
    BenchRecord,
    BenchRecorder,
    BenchTarget,
    Better,
    bench_flag,
    record_value,
)
from tests.support.builds import REPO, BuildHandle, ensure_build
from tests.support.metrics_synth import SynthEnv, release
from tests.support.metrics_truth import Truth, read_truth

from herness.metrics.compute import compute_metric
from herness.metrics.scoring import run_scoring
from herness.model._build_support import db_settings
from herness.model.settings import BuildSettings

pytestmark = [pytest.mark.integration, pytest.mark.slow]

FULL_ENV: Final = "HERNESS_BENCH_FULL"
# `ensure_build` is typed tiny|small (T11-34) but passes the scale on to `generate` unchanged.
FULL_SCALE: Final = cast("Scale", "full")
FUNDING_WINDOW_DAYS: Final = 1095  # SN-1: the E2 links are outside the shipped 365 days
# Harness value (w50-s11 controller ruling): the production 75 % (94 GB on the 125.7 GB bench
# host) leaves the host under 10 % free during the full-scale `funding` step; 60 % keeps it
# usable. The row measures ranks and ratios, so the limit cannot change a verdict.
HARNESS_MEMORY_LIMIT: Final = "60%"
LINK_SCORE: Final = 0.5
MIN_PURITY: Final = 0.5  # small-scale guard only: the spec row checks the rank
_TOP: Final = 5.5  # "outside the top 5" is rank >= 6; "in the top 10" is rank < 10.5
_FIRST: Final = 1.5  # "rank 1" is rank < 1.5


@dataclasses.dataclass(frozen=True, slots=True)
class Check:
    """One BT11-05 measurement with its pass threshold (`better` as in `BenchTarget`)."""

    metric: str
    value: float
    unit: str
    threshold: float
    better: Better

    def target(self, target_id: str, scale: str) -> BenchTarget:
        return BenchTarget(
            target_id, "11", self.metric, self.unit, self.threshold, scale, self.better
        )


def _beyond(ranks: Mapping[str, int], key: str) -> float:
    """The rank of `key`, or one past the ranked entries when it is not ranked."""
    return float(ranks.get(key, len(ranks) + 1))


def _noise_by_service(con: duckdb.DuckDBPyConnection, truth: Truth) -> dict[str, float]:
    """service -> pooled `alert_noise_ratio` over the periods of the span (None: no sample)."""
    result = compute_metric(
        "alert_noise_ratio", "service", None, "quarter", window=(truth.start, truth.end), con=con
    )
    num: dict[str, float] = {}
    den: dict[str, float] = {}
    for row in result.rows:
        num[row.entity_id] = num.get(row.entity_id, 0.0) + (row.numerator or 0.0)
        den[row.entity_id] = den.get(row.entity_id, 0.0) + (row.denominator or 0.0)
    return {sid: num[sid] / den[sid] for sid in den if den[sid] > 0}


def _t2c_cluster(con: duckdb.DuckDBPyConnection, members: list[str]) -> tuple[str | None, float]:
    """(cluster holding most T2c members, its share of that cluster's members)."""
    con.register("t2c_members", pa.table({"record_id": members}))
    try:
        row = con.execute(
            "SELECT cluster_id, count(*) AS hit FROM enrich.cluster_member"
            " WHERE record_id IN (SELECT record_id FROM t2c_members)"
            " GROUP BY cluster_id ORDER BY hit DESC, cluster_id LIMIT 1"
        ).fetchone()
        size = (
            None
            if row is None
            else con.execute(
                "SELECT count(*) FROM enrich.cluster_member WHERE cluster_id = ?", [row[0]]
            ).fetchone()
        )
    finally:
        con.unregister("t2c_members")
    if row is None or size is None:
        return None, 0.0
    return str(row[0]), int(row[1]) / int(size[0])


def _links(con: duckdb.DuckDBPyConnection) -> set[tuple[str, str]]:
    rows = con.execute(
        "SELECT incident_id, change_id FROM enrich.incident_change_link WHERE score >= ?",
        [LINK_SCORE],
    ).fetchall()
    return {(str(i), str(c)) for i, c in rows}


def measure_truths(handle: BuildHandle, con: duckdb.DuckDBPyConnection) -> list[Check]:
    """The BT11-05 measurements of a scored build, read through `con` (truth read here only)."""
    truth, plants = read_truth(handle.root), handle.truth.plants
    team = plants.T1_bad_team.team_id
    org = {
        str(r[0]): int(r[1])
        for r in con.execute(
            "SELECT entity_id, min(rank) FROM score.org WHERE entity_type = 'team'"
            " GROUP BY entity_id"
        ).fetchall()
    }
    funding = {
        str(r[0]): int(r[1])
        for r in con.execute("SELECT candidate_id, rank FROM score.funding").fetchall()
    }
    decoy = con.execute(
        "SELECT record_id FROM core.work_item WHERE key = ?", [plants.T2_roi_epic.decoy_epic_key]
    ).fetchall()
    cluster, purity = _t2c_cluster(con, truth.members("T2c"))
    fix = _beyond(funding, f"cluster_fix:{cluster}")
    noise = _noise_by_service(con, truth)
    t4 = plants.T4_noisy_service.service_id
    t4_value = noise.get(t4, 0.0)
    t4_rank = 1 + sum(1 for value in noise.values() if value > t4_value)
    found, planted = _links(con), set(truth.change_pairs())
    hits = len(found & planted)
    return [
        Check("t1_org_rank", _beyond(org, team), "rank", _FIRST, "lower"),
        Check(
            "t1_teams_at_rank_1", float(sum(1 for r in org.values() if r == 1)), "n", 1.5, "lower"
        ),
        Check(
            "t2_funding_rank",
            _beyond(funding, plants.T2_roi_epic.epic_record_id),
            "rank",
            _FIRST,
            "lower",
        ),
        Check(
            "e2d_funding_rank",
            _beyond(funding, str(decoy[0][0])) if decoy else float(len(funding) + 1),
            "rank",
            _TOP + 0.5,
            "higher",
        ),
        Check("t2c_cluster_fix_rank", fix, "rank", 10.5, "lower"),
        Check("t4_noise_rank", float(t4_rank), "rank", _FIRST, "lower"),
        Check("t4_noise_value", t4_value, "ratio", 0.9, "higher"),
        Check("t3_link_precision", hits / len(found) if found else 0.0, "ratio", 0.80, "higher"),
        Check("t3_link_recall", hits / len(planted) if planted else 0.0, "ratio", 0.70, "higher"),
        Check(
            "t5_org_rank", _beyond(org, plants.T5_confounder.team_id), "rank", _TOP + 0.5, "higher"
        ),
        Check("t2c_cluster_purity", purity, "ratio", 0.0, "higher"),  # information, never fails
    ]


def _open_copy(copy: Path, spill: Path) -> duckdb.DuckDBPyConnection:
    """Open the copy with the settings `open_for_build` gives the pipeline's own connection:
    production `build.memory_limit` (75 % of the RAM) and all threads, a spill folder under
    `work`, extensions off and `preserve_insertion_order` off. A bare `duckdb.connect` ran
    the full-scale `funding` step out of temp space (65.7 GiB at 5.7M incidents)."""
    cfg = db_settings(BuildSettings(memory_limit=HARNESS_MEMORY_LIMIT))
    spill.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(
        str(copy),
        read_only=False,
        config={
            "autoinstall_known_extensions": False,
            "autoload_known_extensions": False,
            "threads": cfg.threads if cfg.threads is not None else (os.cpu_count() or 1),
            "memory_limit": cfg.memory_limit,
            "temp_directory": str(spill),
            "preserve_insertion_order": False,
        },
    )
    con.execute("SET GLOBAL TimeZone = 'UTC'")
    return con


@contextlib.contextmanager
def scored_copy(handle: BuildHandle, work: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    """A scored copy of the build file: real `run_scoring` on a copy set back to `building`."""
    copy = work / "scored.duckdb"
    shutil.copyfile(handle.warehouse, copy)
    SynthEnv.create(handle.root, work, funding_window_days=FUNDING_WINDOW_DAYS).activate()
    con = _open_copy(copy, work / "spill")
    try:
        con.execute("UPDATE meta.build SET status='building' WHERE build_id = ?", [handle.build_id])
        run_scoring(handle.build_id, con=con)
        yield con
    finally:
        con.close()
        release()


def run_truths(
    handle: BuildHandle, work: Path, recorder: BenchRecorder, *, scale: str
) -> dict[str, BenchRecord]:
    """Score a copy of `handle`'s build, measure, and record every check; metric -> record."""
    with scored_copy(handle, work) as con:
        checks = measure_truths(handle, con)
    target_id = "BT11-05" if scale == "full" else f"BT11-05/{scale}"
    return {c.metric: record_value(recorder, c.target(target_id, scale), c.value) for c in checks}


def _failed(records: Mapping[str, BenchRecord], names: Sequence[str]) -> list[str]:
    return [
        f"{n} {records[n].value:.3g} (needs {records[n].better} than {records[n].threshold:g})"
        for n in names
        if not records[n].passed
    ]


# TEMPORARY MARKER (w50-s11 full-scale run on 7a0803a): remove when T11-34 gives the build
# fixture real clusters and T03-26 fixes the time_ci_window link band. The run records every
# BT11-05 value before the assertion, so the numbers stay in the recorder while this stands.
@pytest.mark.xfail(
    raises=AssertionError,
    strict=True,
    reason="product gaps at seed 42 full (owners T11-34, T03-26): t2_funding_rank 8 and "
    "t2c_cluster_fix_rank 425 (no cluster_fix candidate) because the HashEncoder fixture yields "
    "two clusters of 4,882,066 and 257,345 incidents, so 27 cluster-wide candidates each absorb "
    "180,791 incidents (496M USD annual pain) and outrank the T2 epic (309,464 incidents, 849.6M "
    "USD, effort 120k) on WSJF; t3_link_precision 2.49e-05 (6,192,121 links at score >= 0.5, "
    "recall 0.957; the T03-26 time_ci_window band). T1, T4, T5 and E2d pass.",
)
def test_bt11_05_planted_truths_full(
    request: pytest.FixtureRequest, tmp_path: Path, bench_recorder: BenchRecorder
) -> None:
    """BT11-05 seed 42 `full` build: T1 org rank 1; T2 funding rank 1 and E2d outside the top
    5; T2c cluster_fix in the top 10; T4 first with alert_noise_ratio >= 0.9; T3 link
    precision >= 0.80 and recall >= 0.70 at score >= 0.5; T5 outside the top 5 of score.org."""
    if bench_flag(request.config, FULL_ENV) != "1":
        pytest.skip(
            f"full-scale lake (tens of GB, deferred to an idle-host session); set {FULL_ENV}=1"
        )
    option = request.config.getoption("synth_builds_dir")
    handle = ensure_build(
        42, FULL_SCALE, repo_root=REPO, cache_dir=None if option is None else Path(option)
    )
    records = run_truths(handle, tmp_path, bench_recorder, scale="full")
    assert not _failed(records, [c for c in records if c != "t2c_cluster_purity"])


@pytest.fixture(scope="module")
def small_records(
    small_build: BuildHandle,
    bench_recorder: BenchRecorder,
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, BenchRecord]:
    """The BT11-05 checks on the seed 42 `small` build, measured once per module."""
    return run_truths(
        small_build, tmp_path_factory.mktemp("bt11_05"), bench_recorder, scale="small"
    )


def test_bt11_05_small_scoring_truths(small_records: Mapping[str, BenchRecord]) -> None:
    """BT11-05 small scale, checks that follow the real scoring: one team alone at org rank 1,
    T4 first with alert_noise_ratio >= 0.9, T5 and the decoy E2d outside the top 5 (T1's own
    rank is the row below)."""
    names = ["t1_teams_at_rank_1", "t4_noise_rank", "t4_noise_value"]
    assert not _failed(small_records, [*names, "t5_org_rank", "e2d_funding_rank"])


def test_bt11_05_small_t1_org_rank_1(small_records: Mapping[str, BenchRecord]) -> None:
    """BT11-05 small scale: the T1 team has score.org rank 1."""
    assert not _failed(small_records, ["t1_org_rank"])


def test_bt11_05_small_t3_link_recall(small_records: Mapping[str, BenchRecord]) -> None:
    """BT11-05 small scale: at score >= 0.5 at least 70 % of the planted T3 pairs are linked."""
    assert not _failed(small_records, ["t3_link_recall"])


def test_bt11_05_small_t2_epic_funding_rank_1(small_records: Mapping[str, BenchRecord]) -> None:
    """BT11-05 small scale: the T2 epic has score.funding rank 1."""
    assert not _failed(small_records, ["t2_funding_rank"])


@pytest.mark.xfail(
    raises=AssertionError,
    strict=True,
    reason="product gap T11-34 fixture: the cluster that holds the T2c members is one of the "
    "two HashEncoder mega-clusters (purity ~0.00), so its cluster_fix rank 1 is not the plant",
)
def test_bt11_05_small_t2c_cluster_fix_is_the_plant(
    small_records: Mapping[str, BenchRecord],
) -> None:
    """BT11-05 small scale: the T2c cluster_fix is in the top 10 and the cluster is mostly T2c
    (purity >= 0.5; the spec row checks the rank only, which a mega-cluster satisfies)."""
    rank = small_records["t2c_cluster_fix_rank"]
    assert rank.passed, (
        f"t2c_cluster_fix_rank {rank.value:g}: the cluster holding the T2c members ranks below 10 "
        "or is not a funding cluster_fix candidate (a rank one past the list means not ranked)"
    )
    purity = small_records["t2c_cluster_purity"].value
    assert purity >= MIN_PURITY, f"T2c cluster purity {purity:.4f} < {MIN_PURITY} (mega-cluster)"


def test_bt11_05_small_t3_link_precision(small_records: Mapping[str, BenchRecord]) -> None:
    """BT11-05 small scale: T3 link precision >= 0.80 at score >= 0.5."""
    assert not _failed(small_records, ["t3_link_precision"])
