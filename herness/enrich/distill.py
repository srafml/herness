"""Distillation job: one initial or active round and its spec 08 handler (U03-135 ... U03-137).

Design 03 §5.8, §5.9; flows F03-13, F03-14, F03-18. `run_distill` holds
``ctx.gpu_scope("decider")`` for its body (R-43) after the config checks that raise
ConfigError; the LLM teacher adds a nested ``reasoning`` scope from teacher selection to the
teacher stop, entered and left through one ExitStack. Resumable: ``ctx.save_state`` after
each step, a rerun continues at the saved step. ``CURRENT`` is never changed (human ``laya
accept``, TH03-04). Data steps live in the private sibling `_distill_steps` (T03-32 spec
note). Reports, logs and errors carry counts and ids only (TH03-03).
"""

from __future__ import annotations

import contextlib
import dataclasses
import math
import time
from collections.abc import Callable, Mapping
from functools import partial
from typing import TYPE_CHECKING, Any, Final, Literal, Self

import pyarrow as pa
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from herness.core import time as clock
from herness.core.errors import ConfigError, ModelUnavailable, RetryableError
from herness.core.logging import get_logger
from herness.core.resilience import process_state
from herness.core.types import JobOutcome
from herness.enrich import _distill_steps as steps
from herness.enrich.calibrate import CalibrationStore
from herness.enrich.deciders import build_decider
from herness.enrich.deciders.llm import LlmDecider
from herness.enrich.evaluate import evaluate_candidate
from herness.enrich.gpu import YieldRequested, release_cuda
from herness.enrich.laya_admin import accept_model, rollback_model
from herness.enrich.review_items import create_if_absent

if TYPE_CHECKING:
    import duckdb

    from herness.core.jobs import JobContext
    from herness.enrich.decide import Decider
    from herness.enrich.deciders.llm import CompletionClient

__all__ = [
    "DistillReport",
    "LlmFactory",
    "YieldRequested",
    "accept_model",
    "make_distill_handler",
    "rollback_model",
    "run_distill",
]

# T03-28 LlmFactory (local until pipeline.py merges; identical alias)
type LlmFactory = Callable[
    [Literal["enrich_decider", "cluster_namer"]], tuple[CompletionClient, str, int]
]
# end T03-28 LlmFactory

type Step = Literal["prepared", "teacher_done", "trained", "evaluated"]
type TeacherName = Literal["openjev", "llm"]
_ROUND_KINDS: Final = ("initial", "active")
SPOT_RATE: Final = 0.01  # spot-checks: 1 % of the teacher rows, within min and max
LOW_PROB: Final = 0.7
MIN_REVIEWS: Final = 100
_SPOT_KEYS: Final = ("purpose", "question", "content_hash")  # U03-83 match keys and statuses
_SPOT_BLOCKING: Final = ("pending", "approved", "rejected")

_log = get_logger("enrich.distill")


class DistillReport(BaseModel):
    """Result of one ``distill`` run, stored by spec 08 in ``job.result`` (U03-135).

    Counts and ids only. Invariant: ``stopped`` implies ``version is None``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str | None
    round_kind: Literal["initial", "active"]
    round: int
    teacher: TeacherName | None
    teacher_version: str | None
    n_sample: int
    n_train: int
    n_val: int
    blocked_questions: list[str]
    gold_frozen_questions: list[str]
    gold_items_created: int
    accepted_proposed: list[str]
    macro_metric: float | None
    stopped: bool
    stop_reason: Literal["none", "min_gain", "max_rounds"]
    durations_s: dict[str, float]

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.stopped and self.version is not None:
            msg = "a stopped round has no version"
            raise ValueError(msg)
        return self


class _State(BaseModel):
    """Saved as ``{"distill": ...}`` after each step (U03-136). Spec note (T03-32): besides
    version, step, teacher and round it keeps what later steps and the report need."""

    model_config = ConfigDict(extra="forbid")

    version: str
    step: Step
    teacher: TeacherName | None = None
    round: int
    parent: str | None = None
    teacher_version: str | None = None
    n_sample: int = 0
    blocked: list[str] = []
    gold_items_created: int = 0
    n_train: int = 0
    n_val: int = 0
    accepted_proposed: list[str] = []
    macro_metric: float | None = None
    durations: dict[str, float] = {}


@dataclasses.dataclass(frozen=True)
class _Teacher:
    name: TeacherName
    decider: Decider


def _load_state(ctx: JobContext) -> _State | None:
    saved = ctx.load_state().get("distill")
    try:
        return None if saved is None else _State.model_validate(saved)
    except ValidationError as exc:
        msg = "distill: saved job state is invalid"
        raise ConfigError(msg) from exc


def _advance(ctx: JobContext, state: _State, step: Step, started: float) -> None:
    """Record ``step``: duration, ``save_state`` and ``enrich.distill.step_completed``."""
    elapsed = round(time.monotonic() - started, 3)
    state.step, state.durations[step] = step, elapsed
    ctx.save_state({"distill": state.model_dump(mode="json")})
    _log.info("enrich.distill.step_completed", version=state.version, step=step,
              duration_s=elapsed)  # fmt: skip


def _off_network(client: CompletionClient) -> bool:
    """The chain registry's ``off_network`` of the client; unknown -> False (scope entered)."""
    chains = process_state().chains
    try:
        return chains is not None and bool(chains.config(client.name).off_network)
    except (ConfigError, KeyError):
        return False


def _select_teacher(
    run: steps.Run, ctx: JobContext, stack: contextlib.ExitStack[bool | None],
    llm: Callable[[], tuple[CompletionClient, str, int]] | None,
) -> _Teacher:  # fmt: skip
    """Step 3: OpenJev when enabled, started and healthy (stopped when ``stack`` closes);
    else the LLM (3 votes) in a nested ``reasoning`` scope unless off-network."""
    deciders, reason = run.cfg.models.deciders, "disabled"
    if deciders.openjev.enabled:
        stack.callback(ctx.services.stop, "openjev")
        try:
            ctx.services.start("openjev")
            decider = build_decider("openjev", cfg=run.cfg, depth="standard", paths=run.paths,
                                    llm=None, samples_override=steps.SAMPLES)  # fmt: skip
            decider.health()
        except RetryableError:
            reason = "unavailable"
            stack.close()  # stops OpenJev before the LLM needs the GPU
        else:
            return _selected("openjev", decider, "enabled")
    if llm is None:
        msg = "no distillation teacher: openjev unavailable and no llm client"
        raise ModelUnavailable(msg)
    client, version, concurrency = llm()
    if not _off_network(client):
        stack.enter_context(ctx.gpu_scope("reasoning"))
    temperature = deciders.llm.temperature
    teacher = LlmDecider(client, version=version, votes=steps.SAMPLES, temperature=temperature,
                         max_concurrency=concurrency)  # fmt: skip
    return _selected("llm", teacher, reason)


def _selected(name: TeacherName, decider: Decider, reason: str) -> _Teacher:
    _log.info("enrich.distill.teacher_selected", teacher=name,
              teacher_version=decider.version, reason=reason)  # fmt: skip
    return _Teacher(name, decider)


def _prob(row: Mapping[str, Any]) -> float:
    return float(dict(row["distribution"]).get(row["answer"], 0.0))


def _spot_checks(run: steps.Run, rows: pa.Table) -> None:
    """Step 6: per question ``max(min, min(max, 1 %))`` teacher rows, half uniform (hash
    order), half with teacher probability < 0.7, as ``label_check`` items (U03-148)."""
    distill = run.cfg.decisions.distill
    for q in run.qs.questions:
        mine = sorted((r for r in rows.to_pylist() if r["question"] == q.id),
                      key=lambda r: r["content_hash"])  # fmt: skip
        floor = math.floor(SPOT_RATE * len(mine))
        n = min(len(mine), max(distill.spot_check_min, min(distill.spot_check_max, floor)))
        chosen, rest = mine[: n // 2], mine[n // 2 :]
        low = [r for r in rest if _prob(r) < LOW_PROB][: n - len(chosen)]
        taken = {r["content_hash"] for r in low}
        fill = [r for r in rest if r["content_hash"] not in taken]
        chosen += low + fill[: n - len(chosen) - len(low)]
        payloads = [{
            "record_id": r["record_id"], "content_hash": r["content_hash"], "question": q.id,
            "question_fingerprint": q.fingerprint, "question_set_version": run.qs.version,
            "answer": r["answer"], "probability": _prob(r), "decider": r["decider"],
            "decider_version": r["decider_version"], "purpose": "spot_check",
            "text_ref": "enrich.text_redacted",
        } for r in chosen]  # fmt: skip
        if payloads:
            made, _ = create_if_absent("label_check", payloads, match_keys=_SPOT_KEYS,
                                       blocking_statuses=_SPOT_BLOCKING,
                                       scope={"question_set_version": run.qs.version},
                                       now=clock.now())  # fmt: skip
            _log.info("enrich.spot_check.created", question=q.id, count=made, purpose="spot_check")


def _blocked(run: steps.Run) -> list[str]:
    """Questions whose reviewed disagreement with the teacher exceeds ``block_disagreement``
    with at least 100 reviews (step 6)."""
    teacher, limit = steps.latest_teacher(run.store), run.cfg.decisions.distill.block_disagreement
    human = run.store.latest_human()
    names = ("content_hash", "question", "question_fingerprint", "answer")
    reviews: dict[str, list[bool]] = {}
    for h, qid, fp, answer in zip(*(human.column(n).to_pylist() for n in names), strict=True):
        if (h, qid, fp) in teacher:
            reviews.setdefault(qid, []).append(teacher[(h, qid, fp)] != answer)
    blocked = []
    for q in run.qs.questions:
        seen = reviews.get(q.id, [])
        rate = sum(seen) / len(seen) if seen else 0.0
        if len(seen) >= MIN_REVIEWS and rate > limit:
            _log.warning("enrich.distill.question_blocked", question=q.id,
                         disagreement=round(rate, 4), reviews=len(seen))  # fmt: skip
            blocked.append(q.id)
    return blocked


def _teacher_phase(
    run: steps.Run, ctx: JobContext, wh: duckdb.DuckDBPyConnection, state: _State,
    llm_factory: LlmFactory | None,
) -> None:  # fmt: skip
    """Steps 3-8: teacher, sample, labels, spot-checks, gold requests, teacher stop."""
    started = time.monotonic()
    excluded = steps.gold_exclusion(run)
    ranked = steps.active_ranked(run, ctx, wh, excluded) if run.round_kind == "active" else None
    llm = None if llm_factory is None else partial(llm_factory, "enrich_decider")
    with contextlib.ExitStack() as stack:  # closing it is step 8: stop openjev / leave reasoning
        teacher = _select_teacher(run, ctx, stack, llm)
        if ranked is None:
            sample = steps.initial_sample(run, wh, teacher.name, excluded, state.version)
        else:
            active = run.cfg.decisions.distill.active
            size = active.per_round if teacher.name == "openjev" else active.per_round_llm_teacher
            sample = ranked.slice(0, size)
        records = pa.concat_tables([sample.select(["record_id", "entity", "content_hash", "text"]),
                                    steps.gold_records(run, wh)])  # fmt: skip
        steps.label(run, ctx, teacher.decider, records, samples=steps.SAMPLES, stage="teacher")
        version = teacher.decider.version
        _spot_checks(
            run, steps.append_teacher_rows(run, (teacher.name, version), sample, state.round)
        )
        state.blocked = _blocked(run)
        state.gold_items_created = steps.request_gold_items(run, wh)
    release_cuda()
    state.teacher, state.teacher_version, state.n_sample = teacher.name, version, sample.num_rows
    _advance(ctx, state, "teacher_done", started)


def _evaluate_phase(
    run: steps.Run, ctx: JobContext, wh: duckdb.DuckDBPyConnection, state: _State
) -> None:
    """Steps 11-12: candidate inference on gold, `evaluate_candidate` (eval.json, calibration)."""
    started = time.monotonic()
    steps.infer_gold(run, ctx, wh, state.version)
    doc = evaluate_candidate(
        version=state.version, qs=run.qs, cfg=run.cfg.decisions, store=run.store,
        cache=run.cache, calibration=CalibrationStore(run.paths),
        teacher=(str(state.teacher), str(state.teacher_version)), paths=run.paths,
        now=clock.now(), blocked=frozenset(state.blocked),
    )  # fmt: skip
    entries = doc["questions"]
    assert isinstance(entries, dict)  # noqa: S101 - evaluate_candidate's document shape
    state.accepted_proposed = sorted(q for q, e in entries.items() if e["accepted_proposed"])
    macro = doc["macro_metric"]
    state.macro_metric = float(macro) if isinstance(macro, int | float) else None
    _advance(ctx, state, "evaluated", started)


def run_distill(
    *,
    round_kind: Literal["initial", "active"],
    ctx: JobContext,
    llm_factory: LlmFactory | None = None,
) -> DistillReport:
    """One distillation round (U03-136, flows F03-13 and F03-14).

    Leaves a candidate version directory (weights, ``manifest.json`` with ``status =
    "candidate"``, ``calibration.json``, ``eval.json``) or a stopped report; never changes
    ``CURRENT``. Raises ConfigError (config, before GPU work), ModelUnavailable (no teacher;
    retried by spec 08), YieldRequested (after a checkpoint) and store errors.
    """
    run = steps.load_run(round_kind)
    with ctx.gpu_scope("decider"), steps.warehouse() as wh:
        started = time.monotonic()
        frozen = steps.refresh_gold(run)
        state = _load_state(ctx)
        if state is None:
            round_no, parent, reason = steps.plan_round(run)
            if reason != "none":
                _log.info("enrich.distill.stopped", round=round_no, stop_reason=reason)
                durations = {"prepared": round(time.monotonic() - started, 3)}
                stopped = _State(version="", step="prepared", round=round_no, durations=durations)
                return _report(run, stopped, frozen, reason)
            state = _State(version=steps.create_version(run), step="prepared", round=round_no,
                           parent=parent)  # fmt: skip
            _advance(ctx, state, "prepared", started)
        if state.step == "prepared":
            _teacher_phase(run, ctx, wh, state, llm_factory)
        if state.step == "teacher_done":
            started = time.monotonic()
            plan = state.model_dump()
            state.n_train, state.n_val = steps.train_candidate(run, ctx, wh, plan)
            _advance(ctx, state, "trained", started)
        if state.step == "trained":
            _evaluate_phase(run, ctx, wh, state)
        return _report(run, state, frozen, "none")


def _report(run: steps.Run, state: _State, frozen: list[str],
            reason: Literal["none", "min_gain", "max_rounds"]) -> DistillReport:  # fmt: skip
    return DistillReport(
        version=None if reason != "none" else state.version, round_kind=run.round_kind,
        round=state.round, teacher=state.teacher, teacher_version=state.teacher_version,
        n_sample=state.n_sample, n_train=state.n_train, n_val=state.n_val,
        blocked_questions=state.blocked, gold_frozen_questions=frozen,
        gold_items_created=state.gold_items_created,
        accepted_proposed=state.accepted_proposed, macro_metric=state.macro_metric,
        stopped=reason != "none", stop_reason=reason, durations_s=dict(state.durations),
    )  # fmt: skip


def make_distill_handler(llm_factory: LlmFactory | None) -> Callable[[JobContext], JobOutcome]:
    """The spec 08 handler of job kind ``distill`` (U03-137; one argument, R-42).

    Reads ``ctx.job.payload["round_kind"]`` (default ``"initial"``; another value is a
    ConfigError) and returns ``done`` with the report as result, or ``yield`` on
    YieldRequested; HernessErrors propagate to spec 08. The composition root registers it.
    """

    def handle(ctx: JobContext) -> JobOutcome:
        round_kind = ctx.job.payload.get("round_kind", "initial")
        if round_kind not in _ROUND_KINDS:
            msg = "distill payload round_kind must be initial or active"
            raise ConfigError(msg)
        kind: Literal["initial", "active"] = "active" if round_kind == "active" else "initial"
        try:
            report = run_distill(round_kind=kind, ctx=ctx, llm_factory=llm_factory)
        except YieldRequested:
            return JobOutcome(status="yield")
        return JobOutcome(status="done", result=report.model_dump(mode="json"))

    return handle
