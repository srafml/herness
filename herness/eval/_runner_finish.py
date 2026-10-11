"""Run row, baseline, metrics, thresholds and the summary of a golden eval run.

Private sibling of `herness.eval.runner` (§2 module map): flow F11-07 step 3 (run row),
steps 10-11 (aggregate, `skeptic_catch`, thresholds against the chosen baseline, the
`summary.json` body with its provenance: `git_sha`, `config_hash`, suite SHA-256, judge
model and prompt hash, TH11-11) and step 13 (finish the row, log `eval.run.completed`,
metric samples through `record_metric_samples`, R-12). The runner writes the report files (step 12,
`herness.eval.report`) between `finish_run` and `close_run`. The run row is written through
`herness.store.ops.runs` inside `run_write` (R-08, R-10). The summary holds run metadata,
metrics and threshold rows only, never question or answer text.
"""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from herness.core import time as clock
from herness.core.config import config_hash, get_config
from herness.core.errors import ConfigError
from herness.core.logging import get_logger
from herness.core.types import MetricSample
from herness.eval._runner_baseline import BaselineChoice
from herness.eval._runner_question import required_rubric_skipped
from herness.eval.judge import judge_prompt_hash
from herness.eval.metrics import (
    EvalMetrics,
    ThresholdResult,
    aggregate_metrics,
    check_thresholds,
    skeptic_catch,
)
from herness.model.meta import git_sha
from herness.store.ops.core import run_write
from herness.store.ops.metrics import record_metric_samples
from herness.store.ops.runs import RunRow, get_run, insert_run, set_run_status, update_run_fields

if TYPE_CHECKING:
    from datetime import datetime

    from herness.eval._runner_question import QuestionEnv
    from herness.eval.golden import EvalQuestion
    from herness.eval.results import QuestionResult
    from herness.eval.settings import Thresholds

__all__ = ["Outcome", "RunMeta", "close_run", "finish_run", "insert_eval_run", "mark_failed",
           "reopen_run", "suite_label"]  # fmt: skip

SUMMARY_SCHEMA: Final = 1
_COMPONENT: Final = "eval"
_ACTIVE: Final = frozenset({"running"})
_CODE_DIR: Final = Path(__file__).resolve().parents[2]  # the checkout the code runs from

_log = get_logger("eval.runner")


@dataclass(frozen=True)
class RunMeta:
    """Identity of one eval run as written to the run row and `summary.json`."""

    run_id: str
    profile: str
    depth: str
    build_id: str
    dataset_kind: str
    dataset_scale: str
    suite: str
    suite_version: int
    suite_sha256: str
    mock: bool
    started_at: datetime
    dataset_seed: int | None = None
    baseline: str | None = None  # the baseline compared against (`run.meta.baseline`)
    compare_profile: str | None = None  # `--compare PROFILE` on the primary run (T11-38)


@dataclass(frozen=True)
class Outcome:
    """What the run ends with: the summary, pass/fail, the exit code (U11-69), the metrics
    and threshold rows, and the finish time."""

    summary: dict[str, Any]
    passed: bool
    exit_code: int
    metrics: EvalMetrics
    rows: list[ThresholdResult]
    finished: datetime


def suite_label(suite: str) -> str:
    """`golden` for the configured suite, else `custom` (a path is no metric or log label)."""
    return "golden" if suite == "golden" else "custom"


def insert_eval_run(meta: RunMeta, *, data_root: Path) -> None:
    """Insert the `run` row (`kind = eval`, `status = running`; F11-07 step 3). No
    `meta.job_id`: `find_run_by_job` names review runs (U06-79)."""
    row = RunRow(
        run_id=meta.run_id, kind="eval", depth=meta.depth, profile=meta.profile,
        build_id=meta.build_id, status="running", started_at=meta.started_at, finished_at=None,
        token_usage={}, cost_usd=Decimal(0), config_hash=config_hash(get_config()),
        meta={
            "suite": meta.suite, "suite_version": meta.suite_version,
            "suite_sha256": meta.suite_sha256, "baseline": meta.baseline,
            "compare_profile": meta.compare_profile,
            "mock": meta.mock, "dataset_kind": meta.dataset_kind,
            "dataset_root": data_root.as_posix(), "resume_of": None,
        },
    )  # fmt: skip
    run_write(lambda conn: insert_run(conn, row), op="eval_run_insert")


def reopen_run(run_id: str, now: datetime) -> RunRow:
    """The run row of a resumed eval, set back to `running` (F11-10); never a new row."""
    row = get_run(run_id)
    if row is None or row.kind != "eval" or row.build_id is None:
        msg = f"eval run not found: run_id={run_id}"
        raise ConfigError(msg)
    statuses = {"running", "done", "failed"}
    run_write(lambda conn: set_run_status(conn, run_id, "running", statuses, now=now),
              op="eval_run_resume")  # fmt: skip
    return row


def mark_failed(run_id: str, now: datetime) -> None:
    """Close the row as `failed` after a configuration error mid-run."""
    run_write(lambda conn: set_run_status(conn, run_id, "failed", _ACTIVE, now=now),
              op="eval_run_failed")  # fmt: skip


def _metrics(env: QuestionEnv, results: Sequence[QuestionResult],
             questions: Mapping[str, EvalQuestion]) -> EvalMetrics:  # fmt: skip
    """F11-07 step 10: traces and run rows of the distinct pipeline runs, skeptic catch."""
    run_ids = sorted({r.pipeline_run_id for r in results if r.pipeline_run_id is not None})
    traces = {rid: trace for rid in run_ids if (trace := env.trace(rid)) is not None}
    runs = {rid: row for rid in run_ids if (row := get_run(rid)) is not None}
    skeptic = skeptic_catch(env.truth, env.org_outputs)
    return aggregate_metrics(results, traces, runs, skeptic=skeptic, questions=questions)


def finish_run(
    meta: RunMeta, env: QuestionEnv, results: Sequence[QuestionResult], *,
    questions: Mapping[str, EvalQuestion], thresholds: Thresholds, baseline: BaselineChoice,
) -> Outcome:  # fmt: skip
    """Steps 10-11: metrics, thresholds against the baseline, the `summary.json` body; a
    non-comparable default replaces the rows' `no baseline` with its note."""
    m = _metrics(env, results, questions)
    rows = check_thresholds(
        m, profile=meta.profile, depth=meta.depth, baseline=baseline.metrics, results=results,
        dataset_kind=meta.dataset_kind, dataset_scale=meta.dataset_scale, settings=thresholds,
    )  # fmt: skip
    if baseline.note is not None:
        rows = [r.model_copy(update={"detail": r.detail.replace("no baseline", baseline.note)})
                for r in rows]  # fmt: skip
    for row in rows:
        if not row.passed:
            _log.error("eval.threshold.failed", run_id=meta.run_id, check=row.check,
                       actual=row.actual, limit=row.limit)  # fmt: skip
    rubric_skipped = any(required_rubric_skipped(r) for r in results)
    passed = all(row.passed for row in rows) and not rubric_skipped
    code = 0 if passed else 14
    finished = clock.now()
    summary = _summary(meta, env, m, rows, passed=passed, exit_code=code, finished=finished)
    return Outcome(summary, passed, code, m, rows, finished)


def close_run(
    meta: RunMeta, outcome: Outcome, results: Sequence[QuestionResult],
    questions: Mapping[str, EvalQuestion],
) -> None:  # fmt: skip
    """Step 13: run row `done` (tokens, cost), metric samples, `eval.run.completed`."""
    m, finished = outcome.metrics, outcome.finished
    run_write(lambda conn: _close(conn, meta, m, finished), op="eval_run_finish")
    _samples(meta, m, outcome.rows, results, questions, finished)
    _log.info("eval.run.completed", run_id=meta.run_id, passed=outcome.passed,
              exit_code=outcome.exit_code, answer_correctness=m.answer_correctness,
              unsupported_number_rate=m.unsupported_number_rate)  # fmt: skip


def _close(conn: Any, meta: RunMeta, m: EvalMetrics, now: datetime) -> None:  # noqa: ANN401 - sqlite3.Connection
    run_id, baseline = meta.run_id, meta.baseline
    usage = {"eval": {"tokens_in": m.tokens_in, "tokens_out": m.tokens_out}}
    update_run_fields(
        conn, run_id, token_usage=usage, cost_usd=m.cost_usd, meta_patch={"baseline": baseline}
    )  # the one compared at the end (resume)
    set_run_status(conn, run_id, "done", _ACTIVE, now=now)


def run_config_hash(run_id: str) -> str:
    """The `config_hash` of the run row (so `summary.json` matches it, ST11-14); the loaded
    config's hash when the row is gone."""
    row = get_run(run_id)
    return row.config_hash if row is not None else config_hash(get_config())


def _summary(  # noqa: PLR0913 - the summary's parts, keyword-only after the metrics
    meta: RunMeta, env: QuestionEnv, m: EvalMetrics, rows: Sequence[ThresholdResult], *,
    passed: bool, exit_code: int, finished: datetime,
) -> dict[str, Any]:  # fmt: skip
    """The `summary.json` body (§4.2 schema plus `baseline`; no question or answer text)."""
    return {
        "schema": SUMMARY_SCHEMA, "run_id": meta.run_id, "profile": meta.profile,
        "depth": meta.depth, "build_id": meta.build_id, "dataset_kind": meta.dataset_kind,
        "dataset_seed": meta.dataset_seed, "dataset_scale": meta.dataset_scale,
        "git_sha": git_sha(env=os.environ, repo_dir=_CODE_DIR),
        "config_hash": run_config_hash(meta.run_id), "suite_version": meta.suite_version,
        "suite_sha256": meta.suite_sha256,
        "judge_model": env.judge.model if env.judge is not None else None,
        "judge_prompt_sha256": judge_prompt_hash(), "mock": meta.mock, "baseline": meta.baseline,
        "compare_profile": meta.compare_profile,
        "started_at": clock.format_utc(meta.started_at), "finished_at": clock.format_utc(finished),
        "metrics": m.model_dump(mode="json"),
        "thresholds": [row.model_dump(mode="json") for row in rows],
        "passed": passed, "exit_code": exit_code,
    }  # fmt: skip


def _samples(
    meta: RunMeta, m: EvalMetrics, rows: Sequence[ThresholdResult],
    results: Sequence[QuestionResult], questions: Mapping[str, EvalQuestion], now: datetime,
) -> None:  # fmt: skip
    """§8.2 metric samples through `record_metric_samples` (R-12)."""
    per = Counter(
        (r.status, questions[r.id].pipeline if r.id in questions else "unknown") for r in results
    )
    samples = [
        _sample(now, "herness_eval_questions_total", "counter", n,
                {"status": status, "pipeline": pipeline})
        for (status, pipeline), n in sorted(per.items())
    ]  # fmt: skip
    seconds = (now - meta.started_at).total_seconds()
    samples.append(
        _sample(
            now,
            "herness_eval_run_duration_seconds",
            "histogram",
            seconds,
            {"suite": suite_label(meta.suite), "profile": meta.profile, "depth": meta.depth},
        )
    )
    if m.answer_correctness is not None:
        labels = {"profile": meta.profile, "depth": meta.depth, "dataset_kind": meta.dataset_kind}
        samples.append(_sample(now, "herness_eval_answer_correctness_ratio", "gauge",
                               m.answer_correctness, labels))  # fmt: skip
    samples.append(
        _sample(
            now,
            "herness_eval_unsupported_number_rate_ratio",
            "gauge",
            m.unsupported_number_rate,
            {"profile": meta.profile, "depth": meta.depth},
        )
    )
    samples += [
        _sample(now, "herness_eval_thresholds_failed_total", "counter", 1, {"check": row.check})
        for row in rows
        if not row.passed
    ]
    record_metric_samples(samples)


def _sample(
    now: datetime, name: str, kind: str, value: float, labels: dict[str, str]
) -> MetricSample:
    return MetricSample.model_validate({
        "ts": now, "name": name, "kind": kind, "value": float(value), "labels": labels,
        "component": _COMPONENT,
    })  # fmt: skip
