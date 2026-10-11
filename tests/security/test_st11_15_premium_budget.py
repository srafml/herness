"""Security test ST11-15 (TH11-13): an eval on `premium` cannot consume unbounded tokens.

`repeat` is capped at 10 at every entry (CLI payload, job payload, `run_golden`), and a
premium run with 20 chat questions never has more than 8 chat calls in flight (stub
counter). The run uses the unit runner environment (`tests/unit/eval/_runner_env.py`).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.support.ops_store import OpsStoreHandle
from tests.unit.eval import _runner_env as env
from tests.unit.eval._runner_env import (
    AnswerChat,
    FakeCtx,
    chat_q,
    lines,
    make_deps,
    make_root,
    runner_store,
    test_redactor,
)

from herness.core.errors import ConfigError
from herness.eval import runner
from herness.eval.golden import load_suite
from herness.eval.runner import EvalOptions, build_eval_payload, handle_eval, run_golden

pytestmark = pytest.mark.unit
__all__ = ["runner_store", "test_redactor"]  # fixtures


def test_st11_15_repeat_11_rejected(
    runner_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST11-15 `--repeat 11` is rejected by the CLI payload builder, by the job handler's
    payload validation and by `run_golden`; no run row is written."""
    with pytest.raises(ConfigError, match="repeat"):
        build_eval_payload(profile="premium", repeat=11, settings=env.settings())
    con = make_root(runner_store.data_root)
    suite = env.suite_file(tmp_path / "suite.yaml", [chat_q("G01")])
    monkeypatch.setattr(runner._Binding, "factory", None)
    runner.set_deps_factory(lambda c: make_deps(runner_store.data_root, con, job=c))
    payload = {"suite": str(suite), "profile": "premium", "repeat": 11}
    with pytest.raises(ConfigError, match="repeat"):
        handle_eval(FakeCtx(payload))  # type: ignore[arg-type]
    deps = make_deps(runner_store.data_root, con)
    with pytest.raises(ConfigError, match=r"repeat must be 1\.\.10"):
        run_golden(load_suite(suite), "premium", "fast", repeat=11, deps=deps,
                   options=EvalOptions())  # fmt: skip
    assert not (runner_store.data_root / "reports").exists()


def test_st11_15_premium_concurrency_capped_at_8(
    runner_store: OpsStoreHandle, tmp_path: Path
) -> None:
    """ST11-15 a premium run with 20 chat questions: the stub's concurrency counter never
    exceeds 8, every question is answered once."""
    con = make_root(runner_store.data_root)
    questions = [chat_q(f"G{i:02d}") for i in range(1, 21)]
    suite = env.suite_file(tmp_path / "suite.yaml", questions)
    chat = AnswerChat(delay_s=0.05)
    deps = make_deps(runner_store.data_root, con, chat=chat)
    run = run_golden(load_suite(suite), "premium", "fast", deps=deps, options=EvalOptions())
    assert len(chat.calls) == 20
    assert 1 < chat.max_running <= runner.PREMIUM_WORKERS == 8
    assert len(lines(run.out_dir / "results.jsonl")) == 20


def test_st11_15_local_runs_sequentially(runner_store: OpsStoreHandle, tmp_path: Path) -> None:
    """ST11-15 profiles other than premium run one question at a time."""
    con = make_root(runner_store.data_root)
    suite = env.suite_file(tmp_path / "suite.yaml", [chat_q(f"G{i:02d}") for i in range(1, 6)])
    chat = AnswerChat(delay_s=0.02)
    deps = make_deps(runner_store.data_root, con, chat=chat)
    run_golden(load_suite(suite), "local", "fast", deps=deps, options=EvalOptions())
    assert chat.max_running == 1
