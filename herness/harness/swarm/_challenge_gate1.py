"""Cross-check evaluation and Verifier gate 1 (impl 06 U06-113, U06-114; design 06 §5.7).

Private sibling of `herness.harness.swarm.challenge`, which re-exports `CrosscheckReader`,
`CrosscheckOutcome`, `evaluate_crosschecks`, `run_gate1` and `execute_verifier`. It also holds
the end-state guard shared by Skeptic and verifier tasks (U06-93 step 6). A crosscheck task's
value is the first `NumberRef` with the target's unit in the findings it posted (D06-16).
"""

from __future__ import annotations

import asyncio
import functools
import re
import sqlite3
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol

from herness.core import time as clock
from herness.core.errors import ConfigError, HernessError
from herness.core.ids import IdKind, new_id
from herness.core.jobs import complete_task, release_task
from herness.core.logging import get_logger
from herness.core.types import CrossCheck, EntityScope, TaskInputs, TaskSpec, VerificationRecord
from herness.harness.blackboard import Blackboard
from herness.harness.findings import compute_dedup_key
from herness.harness.swarm import scheduler as sch
from herness.harness.swarm._dedup_read import TASK_CHUNK, _read_chunk
from herness.harness.swarm.adversarial import crosscheck_agree, is_independent_sql
from herness.harness.swarm.routing import role_budget
from herness.store.ops import (
    finding_statuses,
    get_evidence,
    get_findings,
    insert_tasks,
    list_task_findings,
    run_write,
    select_tasks,
)

if TYPE_CHECKING:
    from herness.core.types import Finding, NumberRef, VerificationResult
    from herness.harness.pipelines.settings import CrosscheckSettings, DepthKnobs
    from herness.harness.swarm.routing import RunEnv
    from herness.harness.swarm.scheduler import PhaseOutcome, PhaseRunner
    from herness.store.ops.runs import TaskRow

__all__ = [
    "CrosscheckOutcome",
    "CrosscheckReader",
    "evaluate_crosschecks",
    "execute_verifier",
    "run_gate1",
]

_log = get_logger("harness.swarm")
_TARGET_NUMBER: Final = re.compile(r"target_number=(n[0-9]+)")
type Writes = Callable[[sqlite3.Connection], None]


# --- shared task plumbing ----------------------------------------------------------------------


async def guard(env: RunEnv, row: TaskRow, body: Callable[[], Awaitable[sch._Outcome]]) -> None:
    """End state of a skeptic or verifier task as U06-93 step 6, with its metrics."""
    started = clock.monotonic()
    try:
        outcome = await body()
    except sch._Backstop:  # the wall-clock backstop of a Skeptic task
        outcome = await sch._complete_timed_out(env, row)
    except asyncio.CancelledError:
        await env.bb.run_on_writer(functools.partial(release_task, row.task_id))  # R-36
        sch._record(row, "released", started)
        task = asyncio.current_task()
        if task is not None and task.cancelling() > 0:
            raise  # outer cancellation
        return
    except HernessError as err:
        outcome = await sch._fail(env, row, err)
    sch._record(row, outcome, started)


def plain_result(summary: str, finding_ids: Sequence[str]) -> dict[str, object]:
    """A `task.result` of a task without model calls."""
    return {
        "summary": summary, "finding_ids": list(finding_ids), "partial": False,
        "stop_cause": "final", "tokens": {"input": 0, "output": 0}, "cost_usd": "0",
        "subtasks": [],
    }  # fmt: skip


async def finish(
    env: RunEnv, row: TaskRow, result: dict[str, object], writes: Writes | None
) -> None:
    """`complete_task` with `writes` in one transaction on the writer, then the log line."""
    await env.bb.run_on_writer(functools.partial(complete_task, row.task_id, result, writes=writes))
    sch._completed(env, row, result)


async def insert(env: RunEnv, specs: Sequence[TaskSpec], op: str) -> None:
    """Insert task rows on the writer; existing ids or dedup keys are skipped."""
    if not specs:
        return
    now = env.clock()

    def fn(conn: sqlite3.Connection) -> None:
        insert_tasks(conn, specs, now=now)

    await env.bb.run_on_writer(lambda: run_write(fn, op=op))


# --- U06-113 cross-check evaluation ------------------------------------------------------------


class CrosscheckReader(Protocol):
    """Reads of `evaluate_crosschecks`: crosscheck tasks, findings and evidence SQL."""

    def crosscheck_tasks(self) -> Sequence[TaskSpec]: ...
    def findings(self, finding_ids: Collection[str]) -> Mapping[str, Finding]: ...
    def task_findings(self, task_id: str) -> Sequence[Finding]: ...
    def sql(self, query_id: str) -> str | None: ...


@dataclass(frozen=True, slots=True)
class CrosscheckOutcome:
    """`CrossCheck` records of one finding and whether they disagree or are incomplete."""

    checks: list[CrossCheck]
    disagree: bool
    incomplete: bool


class _StoreReader:
    """`CrosscheckReader` over the ops store; the run's crosscheck tasks are read once."""

    def __init__(self, run_id: str) -> None:
        rows = select_tasks(run_id, roles={"analyst"})
        self._tasks = [t.spec for t in rows if t.spec.specialty == "crosscheck"]

    def crosscheck_tasks(self) -> Sequence[TaskSpec]:
        return self._tasks

    def findings(self, finding_ids: Collection[str]) -> Mapping[str, Finding]:
        return get_findings(finding_ids)

    def task_findings(self, task_id: str) -> Sequence[Finding]:
        return list_task_findings(task_id)

    def sql(self, query_id: str) -> str | None:
        ev = get_evidence(query_id)
        return None if ev is None else ev.sql


def _chain(f: Finding, reader: CrosscheckReader) -> dict[str, Finding]:
    """`f` and its ancestors through `supersedes`."""
    chain = {f.finding_id: f}
    cur: Finding | None = f
    while cur is not None and cur.supersedes is not None and cur.supersedes not in chain:
        cur = reader.findings([cur.supersedes]).get(cur.supersedes)
        if cur is not None:
            chain[cur.finding_id] = cur
    return chain


def _target(task: TaskSpec, chain: Mapping[str, Finding]) -> NumberRef | None:
    """The `target_number` of `task` in its original target finding, when in the chain."""
    ids = [fid for fid in task.inputs.finding_ids if fid in chain]
    match = _TARGET_NUMBER.search(task.inputs.notes or "")
    if not ids or match is None:
        return None
    return next((n for n in chain[ids[0]].numbers if n.id == match.group(1)), None)


def _candidate(reader: CrosscheckReader, task_id: str, orig: NumberRef) -> NumberRef | None:
    """The task's value (D06-16) when it took an independent query path, else None."""
    numbers = (n for f in reader.task_findings(task_id) for n in f.numbers)
    cand = next((n for n in numbers if n.unit == orig.unit), None)
    if cand is None or cand.query_id == orig.query_id:
        return None
    sql_a, sql_b = reader.sql(orig.query_id), reader.sql(cand.query_id)
    if sql_a is None or sql_b is None or not is_independent_sql(sql_a, sql_b):
        return None
    return cand


def evaluate_crosschecks(
    f: Finding, *, reader: CrosscheckReader, knobs: DepthKnobs, tolerances: CrosscheckSettings
) -> CrosscheckOutcome:
    """`CrossCheck` records of `f` at gate 1 (U06-113).

    Crosscheck tasks targeting `f` or an ancestor through `supersedes` count for the number of
    `f` with the target's `(query_id, column, row_key)`. A value counts when its query differs
    and is independent of the original (`is_independent_sql`; missing evidence → invalid).
    """
    chain = _chain(f, reader)
    found: dict[str, tuple[NumberRef, list[NumberRef]]] = {}
    for task in reader.crosscheck_tasks():
        target = _target(task, chain)
        key = None if target is None else (target.query_id, target.column, target.row_key)
        mine = next((n for n in f.numbers if (n.query_id, n.column, n.row_key) == key), None)
        if mine is None:
            continue
        valid = found.setdefault(mine.id, (mine, []))[1]
        cand = _candidate(reader, task.task_id, mine)
        if cand is not None and all(v.query_id != cand.query_id for v in valid):
            valid.append(cand)
    need, tol = knobs.crosscheck_ways - 1, tolerances
    checks: list[CrossCheck] = []
    disagree = short = False
    for mine, valid in sorted(found.values(), key=lambda pair: int(pair[0].id[1:])):
        a = float(mine.value)
        agree = [
            crosscheck_agree(a, float(v.value), mine.unit, rel_tol=tol.rel_tol,
                             abs_tol_count=tol.abs_tol_count, abs_tol_ratio=tol.abs_tol_ratio)
            for v in valid
        ]  # fmt: skip
        disagree = disagree or not all(agree)
        short = short or len(valid) < need
        checks.append(
            CrossCheck(
                number_id=mine.id, query_ids=[mine.query_id, *(v.query_id for v in valid)],
                values=[a, *(float(v.value) for v in valid)],
                agreed=len(valid) >= need and all(agree),
            )
        )  # fmt: skip
    return CrosscheckOutcome(checks, disagree, short and not disagree)


# --- U06-114 gate 1 ---------------------------------------------------------------------------


def _verifier_task(env: RunEnv, batch: Sequence[str]) -> TaskSpec:
    scope = EntityScope(entity_type="run", entity_ids=[env.run.run_id])
    objective = f"Verify findings batch {batch[0]}..{batch[-1]}."
    return TaskSpec(
        task_id=new_id(IdKind.TASK), run_id=env.run.run_id, role="verifier",
        objective=objective, scope=scope, inputs=TaskInputs(finding_ids=list(batch)), tools=[],
        budget=role_budget("verifier", env.knobs), model_role="verifier",
        dedup_key=compute_dedup_key("verifier", "general", scope, objective),
    )  # fmt: skip


async def _all_proposed(env: RunEnv) -> list[Finding]:
    """Every `proposed` finding of the run, crosscheck-authored ones included, by id."""
    run_id = env.run.run_id
    ids = sorted(t.task_id for t in await asyncio.to_thread(select_tasks, run_id))
    out: list[Finding] = []
    for start in range(0, len(ids), TASK_CHUNK):
        out += await _read_chunk(env.bb, run_id, ids[start : start + TASK_CHUNK])
    return sorted(out, key=lambda f: f.finding_id)


async def run_gate1(env: RunEnv, runner: PhaseRunner) -> PhaseOutcome:
    """Verifier gate 1 over every `proposed` finding, in batches of `verifier_batch` (U06-114)."""
    ids = [f.finding_id for f in await _all_proposed(env)]
    size = env.cfg.swarm.verifier_batch
    batches = [ids[i : i + size] for i in range(0, len(ids), size)]
    await insert(env, [_verifier_task(env, b) for b in batches], "swarm_verifier_tasks")
    out = await runner.run_phase(frozenset({"verifier"}), ledger=env.analysis)
    tally = list((await asyncio.to_thread(finding_statuses, ids)).values()) if ids else []
    _log.info(
        "harness.gate1.completed", run_id=env.run.run_id,
        verified=tally.count("verified"), rejected=tally.count("rejected"),
    )  # fmt: skip
    return out


def _transition(
    conn: sqlite3.Connection, f: Finding, res: VerificationResult, cc: CrosscheckOutcome
) -> None:
    record = VerificationRecord(gate=1, result=res, cross_checks=cc.checks)
    if cc.disagree:
        Blackboard.tx_reject(conn, f.finding_id, "crosscheck_disagree", record, None)
    elif res.passed:
        Blackboard.tx_mark_verified(conn, f.finding_id, record)
    else:
        Blackboard.tx_reject(conn, f.finding_id, "verifier_fail", record, None)


async def execute_verifier(env: RunEnv, row: TaskRow) -> None:
    """Gate 1 of one batch: verify, cross-check, transition and complete in one tx (U06-114).

    A `HernessError` from the Verifier fails the task; its findings stay `proposed`.
    """
    await guard(env, row, functools.partial(_verify, env, row))


async def _verify(env: RunEnv, row: TaskRow) -> sch._Outcome:
    batch = row.spec.inputs.finding_ids
    found = await asyncio.to_thread(get_findings, batch)
    findings = [found[fid] for fid in sorted(found) if found[fid].status == "proposed"]
    build_id = env.run.build_id
    if build_id is None:
        msg = f"run {env.run.run_id} has no build_id"
        raise ConfigError(msg)
    results = await asyncio.to_thread(env.verifier.verify_findings, findings, build_id)

    def crosschecks() -> list[CrosscheckOutcome]:
        reader, tol = _StoreReader(env.run.run_id), env.cfg.swarm.crosscheck
        return [
            evaluate_crosschecks(f, reader=reader, knobs=env.knobs, tolerances=tol)
            for f in findings
        ]

    outcomes = await asyncio.to_thread(crosschecks)
    rows = list(zip(findings, results, outcomes, strict=True))
    passed = sum(1 for _, res, cc in rows if res.passed and not cc.disagree)

    def writes(conn: sqlite3.Connection) -> None:
        for f, res, cc in rows:
            _transition(conn, f, res, cc)

    await finish(env, row, plain_result(f"verified {passed} of {len(batch)}", batch), writes)
    return "done"
