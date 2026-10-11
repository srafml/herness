"""Run row, metrics, thresholds and the minimal summary of a golden eval run.

Private sibling of `herness.eval.runner` (§2 module map): flow F11-07 step 3 (run row),
steps 10-11 (aggregate, `skeptic_catch`, thresholds) and step 13 (finish the row, log
`eval.run.completed`, metric samples through `record_metric_samples`, R-12). The run row is
written through `herness.store.ops.runs` inside `run_write` (R-08, R-10). Until T11-31's
`write_report` lands, `write_summary` writes `summary.json` with run metadata, metrics and
threshold rows only, never question or answer text (spec note after U11-68).
"""

from __future__ import annotations

import json
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
from herness.eval._runner_question import required_rubric_skipped
from herness.eval.invoke import PipelineOutput, review_output
from herness.eval.metrics import (
    EvalMetrics,
    ThresholdResult,
    aggregate_metrics,
    check_thresholds,
    skeptic_catch,
)
from herness.harness.swarm.writer import draft_path
from herness.store.ops.core import run_write
from herness.store.ops.metrics import record_metric_samples
from herness.store.ops.runs import RunRow, get_run, insert_run, set_run_status, update_run_fields

if TYPE_CHECKING:
    from datetime import datetime

    from herness.eval._runner_question import QuestionEnv
    from herness.eval.golden import EvalQuestion
    from herness.eval.results import QuestionResult
    from herness.eval.settings import Thresholds

__all__ = ["Outcome", "RunMeta", "cancel_run", "finish_run", "insert_eval_run", "mark_failed",
           "reopen_run",
           "suite_label"]  # fmt: skip

SUMMARY_SCHEMA: Final = 1
_COMPONENT: Final = "eval"
_ACTIVE: Final = frozenset({"running"})

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


@dataclass(frozen=True)
class Outcome:
    """What the run ends with: the summary, pass/fail and the exit code (U11-69)."""

    summary: dict[str, Any]
    passed: bool
    exit_code: int


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
            "suite_sha256": meta.suite_sha256, "baseline": None, "compare_profile": None,
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


def cancel_run(run_id: str, now: datetime) -> None:
    """Close the row as `canceled` when the job was canceled between questions."""
    run_write(lambda conn: set_run_status(conn, run_id, "canceled", _ACTIVE, now=now),
              op="eval_run_canceled")  # fmt: skip


def _org_outputs(
    env: QuestionEnv, results: Sequence[QuestionResult], questions: Mapping[str, EvalQuestion]
) -> tuple[list[PipelineOutput], list[str]]:
    """The org review outputs of every graded org result: this process's, else rebuilt
    from the run's stored draft and findings (a resumed run, F11-10); notes name the runs
    whose output could not be rebuilt."""
    here = {out.run_id: out for out in env.org_outputs}
    run_ids = sorted({
        r.pipeline_run_id for r in results
        if r.pipeline_run_id and r.status != "error"
        and (q := questions.get(r.id)) is not None and q.pipeline == "org"
    })  # fmt: skip
    outputs: list[PipelineOutput] = []
    notes: list[str] = []
    for rid in run_ids:
        out = here.get(rid) or review_output(rid, draft_path(rid, env.reports_root))
        if out.error is None:
            outputs.append(out)
        else:
            notes.append(f"org output missing: {rid}")
    return outputs, notes


def _metrics(env: QuestionEnv, results: Sequence[QuestionResult],
             questions: Mapping[str, EvalQuestion]) -> EvalMetrics:  # fmt: skip
    """F11-07 step 10: traces and run rows of the distinct pipeline runs, skeptic catch.
    An org result whose output is gone fails the skeptic part: it never passes vacuously."""
    run_ids = sorted({r.pipeline_run_id for r in results if r.pipeline_run_id is not None})
    traces = {rid: trace for rid in run_ids if (trace := env.trace(rid)) is not None}
    runs = {rid: row for rid in run_ids if (row := get_run(rid)) is not None}
    outputs, missing = _org_outputs(env, results, questions)
    skeptic = skeptic_catch(env.truth, outputs)
    if missing and env.truth is not None:
        skeptic = False
    m = aggregate_metrics(results, traces, runs, skeptic=skeptic, questions=questions)
    return m.model_copy(update={"notes": [*m.notes, *missing]}) if missing else m


def finish_run(
    meta: RunMeta, env: QuestionEnv, results: Sequence[QuestionResult], *,
    questions: Mapping[str, EvalQuestion], thresholds: Thresholds, out_dir: Path,
) -> Outcome:  # fmt: skip
    """Steps 10-13: metrics, thresholds, `summary.json`, run row `done`, metric samples."""
    m = _metrics(env, results, questions)
    rows = check_thresholds(
        m, profile=meta.profile, depth=meta.depth, baseline=None, results=results,
        dataset_kind=meta.dataset_kind, dataset_scale=meta.dataset_scale, settings=thresholds,
    )  # fmt: skip
    for row in rows:
        if not row.passed:
            _log.error("eval.threshold.failed", run_id=meta.run_id, check=row.check,
                       actual=row.actual, limit=row.limit)  # fmt: skip
    rubric_skipped = any(required_rubric_skipped(r) for r in results)
    passed = all(row.passed for row in rows) and not rubric_skipped
    code = 0 if passed else 14
    finished = clock.now()
    summary = _summary(meta, m, rows, passed=passed, exit_code=code, finished=finished)
    write_summary(out_dir / "summary.json", summary)
    run_write(lambda conn: _close(conn, meta.run_id, m, finished), op="eval_run_finish")
    _samples(meta, m, rows, results, questions, finished)
    _log.info("eval.run.completed", run_id=meta.run_id, passed=passed, exit_code=code,
              answer_correctness=m.answer_correctness,
              unsupported_number_rate=m.unsupported_number_rate)  # fmt: skip
    return Outcome(summary, passed, code)


def _close(conn: Any, run_id: str, m: EvalMetrics, now: datetime) -> None:  # noqa: ANN401 - sqlite3.Connection
    usage = {"eval": {"tokens_in": m.tokens_in, "tokens_out": m.tokens_out}}
    update_run_fields(conn, run_id, token_usage=usage, cost_usd=m.cost_usd)
    set_run_status(conn, run_id, "done", _ACTIVE, now=now)


def _summary(
    meta: RunMeta, m: EvalMetrics, rows: Sequence[ThresholdResult], *, passed: bool,
    exit_code: int, finished: datetime,
) -> dict[str, Any]:  # fmt: skip
    """The minimal `summary.json` (§4.2 keys this card can fill; no question or answer text)."""
    return {
        "schema": SUMMARY_SCHEMA, "run_id": meta.run_id, "profile": meta.profile,
        "depth": meta.depth, "build_id": meta.build_id, "dataset_kind": meta.dataset_kind,
        "dataset_scale": meta.dataset_scale, "suite_version": meta.suite_version,
        "suite_sha256": meta.suite_sha256, "mock": meta.mock,
        "started_at": clock.format_utc(meta.started_at), "finished_at": clock.format_utc(finished),
        "metrics": m.model_dump(mode="json"),
        "thresholds": [row.model_dump(mode="json") for row in rows],
        "passed": passed, "exit_code": exit_code,
    }  # fmt: skip


def write_summary(path: Path, summary: Mapping[str, Any]) -> None:
    """Write `summary.json` atomically (tmp + `os.replace`); rewritten on resume (§4.2)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


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
