"""Golden eval run, the `eval` job handler, the CLI payload and exit code (U11-66, U11-68,
U11-69; flows F11-07 and F11-10).

`run_golden` pins the `CURRENT` build, inserts (or, on resume, reopens) the ops `run` row,
runs every question x repeat not yet in `results.jsonl` (sequentially, or up to 8 at a time
on `premium`, TH11-13), polls `should_yield` between questions, then aggregates metrics,
checks thresholds, writes `summary.json` and closes the row. `handle_eval` is the one-argument
job handler (R-42) over the deps factory bound by the composition root with
`set_deps_factory` (R-04). The payload model, GPU class and exit code live in the private
sibling `_runner_payload`, the per-question step in `_runner_question` and the run end in
`_runner_finish` (§2 module map); this module re-exports their public names.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, NoReturn, cast

import duckdb
from pydantic import BaseModel, ConfigDict

from herness.core.errors import ConfigError
from herness.core.ids import IdKind, new_id
from herness.core.jobs import register_handler
from herness.core.logging import get_logger
from herness.core.types import Depth, JobOutcome
from herness.eval._runner_finish import (
    RunMeta,
    finish_run,
    insert_eval_run,
    mark_failed,
    reopen_run,
    suite_label,
)
from herness.eval._runner_payload import (
    CLASSIFIER_SUITE,
    EvalPayload,
    build_eval_payload,
    eval_gpu_class,
    eval_idem_key,
    exit_code,
    validate_payload,
)
from herness.eval._runner_question import QuestionEnv, run_question
from herness.eval._runner_support import (
    current_build,
    dataset_kind,
    name_index,
    numerals,
    open_session_for,
)
from herness.eval.golden import EvalQuestion, Suite, load_suite
from herness.eval.invoke import ReviewCache
from herness.eval.results import ResultsLog
from herness.eval.scripted import load_scripts
from herness.eval.truth import load_truth, truth_dir_for

if TYPE_CHECKING:
    from datetime import datetime

    from herness.core.jobs import JobContext
    from herness.eval.judge import RubricJudge
    from herness.eval.settings import EvalConfig
    from herness.harness.memory import MemoryStore
    from herness.harness.pipelines.chat import ChatService
    from herness.harness.swarm import Swarm

__all__ = [
    "EvalDeps", "EvalOptions", "EvalPayload", "EvalRun", "build_eval_payload",
    "eval_gpu_class", "eval_idem_key", "exit_code", "handle_eval", "register_handlers",
    "run_golden", "set_deps_factory",
]  # fmt: skip

PREMIUM_WORKERS: Final = 8  # TH11-13
MAX_REPEAT: Final = 10
_NOT_BUILT: Final = "{feature} is not available until {card}"

_log = get_logger("eval.runner")


@dataclass(frozen=True)
class EvalDeps:
    """Collaborators of one eval run (U11-66); the ops store is reached through module
    functions (R-10). `open_session` is derived from it (`clock`), not a field."""

    warehouse_for: Callable[[str], duckdb.DuckDBPyConnection]
    swarm_for: Callable[[str], Swarm]
    chat_for: Callable[[str], ChatService]
    judge_for: Callable[[], RubricJudge | None]
    memory: MemoryStore | None
    job: JobContext | None
    clock: Callable[[], datetime]
    data_root: Path
    settings: EvalConfig


@dataclass(frozen=True)
class EvalOptions:
    """Question filters and run options (U11-66); `suite` names the suite in `run.meta`."""

    ids: frozenset[str] = frozenset()
    tags: frozenset[str] = frozenset()
    mock: bool = False
    baseline: str | None = None
    set_baseline: str | None = None
    compare_profile: str | None = None
    suite: str = "golden"


class EvalRun(BaseModel):
    """One golden run's identity and outcome (U11-66); `yielded` when it stopped early."""

    model_config = ConfigDict(frozen=True)
    run_id: str
    out_dir: Path
    summary: dict[str, Any]
    passed: bool
    yielded: bool = False


def _not_built(feature: str, card: str) -> NoReturn:
    msg = _NOT_BUILT.format(feature=feature, card=card)
    raise ConfigError(msg)


def _check(repeat: int, options: EvalOptions) -> None:
    if not 1 <= repeat <= MAX_REPEAT:
        msg = f"repeat must be 1..{MAX_REPEAT}"
        raise ConfigError(msg)
    if options.baseline is not None or options.set_baseline is not None:
        _not_built("baseline comparison", "T11-31")
    if options.compare_profile is not None:
        _not_built("profile comparison", "T11-38")


def _select(suite: Suite, options: EvalOptions) -> list[EvalQuestion]:
    """The suite's questions filtered by `--ids` and `--tags`; unknown ids → `ConfigError`."""
    known = {q.id for q in suite.questions}
    unknown = sorted(options.ids - known)
    if unknown:
        msg = f"unknown question ids: {', '.join(unknown)}"
        raise ConfigError(msg)
    return [
        q for q in suite.questions
        if (not options.ids or q.id in options.ids)
        and (not options.tags or options.tags & set(q.tags))
    ]  # fmt: skip


def _lazy[T](make: Callable[[], T]) -> Callable[[], T]:
    """`make()` once, on first use, from any thread."""
    lock = threading.Lock()
    box: list[T] = []

    def get() -> T:
        with lock:
            if not box:
                box.append(make())
            return box[0]

    return get


@dataclass(frozen=True)
class _Plan:
    """The run's identity and pinned build (F11-07 steps 2-3, F11-10)."""

    run_id: str
    build_id: str
    started_at: datetime
    resumed: bool


def _plan(deps: EvalDeps, resume_run_id: str | None) -> _Plan:
    if resume_run_id is None:
        return _Plan(new_id(IdKind.RUN), current_build(deps.data_root), deps.clock(), False)
    if not (deps.data_root / "reports" / "eval" / resume_run_id).is_dir():
        msg = f"eval run directory not found: run_id={resume_run_id}"
        raise ConfigError(msg)
    row = reopen_run(resume_run_id, deps.clock())
    return _Plan(resume_run_id, str(row.build_id), row.started_at, True)


def run_golden(  # noqa: PLR0913 - U11-66 signature (DD11-10 keyword-only deps and options)
    suite: Suite,
    profile: str,
    depth: str,
    *,
    repeat: int = 1,
    judge: bool = True,
    resume_run_id: str | None = None,
    deps: EvalDeps,
    options: EvalOptions,
) -> EvalRun:
    """Run the golden suite on the pinned build (flow F11-07; resume F11-10)."""
    _check(repeat, options)
    if depth not in ("fast", "standard", "deep"):
        msg = "depth must be fast, standard or deep"
        raise ConfigError(msg)
    selected = _select(suite, options)
    plan = _plan(deps, resume_run_id)
    con = deps.warehouse_for(plan.build_id)
    kind = dataset_kind(con)
    truth = load_truth(truth_dir_for(deps.data_root)) if kind == "synthetic" else None
    meta = RunMeta(
        run_id=plan.run_id, profile=profile, depth=depth, build_id=plan.build_id,
        dataset_kind=kind, dataset_scale=truth.scale if truth is not None else "full",
        suite=options.suite, suite_version=suite.version, suite_sha256=suite.sha256,
        mock=options.mock, started_at=plan.started_at,
    )  # fmt: skip
    if not plan.resumed:
        job_id = deps.job.job_id if deps.job is not None else None
        insert_eval_run(meta, data_root=deps.data_root)
        if deps.job is not None:
            deps.job.save_state({"eval_run_id": plan.run_id})  # a retried job resumes the run
        _log.info("eval.run.started", run_id=plan.run_id, job_id=job_id,
                  suite=suite_label(options.suite),
                  profile=profile, depth=depth, build_id=plan.build_id, dataset_kind=kind,
                  mock=options.mock, n_questions=len(selected))  # fmt: skip
    env = QuestionEnv(
        run_id=plan.run_id, profile=profile, depth=cast("Depth", depth),
        build_id=plan.build_id, dataset_kind=kind, truth=truth, con=con,
        judge=deps.judge_for() if judge else None, chat=_lazy(lambda: deps.chat_for(profile)),
        swarm=_lazy(lambda: deps.swarm_for(profile)),
        traces_dir=deps.data_root / "traces", open_session=open_session_for(deps.clock),
        names=name_index(con), numerals=numerals(),
    )  # fmt: skip
    out_dir = deps.data_root / "reports" / "eval" / plan.run_id
    try:
        return _run(env, meta, selected, repeat, deps, out_dir)
    except BaseException:
        mark_failed(plan.run_id, deps.clock())
        raise


def _run(
    env: QuestionEnv, meta: RunMeta, selected: Sequence[EvalQuestion], repeat: int,
    deps: EvalDeps, out_dir: Path,
) -> EvalRun:  # fmt: skip
    log = ResultsLog(out_dir / "results.jsonl")
    done = log.done_keys()
    pending = [(q, r) for r in range(1, repeat + 1) for q in selected if (q.id, r) not in done]
    if done:
        _log.info("eval.run.resumed", run_id=meta.run_id, done=len(done), remaining=len(pending))
    caches = {r: ReviewCache() for r in range(1, repeat + 1)}  # the cache key has no repeat
    workers = PREMIUM_WORKERS if meta.profile == "premium" else 1
    if _execute(env, pending, caches, log, (deps.job, workers)):
        return EvalRun(run_id=meta.run_id, out_dir=out_dir, summary={}, passed=False, yielded=True)
    wanted = {q.id for q in selected}
    results = [r for r in log.read_all() if r.id in wanted and r.repeat <= repeat]
    questions = {q.id: q for q in selected}
    outcome = finish_run(meta, env, results, questions=questions,
                         thresholds=deps.settings.thresholds, out_dir=out_dir)  # fmt: skip
    return EvalRun(run_id=meta.run_id, out_dir=out_dir, summary=outcome.summary,
                   passed=outcome.passed)  # fmt: skip


def _record(env: QuestionEnv, question: EvalQuestion, repeat: int, cache: ReviewCache,
            log: ResultsLog) -> None:  # fmt: skip
    result = run_question(env, question, repeat, cache)
    log.append(result)  # a write error fails the job; resume continues later (step 8)
    _log.info("eval.question.graded", run_id=env.run_id, question_id=question.id, repeat=repeat,
              status=result.status, latency_s=result.latency_s)  # fmt: skip


def _execute(
    env: QuestionEnv,
    pending: Sequence[tuple[EvalQuestion, int]],
    caches: dict[int, ReviewCache],
    log: ResultsLog,
    control: tuple[JobContext | None, int],
) -> bool:
    """Run `pending` with at most `workers` in flight; poll `should_yield` before each
    question (step 9). True when the job asked to yield."""
    job, workers = control
    inflight: set[Future[None]] = set()
    yielded = False
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="eval-q") as pool:
        for question, repeat in pending:
            while len(inflight) >= workers:
                finished, inflight = wait(inflight, return_when=FIRST_COMPLETED)
                for future in finished:
                    future.result()
            if job is not None and job.should_yield():
                yielded = True
                break
            inflight.add(pool.submit(_record, env, question, repeat, caches[repeat], log))
        for future in wait(inflight).done:
            future.result()
    return yielded


# --- U11-68 handle_eval, set_deps_factory -----------------------------------------------------


class _Binding:
    factory: Callable[[JobContext], EvalDeps] | None = None


def set_deps_factory(factory: Callable[[JobContext], EvalDeps]) -> None:
    """Bind the deps factory the composition root builds (R-04); called once at start-up."""
    _Binding.factory = factory


def register_handlers() -> None:
    """Register `handle_eval` for `eval` jobs; idempotent (the same object again is a no-op
    in `register_handler`, U08-55)."""
    register_handler("eval", handle_eval)


def _refuse_unbuilt(payload: EvalPayload) -> None:
    if payload.compare_runs:
        _not_built("run comparison", "T11-38")
    if payload.suite == CLASSIFIER_SUITE:
        _not_built("classifier eval", "T11-35")


def _suite_path(name: str, settings: EvalConfig) -> Path:
    return Path(settings.suite if name == "golden" else name)


def handle_eval(ctx: JobContext) -> JobOutcome:
    """The `eval` job handler (U11-68): payload from `ctx.job.payload` (R-42)."""
    factory = _Binding.factory
    if factory is None:
        msg = "eval dependencies not bound"
        raise ConfigError(msg)
    run_id: str | None = None
    try:
        payload = validate_payload(ctx.job.payload)
        _refuse_unbuilt(payload)
        deps = factory(ctx)
        if payload.mock_llm_dir is not None:
            load_scripts(Path(payload.mock_llm_dir))  # the factory wraps the registries
        suite = load_suite(_suite_path(payload.suite, deps.settings))
        state = ctx.load_state().get("eval_run_id")
        run_id = payload.resume_run_id or (state if isinstance(state, str) else None)
        options = EvalOptions(
            ids=frozenset(payload.ids), tags=frozenset(payload.tags),
            mock=payload.mock_llm_dir is not None, baseline=payload.baseline,
            set_baseline=payload.set_baseline, compare_profile=payload.compare_profile,
            suite=payload.suite,
        )  # fmt: skip
        run = run_golden(
            suite, payload.profile, payload.depth, repeat=payload.repeat,
            judge=not payload.no_judge, resume_run_id=run_id, deps=deps, options=options,
        )  # fmt: skip
    except ConfigError:
        _log.error("eval.run.failed", run_id=run_id, error_type="ConfigError")
        raise
    if run.yielded:
        return JobOutcome(status="yield", result={"run_id": run.run_id})
    code = run.summary.get("exit_code")
    return JobOutcome(
        status="done",
        result={
            "run_id": run.run_id,
            "passed": run.passed,
            "exit_code": code if isinstance(code, int) else 1,
        },
    )
