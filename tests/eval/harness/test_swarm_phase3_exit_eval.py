"""ET06-01 (impl 06 T06-27, T20): the Phase 3 exit smoke of the swarm.

Part (a), `test_et06_01_hybrid_smoke_*`, runs on the dev box with the fake LLM: a standard
funding review in profile `hybrid` (the D5 substitute of T06-26: the hybrid profile on a test
tree with `chat_approved: true`), every role scripted. The local round 1 Skeptic asks for a
revision, so a round 2 runs: its Skeptic is the final one (model role `skeptic_final`) and
goes off-network to `claude-opus` on the scripted Anthropic wire through the real adapter and
the real spec 10 egress guard (spied on), like the Writer (T06-29 gap E). `herness.eval.runner`
is not on this base, so the Swarm public API is driven directly (the `herness eval` path is
the T09-26 carry-over).

Part (b), `test_et06_01_phase3_exit_5m_local30b`, is the spec's literal row (the synthetic 5M
dataset, `local-30b`, at most 90 minutes, 0 unsupported numbers in `draft.json`). It needs the
dev box GPU and the 5M build, so it is the manual Phase 3 exit: it skips unless
`HERNESS_ET06_01_CONFIG_DIR` names a config tree of profile `local` whose `paths.data` holds
the 5M build as the current build and whose `local-30b` client points at the running endpoint.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import pytest
from tests.integration.harness import _swarm_run_env as e
from tests.integration.harness._swarm_run_env import Drv
from tests.support import _f06_hybrid as fh
from tests.support import _f06_swarm as f6
from tests.support import _f08_llm as fl
from tests.support import _t0627_hybrid as th
from tests.support.egress_harness import egress_lines
from tests.support.fake_keyring import MemoryKeyring
from tests.unit.harness._blackboard_env import BUILD_ID

from herness.core import config as c
from herness.core import egress
from herness.core import redact as r
from herness.core.types import Evidence, ReportDraft
from herness.harness.llm.anthropic_client import AnthropicClient
from herness.harness.llm.registry import LLMRegistry
from herness.harness.llm.settings import VerifierSettings
from herness.harness.swarm import _run_steps, handler
from herness.harness.swarm.lifecycle import RunRequest, RunResult
from herness.harness.swarm.run import Swarm
from herness.store.ops import evidence as ops_evidence
from herness.store.ops import record_evidence, select_tasks

pytestmark = pytest.mark.eval

drv = e.drv  # fixtures
test_redactor = e.env_mod.test_redactor  # type: ignore[attr-defined]
__all__ = ["drv", "test_redactor"]

PORTFOLIO_SQL: Final = "SELECT 1200.50 AS expected_impact_usd"
API: Final = "https://api.anthropic.com/v1/messages"
WALL_CLOCK_LIMIT_S: Final = 90 * 60  # the spec's bound for the real run (ET06-01)
CONFIG_DIR_ENV: Final = "HERNESS_ET06_01_CONFIG_DIR"
SKIP_REASON: Final = (
    f"ET06-01 (b) is the manual Phase 3 exit on the dev box (GPU, vLLM local-30b, synthetic 5M "
    f"build): set {CONFIG_DIR_ENV} to a profile-local config tree whose paths.data holds the "
    "5M build as current and whose local-30b client points at the running endpoint"
)


def _rows(ev: Evidence) -> list[dict[str, Any]]:
    """The recorded result of the query (its stored sample), rows as column to cell; a
    non-integer number is a DECIMAL cell, as the warehouse holds money and scores."""
    return [
        {k: Decimal(str(v)) if isinstance(v, float) else v for k, v in row.items()}
        for row in ev.result_sample
    ]


def _has_evidence(query_id: str, build_id: str) -> bool:
    ev = ops_evidence.get_evidence(query_id)
    return ev is not None and ev.build_id == build_id


def _rerun(query_id: str) -> list[dict[str, Any]]:
    ev = ops_evidence.get_evidence(query_id)
    assert ev is not None, query_id
    return _rows(ev)


class Smoke:
    """The hybrid set-up of part (a): config, spied guard, scripted wire, adapter."""

    def __init__(
        self, hcfg: c.HernessConfig, llms: LLMRegistry, guard: fh.SpyGuard, wire: th.FinalWire
    ) -> None:
        self.hcfg, self.llms, self.guard, self.wire = hcfg, llms, guard, wire
        self.opus = AnthropicClient(llms.config("claude-opus"))
        self.elapsed_s = 0.0

    def refusals(self) -> list[dict[str, Any]]:
        """The guard's `blocked` egress log lines."""
        return [x for x in egress_lines(self.hcfg.paths.logs) if x.get("decision") == "blocked"]

    def sent(self) -> list[fh.Seen]:
        """The `POST /v1/messages` requests the guard decided on."""
        return [s for s in self.guard.seen if s.url == API]


@pytest.fixture
def smoke(
    drv: Drv,
    test_redactor: r.Redactor,
    fake_keyring: MemoryKeyring,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Smoke:
    """Profile `hybrid` with the chat approval flag (`fh.hybrid_config`), the real guard
    spied on, the scripted Anthropic wire, and a portfolio evidence row under its real query id
    that the plan context and the Writer's recommendation cite (the `drv` set-up cites a made-up
    id, which an audit by `count_unsupported` rightly finds unsupported)."""
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    hcfg = fh.hybrid_config(tmp_path / "hybrid", Path(drv.hcfg.paths.data))
    monkeypatch.setattr(r._State, "redactor", test_redactor)  # the config load dropped it
    guard = fh.SpyGuard(hcfg, fh.guard_redactor())
    monkeypatch.setattr(egress, "get_guard", lambda: guard)
    fake_keyring.store[("herness", "anthropic.api_key")] = "sk-ant-" + "et0601" + "unused"
    llms = LLMRegistry(hcfg.models, profile="hybrid", egress_enabled=True)
    portfolio = e._evidence(PORTFOLIO_SQL, {}, {"expected_impact_usd": 1200.5})
    assert record_evidence(portfolio)  # under its real, recomputable query id
    wire = th.final_wire(portfolio.query_id)
    wire.router.install(monkeypatch)

    async def plan_context(run: Any, knobs: Any, **kw: Any) -> Any:
        del knobs, kw
        ctx = e.plan_ctx_for(run)
        planned = {**ctx.portfolio, "query_ids": [portfolio.query_id]}
        return ctx.model_copy(update={"portfolio": planned})

    monkeypatch.setattr(_run_steps, "build_plan_context", plan_context)
    return Smoke(hcfg, llms, guard, wire)


def _run(drv: Drv, sm: Smoke, monkeypatch: pytest.MonkeyPatch) -> RunResult:
    """The standard hybrid funding review, timed (`sm.elapsed_s`)."""
    scripts = th.EtScripts()
    f6.use_real_agents(drv, monkeypatch, scripts, llms=sm.llms, real={"claude-opus": sm.opus})
    from dataclasses import replace  # noqa: PLC0415

    deps = replace(drv.deps, hcfg=sm.hcfg)
    swarm = Swarm(sm.hcfg.pipelines, sm.llms, drv.memory, deps=deps)  # type: ignore[arg-type]
    drv.swarms.append(swarm)
    start = time.perf_counter()
    res = asyncio.run(
        swarm.start(RunRequest.model_validate({"kind": "funding_review", "depth": "standard"}))
    )
    sm.elapsed_s = time.perf_counter() - start
    print(f"\nET06-01 (a) hybrid smoke wall time: {sm.elapsed_s:.2f} s (status {res.status})")  # noqa: T201
    return res


def test_et06_01_hybrid_smoke_final_skeptic_and_writer_run_off_network(
    drv: Drv, smoke: Smoke, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ET06-01 a standard hybrid funding review with a revision in round 1 ends `done`:
    the final Skeptic (round 2, model role `skeptic_final`) and the Writer are the only
    off-network callers; the real guard allows every one of their requests (purpose
    `reasoning_final`), refuses none (no PHONE hit on the build ids), the run did not fall
    back (no `fallback` event, no banner `hybrid_fallback`), and the final Skeptic's verdict
    is the one recorded on the revised finding that gate 1 verified."""
    res = _run(drv, smoke, monkeypatch)
    assert res.status == "done"
    wire = smoke.wire
    assert [c.role for c in wire.finals()] == ["skeptic"] * len(wire.finals())
    assert wire.finals()
    assert wire.writers()
    assert {c.role for c in wire.calls} == {"skeptic", "writer"}
    sent = smoke.sent()
    assert len(sent) == len(wire.calls) == len(smoke.wire.router.requests)
    assert {s.purpose for s in sent} == {"reasoning_final"}
    assert [s.blocked for s in sent] == [None] * len(sent)
    assert all(BUILD_ID in s.text() for s in sent)
    assert smoke.refusals() == []
    assert fl.events("fallback") == []
    draft = ReportDraft.model_validate_json(Path(str(res.draft_path)).read_bytes())
    assert "hybrid_fallback" not in draft.banners
    tasks = select_tasks(res.run_id)
    finals = [t for t in tasks if t.role == "skeptic" and t.spec.model_role == "skeptic_final"]
    assert finals
    assert {t.spec.round for t in finals} == {2}
    assert {t.status for t in finals} == {"done"}
    assert {t.spec.dedup_key for t in finals} == {c.key for c in wire.finals()}
    (revision,) = [t for t in tasks if t.role == "analyst" and t.spec.revision_of]
    assert revision.status == "done"
    assert {t.status for t in tasks if t.role == "skeptic" and t.spec.round == 1} == {"done"}
    assert not [t for t in tasks if t.role == "skeptic" and t.spec.round == 1 and t in finals]


def test_et06_01_hybrid_smoke_draft_has_no_unsupported_number(
    drv: Drv, smoke: Smoke, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ET06-01 `draft.json` of the hybrid smoke run holds 0 unsupported numbers by spec 11
    `count_unsupported` (stray numerals, stale number refs, orphan markers) over every
    paragraph and recommendation, with the numbers actually present (total > 0), and
    the wall time of the run is far inside the 90 minute bound of the row."""
    res = _run(drv, smoke, monkeypatch)
    assert res.status == "done"
    draft = ReportDraft.model_validate(json.loads(Path(str(res.draft_path)).read_text("utf-8")))
    unsupported, total = th.unsupported_in(
        draft, evidence=_has_evidence, rerun=_rerun,
        allowed=list(drv.hcfg.app.reports.allowed_numeral_patterns),
        float_rel_tol=VerifierSettings().float_rel_tol,
    )  # fmt: skip
    assert total > 0
    assert unsupported == 0
    assert smoke.elapsed_s < WALL_CLOCK_LIMIT_S


@pytest.mark.gpu
@pytest.mark.skipif(not os.environ.get(CONFIG_DIR_ENV), reason=SKIP_REASON)
def test_et06_01_phase3_exit_5m_local30b() -> None:
    """ET06-01 (T20, manual Phase 3 exit) a standard funding review of the synthetic 5M
    build with `local-30b` ends `done` in at most 90 minutes with 0 unsupported numbers in
    `draft.json` (spec 11 `count_unsupported`, evidence from the ops store, SQL re-run on the
    pinned warehouse). Runs only where `HERNESS_ET06_01_CONFIG_DIR` is set (see the module
    doc); the wall time is printed for the record."""
    from herness.harness.swarm.lifecycle import RunRequest as Request  # noqa: PLC0415
    from herness.harness.warehouse import WarehousePool  # noqa: PLC0415
    from herness.store.layout import data_layout  # noqa: PLC0415

    c.reset_config()
    hcfg = c.init_config("local", config_dir=Path(os.environ[CONFIG_DIR_ENV]), env=dict(os.environ))
    swarm = handler.build_swarm_from_config(hcfg)
    try:
        start = time.perf_counter()
        res = asyncio.run(
            swarm.start(Request.model_validate({"kind": "funding_review", "depth": "standard"}))
        )
        elapsed = time.perf_counter() - start
    finally:
        swarm.close()
    print(f"\nET06-01 (b) 5M local-30b wall time: {elapsed / 60:.1f} min (status {res.status})")  # noqa: T201
    assert res.status == "done"
    assert elapsed <= WALL_CLOCK_LIMIT_S
    draft = ReportDraft.model_validate_json(Path(str(res.draft_path)).read_bytes())
    pool = WarehousePool(data_layout(cfg=hcfg).warehouse, hcfg.models.harness.sql)

    def rerun(query_id: str) -> list[dict[str, Any]]:
        ev = ops_evidence.get_evidence(query_id)
        assert ev is not None, query_id
        cursor = pool.get(draft.build_id).cursor().execute(ev.sql, ev.params or None)
        names = [col[0] for col in cursor.description or ()]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

    try:
        unsupported, total = th.unsupported_in(
            draft, evidence=_has_evidence, rerun=rerun,
            allowed=list(hcfg.app.reports.allowed_numeral_patterns),
            float_rel_tol=hcfg.models.harness.verifier.float_rel_tol,
        )  # fmt: skip
    finally:
        pool.close_all()
    assert total > 0
    assert unsupported == 0
