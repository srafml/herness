"""Shared fixtures of the eval report tests (T11-31): a complete run summary, question
results, threshold rows, an `EvalRun` and a synthetic baseline summary."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from herness.eval.grading import GradeResult
from herness.eval.metrics import EvalMetrics, ThresholdResult
from herness.eval.results import QuestionResult
from herness.eval.runner import EvalRun

__all__ = ["PROVENANCE", "ROWS", "RUN_ID", "SUITE_SHA", "baseline_summary", "eval_run",
           "keys_anywhere", "result", "summary"]  # fmt: skip

RUN_ID = "run_" + "0" * 25 + "A"
SUITE_SHA = "a" * 64
PROVENANCE = ("git_sha", "config_hash", "suite_sha256", "judge_model", "judge_prompt_sha256")


def summary(**over: Any) -> dict[str, Any]:
    """A complete run summary (the §4.2 keys) without metrics and thresholds."""
    base: dict[str, Any] = {
        "schema": 1, "run_id": RUN_ID, "profile": "local", "depth": "standard",
        "build_id": "bld_tiny", "dataset_kind": "synthetic", "dataset_seed": 42,
        "dataset_scale": "small", "git_sha": "0123456789ab", "config_hash": "c" * 16,
        "suite_version": 3, "suite_sha256": SUITE_SHA, "judge_model": "judge-model-1",
        "judge_prompt_sha256": "d" * 16, "mock": False, "baseline": None,
        "started_at": "2026-10-10T08:00:00Z", "finished_at": "2026-10-10T08:05:00Z",
        "passed": False, "exit_code": 14,
    }  # fmt: skip
    base.update(over)
    return base


def result(qid: str, status: str, text: str = "All good.", error: str | None = None
           ) -> QuestionResult:  # fmt: skip
    check = "passed" if status == "passed" else "failed"
    return QuestionResult.model_validate({
        "id": qid, "repeat": 1, "status": status,
        "checks": [GradeResult(check="numeric", status=check, detail={"value": 2})],
        "reference": {}, "final_text": text, "numbers": [], "unsupported": None,
        "tokens_in": 1, "tokens_out": 2, "cost_usd": "0", "latency_s": 1.5, "retries": 0,
        "fallbacks": 0, "repairs": 0, "pipeline_run_id": None, "draft_mode": None,
        "trace_path": None, "error": error,
    })  # fmt: skip


ROWS = [
    ThresholdResult(check="unsupported_numbers", passed=True, actual=0.0, limit=0.0, detail="ok"),
    ThresholdResult(check="correctness", passed=False, actual=0.5, limit=0.8,
                    detail="0.500 below floor 0.80"),
]  # fmt: skip


def eval_run(out: Path, **over: Any) -> EvalRun:
    return EvalRun(run_id=RUN_ID, out_dir=out, summary=summary(**over), passed=False)


def keys_anywhere(value: object) -> Iterator[str]:
    """Every mapping key at any depth of a JSON value."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from keys_anywhere(item)
    elif isinstance(value, list):
        for item in value:
            yield from keys_anywhere(item)


def baseline_summary(**over: Any) -> dict[str, Any]:
    data = summary(
        metrics=EvalMetrics(answer_correctness=0.9, question_status={"G01": "passed"}).model_dump(
            mode="json"
        ),
        thresholds=[r.model_dump() for r in ROWS],
    )
    data.update(over)
    return data
