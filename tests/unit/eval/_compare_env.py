"""Stored eval runs for the comparison tests (T11-38: UT11-100, UT11-101, CV checks).

`store_run` writes what a finished eval run leaves under `<data_root>/reports/eval/<run_id>/`:
a `summary.json` with the §4.2 keys and the run's metrics, and a `results.jsonl` with one
graded result per `(question_id, repeat)`. `numeric` builds a stored numeric check the way
`grade_numeric` records it (reference value and the cited candidates).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from herness.eval.grading import GradeResult
from herness.eval.metrics import EvalMetrics
from herness.eval.results import QuestionResult, ResultsLog

__all__ = ["BUILD", "RUN_A", "RUN_B", "RUN_C", "SUITE", "numeric", "q_result", "store_run"]

RUN_A = "run_01JA0000000000000000000000"  # pragma: allowlist secret - test ULID
RUN_B = "run_01JA0000000000000000000001"  # pragma: allowlist secret - test ULID
RUN_C = "run_01JA0000000000000000000002"  # pragma: allowlist secret - test ULID
BUILD = "bld_01JA0000000000000000000000"  # pragma: allowlist secret - test ULID
SUITE = "a" * 64


def numeric(reference: str, *values: str, passed: bool = True) -> GradeResult:
    """A stored numeric check: reference value and the cited NumberRef candidates."""
    candidates = [{"id": f"n{i}", "value": v} for i, v in enumerate(values, 1)]
    return GradeResult(
        check="numeric", status="passed" if passed else "failed",
        detail={"reference": reference, "query_id": "q_x", "candidates": candidates},
    )  # fmt: skip


def q_result(
    qid: str, status: str = "passed", *, repeat: int = 1, checks: Sequence[GradeResult] = ()
) -> QuestionResult:
    """A graded result of `qid` with the given checks."""
    return QuestionResult.model_validate({
        "id": qid, "repeat": repeat, "status": status, "checks": list(checks),
        "reference": {}, "final_text": "answer", "numbers": [], "unsupported": None,
        "tokens_in": 1, "tokens_out": 1, "cost_usd": "0", "latency_s": 1.0, "retries": 0,
        "fallbacks": 0, "repairs": 0, "pipeline_run_id": None, "draft_mode": None,
        "trace_path": None, "error": None,
    })  # fmt: skip


def store_run(
    data_root: Path, run_id: str, *, profile: str = "local", depth: str = "standard",
    metrics: EvalMetrics | None = None, results: Sequence[QuestionResult] = (),
    **summary: Any,
) -> Path:  # fmt: skip
    """Write `summary.json` and `results.jsonl` of a finished run; returns its directory."""
    out = data_root / "reports" / "eval" / run_id
    out.mkdir(parents=True, exist_ok=True)
    statuses = {r.id: r.status for r in results}
    m = metrics or EvalMetrics(answer_correctness=0.5)
    m = m.model_copy(update={"question_status": statuses})
    body: dict[str, Any] = {
        "schema": 1, "run_id": run_id, "profile": profile, "depth": depth, "build_id": BUILD,
        "dataset_kind": "synthetic", "dataset_seed": 42, "dataset_scale": "small",
        "git_sha": "0123456789ab", "config_hash": "c" * 16, "suite_version": 1,
        "suite_sha256": SUITE, "judge_model": None, "judge_prompt_sha256": "d" * 16,
        "mock": True, "baseline": None, "compare_profile": None,
        "started_at": "2026-10-10T08:00:00Z", "finished_at": "2026-10-10T08:05:00Z",
        "metrics": m.model_dump(mode="json"), "thresholds": [], "passed": True, "exit_code": 0,
    }  # fmt: skip
    body.update(summary)
    (out / "summary.json").write_text(json.dumps(body), encoding="utf-8")
    (out / "results.jsonl").unlink(missing_ok=True)  # a stored run is written afresh
    log = ResultsLog(out / "results.jsonl")
    for r in results:
        log.append(r)
    return out
