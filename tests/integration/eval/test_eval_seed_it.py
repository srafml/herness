"""Integration tests of the T6 seeding and the golden LoRA exclusion (impl 11 U11-49, §5.6;
T11-33): IT11-23, IT11-24.

IT11-23 runs `handle_eval` in-process (the `--inline` path) on the `tiny_build` warehouse of
`_eval_it_env` with the real spec 07 `MemoryStore` and `tests.support.seed_ops.seed_prior_run`
injected through `EvalDeps` (herness.eval never imports tests/). The funding review is the
real swarm driver with scripted agents (`drv`); its plan context takes `prior_context` and
`prior_recs` from the real `MemoryStore.prior_context` (U07-81 via `_plan_ctx._prior_recs`)
and the pipeline's Writer input is the real `_review_common.writer_input`, so the Writer
receives exactly what the funding pipeline hands it. The golden F05 is a test-local question
on tiny_build (T11-32 owns the golden suite and the mock scripts).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest
from tests.integration.eval._eval_it_env import EvalEnv, eval_env, question
from tests.integration.harness import _swarm_run_env as e
from tests.support import seed_ops
from tests.unit.eval._golden_fixtures import write_suite
from tests.unit.eval._runner_env import lines, settings
from tests.unit.harness._blackboard_env import test_redactor
from tests.unit.harness.memory import _lora_env as lora_env
from tests.unit.harness.memory._write_env import PATTERNS, make_writer

from herness.core.types import MemoryRunContext
from herness.eval.golden import load_suite
from herness.eval.runner import EvalDeps, build_eval_payload, exit_code
from herness.harness.memory import MemoryStore, lora
from herness.harness.memory.settings import MemoryConfig
from herness.harness.memory.store import Embedder
from herness.harness.pipelines import _review_common
from herness.harness.swarm import _plan_ctx, _run_steps, writer
from herness.harness.swarm.run import Swarm
from herness.store import ops
from herness.store.ops import core, get_run, query_findings, record_evidence

pytestmark = pytest.mark.integration

drv = e.drv  # fixtures
__all__ = ["drv", "eval_env", "test_redactor"]


def _memory(env: EvalEnv, tmp_path: Path) -> MemoryStore:
    """The real spec 07 facade on the test's ops store (the IT06-37 set-up)."""
    w = make_writer(tmp_path)
    return MemoryStore(
        MemoryConfig(injection_patterns=PATTERNS), conn_factory=core.connection,
        vectors=w.vectors, embedder=Embedder(w.embed, model_name="bge-m3"), redactor=w.redactor,
        llms=None, allowed_numeral_patterns=env.drv.hcfg.app.reports.allowed_numeral_patterns,
        data_root=env.data_root,
    )  # fmt: skip


def _with_memory_context(monkeypatch: pytest.MonkeyPatch, agent: e.Agents) -> list[dict[str, Any]]:
    """Plan context with the real prior context, the real Writer input, and a recorder of
    every Writer task input."""

    async def plan_context(run: Any, knobs: Any, **kw: Any) -> Any:
        del knobs
        run_ctx = MemoryRunContext(
            run_id=run.run_id, run_kind=run.kind, role="planner",
            task_id=kw["planner_task_id"], build_id=run.build_id, profile=run.profile,
        )  # fmt: skip
        pc = await asyncio.to_thread(kw["memory"].prior_context, run_ctx)
        return e.plan_ctx_for(run).model_copy(
            update={"prior_context": pc.rendered, "prior_recs": _plan_ctx._prior_recs(pc.items)}
        )

    def writer_input(self: Any, ctx: Any, verified: list[Any]) -> dict[str, object]:
        return _review_common.writer_input(self.kind, ctx, ["executive_summary"], verified)

    monkeypatch.setattr(_run_steps, "build_plan_context", plan_context)
    monkeypatch.setattr(e.StubPipeline, "writer_input", writer_input)
    seen: list[dict[str, Any]] = []

    async def recording(*args: Any, **kwargs: Any) -> Any:
        seen.append(args[1])
        return await agent(*args, **kwargs)

    monkeypatch.setattr(writer, "run_agent", recording)
    return seen


def test_it11_23_seed_prior_run_then_f05_writer_sees_both_rec_ids(
    eval_env: EvalEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT11-23 `seed_prior_run` (through `EvalDeps`), then golden F05 (funding, `setup:
    seed_prior_run`) with scripted agents: both seeded `rec_id`s are in the Writer input and
    F05 is graded."""
    memory = _memory(eval_env, tmp_path / "mem")
    portfolio = e._evidence("SELECT 1 AS expected_impact_usd", {}, {"expected_impact_usd": 1200})
    assert record_evidence(portfolio.model_copy(update={"query_id": e.PORTFOLIO_QID}))
    writer_inputs = _with_memory_context(monkeypatch, eval_env.drv.agents)
    seeded: list[seed_ops.SeededPriorRun] = []

    def seeder(*args: Any, **kwargs: Any) -> seed_ops.SeededPriorRun:
        seeded.append(seed_ops.seed_prior_run(*args, **kwargs))
        return seeded[-1]

    base_deps = eval_env.deps

    def deps(ctx: Any) -> EvalDeps:
        build_id = eval_env.build.build_id
        base = base_deps(ctx)
        swarm = Swarm(eval_env.drv.hcfg.pipelines, eval_env.drv.llms, memory,
                      deps=dataclasses.replace(eval_env.drv.deps, current_build=lambda: build_id,
                                               build_exists=lambda b: b == build_id))  # fmt: skip
        eval_env.drv.swarms.append(swarm)
        return dataclasses.replace(base, swarm_for=lambda profile: swarm, memory=memory,
                                   seed_prior_run=seeder)  # fmt: skip

    suite = eval_env.suite([question("F05", "funding", setup="seed_prior_run")])
    payload = build_eval_payload(suite=str(suite), profile="synth", depth="fast", ids=["F05"],
                                 no_judge=True, settings=settings())  # fmt: skip
    monkeypatch.setattr(eval_env, "deps", deps)
    _, outcome = eval_env.handle(payload)

    assert outcome.status == "done"
    [prior] = seeded
    truth = eval_env.build.truth.plants.T6_outcomes
    assert set(prior.rec_ids) == {"paid", "unpaid"}
    # the seeded ops rows (U11-49 steps 1-5)
    run = get_run(prior.run_id)
    assert run is not None
    assert (run.kind, run.depth, run.profile, run.status) == ("funding_review", "standard",
                                                              "synth", "done")  # fmt: skip
    assert run.build_id == eval_env.build.build_id
    assert run.started_at == run.finished_at == truth.effective_at - seed_ops.PRIOR_RUN_AGE
    findings = query_findings(prior.run_id, statuses={"verified"}, limit=10)
    assert sorted((f.entity_type, f.entity_id) for f in findings) == sorted(
        ("candidate", side.epic_record_id) for side in (truth.paid, truth.unpaid)
    )
    recs = {r["rec_id"]: r for r in ops.run_recommendations(prior.run_id)}
    assert set(recs) == set(prior.rec_ids.values())
    assert {
        (r["kind"], r["target_type"], r["target_id"], r["expected_metric"]) for r in recs.values()
    } == {
        ("fund", "work_item", side.epic_record_id, "incident_count")
        for side in (truth.paid, truth.unpaid)
    }
    decisions = ops.latest_decisions(list(recs))
    assert {d["decision"] for d in decisions.values()} == {"accepted"}
    # both rec ids reach the Writer; F05 is graded
    assert writer_inputs  # every Writer turn of the funding review got the prior recs
    for writer_input in writer_inputs:
        text = json.dumps(writer_input)
        assert all(rec_id in text for rec_id in prior.rec_ids.values())
    [result] = lines(eval_env.out_dir(outcome.result["run_id"]) / "results.jsonl")
    assert result["id"] == "F05"
    assert result["status"] == "passed"
    assert {c["check"] for c in result["checks"]} >= {"numeric"}
    assert exit_code(outcome.model_dump()) == 0


def test_it11_24_golden_near_memory_question_excluded_from_lora_export(
    ops_store: object, tmp_path: Path
) -> None:
    """IT11-24 a `qa_pair` whose question is at cosine >= 0.90 to a golden question is
    excluded from the T07-20 LoRA export; the golden list is `load_suite`'s questions."""
    del ops_store
    suite = load_suite(write_suite(tmp_path / "golden.yaml", [
        question("G01", question=lora_env.GOLDEN), question("G02"),
    ]))  # fmt: skip
    golden = [q.question for q in suite.questions]
    assert lora_env.GOLDEN in golden
    le = lora_env.make_deps(tmp_path)
    [fp_near, fp_far] = lora_env.fingerprints(2)
    near = lora_env.seed_qa(2001, lora_env.seed_template(1001, fp_near), lora_env.PARAPHRASE)
    far = lora_env.seed_qa(2002, lora_env.seed_template(1002, fp_far), "Which team owns billing?")
    report = lora.export_lora(le.root, golden_questions=golden, deps=le.deps, now=lora_env.NOW)
    out = le.root / report.export_id
    exported = {line["id"] for name in ("train.jsonl", "val.jsonl")
                for line in lora_env.read_lines(out / name)}  # fmt: skip
    assert report.excluded_golden == 1
    assert near not in exported
    assert far in exported
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["golden_exclusion"] == "cosine"
    assert manifest["filters"]["golden_exclusion_cosine"] == 0.9
    # every golden question was embedded for the check: the suite is the source list
    assert set(golden) <= set(le.embedder.calls)
