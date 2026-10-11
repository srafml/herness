"""The classifier branch of the `eval` job (U11-68 step 3; flow F11-09; T11-35).

Private sibling of `herness.eval.runner` (§2 module map), split off because `runner.py` is
at its 400-line limit; imported only by `herness.eval.runner`. `classifier_outcome` pins the
`CURRENT` build (its `dataset_kind`; the truth directory on synthetic builds), runs
`run_classifier` for the configured question set and the default deciders with the run id
bound to the log context, then inserts the eval `run` row (`suite = classifier`), writes the
report files (`classifier.json` plus the classifier section of `report.md` and
`report.html`) into `data/reports/eval/<run_id>/` and closes the row `done` with the
classifier keys in `run.meta`. A `ConfigError` from `run_classifier` (missing `CURRENT`,
manifest, `eval.json` or gold) leaves no row. A failed gate check is `passed` false and
exit code 14, never an error.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Final

from herness.core import time as clock
from herness.core.config import get_config
from herness.core.ids import IdKind, new_id
from herness.core.logging import bind_ids, get_logger
from herness.core.types import JobOutcome
from herness.eval._runner_finish import (
    _CODE_DIR,
    SUMMARY_SCHEMA,
    RunMeta,
    insert_eval_run,
    mark_failed,
    run_config_hash,
)
from herness.eval._runner_payload import CLASSIFIER_SUITE, EXIT_GATE, EXIT_OK
from herness.eval._runner_support import current_build, dataset_kind
from herness.eval.classifier import ClassifierReport, default_deciders, run_classifier
from herness.eval.metrics import EvalMetrics
from herness.eval.report import write_report
from herness.eval.truth import load_truth, truth_dir_for
from herness.model.meta import git_sha
from herness.store.ops.core import run_write
from herness.store.ops.runs import set_run_status, update_run_fields

if TYPE_CHECKING:
    from datetime import datetime

    from herness.eval._runner_payload import EvalPayload
    from herness.eval.runner import EvalDeps

__all__ = ["classifier_outcome"]

_ACTIVE: Final = frozenset({"running"})

_log = get_logger("eval.runner")


def _classifier_meta(report: ClassifierReport, deciders: list[str]) -> dict[str, Any]:
    """The `run.meta` and `summary.json` keys of a classifier run."""
    gold = report.check("gold_sha256")
    return {
        "question_set_version": report.question_set_version,
        "model_version": report.version,
        "gold_sha256": None if gold is None else gold.recomputed,
        "deciders": deciders,
        "passed": report.passed,
    }


def _summary(
    meta: RunMeta, report: ClassifierReport, extra: dict[str, Any], finished: datetime
) -> dict[str, Any]:
    """`summary.json` of a classifier run: the §4.2 keys (no judge, no golden suite: the
    gold digest stands for the suite SHA-256), empty golden metrics, the classifier keys."""
    code = EXIT_OK if report.passed else EXIT_GATE
    return {
        "schema": SUMMARY_SCHEMA, "run_id": meta.run_id, "profile": meta.profile,
        "depth": meta.depth, "build_id": meta.build_id, "dataset_kind": meta.dataset_kind,
        "dataset_seed": meta.dataset_seed, "dataset_scale": meta.dataset_scale,
        "git_sha": git_sha(env=os.environ, repo_dir=_CODE_DIR),
        "config_hash": run_config_hash(meta.run_id), "suite": CLASSIFIER_SUITE,
        "suite_version": None, "suite_sha256": meta.suite_sha256, "judge_model": None,
        "judge_prompt_sha256": None, "mock": False, "baseline": None, "compare_profile": None,
        "started_at": clock.format_utc(meta.started_at), "finished_at": clock.format_utc(finished),
        "passed": report.passed, "exit_code": code, **extra,
    }  # fmt: skip


def classifier_outcome(payload: EvalPayload, deps: EvalDeps) -> JobOutcome:
    """Run the classifier eval of `--suite classifier` (U11-68 step 3, F11-09)."""
    run_id, started = new_id(IdKind.RUN), deps.clock()
    build_id = current_build(deps.data_root)
    con = deps.warehouse_for(build_id)
    kind = dataset_kind(con)
    truth_dir = truth_dir_for(deps.data_root) if kind == "synthetic" else None
    truth = None if truth_dir is None else load_truth(truth_dir)
    deciders = default_deciders()
    with bind_ids(run_id=run_id):
        report = run_classifier(
            get_config().decisions.question_set_version, deciders, data_root=deps.data_root,
            warehouse=con, truth_dir=truth_dir, settings=deps.settings.classifier,
        )  # fmt: skip
    extra = _classifier_meta(report, deciders)
    meta = RunMeta(
        run_id=run_id, profile=payload.profile, depth=payload.depth, build_id=build_id,
        dataset_kind=kind, dataset_scale="full" if truth is None else truth.scale,
        suite=CLASSIFIER_SUITE, suite_version=None, suite_sha256=extra["gold_sha256"],
        mock=False, started_at=started, dataset_seed=None if truth is None else truth.seed,
    )  # fmt: skip
    insert_eval_run(meta, data_root=deps.data_root)
    out_dir = deps.data_root / "reports" / "eval" / run_id
    try:
        _finish(meta, report, extra, deps)
    except BaseException:
        mark_failed(run_id, deps.clock())
        raise
    code = EXIT_OK if report.passed else EXIT_GATE
    _log.info("eval.run.completed", run_id=run_id, passed=report.passed, exit_code=code,
              suite=CLASSIFIER_SUITE, out_dir=out_dir.name)  # fmt: skip
    return JobOutcome(
        status="done", result={"run_id": run_id, "passed": report.passed, "exit_code": code}
    )


def _finish(meta: RunMeta, report: ClassifierReport, extra: dict[str, Any], deps: EvalDeps) -> None:
    """F11-09 step 7: the report files, then the row `done` with the classifier keys."""
    from herness.eval.runner import EvalRun  # noqa: PLC0415 - runner imports this module

    finished = deps.clock()
    out_dir = deps.data_root / "reports" / "eval" / meta.run_id
    summary = _summary(meta, report, extra, finished)
    run = EvalRun(run_id=meta.run_id, out_dir=out_dir, summary=summary, passed=report.passed)
    write_report(run, out_dir, results=[], metrics=EvalMetrics(), thresholds=[], baseline=None,
                 classifier=report)  # fmt: skip

    def close(conn: Any) -> None:  # noqa: ANN401 - sqlite3.Connection
        update_run_fields(conn, meta.run_id, meta_patch=extra)
        set_run_status(conn, meta.run_id, "done", _ACTIVE, now=finished)

    run_write(close, op="eval_run_finish")
