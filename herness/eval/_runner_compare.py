"""`--compare-runs` and `--compare PROFILE` of the `eval` job (U11-68 steps 2 and 4, T11-38).

Private sibling of `herness.eval.runner` (§2 module map). `compare_runs_outcome` only
compares stored runs (`herness.eval.report.compare`): the job is `done` with the newest run
id, `passed` unless a harness defect was found. `run_eval` runs the golden suite once, or for
`--compare` twice: under the profile, then under the compare profile on the same pinned
build (same deps; a moved `CURRENT` is refused), then compares the two runs, writes the
comparison into the newer run and re-renders its report with the comparison section. The
job state keeps both run ids (`eval_run_id`, `compare_run_id`), so a yield or retry resumes
each run from its `results.jsonl`; a primary run that already finished is read back from its
`summary.json` instead of being finished again. A resumed run keeps the build pinned in its
`run` row, and the two runs' build ids are checked equal before they are compared.
The combined verdict (both runs passed, no harness defect) and `compare_run_id` exist only in
the job outcome: each run's own `summary.json` and report keep that run's own verdict.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from herness.core.errors import ConfigError
from herness.core.types import JobOutcome
from herness.eval._report_compare import load_summary, run_dir
from herness.eval._runner_support import current_build
from herness.eval.metrics import EvalMetrics, ThresholdResult
from herness.eval.report import ComparisonTable, compare, load_baseline, write_report
from herness.eval.results import ResultsLog

if TYPE_CHECKING:
    from pydantic import JsonValue

    from herness.core.jobs import JobContext
    from herness.core.jobs.ports import JobRow, ServiceControl, StopReason
    from herness.core.types import GpuClass, JobKind
    from herness.eval._runner_payload import EvalPayload
    from herness.eval.golden import Suite
    from herness.eval.runner import EvalDeps, EvalOptions, EvalRun

__all__ = ["COMPARE_STATE_KEY", "compare_runs_outcome", "run_eval"]

COMPARE_STATE_KEY: Final = "compare_run_id"
_RUN_STATE_KEY: Final = "eval_run_id"

type Golden = Callable[..., EvalRun]


def _code(*, passed: bool) -> int:
    return 0 if passed else 14


def compare_runs_outcome(run_ids: Sequence[str], deps: EvalDeps) -> JobOutcome:
    """U11-68 step 2: compare the stored runs only; exit 14 on a harness defect."""
    table = compare(run_ids, data_root=deps.data_root)
    passed = not table.harness_defects
    result: dict[str, JsonValue] = {
        "run_id": table.newest_run_id, "passed": passed, "exit_code": _code(passed=passed),
    }  # fmt: skip
    return JobOutcome(status="done", result=result)


class _CompareJob:
    """The job context of the compare run: its run id goes to `compare_run_id` in the job
    state, next to the primary run's `eval_run_id`; all else is the real context's."""

    def __init__(self, inner: JobContext) -> None:
        self._inner = inner

    @property
    def job(self) -> JobRow:
        return self._inner.job

    @property
    def job_id(self) -> str:
        return self._inner.job_id

    @property
    def kind(self) -> JobKind:
        return self._inner.kind

    @property
    def attempt(self) -> int:
        return self._inner.attempt

    @property
    def services(self) -> ServiceControl:
        return self._inner.services

    @property
    def stop_reason(self) -> StopReason | None:
        return self._inner.stop_reason

    def should_yield(self) -> bool:
        return self._inner.should_yield()

    def heartbeat(self, note: str | None = None) -> None:
        self._inner.heartbeat(note)

    def require_gpu_class(self, cls: GpuClass, *, timeout_s: float | None = None) -> None:
        self._inner.require_gpu_class(cls, timeout_s=timeout_s)

    def gpu_scope(self, cls: GpuClass) -> AbstractContextManager[None]:
        return self._inner.gpu_scope(cls)

    def save_state(self, state: dict[str, JsonValue]) -> None:
        merged = self._inner.load_state() | {COMPARE_STATE_KEY: state.get(_RUN_STATE_KEY)}
        self._inner.save_state(merged)

    def load_state(self) -> dict[str, JsonValue]:
        return self._inner.load_state()


@dataclasses.dataclass(frozen=True)
class _Primary:
    """The primary run as far as the comparison needs it."""

    run_id: str
    out_dir: Path
    summary: dict[str, Any]
    passed: bool


def _compare_id(deps: EvalDeps) -> str | None:
    value = deps.job.load_state().get(COMPARE_STATE_KEY) if deps.job is not None else None
    return value if isinstance(value, str) else None


def _finished(run_id: str | None, deps: EvalDeps) -> _Primary | None:
    """The primary run read back from its `summary.json` when it already finished."""
    if run_id is None:
        return None
    out = run_dir(run_id, deps.data_root)  # `run_<ulid>` checked before the path is joined
    if not (out / "summary.json").is_file():
        return None
    summary = load_summary(run_id, deps.data_root)
    return _Primary(run_id, out, summary, summary.get("passed") is True)


def run_eval(
    golden: Golden, suite: Suite, payload: EvalPayload, *, resume_run_id: str | None,
    deps: EvalDeps, options: EvalOptions,
) -> EvalRun:  # fmt: skip
    """U11-68 step 4: `golden` (`run_golden`) once, or twice and compared for `--compare`.
    For `--compare` the returned run is the primary's, with the combined `passed` and the
    compare run's id in its summary (`compare_run_id`)."""

    def run(profile: str, run_id: str | None, d: EvalDeps, o: EvalOptions) -> EvalRun:
        return golden(
            suite, profile, payload.depth, repeat=payload.repeat, judge=not payload.no_judge,
            resume_run_id=run_id, deps=d, options=o,
        )  # fmt: skip

    if payload.compare_profile is None:
        return run(payload.profile, resume_run_id, deps, options)
    second_id = _compare_id(deps)
    primary = _finished(resume_run_id, deps) if second_id is not None else None
    if primary is None:
        first = run(payload.profile, resume_run_id, deps, options)
        if first.yielded or first.canceled:
            return first
        primary = _Primary(first.run_id, first.out_dir, first.summary, first.passed)
    if second_id is None and current_build(deps.data_root) != primary.summary.get("build_id"):
        msg = "the current build changed after the primary run; --compare needs the same build"
        raise ConfigError(msg)
    job = _CompareJob(deps.job) if deps.job is not None else None
    second = run(
        payload.compare_profile, second_id, dataclasses.replace(deps, job=job),
        dataclasses.replace(options, baseline=None, set_baseline=None, compare_profile=None),
    )  # fmt: skip
    if second.yielded or second.canceled:
        return second.model_copy(update={"run_id": primary.run_id, "out_dir": primary.out_dir})
    if second.summary.get("build_id") != primary.summary.get("build_id"):
        msg = "the compared runs are on different builds; --compare needs the same build"
        raise ConfigError(msg)
    table = compare([primary.run_id, second.run_id], data_root=deps.data_root)
    _rerender(second, table, deps)
    passed = primary.passed and second.passed and not table.harness_defects
    summary = primary.summary | {"exit_code": _code(passed=passed),
                                 COMPARE_STATE_KEY: second.run_id}  # fmt: skip
    update = {"run_id": primary.run_id, "out_dir": primary.out_dir, "summary": summary,
              "passed": passed}  # fmt: skip
    return second.model_copy(update=update)


def _rerender(run: EvalRun, table: ComparisonTable, deps: EvalDeps) -> None:
    """Write `run`'s report again from what it stored, now with the comparison section."""
    summary = run.summary
    metrics = EvalMetrics.model_validate(summary["metrics"])
    rows = [ThresholdResult.model_validate(row) for row in summary["thresholds"]]
    log = ResultsLog(run.out_dir / "results.jsonl")
    results = [r for r in log.read_all() if r.id in metrics.question_status]
    name = summary.get("baseline")
    baseline = None
    if isinstance(name, str):
        baseline = load_baseline(name, repo_root=deps.repo_root or Path.cwd(),
                                 suite_sha256=str(summary.get("suite_sha256")))  # fmt: skip
    write_report(run, run.out_dir, results=results, metrics=metrics, thresholds=rows,
                 baseline=baseline, classifier=None, comparison=table)  # fmt: skip
