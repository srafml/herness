"""Input set, task input and fix-pass input of the Writer step (impl 06 U06-115 steps 2-4, 7).

Private sibling of `herness.harness.swarm.writer` (T06-19 split). `gather_input` reads the
run's tasks, its `verified` and `rejected` findings not authored by crosscheck tasks (paged by
author task chunks), dedups the verified ones in memory with the U06-103 mode rules (R-31: the
duplicates are left out, statuses unchanged), orders them by `challenge_priority`, keeps
`writer_max_findings` plus the best cut finding of every must-cover entity, and builds the
pipeline's Writer input with `contested`, `dead_tasks`, `open_concerns` and
`crosscheck_incomplete`. `task_input_for` gives the local input or, off-network, the evidence
pack (U06-123) whose pseudonyms are checkpointed first (U06-140).
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from herness.core.errors import ConfigError, FatalError, HernessError
from herness.core.jobs import save_checkpoint
from herness.core.types import SwarmTaskState
from herness.harness.swarm import hybrid
from herness.harness.swarm._dedup_read import TASK_CHUNK, _read_chunk
from herness.harness.swarm.dedup import DedupMode, _vectors, dedup_components
from herness.harness.swarm.draft_rules import DraftExtras, analysis_exhausted
from herness.harness.swarm.routing import build_task_input
from herness.store.ops import select_tasks

if TYPE_CHECKING:
    from herness.core.types import Finding, FindingStatus, ReportDraft, TaskSpec
    from herness.harness.pipelines.base import PlanContext
    from herness.harness.swarm.routing import Route, RunEnv
    from herness.store.ops.runs import TaskRow

__all__ = ["WriterInput", "fix_input", "gather_input", "task_input_for", "writer_output"]


@dataclass(frozen=True, slots=True)
class WriterInput:
    """Everything the Writer step reads before its first call."""

    plan_ctx: PlanContext
    tasks: list[TaskRow]
    deduped: list[Finding]  # verified, not crosscheck-authored, duplicates left out
    selected: list[Finding]  # the Writer's findings: the cut of `deduped`
    rejected: list[Finding]
    inp: dict[str, object]
    extras: DraftExtras

    @property
    def by_id(self) -> dict[str, Finding]:
        return {f.finding_id: f for f in self.deduped}


async def _read(env: RunEnv, task_ids: Sequence[str], status: FindingStatus) -> list[Finding]:
    out: list[Finding] = []
    for start in range(0, len(task_ids), TASK_CHUNK):
        chunk = task_ids[start : start + TASK_CHUNK]
        out += await _read_chunk(env.bb, env.run.run_id, chunk, status=status)
    return sorted(out, key=lambda f: f.finding_id)


async def _dedup(env: RunEnv, findings: list[Finding]) -> list[Finding]:
    """`findings` without the duplicates U06-102 finds (same mode rules as U06-103)."""
    mode: DedupMode = "query_set" if env.run.depth == "fast" else "embedding"
    vectors = await _vectors(env, findings) if mode == "embedding" and findings else None
    components = dedup_components(
        findings, mode=mode if vectors is not None else "query_set", vectors=vectors,
        cosine=env.cfg.swarm.dedup.cosine,
    )  # fmt: skip
    dups = {fid for _, ids in components for fid in ids}
    return [f for f in findings if f.finding_id not in dups]


def _cut(env: RunEnv, plan_ctx: PlanContext, findings: list[Finding]) -> list[Finding]:
    """Priority order, `writer_max_findings`, then the best cut finding per must-cover entity."""
    ordered = sorted(
        findings, key=lambda f: (-env.pipeline.challenge_priority(f, plan_ctx), f.finding_id)
    )
    keep = ordered[: env.knobs.writer_max_findings]
    must = env.pipeline.must_cover(plan_ctx)
    seen = {f"{f.entity_type}:{f.entity_id}" for f in keep}
    for f in ordered[env.knobs.writer_max_findings :]:
        key = f"{f.entity_type}:{f.entity_id}"
        if key in must and key not in seen:
            seen.add(key)
            keep.append(f)
    return keep


def _last_verdict(f: Finding) -> str | None:
    return f.challenge[-1].verdict if f.challenge else None


def _dead_tasks(tasks: Sequence[TaskRow]) -> list[dict[str, object]]:
    return [
        {"task_id": t.task_id, "role": t.role, "objective": t.spec.objective,
         "last_error": None if t.last_error is None else str(t.last_error.get("class"))}
        for t in tasks if t.status == "dead"
    ]  # fmt: skip


async def gather_input(env: RunEnv) -> WriterInput:
    """The Writer's input set, input and draft extras (U06-115 steps 2 and 3)."""
    plan_ctx = env.plan_ctx
    if plan_ctx is None:
        msg = f"run {env.run.run_id} has no plan context"
        raise ConfigError(msg)
    tasks = await asyncio.to_thread(select_tasks, env.run.run_id)
    authors = sorted(t.task_id for t in tasks if t.spec.specialty != "crosscheck")
    deduped = await _dedup(env, await _read(env, authors, "verified"))
    rejected = await _read(env, authors, "rejected")
    selected = _cut(env, plan_ctx, deduped)
    contested = [f.finding_id for f in rejected if _last_verdict(f) == "reject"]
    dead = _dead_tasks(tasks)
    inp = env.pipeline.writer_input(plan_ctx, selected) | {
        "contested": contested,
        "dead_tasks": dead,
        "open_concerns": [
            {"finding_id": f.finding_id, "required_actions": list(f.challenge[-1].required_actions)}
            for f in selected if _last_verdict(f) == "revise"
        ],
        "crosscheck_incomplete": [
            f.finding_id for f in selected
            if f.verification is not None and any(not c.agreed for c in f.verification.cross_checks)
        ],
    }  # fmt: skip
    exhausted = env.analysis.exhausted or analysis_exhausted(tasks)
    extras = DraftExtras(contested=contested, dead_tasks=dead, budget_exhausted=exhausted)
    return WriterInput(plan_ctx, tasks, deduped, selected, rejected, inp, extras)


# --- task input (U06-115 steps 4 and 7) -------------------------------------------------------


async def task_input_for(
    env: RunEnv, spec: TaskSpec, route: Route, wi: WriterInput
) -> tuple[dict[str, object], hybrid.Pseudonymizer | None]:
    """Local input (U06-141) or, off-network, the evidence pack with its pseudonyms saved in
    the task checkpoint before the first call (U06-123, U06-140, R-21, TH06-06)."""
    if not route.off_network:
        return build_task_input("writer", wi.inp), None
    keys = {(f.entity_type, f.entity_id) for f in wi.selected}
    names: dict[tuple[str, str], str | None] = {}
    for kind in sorted({k for k, _ in keys}):
        ids = sorted(e for k, e in keys if k == kind)
        found = await asyncio.to_thread(env.catalog.names, kind, ids)
        names |= {(kind, e): found.get(e) for e in ids}
    try:
        pseudo = hybrid.Pseudonymizer([(k, e, names[k, e]) for k, e in sorted(keys)])
        pack = hybrid.build_evidence_pack(
            spec, findings=wi.selected, inp=wi.inp, pseudo=pseudo,
            max_tokens=env.cfg.hybrid.max_input_tokens_per_call, cfg=route.config,
        )  # fmt: skip
    except (HernessError, ValueError) as exc:
        msg = f"evidence pack unavailable: task_id={spec.task_id}"
        raise FatalError(msg) from exc
    state = SwarmTaskState(phase="writing", pseudonyms=pseudo.mapping()).model_dump(mode="json")
    save = functools.partial(save_checkpoint, spec.task_id, "state", state)
    await env.bb.run_on_writer(save)
    return pack, pseudo


def writer_output(draft: ReportDraft) -> dict[str, object]:
    """The Writer-authored fields of `draft`, as the `previous` of a fix pass."""
    data = draft.model_dump(
        mode="json", include={"title", "sections", "recommendations", "caveats",
                              "prior_outcomes_commentary"},
    )  # fmt: skip
    for rec in data["recommendations"]:
        rec.pop("rank", None)
        rec.pop("rec_id", None)
    return data


def fix_input(
    pseudo: hybrid.Pseudonymizer | None, task_id: str, payload: dict[str, object]
) -> dict[str, object]:
    """A fix-pass payload: wrapped locally (U06-141), pseudonymized and redacted off-network."""
    if pseudo is None:
        return build_task_input("writer", payload)
    try:
        return cast("dict[str, object]", hybrid._scrub(pseudo, payload))
    except hybrid._Unredactable:
        msg = f"fix pass input redaction failed: task_id={task_id}"
        raise FatalError(msg) from None
