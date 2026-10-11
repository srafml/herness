"""tests.support.eval_golden_run: one timed mock golden run and its pinned gaps (T11-32).

`cached_build(seed, scale, factory)` builds a scored synthetic build once per test process
(`eval_golden.scored_build` under a session tmp dir). `run_golden_job(build, work, ...)` runs
one `eval` job in-process through `golden_env` and returns its `GoldenRun`: the job
outcome and exit code, `summary.json`, the `results.jsonl` lines and the wall time.
`pinned_gaps(build)` names the known gaps that make threshold rows fail on a build, each
with its owner, so the tests can mark them as conditional strict xfails (w49-s11e rulings):

* `seed_prior_run` (F05) is a `suite_error` until T11-33 wires `tests/support/seed_ops.py`;
* the T5 confounder team ranks inside the `score.org` top five at `tiny` scale, so the org
  review's ranked entities hold it and `skeptic_catch` is false (design 11 §5.1.5 expects it
  outside the top five; seen on the seed 42 `tiny` scored build, not on `small`).
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import time
from pathlib import Path
from typing import Any, Final

import duckdb
import pytest
from tests.support.eval_golden import MOCK_SCRIPTS, SUITE, GoldenBuild, golden_env, scored_build

from herness.core.resilience import ProcessState, reset_process_state
from herness.eval.runner import exit_code

__all__ = [
    "GoldenRun",
    "PinnedGap",
    "cached_build",
    "golden_payload",
    "pinned_gaps",
    "run_golden_job",
]

SEED_OPS: Final = "tests.support.seed_ops"
T5_DETAIL: Final = "T5 confounder not caught"
_BUILDS: dict[tuple[int, str], GoldenBuild] = {}


@dataclasses.dataclass(frozen=True)
class PinnedGap:
    """A known gap that fails a threshold row on this base, and the card that owns it."""

    check: str
    reason: str
    owner: str


@dataclasses.dataclass(frozen=True)
class GoldenRun:
    """One finished eval job."""

    run_id: str
    exit_code: int
    passed: bool
    summary: dict[str, Any]
    results: list[dict[str, Any]]
    wall_s: float
    out_dir: Path

    def threshold(self, check: str) -> dict[str, Any]:
        return next(t for t in self.summary["thresholds"] if t["check"] == check)

    def result(self, qid: str) -> dict[str, Any]:
        return next(r for r in self.results if r["id"] == qid)


def cached_build(seed: int, scale: str, factory: pytest.TempPathFactory) -> GoldenBuild:
    """The scored build of `seed`/`scale`, built once per process."""
    key = (seed, scale)
    if key not in _BUILDS:
        _BUILDS[key] = scored_build(seed, scale, factory.mktemp(f"golden-{seed}-{scale}"))
    return _BUILDS[key]


def golden_payload(mock_dir: Path | None = MOCK_SCRIPTS, **extra: Any) -> dict[str, Any]:
    """An `eval` job payload over the committed suite, profile local, depth standard."""
    payload: dict[str, Any] = {"suite": str(SUITE), "profile": "local", "depth": "standard",
                               "repeat": 1}  # fmt: skip
    if mock_dir is not None:
        payload["mock_llm_dir"] = str(mock_dir)
    return payload | extra


def _quiet_process_state() -> ProcessState:
    """Fresh spec 08 process state with no-op sleeps (retry waits do not slow the run)."""

    async def no_asleep(_seconds: float) -> None:
        return None

    state = reset_process_state()
    state.sleep = lambda _seconds: None
    state.asleep = no_asleep
    return state


def run_golden_job(
    build: GoldenBuild, work: Path, payload: dict[str, Any], *, repo_root: Path | None = None,
    chat_verifier: Any | None = None,
) -> GoldenRun:  # fmt: skip
    """Run one `eval` job on a copy of `build` under `work` (the `--inline` path)."""
    _quiet_process_state()
    try:
        with golden_env(build, work, repo_root=repo_root, chat_verifier=chat_verifier) as env:
            start = time.perf_counter()
            _ctx, outcome = env.handle(payload)
            wall = time.perf_counter() - start
            result = outcome.result
            out = env.out_dir(result["run_id"])
            summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
            lines = (out / "results.jsonl").read_text(encoding="utf-8").splitlines()
            return GoldenRun(
                run_id=str(result["run_id"]), exit_code=exit_code(outcome.model_dump()),
                passed=bool(result["passed"]), summary=summary,
                results=[json.loads(line) for line in lines], wall_s=wall, out_dir=out,
            )  # fmt: skip
    finally:
        reset_process_state()


def _t5_rank(build: GoldenBuild) -> int | None:
    team = build.truth.plants.T5_confounder.team_id
    con = duckdb.connect(str(build.warehouse), read_only=True)
    try:
        row = con.execute(
            "SELECT min(rank) FROM score.org WHERE entity_type = 'team' AND entity_id = ?", [team]
        ).fetchone()
    finally:
        con.close()
    return None if row is None or row[0] is None else int(row[0])


def pinned_gaps(build: GoldenBuild) -> list[PinnedGap]:
    """The known gaps that fail threshold rows on `build` on this base (module doc)."""
    gaps: list[PinnedGap] = []
    if importlib.util.find_spec(SEED_OPS) is None:
        gaps.append(PinnedGap("suite_errors", "F05 setup seed_prior_run is not wired", "T11-33"))
    rank = _t5_rank(build)
    if rank is not None and rank <= 5:
        gaps.append(PinnedGap(
            "planted_truth", f"T5 team ranks {rank} in score.org (tiny-scale plant)",
            "controller (T5 plant at tiny scale: T11-10 generator / T04 scoring)",
        ))  # fmt: skip
    return gaps
