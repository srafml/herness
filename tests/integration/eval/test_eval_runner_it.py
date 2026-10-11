"""Integration tests of the eval runner (F11-07, U11-62, U11-66, U11-68): IT11-20..IT11-22.

`handle_eval` runs in-process (the `herness eval --inline` path, R-45; the CLI command is
T09-24's) on the `tiny_build` warehouse; reviews go through the real swarm driver with
scripted agents (`_eval_it_env`), chat through a scripted chat service, and the exit code
comes from `exit_code(outcome)` (U11-69).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from tests.integration.eval._eval_it_env import EvalEnv, eval_env, question
from tests.integration.harness import _swarm_run_env as e
from tests.unit.eval._runner_env import lines, settings
from tests.unit.harness._blackboard_env import test_redactor

from herness.eval.runner import build_eval_payload, exit_code
from herness.store.ops.core import read_all

pytestmark = pytest.mark.integration

drv = e.drv  # fixtures
__all__ = ["drv", "eval_env", "test_redactor"]


def _payload(suite: object, ids: str, **extra: Any) -> dict[str, Any]:
    return build_eval_payload(
        suite=str(suite), profile="synth", depth="fast", ids=ids.split(","), no_judge=True,
        settings=settings(), **extra,
    )  # fmt: skip


def _review_runs() -> list[str]:
    rows = read_all("SELECT kind FROM run WHERE kind != 'eval' ORDER BY started_at", ())
    return [str(r["kind"]) for r in rows]


def test_it11_20_golden_run_inline_exit_0(eval_env: EvalEnv) -> None:
    """IT11-20 `--ids G01,G03,F01,O01` on tiny_build: job `done`; `results.jsonl` 4 lines;
    `summary.json` `mock = true`; exit 0 (real funding and org review runs)."""
    suite = eval_env.suite(
        [question("G01"), question("G02"), question("G03"), question("F01", "funding"),
         question("O01", "org")]
    )  # fmt: skip
    mock = eval_env.tmp / "mock"
    mock.mkdir()
    (mock / "judge.yaml").write_text(
        "- match: {model_role: eval_judge}\n  turns:\n    - final: {text: ok}\n", encoding="utf-8"
    )
    _, outcome = eval_env.handle(_payload(suite, "G01,G03,F01,O01", mock_llm_dir=str(mock)))

    assert outcome.status == "done"
    out = eval_env.out_dir(outcome.result["run_id"])
    results = lines(out / "results.jsonl")
    assert [r["id"] for r in results] == ["G01", "G03", "F01", "O01"]
    assert {r["id"]: r["status"] for r in results} == dict.fromkeys(
        ["G01", "G03", "F01", "O01"], "passed"
    )
    by_id = {r["id"]: r for r in results}
    assert by_id["F01"]["draft_mode"] == "full"
    assert by_id["O01"]["pipeline_run_id"] != by_id["F01"]["pipeline_run_id"]
    assert all(r["unsupported"]["unsupported"] == 0 for r in results)
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["mock"] is True
    assert summary["build_id"] == eval_env.build.build_id
    assert summary["dataset_kind"] == "synthetic"
    assert exit_code(outcome.model_dump()) == 0
    assert _review_runs() == ["funding_review", "org_review"]
    assert len(eval_env.chat.calls) == 2


def test_it11_21_error_and_suite_error_exit_14(eval_env: EvalEnv) -> None:
    """IT11-21 one question raising in the pipeline and one bad reference SQL: statuses
    `error` and `suite_error`, the other questions graded, exit 14."""
    broken = question("G02", question="Which team breaks the chat?")
    bad_sql = question("G04", expected={"numeric": {
        "reference_sql": "SELECT value FROM core.no_such_table", "unit": "count"}})  # fmt: skip
    suite = eval_env.suite([question("G01"), broken, question("G03"), bad_sql,
                            question("F01", "funding")])  # fmt: skip
    eval_env.chat.errors = frozenset({"Which team breaks the chat?"})
    _, outcome = eval_env.handle(_payload(suite, "G01,G02,G03,G04,F01"))

    assert outcome.status == "done"
    results = lines(eval_env.out_dir(outcome.result["run_id"]) / "results.jsonl")
    statuses = {r["id"]: r["status"] for r in results}
    assert statuses == {"G01": "passed", "G02": "error", "G03": "passed",
                        "G04": "suite_error", "F01": "passed"}  # fmt: skip
    assert outcome.result["passed"] is False
    assert exit_code(outcome.model_dump()) == 14


def test_it11_22_premium_shared_review_and_concurrency(eval_env: EvalEnv) -> None:
    """IT11-22 premium with three org questions: one shared org review run; at most 8 chat
    calls at once (stub counter), more than one in parallel."""
    orgs = [question(f"O0{i}", "org") for i in (1, 2, 3)]
    chats = [question(f"G{i:02d}") for i in range(1, 13)]
    suite = eval_env.suite(orgs + chats)
    eval_env.chat.delay_s = 0.05
    payload = _payload(suite, ",".join(q["id"] for q in orgs + chats)) | {"profile": "premium"}
    _, outcome = eval_env.handle(payload)

    assert outcome.status == "done"
    results = lines(eval_env.out_dir(outcome.result["run_id"]) / "results.jsonl")
    assert len(results) == 15
    org_runs = {r["pipeline_run_id"] for r in results if r["id"].startswith("O")}
    assert len(org_runs) == 1
    assert _review_runs() == ["org_review"]
    assert set(eval_env.chat.modes) == {"cloud"}  # OI-3: premium chat runs in cloud mode
    assert 1 < eval_env.chat.max_running <= 8
