"""IT06-07 and IT06-09 (impl 06 T06-27): spawn caps and dedup on full swarm runs.

Both rows drive a whole review through the real run driver (`Swarm.start`), real scheduler,
spawn broker, Blackboard, dedup, challenge and Writer steps, over the T06-22 `drv` set-up with
every role on the real spec 05 loop and spec 11 scripts (`tests.support._f06_swarm`). Expected
results are read from the ops store rows (`task`, `finding`) and from the run's real trace file
(`spawn_decision` events, the only record of a denied request).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from tests.integration.harness import _swarm_run_env as e
from tests.integration.harness._swarm_run_env import Drv
from tests.support import _f06_swarm as f6
from tests.support import _t0627_spawn as sp

from herness.core.types import ReportDraft
from herness.harness.swarm import _run_env
from herness.harness.swarm.lifecycle import RunResult
from herness.store.ops import query_findings, select_tasks

pytestmark = pytest.mark.integration

drv = e.drv  # fixtures
test_redactor = e.env_mod.test_redactor  # type: ignore[attr-defined]
__all__ = ["drv", "test_redactor"]


def _use(drv: Drv, mp: pytest.MonkeyPatch, *names: str, scripts: sp.SpawnScripts | None) -> None:
    """Plan the acceptance tasks `names` and run every role on `scripts`."""
    mp.setattr(
        _run_env, "get_pipeline",
        lambda kind, reader, window_end: sp.AcceptancePipeline(kind, names),
    )  # fmt: skip
    f6.use_real_agents(drv, mp, scripts or sp.SpawnScripts())


def _go(drv: Drv, depth: str, **override: int) -> tuple[RunResult, list[dict[str, Any]]]:
    swarm = drv.swarm(tracer_factory=_run_env._review_tracer)
    request = drv.request(depth=depth, budget_override=override or None)
    res = asyncio.run(swarm.start(request))
    path = Path(drv.hcfg.paths.data) / "traces" / f"{res.run_id}.jsonl"
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return res, [ev for ev in events if ev["type"] == "spawn_decision"]


def _reasons(decisions: list[dict[str, Any]], parent: str) -> list[str | None]:
    """The reasons (None = approved) of the decisions on `parent`, in request order."""
    return [ev["reason"] for ev in decisions if ev["parent_task_id"] == parent]


def _task_of(run_id: str, key: str) -> str:
    (task,) = [t for t in select_tasks(run_id) if t.spec.dedup_key == key]
    return task.task_id


def test_it06_07_requests_beyond_the_caps_are_denied_with_the_reason_of_their_rule(
    drv: Drv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT06-07 analysts of a standard run (`max_spawn_depth` raised to 2) request sub-tasks
    beyond each cap; every denial carries the reason of its rule (U06-65): a duplicate
    `duplicate:<task id of the first>`, a missing team `bad_scope`, a fourth child
    `max_children`, a child of a depth-2 task `max_depth`, a child with wider tools
    `tool_escalation`. Denied requests add no task row; the approved ones do; and no task
    row of the run has depth > 2."""
    _use(drv, monkeypatch, "P", "Q", "R", scripts=None)
    res, decisions = _go(drv, "standard", max_spawn_depth=2)
    assert res.status == "done"
    tasks = select_tasks(res.run_id)
    by_parent: dict[str, list[Any]] = {}
    for t in tasks:
        if t.parent_task_id is not None:
            by_parent.setdefault(t.parent_task_id, []).append(t)

    parent_p = _task_of(res.run_id, sp.p_key())
    children = sorted(by_parent[parent_p], key=lambda t: t.spec.objective)
    assert [t.spec.objective for t in children] == [
        "Follow up A on team t2.", "Follow up B on team t2.", "Follow up C on team t2.",
    ]  # fmt: skip
    first = children[0].task_id
    assert _reasons(decisions, parent_p) == [
        None, f"duplicate:{first}", "bad_scope", None, None, "max_children",
    ]  # fmt: skip
    assert _reasons(decisions, _task_of(res.run_id, sp.q_key())) == ["max_depth"]
    assert _reasons(decisions, _task_of(res.run_id, sp.r_key())) == ["tool_escalation"]
    assert [t.spec.objective for t in by_parent[first]] == ["Follow up G on team t2."]
    assert _reasons(decisions, first) == [None]
    assert all(t.spec.depth == 1 for t in children)
    assert [t.spec.depth for t in by_parent[first]] == [2]
    assert not [t for t in tasks if t.spec.objective.startswith("Follow up D")]
    assert not [t for t in tasks if "missing team" in t.spec.objective]
    assert not [t for t in tasks if t.spec.objective.startswith(("Follow up X", "Follow up E"))]
    assert max(t.spec.depth for t in tasks) <= 2
    assert {ev["parent_task_id"] for ev in decisions if ev["approved"]} == {parent_p, first}


def test_it06_07_spawn_disabled_in_fast_mode(drv: Drv, monkeypatch: pytest.MonkeyPatch) -> None:
    """IT06-07 a fast run (`max_spawn_depth` 0) denies a request with `spawn_disabled`; the
    run stays at depth 0 and gains no task."""
    _use(drv, monkeypatch, "P", scripts=None)
    res, decisions = _go(drv, "fast")
    assert res.status == "done"
    assert _reasons(decisions, _task_of(res.run_id, sp.p_key())) == ["spawn_disabled"] * 6
    assert all(t.parent_task_id is None and t.spec.depth == 0 for t in select_tasks(res.run_id))


def test_it06_07_task_cap_denies_with_max_tasks(drv: Drv, monkeypatch: pytest.MonkeyPatch) -> None:
    """IT06-07 with `max_tasks_per_run` 2 the planned task and one approved child fill the
    cap: the next request is denied `max_tasks` and no task beyond the cap is inserted."""
    scripts = sp.SpawnScripts(asks={"P": sp.ASKS["P"][:1] + sp.ASKS["P"][3:5]}, child_asks={})
    _use(drv, monkeypatch, "P", scripts=scripts)
    res, decisions = _go(drv, "standard", max_tasks_per_run=2, max_spawn_depth=2)
    assert res.status in {"done", "partial"}
    parent_p = _task_of(res.run_id, sp.p_key())
    assert _reasons(decisions, parent_p) == [None, "max_tasks", "max_tasks"]
    analysts = [t for t in select_tasks(res.run_id) if t.role == "analyst"]
    assert len(analysts) == 2
    assert max(t.spec.depth for t in select_tasks(res.run_id)) <= 2


def test_it06_07_run_budget_denies_with_budget(drv: Drv, monkeypatch: pytest.MonkeyPatch) -> None:
    """IT06-07 with a run token budget below the cost of a child (half the analyst budget)
    the request is denied `budget` and nothing is spawned."""
    scripts = sp.SpawnScripts(asks={"P": sp.ASKS["P"][:1]}, child_asks={})
    _use(drv, monkeypatch, "P", scripts=scripts)
    res, decisions = _go(drv, "standard", max_spawn_depth=2, run_tokens=40_000)
    parent_p = _task_of(res.run_id, sp.p_key())
    assert _reasons(decisions, parent_p) == ["budget"]
    assert all(t.parent_task_id is None for t in select_tasks(res.run_id))


KEEP = ("Team t1 had [[n1]] incidents", 0.9)
DUPLICATE = ("Team t1 recorded [[n1]] incidents this year", 0.6)
OTHER_TEAM = ("Team t2 had [[n1]] incidents", 0.8)


def test_it06_09_two_similar_findings_one_is_merged_into_the_other(
    drv: Drv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT06-09 two analysts of one run post two similar findings (same team, same cited
    number, one `query_set`); after the analyst phase exactly one is `merged` into the other
    (U06-103, the duplicate with the lower `confidence x numbers`), the survivor goes on to
    the Skeptic and is `verified`, the merged one is never challenged, and a finding of
    another team is untouched. The Writer's draft cites the survivor only."""
    drv.set_pipeline(t1=2, t2=1)
    claims = {"0": KEEP, "1": DUPLICATE, "50": OTHER_TEAM}
    f6.use_real_agents(drv, monkeypatch, sp.DupScripts(claims=claims))
    res = asyncio.run(drv.swarm().start(drv.request(depth="standard")))
    assert res.status == "done"
    rows = {f.claim: f for f in query_findings(res.run_id, limit=100)}
    assert set(rows) == {KEEP[0], DUPLICATE[0], OTHER_TEAM[0]}
    keep, dup, other = rows[KEEP[0]], rows[DUPLICATE[0]], rows[OTHER_TEAM[0]]
    assert [f.status for f in rows.values() if f.status == "merged"] == ["merged"]
    assert (dup.status, dup.merged_into) == ("merged", keep.finding_id)
    assert (keep.status, keep.merged_into) == ("verified", None)
    assert (other.status, other.merged_into) == ("verified", None)
    assert dup.challenge == []
    assert [c.verdict for c in keep.challenge] == ["uphold"]
    draft = ReportDraft.model_validate_json(Path(str(res.draft_path)).read_bytes())
    cited = {fid for rec in draft.recommendations for fid in rec.finding_ids}
    cited |= {fid for sec in draft.sections for p in sec.paragraphs for fid in p.finding_ids}
    assert keep.finding_id in cited
    assert dup.finding_id not in cited
