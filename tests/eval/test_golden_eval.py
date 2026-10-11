"""ET11-01, ET11-02: the golden suite end to end (impl 11 §11.7, U11-75; T11-32).

ET11-01 runs `herness eval --mock-llm tests/eval/mock_scripts` in-process: `handle_eval`
on the `--inline` path (R-45) with the factory of `tests.support.eval_golden` (the CLI and
its composition root are T09-24's), over the committed suite on a scored synthetic build.
The chat service, the swarm and the rubric judge are real; their model calls answer from
the committed scripts. The `tiny` build is seed 42 (truth `tests/fixtures/truth/42-tiny`):
the `tiny_build` fixture keeps the fake score hook and has no `score.*` tables, and seed 7
`tiny` does not rank the T1 plant first (spec note T11-32).

Known gaps on this base fail two threshold rows; each is a conditional strict xfail naming
its owner (`eval_golden_run.pinned_gaps`), so the row passes, unedited, once the gap closes:
F05 needs `seed_prior_run` (T11-33), and the T5 team ranks inside the org top five at `tiny`
scale. `test_et11_01_..._unpinned_rows` asserts everything else of the row on the same run.
ET11-02 is the real-model run (gate G3.2); it skips unless `HERNESS_LLM_URL` names the
local model server (the BT05-10 convention).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

import pytest
from tests.support.eval_golden import MOCK_SCRIPTS
from tests.support.eval_golden_run import (
    T5_DETAIL,
    GoldenRun,
    PinnedGap,
    cached_build,
    golden_payload,
    pinned_gaps,
    run_golden_job,
)

pytestmark = pytest.mark.eval

LIMIT_S: Final = 300.0  # ET11-01 "< 5 min"
QUESTIONS: Final = 36
URL_ENV: Final = "HERNESS_LLM_URL"


@pytest.fixture(scope="module")
def tiny_run(tmp_path_factory: pytest.TempPathFactory) -> tuple[GoldenRun, list[PinnedGap]]:
    build = cached_build(42, "tiny", tmp_path_factory)
    run = run_golden_job(build, tmp_path_factory.mktemp("et11-01"), golden_payload())
    return run, pinned_gaps(build)


def _xfail_pinned(request: pytest.FixtureRequest, gaps: list[PinnedGap]) -> None:
    if gaps:
        reason = "; ".join(f"{g.check}: {g.reason} (owner {g.owner})" for g in gaps)
        request.applymarker(pytest.mark.xfail(True, reason=reason, strict=True))


def _assert_every_threshold(run: GoldenRun) -> None:
    failed = [(t["check"], t["detail"]) for t in run.summary["thresholds"] if not t["passed"]]
    assert failed == []
    assert run.summary["metrics"]["unsupported_number_rate"] == 0
    assert (run.exit_code, run.passed) == (0, True)


def test_et11_01_mock_golden_meets_every_threshold(
    tiny_run: tuple[GoldenRun, list[PinnedGap]], request: pytest.FixtureRequest
) -> None:
    """ET11-01 mock golden run on the tiny build: every §5.3.6 threshold passes,
    unsupported_number_rate = 0, exit 0, under 5 minutes (strict xfail while a pinned gap
    is open)."""
    run, gaps = tiny_run
    _xfail_pinned(request, gaps)
    assert run.wall_s < LIMIT_S
    _assert_every_threshold(run)


def test_et11_01_mock_golden_unpinned_rows(tiny_run: tuple[GoldenRun, list[PinnedGap]]) -> None:
    """ET11-01 the same run: every question is answered and graded, no number is
    unsupported, every threshold row passes except a pinned one, and a pinned row fails
    only for its pinned reason; the run takes under 5 minutes."""
    run, gaps = tiny_run
    pinned = {g.check for g in gaps}
    assert run.wall_s < LIMIT_S
    assert len(run.results) == QUESTIONS
    assert run.summary["mock"] is True
    assert run.summary["metrics"]["unsupported_number_rate"] == 0
    assert all((r["unsupported"] or {}).get("unsupported", 0) == 0 for r in run.results)
    assert [r["id"] for r in run.results if r["status"] == "error"] == []
    rows = {t["check"]: t for t in run.summary["thresholds"]}
    assert [c for c, t in rows.items() if not t["passed"] and c not in pinned] == []
    if "suite_errors" in pinned:
        assert [r["id"] for r in run.results if r["status"] == "suite_error"] == ["F05"]
    if "planted_truth" in pinned:
        assert rows["planted_truth"]["detail"] == T5_DETAIL  # G03, G06, F01, O01 all passed
    assert run.summary["metrics"]["answer_correctness"] >= 0.80  # local-standard floor


@pytest.mark.slow
def test_et11_01_mock_golden_meets_every_threshold_on_small(
    tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest
) -> None:
    """ET11-01 the mock golden run on seed 42 `small` (the baseline build): every §5.3.6
    threshold passes and unsupported_number_rate = 0 (strict xfail while F05 waits for
    T11-33)."""
    build = cached_build(42, "small", tmp_path_factory)
    run = run_golden_job(build, tmp_path_factory.mktemp("et11-01-small"), golden_payload())
    _xfail_pinned(request, pinned_gaps(build))
    _assert_every_threshold(run)


@pytest.mark.gpu
@pytest.mark.slow
def test_et11_02_real_local_models_meet_every_threshold(
    tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest
) -> None:
    """ET11-02 seed 42 `small`, profile local, depth standard, real local models: every
    §5.3.6 threshold passes with at least 30 graded questions (gate G3.2; dev box)."""
    if not os.environ.get(URL_ENV):
        pytest.skip(f"real local models: set {URL_ENV} to the model server (dev box, G3.2)")
    build = cached_build(42, "small", tmp_path_factory)
    run = run_golden_job(build, tmp_path_factory.mktemp("et11-02"), golden_payload(None))
    _xfail_pinned(request, [g for g in pinned_gaps(build) if g.check == "suite_errors"])
    graded = [r for r in run.results if r["status"] in {"passed", "failed", "error"}]
    assert len(graded) >= 30
    _assert_every_threshold(run)


def test_et11_01_committed_scripts_cover_every_question() -> None:
    """ET11-01 the committed script directory loads and holds a chat script, the review
    roles and a judge script for every rubric question of the suite (U11-75)."""
    from tests.support.eval_golden import SUITE  # noqa: PLC0415

    from herness.eval.golden import load_suite  # noqa: PLC0415
    from herness.eval.scripted import load_scripts  # noqa: PLC0415

    suite = load_suite(SUITE)
    book = load_scripts(MOCK_SCRIPTS)
    matches = {(s.match.role, s.match.model_role, s.match.dedup_key) for s in book.scripts}
    assert ("chat", "*", "chat") in matches
    roles = {s.match.role for s in book.scripts}
    assert {"planner", "skeptic", "writer", "analyst_ops", "analyst_org"} <= roles
    rubric = {q.id for q in suite.questions if q.expected.rubric is not None}
    judged = {s.match.dedup_key for s in book.scripts if s.match.model_role == "eval_judge"}
    assert rubric == judged
    chat = next(s for s in book.scripts if s.match.role == "chat")
    n_chat = sum(q.pipeline == "chat" for q in suite.questions)
    assert len(chat.turns) == 4 * n_chat  # tool call, text, answer, correction classifier
    assert len(suite.questions) == QUESTIONS
    assert Path(MOCK_SCRIPTS).is_dir()
