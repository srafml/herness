"""Spawn-cap and dedup acceptance set-up (impl 06 T06-27; IT06-07, IT06-09).

Builds on the T06-22 `drv` set-up and the T06-26 real-loop scripts (`tests.support._f06_swarm`).
`AcceptancePipeline` plans up to three deterministic analyst tasks on the stub pipeline, each
with explicit tools so it can call `request_subtask`: `P` (team t1, depth 0), `Q` (team t2,
planted at depth 2: a parent whose child would be depth 3) and `R` (team t2, tools without
`list_findings`: a parent whose child would be wider). `SpawnScripts` makes each ask for
sub-tasks per `ASKS` before it posts its finding, and an approved sub-task ask as `CHILD_ASKS`
says. All names are import-safe (no work at import).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from tests.integration.harness import _swarm_run_env as e
from tests.support import _f06_swarm as f6

from herness.core.types import TaskBudget, TaskSpec
from herness.harness.swarm.routing import default_tools

__all__ = [
    "ASKS",
    "CHILD_ASKS",
    "AcceptancePipeline",
    "DupScripts",
    "SpawnScripts",
    "ask",
    "p_key",
    "post",
    "q_key",
    "r_key",
]

WORDS: Final = ("zero", "one", "two", "three", "four", "five", "six")
P_N, Q_N, R_N = 0, 50, 51  # `det_spec` numbers of the three planned tasks (objective suffix)


def ask(
    objective: str, *, ids: Sequence[str] = ("t2",), reason: str = "follow the incident trail"
) -> dict[str, Any]:
    """`request_subtask` arguments for a team-scoped follow-up (`reason` is free text: it is
    not part of the task's dedup key, and a call with identical arguments is never sent twice)."""
    return {
        "objective": objective, "specialty": "ops", "entity_type": "team",
        "entity_ids": list(ids), "reason": reason,
    }  # fmt: skip


def _key(team: str, n: int) -> str:
    return e.det_spec("run_" + "0" * 26, team, n, must=False).dedup_key


def p_key() -> str:
    return _key("t1", P_N)


def q_key() -> str:
    return _key("t2", Q_N)


def r_key() -> str:
    return _key("t2", R_N)


type Asks = Mapping[str, list[tuple[str, dict[str, Any]]]]

# Requests of each planned task, in order: (name, arguments).
ASKS: Final[Asks] = {
    "P": [
        ("child_a", ask("Follow up A on team t2.")),
        ("duplicate_a", ask("Follow up A on team t2.", reason="asked again")),
        ("bad_scope", ask("Follow up on a missing team.", ids=["t_missing"])),
        ("child_b", ask("Follow up B on team t2.")),
        ("child_c", ask("Follow up C on team t2.")),
        ("over_children", ask("Follow up D on team t2.")),
    ],
    "Q": [("over_depth", ask("Follow up X on team t2."))],
    "R": [("wider_tools", ask("Follow up E on team t2."))],
}
# Requests of an approved child, by the objective of the child (its follow-up is depth 2).
CHILD_ASKS: Final[Asks] = {
    "Follow up A on team t2.": [("grandchild", ask("Follow up G on team t2."))],
}


class AcceptancePipeline(e.StubPipeline):
    """Stub pipeline whose tasks are `P`, `Q` and `R` (those in `names`, see the module doc)."""

    def __init__(self, kind: str, names: tuple[str, ...] = ("P", "Q", "R")) -> None:
        super().__init__(kind, 0, 0, t2_must=False)
        self.names = names

    def deterministic_tasks(self, ctx: Any) -> list[TaskSpec]:
        base = default_tools("analyst", "general", ctx.depth, child_depth=0, knobs=ctx.knobs)
        full = sorted({*base, "request_subtask", "list_findings", "post_finding"})
        narrow = sorted(set(full) - {"list_findings"})
        run_id = ctx.run_id
        budget = TaskBudget(max_steps=25, max_tokens=80_000, wall_clock_s=600)
        tasks = {
            "P": e.det_spec(run_id, "t1", P_N, must=True).model_copy(
                update={"tools": full, "budget": budget}
            ),
            "Q": e.det_spec(run_id, "t2", Q_N, must=False).model_copy(
                update={"tools": full, "depth": 2, "budget": budget}
            ),
            "R": e.det_spec(run_id, "t2", R_N, must=False).model_copy(
                update={"tools": narrow, "budget": budget}
            ),
        }
        return [tasks[n] for n in self.names]

    def must_cover(self, ctx: Any) -> set[str]:
        del ctx
        return {"team:t1"}


def post(spec: TaskSpec, claim: str, *, confidence: float = 0.8) -> dict[str, Any]:
    """A scripted turn posting one finding of the task's team (real `post_finding` arguments)."""
    team = spec.scope.entity_ids[0]
    number = e._nref(1, e.incident_qid(team), "count", "incidents", 7) | {"format": None}
    args = {
        "claim": claim, "entity_type": "team", "entity_id": team, "query_ids": [],
        "confidence": confidence, "numbers": [number],
    }  # fmt: skip
    return {"tool_calls": [{"name": "post_finding", "arguments": args}]}


def _tag(spec: TaskSpec) -> str:
    """A word of letters that is unique to the task (a claim may not hold numerals)."""
    return "".join(chr(ord("a") + int(ch, 16)) for ch in spec.dedup_key[-6:])


@dataclass
class SpawnScripts(f6.TaskScripts):
    """Analysts post a finding, then ask for sub-tasks per `asks` / `child_asks` (a further
    finding after every two requests, as the loop stops a task after four steps in a row that
    add no finding or query)."""

    asks: Asks = field(default_factory=lambda: ASKS)
    child_asks: Asks = field(default_factory=lambda: CHILD_ASKS)

    def _requests(self, spec: TaskSpec) -> list[tuple[str, dict[str, Any]]]:
        by_key = {p_key(): "P", q_key(): "Q", r_key(): "R"}
        if spec.dedup_key in by_key:
            return self.asks.get(by_key[spec.dedup_key], [])
        return self.child_asks.get(spec.objective, [])

    def _analyst(self, spec: TaskSpec) -> list[dict[str, Any]]:
        team, tag = spec.scope.entity_ids[0], _tag(spec)
        steps = [post(spec, f"Team {team} had [[n1]] incidents ({tag})")]
        for i, (_name, args) in enumerate(self._requests(spec), start=1):
            steps.append({"tool_calls": [{"name": "request_subtask", "arguments": args}]})
            if i % 2 == 0:
                steps.append(post(spec, f"Team {team} had [[n1]] incidents ({tag}, {WORDS[i]})"))
        done = {
            "final": {"output": {"summary": "looked", "unknowns": [], "suggested_followups": []}}
        }
        return [*steps, {"final": {"text": "posted"}}, done]


@dataclass
class DupScripts(f6.TaskScripts):
    """Each planned analyst posts the finding `claims` gives for its objective's number
    (`det_spec` suffix) as `(claim, confidence)`; nothing else (IT06-09)."""

    claims: Mapping[str, tuple[str, float]] = field(default_factory=dict)

    def _analyst(self, spec: TaskSpec) -> list[dict[str, Any]]:
        claim, confidence = self.claims[spec.objective.rsplit("(", 1)[1].rstrip(".)")]
        done = {
            "final": {"output": {"summary": "looked", "unknowns": [], "suggested_followups": []}}
        }
        return [post(spec, claim, confidence=confidence), {"final": {"text": "posted"}}, done]
