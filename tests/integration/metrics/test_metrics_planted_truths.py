"""Design 04 §10.3 planted truths on synthetic data (impl 04 IT04-08, T04-22).

`synth_data.py --scale small` (nightly: `full`) is generated, built with the real spec 02
build, given the truth-planted enrichment of `tests.support.metrics_synth` and scored with
default steps. From spec 04 outputs alone: the planted high-ROI epic has
`score.funding.rank = 1`; the planted bad team has `rank = 1` among teams in `score.org`;
its top `score.action_lever` names the planted metric; the planted recurring cluster
without an epic is a `cluster_fix` candidate in the top 10. The truth comparison lives here
under `tests/`; IT04-11 checks nothing under `herness/metrics/` reads truth files (R-64).

Both variants carry `slow` (pyproject: "small or full scale"). The `full` variant reuses a
pre-generated root named by `HERNESS_SYNTH_FULL_ROOT` (nightly cache) when set.

Spec note SN-1 (CO-T04-22a, owner T11): the generator plants the E2 remote links (T2
epic) at `--start` + 30 days (tools/synth/plants_delivery.py), about three years before
`--end` at `small`/`full`, outside the shipped 365-day funding window; with it the epic has no
in-window link and ranks 4 (its cluster becomes a `cluster_fix`). Until CO-T04-22a is
resolved the tests set `scoring.funding.window_days` to 1095 (the allowed maximum).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Final

import pytest
from tests.support.metrics_synth import (
    SynthEnv,
    cluster_id,
    generate_root,
    remove_builds,
    scored_build,
)

from herness.store import warehouse

pytestmark = [pytest.mark.integration, pytest.mark.slow]

SEED: Final = 42
# CO-T04-22a: E2 links sit at start + 30 d, outside the shipped 365-day window (SN-1).
FUNDING_WINDOW_DAYS: Final = 1095
TOP_CLUSTER_FIX: Final = 10
FULL_ROOT_ENV: Final = "HERNESS_SYNTH_FULL_ROOT"


def assert_planted_truths(env: SynthEnv, build_id: str) -> None:
    """Design 04 §10.3 on the scored build, read back through the read-only reader."""
    t1 = env.truth.plant("T1_bad_team")
    epic = str(env.truth.plant("T2_roi_epic")["epic_record_id"])
    team, metric = str(t1["team_id"]), str(t1["metric"])
    fix = "cluster_fix:" + cluster_id("T2c")
    con = warehouse.open_readonly(build_id, layout=env.layout)
    try:
        funding = dict(con.execute("SELECT candidate_id, rank FROM score.funding").fetchall())
        teams = con.execute(
            "SELECT DISTINCT entity_id FROM score.org WHERE entity_type = 'team' AND rank = 1"
        ).fetchall()
        lever = con.execute(
            "SELECT metric FROM score.action_lever WHERE entity_type = 'team'"
            " AND entity_id = ? ORDER BY delta_usd DESC, metric, target_kind LIMIT 1",
            [team],
        ).fetchall()
    finally:
        con.close()
    assert funding.get(epic) == 1, f"T2 epic rank {funding.get(epic)}"
    assert [r[0] for r in teams] == [team]
    assert lever == [(metric,)]
    assert fix in funding, "the T2c cluster is not a funding candidate"
    assert funding[fix] <= TOP_CLUSTER_FIX


def _run(root: Path, work: Path) -> None:
    env = SynthEnv.create(root, work, funding_window_days=FUNDING_WINDOW_DAYS)
    try:
        build = scored_build(env)
        assert build.report.steps_done[-1] == "check"
        assert_planted_truths(env, build.build_id)
    finally:
        remove_builds(env)


@pytest.mark.timeout(1800)
def test_it04_08_planted_truths_small(tmp_path: Path) -> None:
    """IT04-08 `synth_data.py --scale small`: the design 04 §10.3 planted truths hold on
    the scored build (T2 epic funding rank 1, T1 team org rank 1 with its planted metric as
    top lever, T2c `cluster_fix` in the funding top 10)."""
    root = generate_root(SEED, "small", tmp_path / "synth")
    _run(root, tmp_path / "work")


@pytest.mark.xfail(
    strict=True,
    reason="CO-T04-22b (seed 42 full, 2026-10-09): T2 epic funding rank 1 and T1 top lever"
    " mttr_hours hold, but T1 team is org rank 3 (composite 1.624 vs 1.831 for a crit_lo"
    " team scored on 6 of 10 metrics) and the T2c cluster_fix is funding rank 60 of 427"
    " (needs <= 10)",
)
@pytest.mark.timeout(7200)
def test_it04_08_planted_truths_full(tmp_path: Path) -> None:
    """IT04-08 nightly `full` variant of the design 04 §10.3 planted truths (reuses the root
    named by HERNESS_SYNTH_FULL_ROOT when set, else generates one and removes it after)."""
    cached = os.environ.get(FULL_ROOT_ENV)
    root = Path(cached) if cached else generate_root(SEED, "full", tmp_path / "synth")
    try:
        _run(root, tmp_path / "work")
    finally:
        if not cached:
            shutil.rmtree(root, ignore_errors=True)
