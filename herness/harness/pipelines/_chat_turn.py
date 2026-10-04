"""One chat turn on its worker event loop (impl 06 U06-129 steps 5-6); private to `chat`.

Private sibling of `herness.harness.pipelines.chat` (T06-25 split, with `_chat_rows`).
`run_turn` creates the chat run and task, runs the `chat` role through spec 05 `run_agent`,
escalates on a budget stop, streams and verifies the answer, writes the reply row and saves
the session turn after the answer (R-32). Process tools are observed through the trace
`tool_call` event because spec 05 accepts only spec 06 tools as task tools (TH05-22); the
spec 06 tools are `ObservedTool` wrappers. Both share the turn's evidence `seen` set.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from functools import partial
from typing import TYPE_CHECKING, Final, cast

from pydantic import JsonValue

from herness.core import time as clock
from herness.core.errors import (
    BudgetExceeded,
    ConfigError,
    EgressBlocked,
    HernessError,
    ModelUnavailable,
    NotFound,
    QueryError,
)  # fmt: skip
from herness.core.logging import get_logger
from herness.core.types import (
    ChatAnswer,
    CorrectionCapturedEvent,
    EscalatedEvent,
    EvidenceEvent,
    FinalEvent,
    MemoryRunContext,
    SqlLimits,
    TaskBudget,
    TokenEvent,
    ToolContext,
    ToolEvent,
    VerificationEvent,
    VerificationResult,
)  # fmt: skip
from herness.harness.blackboard import Blackboard
from herness.harness.budget import RunBudget
from herness.harness.findings import EntityCatalog
from herness.harness.gates import build_gates
from herness.harness.llm.base import egress_purpose_for
from herness.harness.loop import HarnessHooks, run_agent
from herness.harness.pipelines._chat_rows import (
    Turn,
    TurnEnv,
    TurnStatus,
    cleanup,
    create_run,
    finish,
    model_role,
    render,
)  # fmt: skip
from herness.harness.pipelines.chat_support import (
    CHAT_TOOLS,
    EGRESS_NOTICE,
    NO_VERIFIED_ANSWER,
    ObservedTool,
    chunk_text,
    has_review_intent,
    trim_failing_claims,
)  # fmt: skip
from herness.harness.roles.base import get_role
from herness.harness.swarm.escalation import escalate_to_review
from herness.harness.swarm.routing import build_task_input
from herness.harness.swarm.tools import EscalateTool, ListFindingsTool

if TYPE_CHECKING:
    from herness.core.types import AsyncTool, Tool, TraceEmitter
    from herness.harness.llm.settings import ClientConfig

__all__ = ["TURN_GRACE_S", "Turn", "TurnEnv", "run_turn"]

type _Output = tuple[dict[str, JsonValue] | None, str]

TURN_GRACE_S: Final = 30  # backstop over the loop's own wall clock (as U06-93)
ESCALATE_STOPS: Final = frozenset({"budget", "task_tokens", "task_budget", "no_progress"})
_FUNDING_RE: Final = re.compile(r"fund|invest|epic|initiative|candidate", re.IGNORECASE)

_log = get_logger("harness.chat")


class _ToolObserver:
    """`TraceEmitter` that turns each traced process-tool call into chat events (U06-130)."""

    def __init__(self, inner: TraceEmitter, turn: Turn, skip: frozenset[str]) -> None:
        self._inner, self._turn, self._skip = inner, turn, skip

    @property
    def run_id(self) -> str:
        return self._inner.run_id

    @property
    def task_id(self) -> str | None:
        return self._inner.task_id

    def emit(self, type: str, /, **fields: object) -> str:  # noqa: A002 - TraceEmitter name
        name = fields.get("tool")
        if type == "tool_call" and isinstance(name, str) and name not in self._skip:
            raw = fields.get("query_ids")
            qids = [q for q in raw if isinstance(q, str)] if isinstance(raw, list) else []
            ok = fields.get("ok") is True
            self._turn.emit(ToolEvent(name=name, query_id=qids[0] if qids else None, ok=ok))
            for qid in qids:
                if qid not in self._turn.seen:
                    self._turn.seen.add(qid)
                    self._turn.emit(EvidenceEvent(query_id=qid))
        return self._inner.emit(type, **fields)


async def run_turn(env: TurnEnv, t: Turn) -> None:
    """Steps 5-6 of U06-129; every outcome ends with the reply row `done` or `failed`."""
    t.run_mode, t.lock, started = t.mode, asyncio.Lock(), clock.monotonic()
    try:
        status = await _answer(env, t, started)
    except asyncio.CancelledError:  # the consumer stopped reading (stop flag)
        cleanup(env, t, None)
        return
    except HernessError as exc:
        cleanup(env, t, exc)
        return
    finally:
        if t.bb is not None:
            t.bb.close()
        if t.tracer is not None:
            t.tracer.close()
    if status != "unverified":
        await _save_turn(env, t)


async def _answer(env: TurnEnv, t: Turn, started: float) -> TurnStatus:
    deps = env.deps
    build_id = deps.current_build()
    if build_id is None:
        msg = "no promoted warehouse build"
        raise NotFound(msg)
    t.build_id = build_id
    budget = env.cfg.pipelines.chat.budget.to_task_budget()
    create_run(env, t, budget)
    session = await asyncio.to_thread(env.memory.session_load, t.session_id)
    run_ctx = MemoryRunContext(
        run_id=t.run_id, run_kind="chat", role="chat", task_id=t.task_id, build_id=build_id,
        profile=deps.hcfg.profile, session_id=t.session_id, user_ref=t.user_ref,
    )  # fmt: skip
    prior = await asyncio.to_thread(env.memory.prior_context, run_ctx)
    payload: dict[str, object] = {
        "question": t.question, "message_id": t.message_id,
        "session": session.model_dump(mode="json"), "prior_context": prior.rendered,
    }  # fmt: skip
    ledger = RunBudget("chat", tokens_cap=budget.max_tokens, run_id=t.run_id)
    output, stop = await _agent(env, t, payload, ledger, budget)
    if stop in ESCALATE_STOPS and has_review_intent(t.question, {})[0]:
        await _escalate(env, t, t.question)
    answer = _answer_of(output, t)
    for chunk in chunk_text(render(answer.text, answer.numbers)):
        t.emit(TokenEvent(text=chunk))
    answer, status, v, removed = await _verify(env, t, answer, (ledger, budget))
    if output is None and status == "verified":
        status = "partial"  # the loop stopped without a final answer
    t.emit(VerificationEvent(result=v, status=status, removed_claims=removed))
    latency_ms = round((clock.monotonic() - started) * 1000)
    finish(env, t, answer, status, latency_ms, ledger)
    t.emit(FinalEvent(answer=answer, run_id=t.run_id))
    _log.info(
        "harness.chat.turn_completed",
        run_id=t.run_id, mode=t.mode, status=status, latency_ms=latency_ms,
    )  # fmt: skip
    labels = {"mode": t.mode, "verified": status}
    deps.metrics.record_counter("herness_harness_chat_turns_total", component="harness",
                                labels=labels)  # fmt: skip
    deps.metrics.record_histogram("herness_harness_chat_latency_seconds", latency_ms / 1000,
                                  component="harness", labels={"mode": t.mode})  # fmt: skip
    return status


async def _agent(
    env: TurnEnv, t: Turn, payload: Mapping[str, object], ledger: RunBudget, budget: TaskBudget
) -> _Output:
    """Step 5d: one `run_agent`; `EgressBlocked` in `cloud` reruns once on the local model."""
    task_input = build_task_input("chat", payload)
    try:
        return await _run_once(env, t, task_input, ledger, budget)
    except EgressBlocked:
        if t.run_mode != "cloud":
            raise
        t.run_mode, t.notice = "small_model", True
        _log.warning("harness.chat.egress_fallback", run_id=t.run_id)
        return await _run_once(env, t, task_input, ledger, budget)


async def _run_once(
    env: TurnEnv, t: Turn, task_input: dict[str, object], ledger: RunBudget, budget: TaskBudget
) -> _Output:
    """Step 5c and the `run_agent` call; a run ledger stop is the `budget` stop reason."""
    key = env.deps.jobs.chat_model_profile(t.run_mode)
    if key is None:
        msg = f"no chat model for mode {t.run_mode}"
        raise ModelUnavailable(msg)
    t.client_key, role_name = key, model_role(t.run_mode)
    config, client = env.llms.config(key), env.llms.client(key)
    ctx = _tool_context(env, t, config, (ledger, budget), role_name)
    assert t.tracer is not None  # noqa: S101 - set by step 5a
    hooks = HarnessHooks(
        registry=env.llms, gates=build_gates({key: config}, mode="chat"), chain=None,
        compactor=env.memory.compactor(config, ctx=ctx), task_id=None, phase=None,
        stop=t.stop.is_set, on_text_delta=None, tracer=t.tracer,
    )  # fmt: skip
    role = get_role("chat", model_role=role_name)
    data = cast("dict[str, JsonValue]", task_input)  # JSON by construction (U06-141)
    try:
        async with asyncio.timeout(budget.wall_clock_s + TURN_GRACE_S):
            res = await run_agent(role, data, ctx, client, config, hooks)
    except BudgetExceeded:
        return None, "budget"
    except TimeoutError:
        msg = "chat turn timed out"
        raise ModelUnavailable(msg) from None
    return res.output, res.stop_reason


def _tool_context(
    env: TurnEnv, t: Turn, config: ClientConfig, limits: tuple[RunBudget, TaskBudget],
    model_role: str,
) -> ToolContext:  # fmt: skip
    """The chat `ToolContext`: `CHAT_TOOLS[mode]`, observed spec 06 tools (step 5c)."""
    deps, (ledger, budget) = env.deps, limits
    names = list(CHAT_TOOLS[t.mode])
    own: dict[str, Tool | AsyncTool] = {
        "list_findings": ListFindingsTool(bb=None, run_id=None, past_reader=deps.past_reader),
        "escalate": EscalateTool(partial(_escalate_tool, env, t)),
    }
    observed: dict[str, Tool | AsyncTool] = {
        name: ObservedTool(tool, t.emit, seen=t.seen) for name, tool in own.items() if name in names
    }
    sql = deps.hcfg.models.harness.sql
    assert t.tracer is not None  # noqa: S101 - set by step 5a
    return ToolContext(
        run_id=t.run_id, task_id=t.task_id, build_id=t.build_id, role="chat",
        specialty="general", depth="fast", profile=deps.hcfg.profile, tool_names=names,
        task_tools=observed, warehouse=deps.warehouses.get(t.build_id), ops=deps.ops,
        vectors=deps.vectors, budgets=budget.to_budgets(deps.clock()), ledger=ledger,
        sql_limits=SqlLimits(
            return_rows=sql.return_rows, scan_rows=sql.scan_rows, timeout_s=sql.timeout_s["fast"]
        ),
        egress_purpose=egress_purpose_for(model_role) if config.off_network else None,
        tracer=_ToolObserver(cast("TraceEmitter", t.tracer), t, frozenset(observed)),
    )  # fmt: skip


async def _escalate_tool(env: TurnEnv, t: Turn, question: str, reason: str) -> tuple[str, str]:
    del reason
    return await _escalate(env, t, question)


async def _escalate(env: TurnEnv, t: Turn, question: str) -> tuple[str, str]:
    """Escalate once per turn to a mini swarm (U06-134) and stream `escalated`."""
    assert t.lock is not None  # noqa: S101 - set by run_turn
    async with t.lock:
        if t.escalation is not None:
            return t.escalation
        if t.bb is None:
            catalog = EntityCatalog(env.deps.warehouses.get(t.build_id))
            t.bb = Blackboard(t.run_id, build_id=t.build_id, catalog=catalog, allowed_numerals=())
        intent = has_review_intent(question, {})[1]
        kind = intent or ("funding_review" if _FUNDING_RE.search(question) else "org_review")
        t.escalation = await escalate_to_review(
            t.bb, env.deps.hcfg, session_id=t.session_id, message_id=t.message_id,
            question=question, kind=kind, focus=None, jobs=env.deps.jobs, now=env.deps.clock(),
        )  # fmt: skip
        t.emit(EscalatedEvent(run_id=t.escalation[0], job_id=t.escalation[1]))
        return t.escalation


def _answer_of(output: Mapping[str, JsonValue] | None, t: Turn) -> ChatAnswer:
    """Step 5f: the `ChatAnswer`; no final output gives the no-answer text; notice prefixed."""
    if output is None:
        answer = ChatAnswer(text=NO_VERIFIED_ANSWER, numbers=[], query_ids=[])
    else:
        answer = ChatAnswer.model_validate(output)
    if t.notice:
        answer = answer.model_copy(update={"text": f"{EGRESS_NOTICE}\n\n{answer.text}"})
    return answer


async def _check(env: TurnEnv, t: Turn, answer: ChatAnswer) -> VerificationResult | None:
    """`verify_answer` in a thread; a missing warehouse (`ConfigError`, `QueryError`) → None."""
    try:
        return await asyncio.to_thread(env.deps.verifier.verify_answer, answer, t.build_id)
    except (ConfigError, QueryError) as exc:
        _log.warning("harness.chat.unverified", run_id=t.run_id, error_type=type(exc).__name__)
        return None


async def _verify(
    env: TurnEnv, t: Turn, answer: ChatAnswer, limits: tuple[RunBudget, TaskBudget]
) -> tuple[ChatAnswer, TurnStatus, VerificationResult, list[str]]:
    """Step 5g: verify, one repair turn with the report, re-verify, else trim (`partial`)."""
    v = await _check(env, t, answer)
    if v is not None and not v.passed:
        payload = {
            "question": t.question, "message_id": t.message_id,
            "previous": answer.model_dump(mode="json"),
            "verifier_report": v.model_dump(mode="json"),
        }  # fmt: skip
        output, _stop = await _agent(env, t, payload, *limits)
        if output is not None:
            answer = _answer_of(output, t)
            v = await _check(env, t, answer)
    if v is None:
        empty = VerificationResult(
            build_id=t.build_id, passed=True, items=[], n_numbers=0, n_failed=0,
            verified_at=env.deps.clock(), duration_ms=0,
        )  # fmt: skip
        return answer, "unverified", empty, []
    if v.passed:
        return answer, "verified", v, []
    answer, removed = trim_failing_claims(answer, v)
    return answer, "partial", v, removed


async def _save_turn(env: TurnEnv, t: Turn) -> None:
    """Step 5k: after the answer (R-32); a capture streams `correction_captured`."""
    try:
        memory_id = await asyncio.to_thread(env.memory.session_save_turn, t.session_id, t.run_id)
    except HernessError as exc:
        _log.warning("harness.chat.session_save_failed", run_id=t.run_id,
                     error_type=type(exc).__name__)  # fmt: skip
        return
    if memory_id is not None:
        t.emit(CorrectionCapturedEvent(memory_id=memory_id))
        _log.info("harness.chat.correction_captured", run_id=t.run_id, memory_id=memory_id)
