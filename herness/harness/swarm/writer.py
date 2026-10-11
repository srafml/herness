"""Writer step, gate 2 and the draft file (impl 06 U06-115, U06-121; design 06 §5.8, §5.9).

`run_writer_step` runs the Writer task over the verified input set (private sibling
`_writer_input`), applies the recommendation rules, the extra-text checks and Verifier gate 2
with up to `writer_fix_passes` fix passes (each failing draft kept as `draft.pass<k>.json` for
resume), removes what still fails, computes coverage and writes `draft.json` atomically; a dead
Writer gets the findings-only draft (U06-142, R-49). Called by the run driver, not the scheduler.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import functools
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

from pydantic import JsonValue, ValidationError

from herness.core.errors import ConfigError, FatalError, HernessError
from herness.core.jobs import claim_task, complete_task, release_task
from herness.core.logging import get_logger
from herness.core.types import ReportDraft
from herness.harness.loop import run_agent
from herness.harness.swarm import hybrid
from herness.harness.swarm import scheduler as sch
from herness.harness.swarm._challenge_gate1 import insert, plain_result
from herness.harness.swarm._draft_checks import Failure
from herness.harness.swarm._routing_ctx import with_cost_left
from herness.harness.swarm._writer_input import (
    WriterInput,
    entry_state,
    fix_input,
    gather_input,
    last_pass,
    log_unremovable,
    task_input_for,
    writer_output,
    writer_spec,
)
from herness.harness.swarm.adversarial import _sql_path
from herness.harness.swarm.draft_rules import (
    analysis_exhausted,
    apply_gate2_removals,
    build_findings_only_draft,
    check_extra_text,
    check_recommendation_rules,
    compute_coverage,
    empty_verification,
    fill_draft,
)
from herness.harness.swarm.routing import (
    AnsweredClients,
    build_hooks,
    build_tool_context,
    route_task,
)
from herness.harness.swarm.tools import build_task_tools
from herness.store.ops import get_evidence, get_task, get_task_by_dedup

if TYPE_CHECKING:
    from herness.core.types import AgentResult, Depth, ToolContext, VerificationResult
    from herness.harness.loop import HarnessHooks
    from herness.harness.swarm.routing import Route, RunEnv
    from herness.store.ops.runs import TaskRow

__all__ = ["draft_path", "run_writer_step", "write_draft_atomic"]

_log = get_logger("harness.swarm")
_RUN_ID: Final = re.compile(r"run_[0-9A-HJKMNP-TV-Z]{26}")


# --- U06-121 path and atomic write --------------------------------------------------------------


def draft_path(run_id: str, reports_root: Path) -> Path:
    """`<reports_root>/<run_id>/draft.json`; a bad id or an escaping path → `ConfigError`."""
    if _RUN_ID.fullmatch(run_id) is None:
        msg = "invalid run_id"
        raise ConfigError(msg)
    path = reports_root / run_id / "draft.json"
    if not path.parent.resolve().is_relative_to(reports_root.resolve()):
        msg = "draft path escapes the reports root"
        raise ConfigError(msg)
    return path


def write_draft_atomic(path: Path, draft: ReportDraft) -> None:
    """Write `draft` to a temp file beside `path`, fsync it and `os.replace` it (ENG §3.5)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(draft.model_dump_json(indent=2))
            fh.flush()
            os.fsync(fh.fileno())
        Path(tmp).replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            Path(tmp).unlink()
        raise


# --- one Writer task ----------------------------------------------------------------------------


@dataclass(slots=True)
class _Writer:
    """One claimed Writer task: route, tool context, hooks, pseudonyms, the clients that
    answered (`watch`, the context's tracer) and agent results."""

    env: RunEnv
    row: TaskRow
    wi: WriterInput
    route: Route
    ctx: ToolContext
    hooks: HarnessHooks
    pseudo: hybrid.Pseudonymizer | None
    watch: AnsweredClients
    runs: list[AgentResult] = field(default_factory=list)

    async def draft(self, task_input: dict[str, object], *, first: bool) -> ReportDraft:
        """One pass: run the agent, restore and validate its output, fill the draft (steps 5-6)."""
        spec, route, env = self.row.spec, self.route, self.env
        resume = sch._resume_from(self.row.checkpoint) if first else None
        # Every pass may spend only what the passes before it left of the cost cap (I-1).
        self.ctx = with_cost_left(self.ctx, env.run, route, env.writer)
        try:
            async with asyncio.timeout(spec.budget.wall_clock_s + sch.WALL_CLOCK_GRACE_S):
                res = await run_agent(
                    route.role_spec, cast("dict[str, JsonValue]", task_input), self.ctx,
                    route.client, route.config, self.hooks, resume_from=resume,
                )  # fmt: skip
        except TimeoutError:
            msg = f"writer wall clock exceeded: task_id={spec.task_id}"
            raise FatalError(msg) from None
        sch._add_usage(env, "writer", res)
        self.runs.append(res)
        if res.status == "failed" or res.output is None:
            msg = f"writer produced no output: task_id={spec.task_id} stop={res.stop_reason}"
            raise FatalError(msg)
        out = res.output if self.pseudo is None else self.pseudo.restore_obj(res.output)
        try:
            model = ReportDraft.writer_output_model().model_validate(out)
        except ValidationError as exc:
            msg = f"writer output invalid: task_id={spec.task_id}"
            raise FatalError(msg) from exc
        # A cost-cap route fallback, or a chain fallback to a local client (EgressBlocked).
        fallback = route.fallback_local or self.watch.fell_back_local(route, env.llms)
        extras = dataclasses.replace(self.wi.extras, hybrid_fallback=fallback)
        return fill_draft(
            model, run=env.run, pipeline=env.pipeline, plan_ctx=self.wi.plan_ctx,
            findings=self.wi.by_id, extras=extras,
            verification=empty_verification(_build_id(env), env.clock()),
        )  # fmt: skip

    def result(self, extra: dict[str, object]) -> dict[str, object]:
        """`task.result` with the tokens and cost of every pass."""
        result = plain_result("draft written", []) | extra
        usage = [r.usage for r in self.runs]
        result["tokens"] = {
            "input": sum(
                u.input_tokens + u.cache_read_tokens + u.cache_write_tokens for u in usage
            ),
            "output": sum(u.output_tokens for u in usage),
        }
        result["cost_usd"] = str(sum(r.cost_usd for r in self.runs))
        return result


def _build_id(env: RunEnv) -> str:
    if env.run.build_id is None:
        msg = f"run {env.run.run_id} has no build_id"
        raise ConfigError(msg)
    return env.run.build_id


# --- gate 2 -------------------------------------------------------------------------------------


def _tables(query_id: str) -> frozenset[str]:
    """Tables read by the evidence SQL of `query_id` (sqlglot); empty when unknown."""
    ev = get_evidence(query_id)
    path = None if ev is None else _sql_path(ev.sql)
    return frozenset() if path is None else path[0]


def _evaluate(
    env: RunEnv, wi: WriterInput, draft: ReportDraft
) -> tuple[list[Failure], VerificationResult]:
    """Rules, extra text and `verify_draft` of one draft (U06-115 step 6); runs in a thread."""
    # Only this run's verified input set counts as verified (fail closed, review M5).
    statuses = dict.fromkeys(wi.by_id, "verified")
    levers = wi.inp.get("levers")
    failures = check_recommendation_rules(
        draft, statuses=statuses, tables_of=functools.cache(_tables),
        portfolio_query_ids=frozenset(wi.plan_ctx.portfolio["query_ids"]),
        levers=levers if isinstance(levers, list) else [],
    )  # fmt: skip
    failures += check_extra_text(draft, env.hcfg.app.reports.compiled_numeral_patterns)
    v = env.verifier.verify_draft(draft, _build_id(env))
    failures += [{"where": item.where, "reason": "gate2"} for item in v.items if not item.passed]
    return failures, v


async def _write(env: RunEnv, w: _Writer, path: Path) -> bool:
    """Passes, removals, coverage and `draft.json` of one claimed Writer task (steps 5-10)."""
    task_input, pseudo = await task_input_for(env, w.row.spec, w.route, w.wi)
    w.pseudo = pseudo
    stored = await asyncio.to_thread(last_pass, path)
    k, draft = stored if stored is not None else (0, await w.draft(task_input, first=True))
    failures, v = await asyncio.to_thread(_evaluate, env, w.wi, draft)
    on_disk = stored is not None  # a loaded pass file is not re-written (review M2)
    while failures and k < env.knobs.writer_fix_passes:
        if not on_disk:
            await asyncio.to_thread(
                write_draft_atomic, path.with_name(f"draft.pass{k}.json"), draft
            )
        on_disk = False
        report = [i.model_dump(mode="json") for i in v.items if not i.passed]
        fix: dict[str, object] = {
            "previous": writer_output(draft),
            "rule_violations": failures,
            "verifier_report": report,
        }
        draft = await w.draft(task_input | fix_input(w.pseudo, w.row.task_id, fix), first=False)
        k += 1
        failures, v = await asyncio.to_thread(_evaluate, env, w.wi, draft)
    log_unremovable(env.run.run_id, failures)
    draft = apply_gate2_removals(draft, failures)
    draft = draft.model_copy(update={"ranked_entities": env.pipeline.ranked_entities(draft)})
    v = await asyncio.to_thread(env.verifier.verify_draft, draft, _build_id(env))
    wi = w.wi
    coverage, partial = compute_coverage(
        wi.tasks, [*wi.deduped, *wi.rejected], env.pipeline.must_cover(wi.plan_ctx),
        gate2_passed=v.passed, n_recs=len(draft.recommendations),
        analysis_exhausted=analysis_exhausted(wi.tasks),
        min_done_fraction=env.cfg.swarm.coverage.min_done_fraction,
    )  # fmt: skip
    banners = [*draft.banners, *([] if coverage.publishable else ["partial_run"])]
    draft = draft.model_copy(update={
        "coverage": coverage, "verification": v, "banners": banners,
        "flags": draft.flags | {"partial_coverage": partial},
    })  # fmt: skip
    await asyncio.to_thread(write_draft_atomic, path, draft)
    extra = {"draft_path": str(path), "fix_passes": k, "publishable": coverage.publishable}
    result = w.result(extra)
    await env.bb.run_on_writer(functools.partial(complete_task, w.row.task_id, result))
    run_id = env.run.run_id
    _log.info("harness.gate2.completed", run_id=run_id, passed=v.passed,
              removed=len(draft.removed), fix_passes=k)  # fmt: skip
    _log.info("harness.draft.written", run_id=run_id, path=str(path))
    return coverage.publishable


async def _attempt(env: RunEnv, row: TaskRow, wi: WriterInput, path: Path) -> bool:
    spec = row.spec
    route = route_task(
        spec, llms=env.llms, depth=cast("Depth", env.run.depth), profile=env.run.profile,
        ledger=env.writer,
    )  # fmt: skip
    tools = build_task_tools(spec, bb=env.bb, broker=env.broker)
    ctx = build_tool_context(env, spec, route, task_tools=tools, ledger=env.writer, now=env.clock())
    watch = AnsweredClients(ctx.tracer)
    ctx = ctx.model_copy(update={"tracer": watch})
    w = _Writer(env, row, wi, route, ctx, build_hooks(env, spec, route, ctx), None, watch)
    return await _write(env, w, path)


async def _findings_only(env: RunEnv, wi: WriterInput, path: Path) -> bool:
    """The findings-only `draft.json` of a dead Writer task (U06-115 step 11, R-49)."""
    build_id = _build_id(env)
    draft = await asyncio.to_thread(
        lambda: build_findings_only_draft(
            env.run, wi.deduped, pipeline=env.pipeline, plan_ctx=wi.plan_ctx, extras=wi.extras,
            tasks=wi.tasks, verify=lambda d: env.verifier.verify_draft(d, build_id),
        )
    )  # fmt: skip
    await asyncio.to_thread(write_draft_atomic, path, draft)
    _log.info("harness.draft.written", run_id=env.run.run_id, path=str(path))
    return False


async def run_writer_step(env: RunEnv) -> bool:
    """Writer, rules, gate 2 with fix passes, coverage and `draft.json` (U06-115); publishable.

    `done` with `draft.json` returns the stored verdict; `pending` is claimed and run, and
    claimed again after a retryable failure (resuming from the newest pass file); `dead` at
    entry or from `fail_task` gets the findings-only draft. Any other state, or a lost claim,
    raises `FatalError` and writes nothing (`entry_state`). Cancellation releases (R-36).
    """
    path = draft_path(env.run.run_id, Path(env.hcfg.paths.data) / "reports")
    spec = writer_spec(env)
    await insert(env, [spec], "swarm_writer_task")
    row = await asyncio.to_thread(get_task_by_dedup, env.run.run_id, spec.dedup_key)
    if row is None:  # pragma: no cover - inserted just above
        msg = f"writer task missing: run_id={env.run.run_id}"
        raise FatalError(msg)
    state = entry_state(row.status, path.is_file())
    if state == "stored":
        return ReportDraft.model_validate_json(path.read_bytes()).coverage.publishable
    wi = await gather_input(env)
    while state == "run":
        if not await env.bb.run_on_writer(functools.partial(claim_task, row.task_id)):
            msg = f"writer task claim lost: task_id={row.task_id}"
            raise FatalError(msg)
        try:
            return await _attempt(env, row, wi, path)
        except asyncio.CancelledError:
            await env.bb.run_on_writer(functools.partial(release_task, row.task_id))
            raise
        except HernessError as err:
            dead = await sch._fail(env, row, err) == "dead"
            state = "findings_only" if dead else "run"
        row = await asyncio.to_thread(get_task, row.task_id) or row
    return await _findings_only(env, wi, path)
