"""IT06-10, IT06-28, IT06-29 at unit level (T06-17): `evaluate_crosschecks` over a fake
`CrosscheckReader`, the scheduler dispatch of skeptic and verifier tasks to `challenge`."""

from __future__ import annotations

import asyncio
from collections.abc import Collection, Mapping, Sequence

import pytest
from tests.unit.harness.swarm._swarm_findings import fid, finding, knobs, number, qid, tid

from herness.core.types import EntityScope, Finding, TaskInputs, TaskSpec
from herness.harness.findings import compute_dedup_key
from herness.harness.pipelines.settings import CrosscheckSettings
from herness.harness.swarm import challenge, scheduler
from herness.harness.swarm.routing import default_tools

pytestmark = pytest.mark.unit

_ORIG_SQL = "SELECT count(*) AS incidents FROM core.incident WHERE team_id = 't1'"
_OTHER_SQL = "SELECT sum(n) AS incidents FROM metrics.incident_fact WHERE team_id = 't1'"
_TOL = CrosscheckSettings()


class Reader:
    """In-memory `CrosscheckReader`."""

    def __init__(
        self,
        tasks: Sequence[TaskSpec] = (),
        findings: Sequence[Finding] = (),
        posted: Mapping[str, list[Finding]] | None = None,
        sql: Mapping[str, str] | None = None,
    ) -> None:
        self.tasks = list(tasks)
        self.by_id = {f.finding_id: f for f in findings}
        self.posted = dict(posted or {})
        self.sqls = dict(sql or {})

    def crosscheck_tasks(self) -> Sequence[TaskSpec]:
        return self.tasks

    def findings(self, finding_ids: Collection[str]) -> Mapping[str, Finding]:
        return {i: self.by_id[i] for i in finding_ids if i in self.by_id}

    def task_findings(self, task_id: str) -> Sequence[Finding]:
        return self.posted.get(task_id, [])

    def sql(self, query_id: str) -> str | None:
        return self.sqls.get(query_id)


def xc_task(i: int, target: str, number_id: str = "n1") -> TaskSpec:
    scope = EntityScope(entity_type="team", entity_ids=["t1"])
    objective = f"Compute independently way {i}"
    return TaskSpec(
        task_id=tid(i), run_id="run_" + "0" * 26, role="analyst", specialty="crosscheck",
        objective=objective, scope=scope,
        inputs=TaskInputs(finding_ids=[target], notes=f"target_number={number_id}"),
        tools=default_tools(
            "analyst", "crosscheck", "standard", child_depth=0, knobs=knobs("standard")
        ),
        budget=knobs("standard").analyst_budget, model_role="analyst",
        dedup_key=compute_dedup_key("analyst", "crosscheck", scope, objective),
    )  # fmt: skip


def posted(i: int, value_q: int, *, unit: str = "count") -> Finding:
    """A crosscheck finding with one number of `unit` through query `value_q`."""
    n = number(1, q=value_q, unit=unit, column="other")
    return finding(i, numbers=[n], task=50 + i, claim="Recomputed [[n1]]")


def ways(n: int) -> object:
    return knobs("standard").model_copy(update={"crosscheck_ways": n})


def test_it06_10_agreeing_value_through_supersedes_chain() -> None:
    """IT06-10 a crosscheck task on the original counts for the revised finding's number with
    the same (query_id, column, row_key); an independent agreeing value is agreed."""
    orig = finding(1)
    revised = finding(2).model_copy(update={"supersedes": orig.finding_id})
    reader = Reader(
        [xc_task(10, orig.finding_id)], [orig], {tid(10): [posted(3, 2)]},
        {qid(1): _ORIG_SQL, qid(2): _OTHER_SQL},
    )  # fmt: skip
    out = challenge.evaluate_crosschecks(revised, reader=reader, knobs=ways(2), tolerances=_TOL)  # type: ignore[arg-type]
    assert out == challenge.CrosscheckOutcome(out.checks, disagree=False, incomplete=False)
    [cc] = out.checks
    assert (cc.number_id, cc.query_ids, cc.values, cc.agreed) == (
        "n1",
        [qid(1), qid(2)],
        [7.0, 7.0],
        True,
    )


def test_it06_29_invalid_values_make_it_incomplete() -> None:
    """IT06-29 missing evidence, a dependent SQL path, the same query, a wrong unit or no post
    leave the number without valid values: incomplete, not agreed, no disagreement."""
    f = finding(1)
    tasks = [xc_task(i, f.finding_id) for i in range(10, 15)]
    reader = Reader(
        tasks, [f],
        {tid(10): [posted(3, 2)], tid(11): [posted(4, 3)], tid(12): [posted(5, 1)],
         tid(13): [posted(6, 2, unit="usd")]},
        {qid(1): _ORIG_SQL, qid(3): _ORIG_SQL},
    )  # fmt: skip
    out = challenge.evaluate_crosschecks(f, reader=reader, knobs=ways(2), tolerances=_TOL)  # type: ignore[arg-type]
    assert (out.disagree, out.incomplete) == (False, True)
    [cc] = out.checks
    assert (cc.values, cc.agreed) == ([7.0], False)


def test_it06_10_disagreement_and_ways_three() -> None:
    """IT06-10 deep (ways 3): one agreeing and one far-off value → disagree; values kept."""
    f = finding(1)
    far = posted(4, 3).model_copy(
        update={"numbers": [number(1, q=3, column="other").model_copy(update={"value": 99})]}
    )
    reader = Reader(
        [xc_task(10, f.finding_id), xc_task(11, f.finding_id)], [f],
        {tid(10): [posted(3, 2)], tid(11): [far]},
        {qid(1): _ORIG_SQL, qid(2): _OTHER_SQL, qid(3): _OTHER_SQL + " GROUP BY team_id"},
    )  # fmt: skip
    out = challenge.evaluate_crosschecks(f, reader=reader, knobs=ways(3), tolerances=_TOL)  # type: ignore[arg-type]
    assert (out.disagree, out.incomplete) == (True, False)
    assert out.checks[0].values == [7.0, 7.0, 99.0]
    assert out.checks[0].agreed is False


def test_it06_10_unrelated_or_unmatched_tasks_are_skipped() -> None:
    """IT06-10 tasks on other findings, an unknown target number or a number the finding no
    longer has produce no record; a broken supersedes link ends the chain."""
    f = finding(1, numbers=[number(1, q=1, column="incidents")]).model_copy(
        update={"supersedes": fid(9)}
    )
    other = finding(5)
    tasks = [
        xc_task(10, other.finding_id),
        xc_task(11, f.finding_id, "n7"),
        xc_task(12, f.finding_id),
    ]
    tasks[2] = tasks[2].model_copy(update={"inputs": TaskInputs(finding_ids=[f.finding_id])})
    out = challenge.evaluate_crosschecks(
        f,
        reader=Reader(tasks, [other]),
        knobs=ways(2),
        tolerances=_TOL,  # type: ignore[arg-type]
    )
    assert out == challenge.CrosscheckOutcome([], disagree=False, incomplete=False)


def test_it06_28_scheduler_dispatches_challenge_roles(monkeypatch: pytest.MonkeyPatch) -> None:
    """IT06-28 the scheduler seam resolves skeptic and verifier tasks to `challenge` (imported
    on first dispatch); the verifier adapter forwards `(env, row)`; other roles keep the
    generic path."""
    monkeypatch.setattr(scheduler, "_ROLE_EXECUTORS", {})
    seen: list[tuple[object, object]] = []

    async def fake_verifier(env: object, row: object) -> None:
        seen.append((env, row))

    monkeypatch.setattr(challenge, "execute_verifier", fake_verifier)
    assert scheduler._executor("analyst") is None
    assert scheduler._executor("skeptic") is challenge.execute_skeptic
    verifier = scheduler._executor("verifier")
    assert verifier is not None
    assert verifier is scheduler._executor("verifier")
    env, row, ledger = object(), object(), object()
    asyncio.run(verifier(env, row, ledger))  # type: ignore[arg-type]
    assert seen == [(env, row)]
