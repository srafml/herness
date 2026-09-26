"""Fault tests for herness.harness.verifier.Verifier (FT05-03, FT05-05; flow F05-08).

The impl 08 fault hook (T08-08) and its JSON plan loader do not exist yet; the Verifier calls
the `_shims.fault_point` no-op. `_KillPlan` is a test-local stand-in for the JSON plan
`{"point": "verifier.mid_batch", "action": "kill"}`, honoured only with `HERNESS_ENV=test`
like the real loader. The kill is simulated by a `BaseException` that unwinds the verifying
thread (no process is killed); the rerun uses a fresh `Verifier` and pool, as a restarted
process would. Re-point to the `fault_plan` fixture when T08-08 lands.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from tests.support.harness_fakes import FakeOps
from tests.unit.harness import _verifier_standin as sd

from herness.core.types import Evidence, ReportDraft, VerificationResult
from herness.harness.llm.settings import SqlSettings, VerifierSettings
from herness.harness.verifier import Verifier
from herness.harness.warehouse import WarehousePool
from herness.store.ops import _shims

pytestmark = pytest.mark.fault

PLAN = json.dumps({"point": "verifier.mid_batch", "action": "kill"})
PAY = {"team": "payments", "week": "2026-09-21"}


class _Killed(BaseException):
    """The simulated process kill (not an Exception, so nothing in the Verifier catches it)."""


class _KillPlan:
    """Stand-in for a loaded JSON fault plan with one `kill` rule."""

    def __init__(self, plan_json: str) -> None:
        rule = json.loads(plan_json)
        self.point: str = rule["point"]
        self.action: str = rule["action"]
        self.hits: list[str] = []

    def __call__(self, name: str, **labels: str) -> None:
        del labels
        if os.environ.get("HERNESS_ENV") != "test":
            return
        self.hits.append(name)
        if name == self.point and self.action == "kill":
            raise _Killed(name)


@dataclass
class _Env:
    tmp: Path
    ops: FakeOps
    team: Evidence

    def verifier(self, pool: WarehousePool) -> Verifier:
        return Verifier(
            self.ops,
            pool,
            VerifierSettings(),
            allowed_numeral_patterns=sd.PATTERNS,
            sql=SqlSettings(),
        )


@pytest.fixture
def env(tmp_path: Path) -> _Env:
    sd.make_build(tmp_path)
    ops = FakeOps({sd.FND_VERIFIED: "verified"})
    pool = WarehousePool(tmp_path, SqlSettings())
    try:
        team = sd.record(pool, sd.TEAM_SQL)
    finally:
        pool.close_all()
    ops.record_evidence(team)
    return _Env(tmp_path, ops, team)


def _stable(result: VerificationResult) -> dict[str, Any]:
    return result.model_dump(exclude={"verified_at", "duration_ms"})


def _draft(team_qid: str) -> ReportDraft:
    number = sd.ref("n1", 12, team_qid, "incidents", PAY)
    fields = {
        "run_id": "run_" + "C" * 26,
        "kind": "funding_review",
        "depth": "standard",
        "profile": "local",
        "build_id": sd.BUILD_ID,
        "title": "Funding review Q3 2026",
        "sections": [
            {
                "id": "executive_summary",
                "title": "Summary",
                "paragraphs": [
                    {
                        "text": "Payments had [[n1]] incidents.",
                        "numbers": [number.model_dump()],
                        "finding_ids": [sd.FND_VERIFIED],
                    },
                    {"text": "Search had 5 incidents.", "numbers": [], "finding_ids": []},
                ],
            }
        ],
        "recommendations": [],
        "ranked_entities": [],
        "caveats": [],
        "prior_outcomes_commentary": None,
        "banners": [],
        "flags": {},
        "contested": [],
        "removed": [],
        "coverage": {
            "planned_tasks": 1,
            "done_tasks": 1,
            "dead_tasks": 0,
            "must_cover_total": 0,
            "must_cover_done": 0,
            "verified_findings": 0,
            "rejected_findings": 0,
            "publishable": True,
        },
        "dead_tasks": [],
        "query_ids": [team_qid],
        "verification": {
            "build_id": sd.BUILD_ID,
            "passed": True,
            "items": [],
            "n_numbers": 0,
            "n_failed": 0,
            "verified_at": "2026-09-25T12:00:00Z",
            "duration_ms": 0,
        },
    }
    return ReportDraft.model_validate(fields)


def test_ft05_03_kill_mid_batch_then_rerun_identical(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FT05-03 plan `verifier.mid_batch` kill: the batch dies; the rerun twice is identical."""
    draft = _draft(env.team.query_id)
    plan = _KillPlan(PLAN)
    monkeypatch.setattr(_shims, "fault_point", plan)
    monkeypatch.setenv("HERNESS_ENV", "test")
    pool = WarehousePool(env.tmp, SqlSettings())
    try:
        with pytest.raises(_Killed):
            env.verifier(pool).verify_draft(draft, sd.BUILD_ID)
    finally:
        pool.close_all()
    assert plan.hits == ["verifier.mid_batch"]

    monkeypatch.setattr(_shims, "fault_point", lambda name, **labels: None)
    pool = WarehousePool(env.tmp, SqlSettings())
    try:
        verifier = env.verifier(pool)
        first = verifier.verify_draft(draft, sd.BUILD_ID)
        second = env.verifier(pool).verify_draft(draft, sd.BUILD_ID)
    finally:
        pool.close_all()
    assert _stable(first) == _stable(second)
    assert [i.where for i in first.items] == [
        "title",
        "sections[0].title",
        "sections[0].paragraphs[0]",
        "sections[0].paragraphs[1]",
    ]
    assert [i.passed for i in first.items] == [True, True, True, False]


def test_ft05_03_plan_ignored_outside_test_env(env: _Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """FT05-03 the fault plan is honoured only with HERNESS_ENV=test."""
    plan = _KillPlan(PLAN)
    monkeypatch.setattr(_shims, "fault_point", plan)
    monkeypatch.setenv("HERNESS_ENV", "prod")
    pool = WarehousePool(env.tmp, SqlSettings())
    try:
        result = env.verifier(pool).verify_draft(_draft(env.team.query_id), sd.BUILD_ID)
    finally:
        pool.close_all()
    assert len(result.items) == 4
    assert plan.hits == []


class _DeletingOps(FakeOps):
    """Deletes the build file right after the evidence is loaded (FT05-05)."""

    def __init__(self, source: FakeOps, build_file: Path) -> None:
        super().__init__(source.statuses)
        self.evidence = dict(source.evidence)
        self._build_file = build_file

    def get_evidence(self, query_id: str) -> Evidence | None:
        ev = super().get_evidence(query_id)
        self._build_file.unlink(missing_ok=True)
        return ev


def test_ft05_05_build_file_deleted_after_evidence_load(env: _Env) -> None:
    """FT05-05 the build file vanishes after evidence load: query_failed, not passed, no raise."""
    build_file = env.tmp / f"wh-{sd.BUILD_ID}.duckdb"
    ops = _DeletingOps(env.ops, build_file)
    pool = WarehousePool(env.tmp, SqlSettings())
    try:
        verifier = Verifier(
            ops, pool, VerifierSettings(), allowed_numeral_patterns=sd.PATTERNS, sql=SqlSettings()
        )
        numbers = [sd.ref("n1", 12, env.team.query_id, "incidents", PAY)]
        result = verifier.verify_numbers(sd.item("[[n1]]", numbers), sd.BUILD_ID)
        meta = sd.ref("n1", 7, sd.meta_query_id(), "incidents", {"team": "payments"})
        missing = verifier.verify_numbers(sd.item("[[n1]]", [meta]), sd.BUILD_ID)
    finally:
        pool.close_all()
    assert not build_file.exists()
    assert [c.result for c in result.items[0].checks] == ["query_failed"]
    assert not result.passed
    assert [c.result for c in missing.items[0].checks] == ["missing_query"]
