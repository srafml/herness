"""Security tests of the eval reports and baselines (T11-31): ST11-10 (TH11-05), ST11-11
(TH11-03, TH11-14) and ST11-14 (TH11-11).

`report.html` shows model text escaped inside `<pre>`; a baseline is refused on a real build
and a baseline of another suite disables the correctness drop check with `suite changed`;
a completed mock eval records non-empty provenance matching its ops `run` row. The runner
rows use the unit runner environment (`tests/unit/eval/_runner_env.py`).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from tests.support.ops_store import OpsStoreHandle
from tests.unit.eval import _runner_env as env
from tests.unit.eval._runner_env import (
    FakeCtx,
    chat_q,
    make_deps,
    make_root,
    runner_store,
    test_redactor,
)
from tests.unit.eval.test_eval_report import (
    ROWS,
    SUITE_SHA,
    baseline_summary,
    eval_run,
    result,
)

from herness.core.errors import ConfigError
from herness.eval import runner
from herness.eval.golden import load_suite
from herness.eval.judge import judge_prompt_hash
from herness.eval.metrics import EvalMetrics
from herness.eval.report import load_baseline, save_baseline, write_report
from herness.eval.runner import EvalOptions, build_eval_payload, exit_code, handle_eval
from herness.store.ops import get_run
from herness.store.ops.core import read_all

pytestmark = pytest.mark.unit
__all__ = ["runner_store", "test_redactor"]  # fixtures

ATTACK = "<script>alert(1)</script><img src=x onerror=1>"


def test_st11_10_model_text_is_escaped(tmp_path: Path) -> None:
    """ST11-10 final text `<script>alert(1)</script><img src=x onerror=1>`: `report.html`
    contains only escaped forms and no `<script` tag (TH11-05)."""
    out = tmp_path / "run"
    results = [result("G01", "failed", ATTACK, error=f"ToolError: {ATTACK}"),
               result("G02", "passed", ATTACK)]  # fmt: skip
    write_report(eval_run(out, profile=ATTACK), out, results=results, metrics=EvalMetrics(),
                 thresholds=ROWS, baseline=None, classifier=None)  # fmt: skip
    html = (out / "report.html").read_text(encoding="utf-8")
    assert "<script" not in html.lower()
    assert "<img" not in html.lower()
    assert "&lt;script&gt;alert(1)&lt;/script&gt;&lt;img src=x onerror=1&gt;" in html
    assert "<pre>&lt;script&gt;" in html
    assert "onerror=1>" not in html


def test_st11_11_real_build_baseline_refused(
    runner_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST11-11 `--set-baseline` on a run with `dataset_kind = real` is `ConfigError` (exit 3):
    no run row and no baseline file (TH11-14)."""
    con = make_root(runner_store.data_root, kind="real")
    suite = env.suite_file(tmp_path / "suite.yaml", [chat_q("G01")])
    payload = build_eval_payload(
        suite=str(suite),
        profile="local",
        depth="fast",
        set_baseline="synthetic-42-small",
        settings=env.settings(),
    )
    monkeypatch.setattr(runner._Binding, "factory", None)
    runner.set_deps_factory(lambda c: make_deps(runner_store.data_root, con, job=c))
    with pytest.raises(ConfigError, match="synthetic") as caught:
        handle_eval(FakeCtx(payload))  # type: ignore[arg-type]
    assert (
        exit_code({"status": "failed", "last_error": {"class": type(caught.value).__name__}}) == 3
    )
    assert read_all("SELECT run_id FROM run WHERE kind = 'eval'", ()) == []
    assert not (runner_store.data_root.parent / "tests").exists()
    with pytest.raises(ConfigError, match="synthetic"):
        save_baseline("synthetic-42-small", baseline_summary(dataset_kind="real"),
                      repo_root=tmp_path)  # fmt: skip
    assert not (tmp_path / "tests").exists()


def test_st11_11_suite_changed_disables_drop_check(
    runner_store: OpsStoreHandle, tmp_path: Path
) -> None:
    """ST11-11 a baseline with a different `suite_sha256`: the drop check reports
    `detail = "suite changed"` instead of comparing (TH11-03)."""
    repo = runner_store.data_root.parent
    save_baseline("synthetic-42-small", baseline_summary(suite_sha256=SUITE_SHA), repo_root=repo)
    con = make_root(runner_store.data_root)
    suite = env.suite_file(tmp_path / "suite.yaml", [chat_q("G01")])
    assert load_suite(suite).sha256 != SUITE_SHA
    flagged = load_baseline("synthetic-42-small", repo_root=repo,
                            suite_sha256=load_suite(suite).sha256)  # fmt: skip
    assert flagged is not None
    assert flagged.suite_changed is True
    deps = make_deps(runner_store.data_root, con)
    run = runner.run_golden(load_suite(suite), "local", "fast", deps=deps, options=EvalOptions())
    correctness = next(r for r in run.summary["thresholds"] if r["check"] == "correctness")
    assert correctness["detail"] == "suite changed"


class _Judge:
    """Judge stand-in: a model name for the provenance (no rubric question is asked)."""

    model = "judge-model-7b"

    def score(self, *args: object) -> Any:
        raise AssertionError(args)


def test_st11_14_provenance_matches_run_row(
    runner_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST11-14 a completed mock eval: `summary.json` provenance fields are non-empty and the
    ops `run` row matches (TH11-11)."""
    con = make_root(runner_store.data_root)
    suite = env.suite_file(tmp_path / "suite.yaml", [chat_q("G01")])
    mock = tmp_path / "mock"
    mock.mkdir()
    (mock / "judge.yaml").write_text(
        "- match: {model_role: eval_judge}\n  turns:\n    - final: {text: ok}\n", encoding="utf-8"
    )
    payload = build_eval_payload(suite=str(suite), profile="local", depth="fast",
                                 mock_llm_dir=str(mock), settings=env.settings())  # fmt: skip
    monkeypatch.setattr(runner._Binding, "factory", None)
    runner.set_deps_factory(lambda c: make_deps(runner_store.data_root, con, job=c, judge=_Judge))
    outcome = handle_eval(FakeCtx(payload))  # type: ignore[arg-type]
    assert outcome.status == "done"
    run_id = str(outcome.result["run_id"])
    out = runner_store.data_root / "reports" / "eval" / run_id
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    for key in (
        "git_sha",
        "config_hash",
        "suite_sha256",
        "judge_model",
        "judge_prompt_sha256",
        "build_id",
        "profile",
        "depth",
        "dataset_kind",
        "started_at",
        "finished_at",
    ):
        assert isinstance(summary[key], str)
        assert summary[key], key
    assert summary["judge_model"] == "judge-model-7b"
    assert summary["judge_prompt_sha256"] == judge_prompt_hash()
    assert summary["mock"] is True
    row = get_run(run_id)
    assert row is not None
    assert (row.kind, row.status) == ("eval", "done")
    assert summary["run_id"] == row.run_id
    assert summary["config_hash"] == row.config_hash
    assert (summary["build_id"], summary["profile"], summary["depth"]) == (
        row.build_id, row.profile, row.depth,
    )  # fmt: skip
    assert summary["suite_sha256"] == row.meta["suite_sha256"] == load_suite(suite).sha256
    assert (summary["dataset_kind"], summary["mock"]) == (row.meta["dataset_kind"],
                                                          row.meta["mock"])  # fmt: skip
