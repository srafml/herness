"""tests.support.eval_golden_run: one timed mock golden run and its pinned gaps (T11-32).

`cached_build(seed, scale, factory)` builds a scored synthetic build once per test process
(`eval_golden.scored_build` under a session tmp dir). `run_golden_job(build, work, ...)` runs
one `eval` job in-process through `golden_env` and returns its `GoldenRun`: the job
outcome and exit code, `summary.json`, the `results.jsonl` lines and the wall time.
`pinned_gaps(build)` names the known gaps of this base, each with its owner card, so the
tests mark exactly the affected assertion as a conditional strict xfail (w49-s11e rulings):

* `suite_errors`: `seed_prior_run` (F05) is a `suite_error` until T11-33 wires
  `tests/support/seed_ops.py`;
* `t5_caught` (only the T5 part of the planted-truth row): at `tiny` scale the generator's
  90-day span holds no prior-year peak (design 11 §5.1.1, §5.1.5 "every prior year shows the
  same peak"), so spec 04 scoring cannot discount T5's seasonal rise, T5 ranks inside the
  `score.org` top five and `skeptic_catch` is false; owner T11-17 (tiny-scale question). It
  holds on `small`.

`EXPECTED_FAILED` is the exact set of questions the committed static scripts fail, each with
its owner (T11-32 spec note point 6).
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import re
import time
from pathlib import Path
from typing import Any, Final

import pytest
import yaml
from tests.support.eval_golden import MOCK_SCRIPTS, SUITE, GoldenBuild, golden_env, scored_build

from herness.core.resilience import ProcessState, reset_process_state
from herness.eval.runner import exit_code

__all__ = [
    "EXPECTED_FAILED",
    "PLANTED",
    "GoldenRun",
    "PinnedGap",
    "cached_build",
    "chat_answer_patterns",
    "golden_payload",
    "pinned_gaps",
    "run_golden_job",
]

SEED_OPS: Final = "tests.support.seed_ops"
T5_DETAIL: Final = "T5 confounder not caught"
PLANTED: Final = ("G03", "G06", "F01", "O01")  # design 11 §5.3.6 planted-truth questions
# Questions the committed static scripts fail, and who owns each cause (spec note point 6).
EXPECTED_FAILED: Final = {
    "G05": "T11-30: the chat name index resolves team names before org names, and the org "
    "names are also team names",
    "G10": "T11-30: cluster labels are not in the name index; T11-17: two planted clusters "
    "cannot meet topk_contains:5:4",
    "F03": "static script: the Writer cannot cite a run-time finding id, so no number",
    # F05 fails must_mention DELT-102 and must_mention SEQU-103: the static Writer script
    # never names the epics; T11-37 may script a data-aware answer.
    "F05": "static script: the Writer never names the T6 epics (DELT-102, SEQU-103)",
    "O04": "static script: the Writer cannot name the T5 team",
    "O06": "static script: the Writer cannot cite a run-time finding id, so no trend_slope",
}
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


def pinned_gaps(build: GoldenBuild) -> list[PinnedGap]:
    """The known gaps of this base on `build`, with their owner cards (module doc)."""
    gaps: list[PinnedGap] = []
    if importlib.util.find_spec(SEED_OPS) is None:
        gaps.append(PinnedGap("suite_errors", "F05 setup seed_prior_run is not wired", "T11-33"))
    if build.truth.scale == "tiny":
        reason = "tiny has a 90-day span, no prior-year peak: T5 ranks in the org top five"
        gaps.append(PinnedGap("t5_caught", reason, "T11-17"))
    return gaps


TURNS_PER_CHAT_QUESTION: Final = 4  # tool call, final text, JSON answer, correction classifier
_TEMPLATE: Final = re.compile(r"\\\{\\\{.*?\\\}\\\}")


def chat_answer_patterns() -> dict[str, re.Pattern[str]]:
    """Question id -> the committed chat answer of that question as a regex (its
    `{{row.*}}` templates match any text), in suite order: a shifted chat script (one call
    more or less than scripted) shows up as the wrong answer on the first shifted question."""
    from herness.eval.golden import load_suite  # noqa: PLC0415 - only here

    doc = yaml.safe_load((MOCK_SCRIPTS / "chat.yaml").read_text(encoding="utf-8"))
    turns = doc[0]["turns"]
    chat_ids = [q.id for q in load_suite(SUITE).questions if q.pipeline == "chat"]
    out: dict[str, re.Pattern[str]] = {}
    for i, qid in enumerate(chat_ids):
        text = turns[i * TURNS_PER_CHAT_QUESTION + 2]["final"]["output"]["text"]
        out[qid] = re.compile(_TEMPLATE.sub(".+", re.escape(text)))
    return out
