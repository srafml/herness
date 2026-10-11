"""ET11-01, ET11-02: the golden suite end to end (impl 11 §11.7, U11-75; T11-32).

ET11-01 runs `herness eval --mock-llm tests/eval/mock_scripts` in-process: `handle_eval`
on the `--inline` path (R-45) with the factory of `tests.support.eval_golden` (the CLI and
its composition root are T09-24's), over the committed suite on a scored synthetic build.
The chat service, the swarm and the rubric judge are real; their model calls answer from
the committed scripts. The `tiny` build is seed 42 (truth `tests/fixtures/truth/42-tiny`):
the `tiny_build` fixture keeps the fake score hook and has no `score.*` tables, and seed 7
`tiny` does not rank the T1 plant first (spec note T11-32).

Known gaps of this base are conditional strict xfails naming their owner card
(`eval_golden_run.pinned_gaps`), each on exactly the assertion it breaks, so the row passes
unedited once the gap closes: F05 needs `seed_prior_run` (T11-33; the `suite_errors` row),
and at `tiny` scale T5 ranks in the org top five (T11-17; only the T5 part of the planted
truth: `test_et11_01_t5_confounder_caught`). The questions the static scripts fail are an
exact, owner-named set (`EXPECTED_FAILED`), so a new failure or a fix shows. ET11-02 is the
real-model run (gate G3.2); it skips unless `HERNESS_LLM_URL` names the local model server
(the BT05-10 convention).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

import pytest
from tests.support.eval_golden import MOCK_SCRIPTS, SUITE
from tests.support.eval_golden_run import (
    EXPECTED_FAILED,
    PLANTED,
    GoldenRun,
    PinnedGap,
    cached_build,
    chat_answer_patterns,
    golden_payload,
    pinned_gaps,
    run_golden_job,
)

from herness.eval.golden import load_suite
from herness.eval.scripted import load_scripts

pytestmark = pytest.mark.eval

LIMIT_S: Final = 300.0  # ET11-01 "< 5 min"
QUESTIONS: Final = 37  # every id of design §5.3.3 (its heading says 36; the table lists 37)
URL_ENV: Final = "HERNESS_LLM_URL"


@pytest.fixture(scope="module")
def tiny_run(tmp_path_factory: pytest.TempPathFactory) -> tuple[GoldenRun, list[PinnedGap]]:
    build = cached_build(42, "tiny", tmp_path_factory)
    run = run_golden_job(build, tmp_path_factory.mktemp("et11-01"), golden_payload())
    return run, pinned_gaps(build)


def _xfail_if(request: pytest.FixtureRequest, gaps: list[PinnedGap], *checks: str) -> None:
    """Strict xfail for the pinned gaps among `checks` (nothing when none applies)."""
    hit = [g for g in gaps if g.check in checks]
    if hit:
        reason = "; ".join(f"{g.check}: {g.reason} (owner {g.owner})" for g in hit)
        request.applymarker(pytest.mark.xfail(True, reason=reason, strict=True))


def _rows(run: GoldenRun) -> dict[str, dict[str, object]]:
    return {t["check"]: t for t in run.summary["thresholds"]}


def test_et11_01_mock_golden_meets_every_threshold(
    tiny_run: tuple[GoldenRun, list[PinnedGap]], request: pytest.FixtureRequest
) -> None:
    """ET11-01 mock golden run on the tiny build: every §5.3.6 threshold row passes (the
    planted-truth row through its questions G03, G06, F01, O01; its T5 part is
    `test_et11_01_t5_confounder_caught`), unsupported_number_rate = 0, under 5 minutes
    (strict xfail while F05 waits for T11-33)."""
    run, gaps = tiny_run
    _xfail_if(request, gaps, "suite_errors")
    assert run.wall_s < LIMIT_S
    assert run.summary["metrics"]["unsupported_number_rate"] == 0
    assert [run.result(q)["status"] for q in PLANTED] == ["passed"] * len(PLANTED)
    rows = _rows(run)
    failed = [c for c, t in rows.items() if not t["passed"] and c != "planted_truth"]
    assert failed == []
    assert rows["planted_truth"]["passed"] or rows["planted_truth"]["detail"] == (
        "T5 confounder not caught"
    )


def test_et11_01_t5_confounder_caught(
    tiny_run: tuple[GoldenRun, list[PinnedGap]], request: pytest.FixtureRequest
) -> None:
    """ET11-01 the T5 part of the planted-truth row: skeptic_catch is true and the row
    passes (strict xfail at `tiny` scale, owner T11-17: a 90-day span has no prior-year
    peak)."""
    run, gaps = tiny_run
    _xfail_if(request, gaps, "t5_caught")
    assert run.summary["metrics"]["skeptic_catch"] is True
    assert _rows(run)["planted_truth"]["passed"] is True


def test_et11_01_mock_golden_question_outcomes(
    tiny_run: tuple[GoldenRun, list[PinnedGap]],
) -> None:
    """ET11-01 the same run, per question: every question has a result, none errors, no
    number is unsupported, each chat answer is the scripted answer of its own question (no
    shifted script), and the failed set is exactly the owner-named `EXPECTED_FAILED`
    (G05, G10: T11-30/T11-17; F03, O04, O06: static-script limits); F05 is the only
    suite_error while T11-33 is open."""
    run, gaps = tiny_run
    assert len(run.results) == QUESTIONS
    assert run.summary["mock"] is True
    assert all((r["unsupported"] or {}).get("unsupported", 0) == 0 for r in run.results)
    assert [r["id"] for r in run.results if r["status"] == "error"] == []
    for qid, pattern in chat_answer_patterns().items():
        assert pattern.fullmatch(run.result(qid)["final_text"]), qid
    failed = {r["id"] for r in run.results if r["status"] == "failed"}
    assert failed == set(EXPECTED_FAILED)
    suite_errors = [r["id"] for r in run.results if r["status"] == "suite_error"]
    assert suite_errors == (["F05"] if any(g.check == "suite_errors" for g in gaps) else [])


@pytest.mark.slow
def test_et11_01_mock_golden_meets_every_threshold_on_small(
    tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest
) -> None:
    """ET11-01 the mock golden run on seed 42 `small` (the baseline build): every §5.3.6
    threshold passes, T5 included, and unsupported_number_rate = 0 (strict xfail while F05
    waits for T11-33)."""
    build = cached_build(42, "small", tmp_path_factory)
    run = run_golden_job(build, tmp_path_factory.mktemp("et11-01-small"), golden_payload())
    _xfail_if(request, pinned_gaps(build), "suite_errors", "t5_caught")
    assert {r["id"] for r in run.results if r["status"] == "failed"} == set(EXPECTED_FAILED)
    assert [c for c, t in _rows(run).items() if not t["passed"]] == []
    assert run.summary["metrics"]["unsupported_number_rate"] == 0
    assert (run.exit_code, run.passed) == (0, True)


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
    _xfail_if(request, pinned_gaps(build), "suite_errors", "t5_caught")
    graded = [r for r in run.results if r["status"] in {"passed", "failed", "error"}]
    assert len(graded) >= 30
    assert [c for c, t in _rows(run).items() if not t["passed"]] == []
    assert run.summary["metrics"]["unsupported_number_rate"] == 0
    assert (run.exit_code, run.passed) == (0, True)


def test_et11_01_committed_scripts_cover_every_question() -> None:
    """ET11-01 the committed script directory loads and holds a chat script, the review
    roles and a judge script for every rubric question of the suite (U11-75)."""
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
    assert len(chat_answer_patterns()) == n_chat
    assert len(suite.questions) == QUESTIONS
    assert Path(MOCK_SCRIPTS).is_dir()
