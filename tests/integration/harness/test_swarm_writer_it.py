"""IT06-06, IT06-30, ST06-08: the Writer step over the real store, Blackboard writer and Verifier.

The scheduler IT setup (`sched`: migrated ops store, SQLite jobs backend, real Blackboard
writer) with a stub pipeline, the real spec 05 `Verifier` over the `_verifier_standin` DuckDB
build (`metrics.team_week`), and a scripted `run_agent` in `writer` (the Writer output per call,
or an exception). Gate 1 in IT06-06 runs through the real scheduler dispatch (`run_gate1`).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import structlog
from pydantic import JsonValue
from tests.integration.harness import test_swarm_challenge_it as ch_it
from tests.integration.harness import test_swarm_scheduler_it as base_it
from tests.unit.harness import _blackboard_env as env_mod
from tests.unit.harness import _verifier_standin as sd
from tests.unit.harness.swarm import _swarm_drafts as d

from herness.core import time as clock
from herness.core.errors import FatalError, ModelUnavailable
from herness.core.ids import IdKind, new_id
from herness.core.types import AgentResult, Evidence, Finding, ReportDraft, Usage
from herness.harness import verifier as verifier_mod
from herness.harness.blackboard import Blackboard
from herness.harness.llm.settings import SqlSettings, VerifierSettings
from herness.harness.swarm import challenge, writer
from herness.harness.swarm._writer_input import writer_spec
from herness.harness.warehouse import WarehousePool
from herness.store import ops
from herness.store.ops import (
    get_findings,
    insert_finding,
    insert_tasks,
    record_evidence,
    run_write,
    select_tasks,
)

pytestmark = pytest.mark.integration

bb_env = env_mod.bb_env  # fixtures
test_redactor = env_mod.test_redactor
sched = base_it.sched
_KEY = {"team": "payments", "week": "2026-09-21"}  # incidents 12 in the stand-in build


class _StoreOps:
    """The Verifier's `OpsHandle` reads over the real ops store."""

    def get_evidence(self, query_id: str) -> Evidence | None:
        return ops.get_evidence(query_id)

    def finding_statuses(self, finding_ids: Sequence[str]) -> dict[str, str]:
        return ops.finding_statuses(finding_ids)


class _Pipeline(d.StubPipeline):
    kind = "org_review"

    def writer_input(self, ctx: object, verified: list[Finding]) -> dict[str, object]:
        del ctx
        return {"findings": [f.finding_id for f in verified]}


@dataclass
class W:
    """One Writer setup: the scheduler setup, the team query id and the reports directory."""

    s: base_it.Sched
    qid: str
    reports: Path

    @property
    def path(self) -> Path:
        return self.reports / self.s.bb.run_id / "draft.json"

    def draft(self) -> ReportDraft:
        return ReportDraft.model_validate_json(self.path.read_bytes())

    def step(self) -> bool:
        async def go() -> bool:
            self.s.env.wake = asyncio.Event()
            return await writer.run_writer_step(self.s.env)

        return asyncio.run(go())

    def num(self, value: int = 12) -> dict[str, Any]:
        return {"id": "n1", "value": value, "unit": "count", "query_id": self.qid,
                "column": "incidents", "row_key": dict(_KEY)}  # fmt: skip

    def finding(self, value: int = 12, *, verified: bool = True) -> str:
        f = Finding.model_validate(
            {"finding_id": new_id(IdKind.FINDING), "run_id": self.s.bb.run_id,
             "task_id": self.s.bb.task_id, "author_role": "analyst",
             "claim": "Team payments had [[n1]] incidents", "entity_type": "team",
             "entity_id": "payments", "numbers": [self.num(value)], "query_ids": [self.qid],
             "confidence": 0.8, "created_at": clock.now()}
        )  # fmt: skip
        run_write(lambda conn: insert_finding(conn, f), op="test_finding")
        if verified:
            mark = env_mod.verification()
            run_write(lambda c: Blackboard.tx_mark_verified(c, f.finding_id, mark), op="t")
        return f.finding_id

    def para(self, text: str, fid: str, value: int = 12) -> dict[str, Any]:
        return {"text": text, "numbers": [self.num(value)], "finding_ids": [fid]}


@pytest.fixture
def wr(sched: base_it.Sched, tmp_path: Path) -> Iterator[W]:
    """The real Verifier over the stand-in build, the team evidence in the ops store, a stub
    pipeline and plan context, and the reports root under `tmp_path`."""
    sd.make_build(tmp_path / "wh")
    pool = WarehousePool(tmp_path / "wh", SqlSettings())
    ev = sd.record(pool, sd.TEAM_SQL, run_id=sched.bb.run_id)
    assert record_evidence(ev)
    env = sched.env
    env.verifier = verifier_mod.Verifier(
        _StoreOps(), pool, VerifierSettings(), allowed_numeral_patterns=sd.PATTERNS,
        sql=SqlSettings(),
    )  # type: ignore[arg-type]  # fmt: skip
    env.pipeline = _Pipeline()  # type: ignore[assignment]
    env.plan_ctx = d.plan_ctx()
    paths = env.hcfg.paths.model_copy(update={"data": tmp_path / "data"})
    env.hcfg = env.hcfg.model_copy(update={"paths": paths})
    try:
        yield W(sched, ev.query_id, tmp_path / "data" / "reports")
    finally:
        pool.close_all()


def _result(output: dict[str, JsonValue] | None) -> AgentResult:
    return AgentResult(
        status="completed", stop_reason="final", output=output, steps=1,
        usage=Usage(input_tokens=10, output_tokens=5), cost_usd=Decimal("0.01"), query_ids=[],
        finding_ids=[],
    )  # fmt: skip


def script(monkeypatch: pytest.MonkeyPatch, outputs: list[Any]) -> list[dict[str, Any]]:
    """Replace `writer.run_agent`: call `i` answers `outputs[i]` (an output dict, or an
    exception to raise); returns the task inputs seen."""
    calls: list[dict[str, Any]] = []

    async def fake(role: Any, task_input: Any, *args: Any, **kw: Any) -> AgentResult:
        del role, args, kw
        calls.append(task_input)
        out = outputs[len(calls) - 1]
        if isinstance(out, BaseException):
            raise out
        return _result(out)

    monkeypatch.setattr(writer, "run_agent", fake)
    return calls


def out(*paras: dict[str, Any], caveats: Sequence[str] = ()) -> dict[str, JsonValue]:
    return {
        "title": "Org review", "recommendations": [], "caveats": list(caveats),
        "sections": [{"id": "executive_summary", "title": "Summary", "paragraphs": list(paras)}],
        "prior_outcomes_commentary": None,
    }  # fmt: skip


def _writer_row(w: W) -> Any:
    [row] = select_tasks(w.s.bb.run_id, roles={"writer"})
    return row


# --- IT06-06


def test_it06_06_gates_remove_wrong_number_and_stray_numeral(
    wr: W, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT06-06 gate 1 rejects a finding with a wrong number (`verifier_fail`); at gate 2 the
    paragraph with a wrong number and the one with a stray numeral are removed and listed in
    `removed`; the verified paragraph stays and the final verification passes."""
    good, bad = wr.finding(verified=False), wr.finding(99, verified=False)
    ch_it.run(wr.s, challenge.run_gate1)
    found = get_findings([good, bad])
    assert found[good].status == "verified"
    assert found[bad].status == "rejected"
    assert found[bad].verification is not None
    assert found[bad].verification.reason == "verifier_fail"
    wr.s.env.knobs = wr.s.env.knobs.model_copy(update={"writer_fix_passes": 0})
    calls = script(monkeypatch, [out(
        wr.para("Payments closed [[n1]] incidents", good, 13),
        wr.para("Payments spent $2M on [[n1]] incidents", good),
        wr.para("Payments had [[n1]] incidents", good),
    )])  # fmt: skip
    with structlog.testing.capture_logs() as logs:
        assert wr.step() is False
    assert len(calls) == 1
    assert calls[0]["findings"] == [good]
    draft = wr.draft()
    assert [p.text for p in draft.sections[0].paragraphs] == ["Payments had [[n1]] incidents"]
    assert draft.removed == [
        {"where": "sections[0].paragraphs[0]", "reason": "gate2"},
        {"where": "sections[0].paragraphs[1]", "reason": "gate2"},
    ]
    assert draft.verification.passed is True
    assert draft.mode == "full"
    assert "partial_run" in draft.banners
    assert draft.coverage.verified_findings == 1
    assert draft.coverage.rejected_findings == 1
    assert draft.query_ids == [wr.qid]
    row = _writer_row(wr)
    assert row.status == "done"
    assert row.result["draft_path"] == str(wr.path)
    assert row.result["fix_passes"] == 0
    assert row.result["tokens"] == {"input": 10, "output": 5}
    gate2 = [e for e in logs if e["event"] == "harness.gate2.completed"]
    assert [(e["passed"], e["removed"], e["fix_passes"]) for e in gate2] == [(True, 2, 0)]
    written = [e["path"] for e in logs if e["event"] == "harness.draft.written"]
    assert written == [str(wr.path)]


# --- IT06-30


def test_it06_30_fix_pass_used_and_pass_file_loaded_on_resume(
    wr: W, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT06-30 the first draft fails gate 2; the run stops between passes (cancelled, task
    released); the resumed step loads `draft.pass0.json`, runs only the fix pass, which
    passes, and `draft.json` keeps every paragraph; a done step returns the stored verdict."""
    good = wr.finding()
    failing = out(wr.para("Payments had [[n1]] incidents and 3 outages", good))
    script(monkeypatch, [failing, asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        wr.step()
    pass0 = wr.path.with_name("draft.pass0.json")
    assert pass0.is_file()
    assert not wr.path.exists()
    assert _writer_row(wr).status == "pending"
    passing = out(wr.para("Payments had [[n1]] incidents", good))
    calls = script(monkeypatch, [passing])
    assert wr.step() is False  # no recommendation: never publishable
    assert len(calls) == 1
    fix = calls[0]
    assert fix["previous"]["sections"][0]["paragraphs"][0]["text"].endswith("3 outages")
    assert fix["rule_violations"] == [{"where": "sections[0].paragraphs[0]", "reason": "gate2"}]
    assert [i["where"] for i in fix["verifier_report"]] == ["sections[0].paragraphs[0]"]
    assert fix["findings"] == [good]
    draft = wr.draft()
    assert draft.removed == []
    assert draft.verification.passed is True
    assert [p.text for p in draft.sections[0].paragraphs] == ["Payments had [[n1]] incidents"]
    row = _writer_row(wr)
    assert (row.status, row.attempts, row.result["fix_passes"]) == ("done", 1, 1)
    assert json.loads(pass0.read_text(encoding="utf-8"))["sections"][0]["paragraphs"]
    script(monkeypatch, [])
    assert wr.step() is False  # done with draft.json: stored verdict, no model call


def test_it06_30_retryable_failure_claims_again(wr: W, monkeypatch: pytest.MonkeyPatch) -> None:
    """IT06-30 a retryable error fails the attempt (`pending`) and the step claims the task
    again; the second attempt writes the draft."""
    good = wr.finding()
    calls = script(monkeypatch, [ModelUnavailable("down"), out(wr.para("Ok [[n1]]", good))])
    assert wr.step() is False
    assert len(calls) == 2
    row = _writer_row(wr)
    assert (row.status, row.attempts) == ("done", 2)
    assert wr.draft().mode == "full"


def test_it06_30_dead_writer_writes_findings_only_draft(
    wr: W, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT06-30 (U06-115 step 11, R-49) a Writer without output is dead: `draft.json` holds the
    findings-only draft of the verified findings, verified by gate 2; not publishable."""
    good = wr.finding()
    script(monkeypatch, [None])
    assert wr.step() is False
    assert _writer_row(wr).status == "dead"
    draft = wr.draft()
    assert draft.mode == "findings_only"
    assert [p.finding_ids for p in draft.sections[0].paragraphs] == [[good]]
    assert draft.verification.passed is True
    assert "partial_run" in draft.banners
    assert draft.coverage.publishable is False


# --- ST06-08


def test_st06_08_writer_numerals_removed_before_draft(
    wr: W, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST06-08 the Writer adds "$2M" in prose and a numeral in a caveat, and keeps them in
    every fix pass: both are removed before `draft.json` is written."""
    good = wr.finding()
    bad = out(
        wr.para("Payments had [[n1]] incidents", good),
        wr.para("Fixing it saves $2M a year", good),
        caveats=["Covers 90 days of tickets", "Ticket text is redacted"],
    )  # fmt: skip
    passes = wr.s.env.knobs.writer_fix_passes
    calls = script(monkeypatch, [bad] * (passes + 1))
    assert wr.step() is False
    assert len(calls) == passes + 1
    text = wr.path.read_text(encoding="utf-8")
    assert "$2M" not in text
    assert "90 days" not in text
    draft = wr.draft()
    assert draft.caveats == ["Ticket text is redacted"]
    assert {(r["where"], r["reason"]) for r in draft.removed} == {
        ("sections[0].paragraphs[1]", "gate2"),
        ("caveats[0]", "uncited_numeral"),
        ("caveats[0]", "gate2"),
    }
    assert draft.verification.passed is True


def test_st06_08_section_title_numeral_removed_before_draft(
    wr: W, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST06-08 (review round 1) the Writer puts an uncited numeral in a section title in every
    pass: the title is replaced by the section default and listed in `removed`."""
    good = wr.finding()
    bad = out(wr.para("Payments had [[n1]] incidents", good))
    bad["sections"][0]["title"] = "Cut costs 40% now"  # type: ignore[index, call-overload]
    script(monkeypatch, [bad] * (wr.s.env.knobs.writer_fix_passes + 1))
    assert wr.step() is False
    assert "40%" not in wr.path.read_text(encoding="utf-8")
    draft = wr.draft()
    assert draft.sections[0].title == "Executive summary"
    assert draft.removed == [{"where": "sections[0].title", "reason": "gate2"}]
    assert draft.verification.passed is True


# --- review round 1: task-state invariant (U06-115 steps 1 and 11)


def _writer_task(w: W, status: str) -> str:
    spec = writer_spec(w.s.env)
    run_write(lambda conn: insert_tasks(conn, [spec], now=clock.now()), op="test_writer")
    run_write(
        lambda conn: conn.execute("UPDATE task SET status = ? WHERE task_id = ?",
                                  (status, spec.task_id)),
        op="test_writer_status",
    )  # fmt: skip
    return spec.task_id


@pytest.mark.parametrize("status", ["running", "done"])
def test_it06_30_unrecovered_state_fails_closed(
    wr: W, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    """IT06-30 a Writer task left `running` (crash not recovered) or `done` without
    `draft.json` raises FatalError; no model call and no draft or pass file is written."""
    wr.finding()
    _writer_task(wr, status)
    calls = script(monkeypatch, [])
    with pytest.raises(FatalError, match="invariant"):
        wr.step()
    assert calls == []
    assert not wr.reports.exists() or list(wr.reports.rglob("*.json")) == []
    assert _writer_row(wr).status == status


def test_it06_30_dead_at_entry_writes_findings_only(wr: W, monkeypatch: pytest.MonkeyPatch) -> None:
    """IT06-30 a Writer task already `dead` at entry gets the findings-only draft, no call."""
    good = wr.finding()
    _writer_task(wr, "dead")
    calls = script(monkeypatch, [])
    assert wr.step() is False
    assert calls == []
    draft = wr.draft()
    assert draft.mode == "findings_only"
    assert [p.finding_ids for p in draft.sections[0].paragraphs] == [[good]]


def test_it06_30_lost_claim_fails_closed(wr: W, monkeypatch: pytest.MonkeyPatch) -> None:
    """IT06-30 a pending Writer task whose claim is lost raises FatalError, writes nothing."""
    wr.finding()
    monkeypatch.setattr(writer, "claim_task", lambda task_id: False)
    script(monkeypatch, [])
    with pytest.raises(FatalError, match="claim lost"):
        wr.step()
    assert not wr.path.exists()
