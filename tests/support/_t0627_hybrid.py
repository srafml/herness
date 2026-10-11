"""Phase 3 exit smoke set-up (impl 06 T06-27; ET06-01): hybrid run with a final Skeptic.

`EtScripts` are the local `TaskScripts` of the run: the round 1 Skeptic asks for a revision of
the t1 finding (so the swarm plans a round 2), the revision analyst posts the revised finding.
`FinalWire` is the scripted Anthropic wire of the two off-network roles of a hybrid run, the
Writer and the final Skeptic (model role `skeptic_final`): it picks the script of each call by
the heading of the system prompt (the adapters send no DD11-02 headers on the real wire) and
builds it when the call comes, because the ids it must name exist only at run time.
`unsupported_in` runs spec 11 `count_unsupported` over every text unit of a draft (a
paragraph, a recommendation), each with its own numbers (the number ids repeat across units).
All names are import-safe.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

import httpx2
from tests.integration.harness import _swarm_run_env as e
from tests.support import _f06_swarm as f6
from tests.support import _t0627_spawn as sp
from tests.support.fake_llm import ScriptRouter, respx_router

from herness.core.numbers import compile_allowed_patterns
from herness.core.types import SKEPTIC_CHECKS, ReportDraft, TaskSpec
from herness.eval.grading import RerunFn, count_unsupported
from herness.eval.scripted import LLMScript, ScriptBook
from herness.store.ops import core, select_tasks

__all__ = ["EtScripts", "FinalWire", "unsupported_in"]

ACTIONS: Final = ["Cite the incident count query in the claim."]
REVISED: Final = "Team t1 had [[n1]] incidents, revised"


def _verdict(finding_id: str, verdict: str, note: str = "") -> dict[str, Any]:
    checks = [{"check": k, "result": "pass", "note": note} for k in SKEPTIC_CHECKS]
    actions = ACTIONS if verdict == "revise" else []
    return {
        "finding_id": finding_id, "checks": checks, "verdict": verdict,
        "required_actions": actions,
    }  # fmt: skip


class EtScripts(f6.TaskScripts):
    """Local scripts: the round 1 Skeptic revises the t1 finding and upholds the rest."""

    def _skeptic(self, spec: TaskSpec) -> list[dict[str, Any]]:
        revise = spec.round == 1 and spec.scope.entity_ids == ["t1"]
        out = _verdict(spec.inputs.finding_ids[0], "revise" if revise else "uphold")
        return [{"final": {"text": "checked"}}, {"final": {"output": out}}]

    def _analyst(self, spec: TaskSpec) -> list[dict[str, Any]]:
        if spec.revision_of is None:
            return super()._analyst(spec)
        done = {
            "final": {"output": {"summary": "revised", "unknowns": [], "suggested_followups": []}}
        }
        return [sp.post(spec, REVISED), {"final": {"text": "posted"}}, done]


@dataclass
class WireCall:
    """One request the scripted Anthropic wire answered: the role headers of the call."""

    role: str
    model_role: str
    key: str


@dataclass
class FinalWire:
    """The router plus the calls it answered; scripts are built per `(role, dedup key)`."""

    router: ScriptRouter
    portfolio_qid: str = e.PORTFOLIO_QID  # the query id the Writer's recommendation cites
    calls: list[WireCall] = field(default_factory=list)
    books: dict[tuple[str, str], ScriptBook] = field(default_factory=dict)

    def finals(self) -> list[WireCall]:
        """The calls of final Skeptic tasks (model role `skeptic_final`)."""
        return [c for c in self.calls if c.model_role == "skeptic_final"]

    def writers(self) -> list[WireCall]:
        """The calls of the Writer."""
        return [c for c in self.calls if c.role == "writer"]

    def describe(self, role: str) -> tuple[WireCall, str]:
        """The call of `role` and the finding it challenges: a Skeptic call comes from the
        one final-round Skeptic task that is running (the pack names its finding by token)."""
        if role == "writer":
            return WireCall("writer", "writer", ""), ""
        (row,) = core.read_all("SELECT run_id FROM run", ())
        running = [
            t for t in select_tasks(str(row["run_id"]), roles={"skeptic"})
            if t.status == "running" and t.spec.model_role == "skeptic_final"
        ]  # fmt: skip
        (task,) = running
        call = WireCall("skeptic", task.spec.model_role, task.spec.dedup_key)
        return call, task.spec.inputs.finding_ids[0]

    def script(self, role: str, finding_id: str) -> LLMScript:
        """The script of a Writer call (the run's draft) or a final Skeptic call (uphold)."""
        (row,) = core.read_all("SELECT run_id FROM run", ())
        if role == "writer":
            out = json.loads(
                json.dumps(e.writer_output(str(row["run_id"]))).replace(
                    e.PORTFOLIO_QID, self.portfolio_qid
                )
            )
        else:
            out = _verdict(finding_id, "uphold")
        turns = [{"final": {"text": "done"}}, {"final": {"output": out}}] * 3
        return LLMScript.model_validate({"turns": turns, "source": f"et06_01#{role}"})


def _role(request: httpx2.Request) -> str:
    """The role of an Anthropic request, named by the heading of its system prompt."""
    body = json.loads(request.content)
    system = "".join(block["text"] for block in body["system"])
    for role, heading in (("skeptic", "# Skeptic" + chr(10)), ("writer", "# Writer" + chr(10))):
        if heading in system:
            return role
    return "?"


def final_wire(portfolio_qid: str = e.PORTFOLIO_QID) -> FinalWire:
    """A `ScriptRouter` on the Anthropic wire whose scripts are built at the call (see above);
    the Writer cites `portfolio_qid` as the query of its expected-USD number."""
    wire = FinalWire(respx_router(ScriptBook([])), portfolio_qid)
    router = wire.router
    reply = router._reply

    def scripted(request: httpx2.Request, which: Any) -> httpx2.Response:
        role = _role(request)
        call, finding_id = wire.describe(role)
        wire.calls.append(call)
        book_key = (role, finding_id)
        if book_key not in wire.books:
            wire.books[book_key] = ScriptBook([wire.script(role, finding_id)])
        router.book = wire.books[book_key]
        return reply(request, which)

    router._reply = scripted  # type: ignore[method-assign]
    return wire


def unsupported_in(
    draft: ReportDraft,
    *,
    evidence: Callable[[str, str], bool],
    rerun: RerunFn,
    allowed: Sequence[str],
    float_rel_tol: float,
) -> tuple[int, int]:
    """`(unsupported, total)` of spec 11 `count_unsupported` summed over every text unit of
    the draft: each paragraph (own numbers), each recommendation (headline and summary with
    its numbers), and every caveat (no numbers allowed)."""
    patterns = compile_allowed_patterns(list(allowed))
    units: list[tuple[str, list[Any]]] = [
        (p.text, list(p.numbers)) for s in draft.sections for p in s.paragraphs
    ]
    if draft.prior_outcomes_commentary is not None:
        units.append(
            (draft.prior_outcomes_commentary.text, list(draft.prior_outcomes_commentary.numbers))
        )
    units += [(f"{r.headline}. {r.summary}", list(r.numbers)) for r in draft.recommendations]
    units += [(str(caveat), []) for caveat in draft.caveats]
    unsupported = total = 0
    for text, numbers in units:
        report = count_unsupported(
            text, numbers, draft.build_id, evidence=evidence, rerun=rerun,
            allowed_patterns=patterns, float_rel_tol=float_rel_tol,
        )  # fmt: skip
        unsupported += report.unsupported
        total += report.total
    return unsupported, total
