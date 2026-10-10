"""Spec 08 `review` job handler and its composition (impl 06 U06-85).

`review_job_handler(ctx)` (one argument, R-42) resumes the run named by a resume payload or
starts the run of a request payload or of a scheduled `{pipeline, depth}` payload, then answers
`yield` when a non-terminal run stopped for a preemption and `done` otherwise.
`build_swarm_from_config` is the composition root of the job child process (ENG §2.2 exception,
§13 O06-12). `register_handlers` registers the handler for
the worker's composition root (T09-24/T09-27 call it); calling it again is a no-op.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import TYPE_CHECKING, Final

from pydantic import JsonValue, ValidationError

from herness.core.config import HernessConfig, get_config
from herness.core.errors import ConfigError
from herness.core.jobs import register_handler
from herness.core.types import JobOutcome
from herness.harness.swarm.lifecycle import RunRequest, RunResult
from herness.harness.swarm.run import TERMINAL, Swarm, SwarmDeps

if TYPE_CHECKING:
    from herness.core.jobs import JobContext

__all__ = ["build_swarm_from_config", "register_handlers", "review_job_handler"]

_RESUME_KEYS: Final = frozenset({"run_id", "resume", "force", "retry_dead"})
# The scheduled form (`config/resilience.yaml`, spec 08): `pipeline` and `depth` plus the
# `schedule`, `fire_at` and `step` keys the scheduler adds to every chain payload (U08-73).
_SCHEDULED_KEYS: Final = frozenset({"pipeline", "depth", "schedule", "fire_at", "step"})
_PIPELINES: Final = ("funding_review", "org_review")


def build_swarm_from_config(cfg: HernessConfig) -> Swarm:
    """A `Swarm` with the process collaborators of `cfg` (U06-85; composition root)."""
    from herness.harness.llm.registry import LLMRegistry  # noqa: PLC0415 - composition only
    from herness.harness.memory import MemoryStore  # noqa: PLC0415 - composition only

    llms = LLMRegistry(cfg.models, profile=cfg.profile)
    memory = MemoryStore.from_config(cfg)
    return Swarm(cfg.pipelines, llms, memory, deps=SwarmDeps.from_config(cfg))


def _invalid(ctx: JobContext) -> ConfigError:
    return ConfigError(f"review payload invalid: job_id={ctx.job_id}")


def _scheduled_request(payload: Mapping[str, object], ctx: JobContext) -> RunRequest:
    """The request of a scheduled payload `{pipeline, depth?}` (+ scheduler keys), else
    `ConfigError`: one pipeline per job, a closed key set, strict types."""
    pipeline = payload.get("pipeline")
    stamp = (payload.get("schedule", ""), payload.get("fire_at", ""), payload.get("step", 0))
    typed = (isinstance(stamp[0], str), isinstance(stamp[1], str),
             isinstance(stamp[2], int) and not isinstance(stamp[2], bool))  # fmt: skip
    if pipeline not in _PIPELINES or not all(typed):
        raise _invalid(ctx)
    try:
        return RunRequest.model_validate(
            {"kind": pipeline, "depth": payload.get("depth", "standard")}, strict=True
        )
    except ValidationError as exc:
        raise _invalid(ctx) from exc


async def _run(swarm: Swarm, payload: Mapping[str, object], ctx: JobContext) -> RunResult:
    """U06-85 step 2: the resume form, the request form, or `ConfigError`.

    `retry_dead` (spec 08 `enqueue_resume`) is accepted and not used: U06-80 step 6 derives
    `retry_dead` from `force` and a dead planner (spec note).
    """
    run_id, force = payload.get("run_id"), payload.get("force", False)
    if set(payload) <= _RESUME_KEYS and payload.get("resume") is True:
        if not isinstance(run_id, str) or not isinstance(force, bool):
            raise _invalid(ctx)
        return await swarm.resume(run_id, ctx, force=force)
    if "pipeline" in payload and set(payload) <= _SCHEDULED_KEYS:
        return await swarm.start(_scheduled_request(payload, ctx), ctx)
    if set(payload) == {"request"}:
        try:
            req = RunRequest.model_validate(payload["request"])
        except ValidationError as exc:
            raise _invalid(ctx) from exc
        return await swarm.start(req, ctx)
    raise _invalid(ctx)


def review_job_handler(ctx: JobContext) -> JobOutcome:
    """Spec 08 `review` job handler (U06-85, R-42); `ConfigError` fails the job."""
    swarm = build_swarm_from_config(get_config())
    try:
        result = asyncio.run(_run(swarm, ctx.job.payload, ctx))
    finally:
        swarm.close()
    status = result.status
    if status not in TERMINAL and ctx.should_yield():
        return JobOutcome(status="yield", result={"run_id": result.run_id, "status": status})
    out: dict[str, JsonValue] = {
        "run_id": result.run_id, "status": status, "partial": status == "partial"
    }  # fmt: skip
    return JobOutcome(status="done", result=out)


def register_handlers() -> None:
    """Register `review_job_handler` for `review` jobs; idempotent (the same object again is a
    no-op in `register_handler`, U08-55)."""
    register_handler("review", review_job_handler)
