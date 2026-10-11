"""Controller checks (T11-38) of `--compare PROFILE` through `handle_eval` (U11-68 step 4):
the suite runs under the profile and again under the compare profile on the same pinned
build, then `compare` writes the comparison into the newer run and its report re-renders
with the comparison section; yield and resume keep both run ids in the job state.

The unit runner environment (`_runner_env`: fixture warehouse, tmp `CURRENT`, seed-42 truth,
migrated tmp ops store, scripted chat) serves both profiles.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from tests.support.ops_store import OpsStoreHandle
from tests.unit.eval import _runner_env as env
from tests.unit.eval._runner_env import (
    AnswerChat,
    FakeCtx,
    chat_q,
    make_deps,
    make_root,
    runner_store,
    test_redactor,
)

from herness.core.errors import ConfigError
from herness.eval import _runner_compare, runner
from herness.eval.runner import EvalDeps, build_eval_payload, exit_code, handle_eval
from herness.store.ops import get_run
from herness.store.ops.core import read_all

pytestmark = pytest.mark.unit
__all__ = ["runner_store", "test_redactor"]  # fixtures


def _payload(suite: Path, **extra: Any) -> dict[str, Any]:
    return build_eval_payload(
        suite=str(suite), profile="local", compare_profile="hybrid", depth="fast",
        no_judge=True, settings=env.settings(), **extra,
    )  # fmt: skip


def _bind(monkeypatch: pytest.MonkeyPatch, make: Any) -> None:
    monkeypatch.setattr(runner._Binding, "factory", None)
    runner.set_deps_factory(make)


def _read(out: Path, name: str) -> Any:
    text = (out / name).read_text(encoding="utf-8")
    return json.loads(text) if name.endswith(".json") else text


def _eval_rows() -> list[dict[str, Any]]:
    return read_all("SELECT run_id, profile, status FROM run WHERE kind = 'eval' "
                    "ORDER BY started_at", ())  # fmt: skip


def test_cv_t11_38_compare_profile_runs_twice_and_compares(
    runner_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CV-T11-38 `--compare hybrid`: two runs on the same build, identical numbers → no
    harness defect, exit 0; `compare_profile` on the primary run's row and summary; the
    comparison files and report section are in the newer (hybrid) run."""
    con = make_root(runner_store.data_root)
    suite = env.suite_file(tmp_path / "suite.yaml", [chat_q("G01"), chat_q("G02")])
    chat = AnswerChat()
    _bind(monkeypatch, lambda c: make_deps(runner_store.data_root, con, chat=chat, job=c))
    outcome = handle_eval(FakeCtx(_payload(suite)))  # type: ignore[arg-type]

    assert outcome.status == "done"
    primary, second = str(outcome.result["run_id"]), str(outcome.result["compare_run_id"])
    assert outcome.result == {"run_id": primary, "passed": True, "exit_code": 0,
                              "compare_run_id": second}  # fmt: skip
    assert exit_code(outcome.model_dump()) == 0
    assert len(chat.calls) == 4
    rows = _eval_rows()
    assert [(r["run_id"], r["profile"], r["status"]) for r in rows] == [
        (primary, "local", "done"), (second, "hybrid", "done")]  # fmt: skip
    first_row, second_row = get_run(primary), get_run(second)
    assert first_row is not None
    assert second_row is not None
    assert first_row.meta["compare_profile"] == "hybrid"
    assert second_row.meta["compare_profile"] is None
    assert first_row.build_id == second_row.build_id == env.BUILD_ID
    eval_dir = runner_store.data_root / "reports" / "eval"
    assert _read(eval_dir / primary, "summary.json")["compare_profile"] == "hybrid"
    assert _read(eval_dir / second, "summary.json")["compare_profile"] is None
    table = _read(eval_dir / second, "comparison.json")
    assert table["run_ids"] == [primary, second]
    assert table["harness_defects"] == []
    assert table["flips"] == []
    assert "## Comparison" in _read(eval_dir / second, "report.md")
    assert "Comparison" in _read(eval_dir / second, "report.html")
    assert "## Comparison" not in _read(eval_dir / primary, "report.md")
    assert not (eval_dir / primary / "comparison.json").exists()


def test_cv_t11_38_compare_profile_numeric_disagreement_is_exit_14(
    runner_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CV-T11-38 the compare profile cites another number on the same build: the question
    is a harness defect and the job fails its gate (exit 14)."""
    con = make_root(runner_store.data_root)
    suite = env.suite_file(tmp_path / "suite.yaml", [chat_q("G01")])
    chats = {"local": AnswerChat(), "hybrid": AnswerChat(value=5)}

    def make(c: Any) -> EvalDeps:
        deps = make_deps(runner_store.data_root, con, job=c)
        return dataclasses.replace(deps, chat_for=lambda profile: chats[profile])

    _bind(monkeypatch, make)
    outcome = handle_eval(FakeCtx(_payload(suite)))  # type: ignore[arg-type]
    assert (outcome.result["passed"], outcome.result["exit_code"]) == (False, 14)
    second = str(outcome.result["compare_run_id"])
    table = _read(runner_store.data_root / "reports" / "eval" / second, "comparison.json")
    assert table["harness_defects"] == ["G01"]


@pytest.mark.parametrize("after", [1, 3])
def test_cv_t11_38_yield_and_resume_keep_both_runs(
    after: int, runner_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CV-T11-38 a yield in the primary or the compare run returns the primary run id; the
    job state holds both run ids and the requeued job resumes each run (no third run, no
    question asked twice)."""
    con = make_root(runner_store.data_root)
    suite = env.suite_file(tmp_path / "suite.yaml", [chat_q("G01"), chat_q("G02")])
    chat = AnswerChat()
    _bind(monkeypatch, lambda c: make_deps(runner_store.data_root, con, chat=chat, job=c))
    ctx = FakeCtx(_payload(suite), yield_after=after)
    first = handle_eval(ctx)  # type: ignore[arg-type]
    assert first.status == "yield"
    primary = str(first.result["run_id"])
    assert ctx.state["eval_run_id"] == primary
    assert ("compare_run_id" in ctx.state) is (after == 3)
    ctx.yield_after = None
    second = handle_eval(ctx)  # type: ignore[arg-type]
    assert second.status == "done"
    assert second.result["run_id"] == primary
    assert second.result["passed"] is True
    assert ctx.state == {"eval_run_id": primary, "compare_run_id": second.result["compare_run_id"]}
    assert len(chat.calls) == 4
    assert [r["status"] for r in _eval_rows()] == ["done", "done"]


def test_cv_t11_38_resumed_job_with_both_runs_done_only_compares(
    runner_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CV-T11-38 a retried job whose state names both finished runs reads the primary's
    stored summary and finishes the compare run again; no question is asked again."""
    con = make_root(runner_store.data_root)
    suite = env.suite_file(tmp_path / "suite.yaml", [chat_q("G01")])
    chat = AnswerChat()
    _bind(monkeypatch, lambda c: make_deps(runner_store.data_root, con, chat=chat, job=c))
    ctx = FakeCtx(_payload(suite))
    done = handle_eval(ctx)  # type: ignore[arg-type]
    again = handle_eval(ctx)  # type: ignore[arg-type]
    assert again.result == done.result
    assert len(chat.calls) == 2


def test_cv_t11_38_compare_needs_the_same_build(
    runner_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CV-T11-38 when `CURRENT` moved after the primary run, the compare run is refused
    (ConfigError, exit 3) instead of comparing two builds."""
    con = make_root(runner_store.data_root)
    suite = env.suite_file(tmp_path / "suite.yaml", [chat_q("G01")])
    _bind(monkeypatch, lambda c: make_deps(runner_store.data_root, con, job=c))
    monkeypatch.setattr(_runner_compare, "current_build", lambda root: "bld_other")
    with pytest.raises(ConfigError, match="current build changed"):
        handle_eval(FakeCtx(_payload(suite)))  # type: ignore[arg-type]
    assert len(_eval_rows()) == 1


def test_cv_t11_38_set_baseline_once_and_baseline_in_both_reports(
    runner_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CV-T11-38 `--set-baseline` saves the primary run only; a later `--compare` with
    `--baseline` re-renders the compare run's report with both sections."""
    con = make_root(runner_store.data_root)
    suite = env.suite_file(tmp_path / "suite.yaml", [chat_q("G01")])
    _bind(monkeypatch, lambda c: make_deps(runner_store.data_root, con, job=c))
    handle_eval(FakeCtx(_payload(suite, set_baseline="cmp-base")))  # type: ignore[arg-type]
    stored = json.loads((runner_store.data_root.parent / "tests" / "eval" / "baselines" /
                         "cmp-base.json").read_text(encoding="utf-8"))  # fmt: skip
    assert stored["profile"] == "local"
    outcome = handle_eval(FakeCtx(_payload(suite, baseline="cmp-base")))  # type: ignore[arg-type]
    md = _read(runner_store.data_root / "reports" / "eval" / str(outcome.result["compare_run_id"]),
               "report.md")  # fmt: skip
    assert "## Baseline diff" in md
    assert "## Comparison" in md


def test_cv_t11_38_compare_job_context_delegates(tmp_path: Path) -> None:
    """CV-T11-38 the compare run's job context is the real one except `save_state`, which
    stores the compare run id next to the primary's; an unfinished primary is not read
    back from disk."""
    inner = MagicMock()
    inner.load_state.return_value = {"eval_run_id": "run_a"}
    job = _runner_compare._CompareJob(inner)
    assert (job.job, job.job_id, job.kind, job.attempt) == (
        inner.job, inner.job_id, inner.kind, inner.attempt)  # fmt: skip
    assert (job.services, job.stop_reason) == (inner.services, inner.stop_reason)
    assert job.should_yield() is inner.should_yield.return_value
    job.heartbeat("n")
    job.require_gpu_class("reasoning", timeout_s=1.0)
    assert job.gpu_scope("reasoning") is inner.gpu_scope.return_value
    job.save_state({"eval_run_id": "run_b"})
    inner.heartbeat.assert_called_once_with("n")
    inner.require_gpu_class.assert_called_once_with("reasoning", timeout_s=1.0)
    inner.save_state.assert_called_once_with({"eval_run_id": "run_a", "compare_run_id": "run_b"})
    assert job.load_state() == {"eval_run_id": "run_a"}
    deps = make_deps(tmp_path, None)  # type: ignore[arg-type]
    assert _runner_compare._finished(None, deps) is None
    assert _runner_compare._finished("run_01JA0000000000000000000009", deps) is None


def test_cv_t11_38_finished_checks_the_run_id_first(tmp_path: Path) -> None:
    """CV-T11-38 reading a finished primary back checks `run_<ulid>` before any path join."""
    deps = make_deps(tmp_path, None)  # type: ignore[arg-type]
    for bad in ("../outside", "run_x"):
        with pytest.raises(ConfigError, match="run ids must be run_<ulid>"):
            _runner_compare._finished(bad, deps)


def test_cv_t11_38_resumed_compare_run_keeps_its_build(
    runner_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CV-T11-38 a retried job resumes the half-finished compare run on the build pinned in
    its run row, not on a `CURRENT` that moved in between; both runs compare on one build."""
    con = make_root(runner_store.data_root)
    suite = env.suite_file(tmp_path / "suite.yaml", [chat_q("G01"), chat_q("G02")])
    _bind(monkeypatch, lambda c: make_deps(runner_store.data_root, con, job=c))
    ctx = FakeCtx(_payload(suite), yield_after=3)
    assert handle_eval(ctx).status == "yield"  # type: ignore[arg-type]
    assert "compare_run_id" in ctx.state
    moved = lambda root: "bld_other"  # noqa: E731 - CURRENT moved after the yield
    monkeypatch.setattr(runner, "current_build", moved)
    monkeypatch.setattr(_runner_compare, "current_build", moved)
    ctx.yield_after = None
    done = handle_eval(ctx)  # type: ignore[arg-type]
    assert (done.status, done.result["exit_code"]) == ("done", 0)
    rows = read_all("SELECT build_id FROM run WHERE kind = 'eval'", ())
    assert [r["build_id"] for r in rows] == [env.BUILD_ID, env.BUILD_ID]


def test_cv_t11_38_runs_on_different_builds_are_not_compared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CV-T11-38 should the two runs report different builds, `run_eval` refuses to compare
    them (ConfigError) instead of writing a comparison."""
    deps = make_deps(tmp_path, None)  # type: ignore[arg-type]

    def golden(suite: object, profile: str, depth: str, **kw: object) -> runner.EvalRun:
        build = "bld_a" if profile == "local" else "bld_b"
        return runner.EvalRun(run_id=f"r_{profile}", out_dir=tmp_path,
                              summary={"build_id": build}, passed=True)  # fmt: skip

    monkeypatch.setattr(_runner_compare, "current_build", lambda root: "bld_a")
    payload = runner.EvalPayload(profile="local", compare_profile="hybrid", repeat=1)
    with pytest.raises(ConfigError, match="different builds"):
        _runner_compare.run_eval(
            golden, None, payload, resume_run_id=None,  # type: ignore[arg-type]
            deps=deps, options=runner.EvalOptions(),
        )  # fmt: skip
    assert not list(tmp_path.rglob("comparison.json"))
