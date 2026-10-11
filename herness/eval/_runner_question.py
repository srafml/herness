"""One question x repeat of a golden eval run (flow F11-07 steps 5-8).

Private sibling of `herness.eval.runner` (§2 module map). `run_question` runs the setup,
`resolve`, the pipeline (chat per question, reviews through the repeat's `ReviewCache`),
grading and `count_unsupported`, and returns the `QuestionResult`; it never raises for a
question-level failure: a `SuiteError` becomes `suite_error`, anything else `error`. Its
evidence reader, name index and traces come from `_runner_support`; `setup: seed_prior_run`
uses the seeder injected through EvalDeps, once per run (T11-33 note, U11-66).
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

import duckdb

from herness.core import time as clock
from herness.core.logging import get_logger
from herness.eval._runner_support import EvidenceReader, Trace, load_trace
from herness.eval.golden import (
    EvalQuestion,
    ReferenceResult,
    ResolvedQuestion,
    RulesExpected,
    SuiteError,
    resolve,
)
from herness.eval.grading import (
    GradeResult,
    UnsupportedReport,
    count_unsupported,
    grade_entities,
    grade_numeric,
    grade_rubric,
    grade_rules,
)
from herness.eval.invoke import (
    PipelineOutput,
    ReviewCache,
    exception_text,
    invoke_chat,
    invoke_review,
)
from herness.eval.results import QuestionResult, ResultsLog, ResultStatus
from herness.harness.swarm import RunRequest
from herness.store.ops import get_run

if TYPE_CHECKING:
    from herness.core.jobs import JobContext
    from herness.eval.judge import RubricJudge
    from herness.eval.truth import TruthManifest
    from herness.harness.memory import MemoryStore
    from herness.harness.pipelines.chat import ChatService
    from herness.harness.swarm import Swarm

__all__ = ["HEARTBEAT_S", "PriorRunSeeder", "QuestionEnv", "execute", "required_rubric_skipped",
           "run_question"]  # fmt: skip

SETUP_NO_DEPS: Final = "setup seed_prior_run needs EvalDeps.memory and EvalDeps.seed_prior_run"
SETUP_NO_TRUTH: Final = "setup seed_prior_run needs a synthetic build with truth"
_PLACEHOLDER_RE: Final = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*)\}")
_COUNTED: Final = {"retry": "retries", "fallback": "fallbacks", "repair": "repairs"}
_REVIEW_KINDS: Final = {"funding": "funding_review", "org": "org_review"}
_SKIPPED_JUDGE: Final = frozenset({"judge_unavailable", "judge_egress_blocked"})
HEARTBEAT_S: Final = 60.0  # well inside the supervisor stall timeout (1800 s)

_log = get_logger("eval.runner")


class PriorRunSeeder(Protocol):
    """The T6 prior-run seeding of U11-49 (`tests.support.seed_ops.seed_prior_run`)."""

    def __call__(self, memory: MemoryStore, truth: TruthManifest, *,
                 warehouse: duckdb.DuckDBPyConnection, build_id: str) -> object: ...  # fmt: skip


@dataclass
class QuestionEnv:
    """What every question of one run shares (pinned build, judge, pipelines, caches)."""

    run_id: str
    profile: str
    depth: Literal["fast", "standard", "deep"]
    build_id: str
    dataset_kind: Literal["synthetic", "real"]
    truth: TruthManifest | None
    con: duckdb.DuckDBPyConnection
    judge: RubricJudge | None
    chat: Callable[[], ChatService]
    swarm: Callable[[], Swarm]
    traces_dir: Path
    reports_root: Path
    open_session: Callable[[str], str]
    names: Mapping[str, str]
    numerals: tuple[tuple[re.Pattern[str], ...], float]
    references: dict[str, ReferenceResult] = field(default_factory=dict)
    traces: dict[str, Trace | None] = field(default_factory=dict)
    org_outputs: list[PipelineOutput] = field(default_factory=list)
    memory: MemoryStore | None = None
    seeder: PriorRunSeeder | None = None
    seeded: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _setup_lock: threading.Lock = field(default_factory=threading.Lock)

    def trace(self, run_id: str) -> Trace | None:
        """The run's trace events, read once (None when the file is missing)."""
        with self._lock:
            if run_id not in self.traces:
                self.traces[run_id] = load_trace(self.traces_dir / f"{run_id}.jsonl")
            return self.traces[run_id]


def run_question(env: QuestionEnv, question: EvalQuestion, repeat: int,
                 cache: ReviewCache) -> QuestionResult:  # fmt: skip
    """Setup, resolve, invoke, grade and count one question x repeat (F11-07 steps 5-7)."""
    base = _Base(question.id, repeat)
    try:
        if question.setup == "seed_prior_run":
            _seed_prior_run(env)
        resolved = resolve(
            question, env.truth, env.con, build_id=env.build_id,
            dataset_kind=env.dataset_kind, cache=env.references,
        )  # fmt: skip
        if resolved.skip_reason is not None:
            return base.result("skipped", error=f"skipped: {resolved.skip_reason}")
        out = _invoke(env, resolved, cache)
        if out.error is not None:
            kind = out.error.partition(":")[0]
            _log.warning("eval.question.errored", run_id=env.run_id, question_id=question.id,
                         error_type=kind)  # fmt: skip
            return _from_output(env, base.with_ref(resolved), "error", [], out, None)
        checks = _grade(env, resolved, out)
        allowed, rel_tol = env.numerals
        reader = EvidenceReader(env.con)
        unsupported = count_unsupported(
            out.final_text, out.numbers, env.build_id, evidence=reader.exists,
            rerun=reader.rerun, allowed_patterns=allowed, float_rel_tol=rel_tol,
        )  # fmt: skip
    except SuiteError as exc:
        return _suite_error(env, base, exc.message)
    except Exception as exc:  # noqa: BLE001 - question-level failures never raise (U11-66)
        _log.warning("eval.question.errored", run_id=env.run_id, question_id=question.id,
                     error_type=type(exc).__name__)  # fmt: skip
        return base.result("error", error=exception_text(exc))
    return _from_output(env, base.with_ref(resolved), _status(checks), checks, out, unsupported)


def _seed_prior_run(env: QuestionEnv) -> None:
    """Step 5 setup: plant T6 once per run (U11-49); unavailable -> `SuiteError`."""
    if env.memory is None or env.seeder is None:
        raise SuiteError(SETUP_NO_DEPS)
    if env.truth is None:
        raise SuiteError(SETUP_NO_TRUTH)
    with env._setup_lock:
        if env.seeded:
            return
        env.seeder(env.memory, env.truth, warehouse=env.con, build_id=env.build_id)
        env.seeded = True
    _log.info("eval.setup.seeded", run_id=env.run_id, setup="seed_prior_run")


@dataclass(frozen=True)
class _Base:
    qid: str
    repeat: int
    reference: dict[str, Any] = field(default_factory=dict)

    def with_ref(self, resolved: ResolvedQuestion) -> _Base:
        ref = {k: v.model_dump(mode="json") for k, v in resolved.reference.items()}
        return _Base(self.qid, self.repeat, ref)

    def result(self, status: ResultStatus, *, error: str | None) -> QuestionResult:
        return QuestionResult(
            id=self.qid, repeat=self.repeat, status=status, checks=[], reference=self.reference,
            final_text="", numbers=[], unsupported=None, tokens_in=0, tokens_out=0,
            cost_usd="0", latency_s=0.0, retries=0, fallbacks=0, repairs=0,
            pipeline_run_id=None, draft_mode=None, trace_path=None, error=error,
        )  # fmt: skip


def _suite_error(env: QuestionEnv, base: _Base, reason: str) -> QuestionResult:
    _log.warning("eval.suite_error.detected", run_id=env.run_id, question_id=base.qid,
                 reason=reason)  # fmt: skip
    return base.result("suite_error", error=f"SuiteError: {reason}")


def _invoke(env: QuestionEnv, resolved: ResolvedQuestion, cache: ReviewCache) -> PipelineOutput:
    question = resolved.question
    if question.pipeline == "chat":
        mode: Literal["cloud", "live"] = "cloud" if env.profile == "premium" else "live"  # OI-3
        return invoke_chat(
            env.chat(), resolved.text, mode=mode, now=clock.monotonic, name_index=env.names,
            open_session=env.open_session,
        )  # fmt: skip
    req = RunRequest.model_validate({
        "kind": _REVIEW_KINDS[question.pipeline], "depth": env.depth, "profile": env.profile,
        "question": resolved.text if question.framing else None, "build_id": env.build_id,
    })  # fmt: skip
    framing = question.id if question.framing else None

    def review() -> PipelineOutput:
        out = invoke_review(env.swarm(), req, job=None, now=clock.monotonic)
        if question.pipeline == "org":
            env.org_outputs.append(out)
        return out

    return cache.get_or_run(question.pipeline, env.profile, env.depth, framing, review)


def _fill(text: str, values: Mapping[str, str], *, escape: bool = False) -> str:
    def sub(match: re.Match[str]) -> str:
        value = values.get(match.group(1))
        if value is None:
            return match.group(0)
        return re.escape(value) if escape else value

    return _PLACEHOLDER_RE.sub(sub, text)


def _rules(rules: RulesExpected, values: Mapping[str, str]) -> RulesExpected:
    """`rules` with resolved placeholders filled in (escaped inside claim patterns)."""
    if not values:
        return rules
    data = rules.model_dump()
    data["must_mention"] = [
        _fill(m, values) if isinstance(m, str)
        else {"entity": _fill(m["entity"], values),
              "with_any": [_fill(p, values) for p in m["with_any"]]}
        for m in data["must_mention"]
    ]  # fmt: skip
    data["must_not_claim"] = [
        {"entity": _fill(c["entity"], values), "pattern": _fill(c["pattern"], values, escape=True)}
        for c in data["must_not_claim"]
    ]
    return RulesExpected.model_validate(data)


def _grade(env: QuestionEnv, resolved: ResolvedQuestion, out: PipelineOutput) -> list[GradeResult]:
    """Numeric, entity, rule and rubric checks of the question's expected blocks (step 7)."""
    question, ref = resolved.question, resolved.reference
    exp = question.expected
    checks: list[GradeResult] = []
    if exp.numeric is not None:
        checks.append(grade_numeric(exp.numeric, ref["numeric"], out.carriers))
    if exp.entities is not None:
        checks.append(grade_entities(exp.entities, ref["entities"], out.ranked_ids))
    if exp.rules is not None:
        rules = _rules(exp.rules, resolved.placeholders)
        checks += grade_rules(rules, out.final_text, out.verified_claims, out.numbers)
    if exp.rubric is not None:
        rubric = grade_rubric(exp.rubric, question.id, out.final_text, env.judge)
        reason = rubric.detail.get("reason")
        if rubric.status == "skipped" and reason in _SKIPPED_JUDGE:
            _log.warning("eval.judge.skipped", run_id=env.run_id, question_id=question.id,
                         reason=reason)  # fmt: skip
        checks.append(rubric)
    return checks


def _status(checks: Sequence[GradeResult]) -> ResultStatus:
    graded = [c.status for c in checks if c.status != "skipped"]
    return "skipped" if not graded else "failed" if "failed" in graded else "passed"


def required_rubric_skipped(result: QuestionResult) -> bool:
    """A rubric the run asked the judge for was skipped (judge down or blocked; U11-69)."""
    return any(
        c.check == "rubric" and c.status == "skipped" and c.detail.get("reason") in _SKIPPED_JUDGE
        for c in result.checks
    )


def _usage(run_id: str) -> tuple[int, int, str]:
    run = get_run(run_id) if run_id else None
    if run is None:
        return 0, 0, "0"
    tokens_in = tokens_out = 0
    for ledger in run.token_usage.values():
        if isinstance(ledger, Mapping):
            tokens_in += _count(ledger.get("tokens_in"))
            tokens_out += _count(ledger.get("tokens_out"))
    return tokens_in, tokens_out, str(run.cost_usd)


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _from_output(
    env: QuestionEnv, base: _Base, status: ResultStatus, checks: list[GradeResult],
    out: PipelineOutput, unsupported: UnsupportedReport | None,
) -> QuestionResult:  # fmt: skip
    run_id = out.run_id or None
    trace = env.trace(run_id) if run_id is not None else None
    counts = dict.fromkeys(_COUNTED.values(), 0)
    for event in trace or ():
        name = _COUNTED.get(str(event.get("type")))
        if name is not None:
            counts[name] += 1
    tokens_in, tokens_out, cost = _usage(out.run_id)
    trace_path = None if trace is None or run_id is None else f"traces/{run_id}.jsonl"
    return QuestionResult(
        id=base.qid, repeat=base.repeat, status=status, checks=checks, reference=base.reference,
        final_text=out.final_text, numbers=out.numbers, unsupported=unsupported,
        tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost, latency_s=out.latency_s,
        retries=counts["retries"], fallbacks=counts["fallbacks"], repairs=counts["repairs"],
        pipeline_run_id=run_id, draft_mode=out.draft_mode, trace_path=trace_path,
        error=out.error,
    )  # fmt: skip


# --- execution (F11-07 steps 5-9) -------------------------------------------------------------


def _record(env: QuestionEnv, question: EvalQuestion, repeat: int, cache: ReviewCache,
            log: ResultsLog) -> None:  # fmt: skip
    result = run_question(env, question, repeat, cache)
    log.append(result)  # a write error fails the job; resume continues later (step 8)
    _log.info("eval.question.graded", run_id=env.run_id, question_id=question.id, repeat=repeat,
              status=result.status, latency_s=result.latency_s)  # fmt: skip


@dataclass
class _Flight:
    """Questions in flight; heartbeats the job while it waits (reviews run without it)."""

    job: JobContext | None
    inflight: set[Future[None]] = field(default_factory=set)
    graded: int = 0

    def beat(self) -> None:
        if self.job is not None:
            self.job.heartbeat(f"eval graded {self.graded}")

    def settle(self, limit: int) -> None:
        """At most `limit` in flight, heartbeating every `HEARTBEAT_S`; errors propagate."""
        while len(self.inflight) > limit:
            done, self.inflight = wait(self.inflight, timeout=HEARTBEAT_S,
                                       return_when=FIRST_COMPLETED)  # fmt: skip
            for future in done:
                future.result()
            self.graded += len(done)
            self.beat()

    def drain(self) -> None:
        """After an error: wait out the rest in flight with heartbeats (results dropped)."""
        while self.inflight:
            _, self.inflight = wait(self.inflight, timeout=HEARTBEAT_S)
            self.beat()


def execute(
    env: QuestionEnv, pending: Sequence[tuple[EvalQuestion, int]],
    caches: Mapping[int, ReviewCache], log: ResultsLog, control: tuple[JobContext | None, int],
) -> Literal["yield", "cancel"] | None:  # fmt: skip
    """At most `workers` in flight; heartbeat + `should_yield` before each; stop reason or None."""
    job, workers = control
    flight = _Flight(job)
    stop: Literal["yield", "cancel"] | None = None
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="eval-q") as pool:
        try:
            for question, repeat in pending:
                flight.settle(workers - 1)
                flight.beat()
                if job is not None and job.should_yield():
                    stop = "cancel" if job.stop_reason == "cancel" else "yield"
                    break
                future = pool.submit(_record, env, question, repeat, caches[repeat], log)
                flight.inflight.add(future)
            flight.settle(0)
        except BaseException:
            flight.drain()  # the pool shutdown would otherwise wait without heartbeats
            raise
    return stop
