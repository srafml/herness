"""Phase 3 eval gate ET05-01 (flows F05-01, F05-08; T05-28): in-process golden check.

Spec (impl 05 §11): "ET05-01 | F05-01, F05-08 | spec 11 golden suite, `local` profile, standard
depth (marker `eval`) | `herness eval --suite golden` | 0 unsupported numbers in verified
outputs; tool-call success ≥ 90 % (Phase 3 gate)".

The `herness eval` CLI and runner (T09-24, T11-30) and `tests/eval/golden.yaml` (U11-75) are not
built yet, so this is the in-process equivalent over FIXED SYNTHETIC fixtures (w28-s05 ruling;
no network, no real model): each golden question is one scripted chat run
(`tests/fixtures/llm_scripts/et05_01_golden/`, `FakeLLMClient`) of the real `run_agent` with the
real `HarnessHooks`, the real warehouse tools on the stand-in build
(`tests.support.warehouse_tools_build`), the real ops store `evidence` area and a real
`Tracer`. Its `ChatAnswer` is checked by the real `Verifier` and by spec 11's `count_unsupported`
(independent of the Verifier, TH11-12). The metrics follow impl 11 U11-62:
`unsupported_number_rate` = Σ unsupported / Σ total, `tool_call_success_rate` = `tool_call`
trace events with `ok = true` / all `tool_call` events. Thresholds are read from
`config/eval.yaml` (`unsupported_number_rate_max`, `tool_success_min.local`). Negative controls
(`et05_01_controls/`) prove the gate fails on a stray number and on failing tool calls. Every
run must stop on its final within steps, tokens and wall clock (TH05-08; ST05-08 covers the
runaway cases). Carry-over: re-point to `herness eval --suite golden` when T11-30 / T09-24 land.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml
from tests.support import loop_standin as ls
from tests.support import warehouse_tools_build as wb
from tests.support.dispatch_standin import use_test_config
from tests.support.fake_llm import FakeLLMClient
from tests.support.ops_store import OpsStoreHandle
from tests.support.tools_standin import StoreOps

from herness.core import config as c
from herness.core import redact as r
from herness.core.numbers import compile_allowed_patterns
from herness.core.redact_directory import NameDirectory
from herness.core.resilience import ProcessState, bind_ops_backend
from herness.core.settings import RedactionConfig
from herness.core.types import ChatAnswer, Evidence
from herness.eval.grading import count_unsupported
from herness.eval.settings import Thresholds, load_eval_config
from herness.harness import hooks as h
from herness.harness import loop
from herness.harness import warehouse_tools as wt
from herness.harness.llm.settings import SqlSettings, VerifierSettings
from herness.harness.roles import base
from herness.harness.roles.base import get_role
from herness.harness.tracing import Tracer
from herness.harness.verifier import Verifier, _configured_patterns
from herness.harness.warehouse import DuckWarehouse, WarehousePool
from herness.store.ops import evidence as ops_evidence
from herness.store.ops.resilience import SqliteResilienceBackend

pytestmark = pytest.mark.eval

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "tests" / "fixtures" / "llm_scripts"
GOLDEN = sorted((SCRIPTS / "et05_01_golden").glob("*.yaml"))
STRAY_NUMBER = SCRIPTS / "et05_01_controls" / "c1_stray_number.yaml"
FAILING_TOOLS = SCRIPTS / "et05_01_controls" / "c2_failing_tools.yaml"
PROFILE = "local"
TOOLS = ("list_tables", "describe_table", "run_sql")
MAX_STEPS = 8
MAX_TOKENS = 100_000
WALL_CLOCK_S = 120


class _RealOps(StoreOps):
    """`OpsHandle` reading and writing the real ops store `evidence` area (needs `ops_store`)."""

    def get_evidence(self, query_id: str) -> Evidence | None:
        return ops_evidence.get_evidence(query_id)

    def finding_statuses(self, finding_ids: Sequence[str]) -> dict[str, str]:
        return ops_evidence.finding_statuses(finding_ids)


@dataclass
class Outcome:
    """One golden question: the run, its verdicts and its `tool_call` trace events."""

    name: str
    status: str
    stop_reason: str
    steps: int
    tokens: int
    elapsed_s: float
    verified: bool
    verdict: str
    unsupported: int
    total_numbers: int
    tool_ok: list[bool] = field(default_factory=list)


@dataclass
class Env:
    wh: DuckWarehouse
    tmp: Path
    pool: WarehousePool


@pytest.fixture
def env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ops_store: OpsStoreHandle,
    reset_process_state: ProcessState,
) -> Iterator[Env]:
    """IT05-01's setting for the chat role: migrated ops store bound as the spec 08 policy
    backend, test config, the shipped prompts, a test redactor and the warehouse tools on the
    stand-in build, opened through the `WarehousePool` the Verifier re-runs on."""
    del ops_store, reset_process_state
    use_test_config(tmp_path / "cfg")
    bind_ops_backend(SqliteResilienceBackend())
    monkeypatch.setattr(base, "_catalog_describe", list)
    directory = NameDirectory.from_files(None, (), None)
    redactor = r.Redactor(RedactionConfig(directory_file=None), bytes(range(32)), directory)
    monkeypatch.setattr(r._State, "redactor", redactor)
    wb.patch_redaction(monkeypatch)
    wt.register_warehouse_tools()
    wb.make_build(tmp_path / "wh")
    pool = WarehousePool(tmp_path / "wh", SqlSettings())
    try:
        yield Env(pool.get(wb.BUILD_ID), tmp_path, pool)  # one handle for tools and Verifier
    finally:
        pool.close_all()
        c.reset_config()
        get_role("chat").__dict__.pop("_prompts", None)


def _thresholds() -> Thresholds:
    """`config/eval.yaml` thresholds, read only (U11-52 model; the loader drops the checked
    `version: 1` before the owner model, U10-16)."""
    raw = yaml.safe_load((ROOT / "config" / "eval.yaml").read_text(encoding="utf-8"))
    assert raw.pop("version") == 1
    return load_eval_config(raw).thresholds


def _rows(wh: DuckWarehouse, ev: Evidence) -> list[dict[str, object]]:
    """The recorded SQL re-run read-only on the build: rows as column → cell."""
    cur = wh.cursor().execute(ev.sql, ev.params or None)
    names = [col[0] for col in cur.description or ()]
    return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


def _unsupported(env: Env, answer: ChatAnswer) -> tuple[int, int]:
    """spec 11 `count_unsupported` over the answer: (unsupported, total)."""
    patterns = compile_allowed_patterns(list(_configured_patterns()))

    def has_evidence(query_id: str, build_id: str) -> bool:
        ev = ops_evidence.get_evidence(query_id)
        return ev is not None and ev.build_id == build_id

    def rerun(query_id: str) -> list[dict[str, object]]:
        ev = ops_evidence.get_evidence(query_id)
        assert ev is not None
        return _rows(env.wh, ev)

    report = count_unsupported(
        answer.text, answer.numbers, env.wh.build_id, evidence=has_evidence, rerun=rerun,
        allowed_patterns=patterns, float_rel_tol=VerifierSettings().float_rel_tol,
    )  # fmt: skip
    return report.unsupported, report.total


def _ask(env: Env, script: Path, index: int) -> Outcome:
    """One scripted chat run, verified, with its trace read back."""
    run_id = f"run_01J8ET0501{index:016d}"
    settings = c.get_config().models.harness.trace
    root = Tracer(
        run_id, build_id=env.wh.build_id, run_kind="eval", traces_dir=env.tmp / "traces",
        settings=settings,
    )  # fmt: skip
    view = root.bind(task_id="task_1", role="chat")
    ctx = ls.loop_ctx(
        tool_names=TOOLS, warehouse=env.wh, ops=_RealOps(), tracer=view,
        budgets=ls.budgets(max_steps=MAX_STEPS, max_tokens=MAX_TOKENS, wall_clock_s=WALL_CLOCK_S),
    ).model_copy(update={"run_id": run_id, "role": "chat", "specialty": None})  # fmt: skip
    hooks = h.HarnessHooks(
        registry=None, gates={}, chain=None, compactor=None, task_id=None, phase=None,  # type: ignore[arg-type]
        stop=None, on_text_delta=None, tracer=view,
    )  # fmt: skip
    start = time.perf_counter()
    try:
        result = asyncio.run(
            loop.run_agent(
                get_role("chat"), {"question": script.stem}, ctx, FakeLLMClient(script),
                ls.profile(), hooks,
            )
        )  # fmt: skip
    finally:
        root.close()
    elapsed = time.perf_counter() - start
    lines = (env.tmp / "traces" / f"{run_id}.jsonl").read_text(encoding="utf-8").splitlines()
    events: list[Mapping[str, Any]] = [json.loads(line) for line in lines]
    answer = ChatAnswer.model_validate(result.output)
    verifier = Verifier(_RealOps(), env.pool, VerifierSettings(), sql=SqlSettings())
    verdict = verifier.verify_answer(answer, env.wh.build_id)
    unsupported, total = _unsupported(env, answer)
    return Outcome(
        name=script.stem,
        status=result.status,
        stop_reason=result.stop_reason,
        steps=result.steps,
        tokens=result.usage.input_tokens + result.usage.output_tokens,
        elapsed_s=elapsed,
        verified=verdict.passed,
        verdict=verdict.model_dump_json(include={"items"}),
        unsupported=unsupported,
        total_numbers=total,
        tool_ok=[bool(e["ok"]) for e in events if e["type"] == "tool_call"],
    )


def _gate(outcomes: Sequence[Outcome], thresholds: Thresholds) -> dict[str, Any]:
    """The two ET05-01 criteria over all outcomes (impl 11 U11-62 / U11-63 semantics)."""
    total = sum(o.total_numbers for o in outcomes)
    unsupported = sum(o.unsupported for o in outcomes)
    calls = [ok for o in outcomes for ok in o.tool_ok]
    rate = unsupported / total if total else 0.0
    success = sum(calls) / len(calls) if calls else 0.0
    return {
        "unsupported": unsupported,
        "unsupported_rate": rate,
        "tool_calls": len(calls),
        "tool_success": success,
        "unsupported_ok": rate <= thresholds.unsupported_number_rate_max,
        "tool_ok": success >= thresholds.tool_success_min[PROFILE],
    }


def _assert_bounded(outcome: Outcome) -> None:
    """TH05-08: the run ended on its final within the step, token and wall-clock budgets."""
    assert outcome.status == "completed", outcome
    assert outcome.stop_reason == "final", outcome
    assert outcome.steps <= MAX_STEPS
    assert outcome.tokens <= MAX_TOKENS
    assert outcome.elapsed_s < WALL_CLOCK_S


def test_et05_01_golden_zero_unsupported_and_tool_success(env: Env) -> None:
    """ET05-01 fixed synthetic golden set, `local` profile: 0 unsupported numbers in verified
    outputs and tool-call success >= 90 % (thresholds from `config/eval.yaml`); every run
    bounded (TH05-08)."""
    thresholds = _thresholds()
    assert thresholds.unsupported_number_rate_max == 0
    assert thresholds.tool_success_min[PROFILE] == pytest.approx(0.90)
    assert len(GOLDEN) >= 6
    outcomes = [_ask(env, script, i) for i, script in enumerate(GOLDEN)]
    for outcome in outcomes:
        _assert_bounded(outcome)
        assert outcome.verified, (outcome.name, outcome.verdict)
        assert outcome.total_numbers >= 1, outcome.name  # every answer cites a number
    gate = _gate(outcomes, thresholds)
    numbers = sum(o.total_numbers for o in outcomes)
    sys.stderr.write(
        f"ET05-01 unsupported_numbers {gate['unsupported']} of {numbers}"
        f" (target 0, rate <= {thresholds.unsupported_number_rate_max:g});"
        f" tool_call_success {gate['tool_success']:.4f} over {gate['tool_calls']} calls"
        f" (target >= {thresholds.tool_success_min[PROFILE]:.2f});"
        f" {len(outcomes)} questions, max steps {max(o.steps for o in outcomes)}\n"
    )
    assert gate["unsupported"] == 0, gate
    assert gate["tool_calls"] == 12, gate
    assert not all(ok for o in outcomes for ok in o.tool_ok)  # one refused query counted
    assert gate["unsupported_ok"], gate
    assert gate["tool_ok"], gate


def test_et05_01_control_stray_number_fails_the_gate(env: Env) -> None:
    """ET05-01 negative control: an answer with a number outside any marker is unsupported, the
    Verifier rejects it and the gate fails on `unsupported_number_rate`."""
    thresholds = _thresholds()
    outcomes = [_ask(env, script, i) for i, script in enumerate(GOLDEN)]
    control = _ask(env, STRAY_NUMBER, len(GOLDEN))
    _assert_bounded(control)
    assert not control.verified
    assert control.unsupported >= 1
    gate = _gate([*outcomes, control], thresholds)
    assert not gate["unsupported_ok"], gate
    assert gate["tool_ok"], gate  # the failure is the number, not the tools


def test_et05_01_control_failing_tool_calls_fail_the_gate(env: Env) -> None:
    """ET05-01 negative control: a run whose tool calls mostly fail drags the tool-call success
    rate below `tool_success_min.local`; the gate fails on it."""
    thresholds = _thresholds()
    outcomes = [_ask(env, script, i) for i, script in enumerate(GOLDEN)]
    control = _ask(env, FAILING_TOOLS, len(GOLDEN))
    _assert_bounded(control)
    assert control.tool_ok == [False, False, False, True]
    assert control.verified  # the answer itself is supported
    gate = _gate([*outcomes, control], thresholds)
    assert gate["unsupported_ok"], gate
    assert not gate["tool_ok"], gate
