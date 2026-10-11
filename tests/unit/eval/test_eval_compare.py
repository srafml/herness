"""Tests for herness.eval.report.compare and ComparisonTable (U11-71): UT11-100, UT11-101 and
controller checks (T11-38) of run ids, refusals, the suite-changed banner, rendering and the
`--compare-runs` job branch of `handle_eval` (U11-68 step 2).

Runs are stored by `_compare_env.store_run` (a `summary.json` and a `results.jsonl` per run
under a tmp data root); `compare` only reads them (TH11-12: no pipeline re-runs).
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.unit.eval._compare_env import (
    RUN_A,
    RUN_B,
    RUN_C,
    numeric,
    q_result,
    store_run,
)
from tests.unit.eval._runner_env import FakeCtx, make_deps

from herness.core.errors import ConfigError
from herness.eval import report, runner
from herness.eval.grading import GradeResult
from herness.eval.metrics import EvalMetrics
from herness.eval.report import ComparisonTable, compare, write_report
from herness.eval.runner import EvalRun, exit_code, handle_eval

pytestmark = pytest.mark.unit


def _pair(root: Path, *, b_value: str = "7", **b_summary: Any) -> None:
    """Run A (local) and run B (hybrid) on the same build: G01 numeric, G02 flips."""
    store_run(
        root, RUN_A, profile="local", depth="standard",
        metrics=EvalMetrics(answer_correctness=0.5, tokens_in=10, cost_usd=Decimal("1.00")),
        results=[q_result("G01", checks=[numeric("7", "7")]), q_result("G02", "failed")],
        started_at="2026-10-10T08:00:00Z",
    )  # fmt: skip
    store_run(
        root, RUN_B, profile="hybrid", depth="standard",
        metrics=EvalMetrics(answer_correctness=0.75, tokens_in=4, cost_usd=Decimal("1.50")),
        results=[q_result("G01", checks=[numeric("7", b_value)]), q_result("G02", "passed")],
        started_at="2026-10-10T09:00:00Z", **b_summary,
    )  # fmt: skip


# --- UT11-100 compare: rows, deltas, flips --------------------------------------------------------


def test_ut11_100_rows_deltas_and_flips(tmp_path: Path) -> None:
    """UT11-100 two stored runs: one row per run with (profile, depth) and its metrics,
    deltas against the first run, the G02 flip listed; files in the newest run's dir."""
    _pair(tmp_path)
    table = compare([RUN_A, RUN_B], data_root=tmp_path)
    assert isinstance(table, ComparisonTable)
    assert [(r.run_id, r.profile, r.depth) for r in table.rows] == [
        (RUN_A, "local", "standard"), (RUN_B, "hybrid", "standard")]  # fmt: skip
    assert table.rows[1].metrics["answer_correctness"] == 0.75
    (delta,) = table.deltas
    assert delta.run_id == RUN_B
    assert delta.metrics["answer_correctness"] == pytest.approx(0.25)
    assert delta.metrics["tokens_in"] == -6
    assert delta.metrics["cost_usd"] == Decimal("0.50")
    assert delta.metrics["chat_latency_p95_s"] is None  # None on either side
    assert [f.model_dump() for f in table.flips] == [{
        "question_id": "G02", "repeat": 1, "from_run": RUN_A, "to_run": RUN_B,
        "before": "failed", "after": "passed"}]  # fmt: skip
    assert table.harness_defects == []
    assert table.suite_changed is False
    assert table.newest_run_id == RUN_B
    out = tmp_path / "reports" / "eval" / RUN_B
    stored = json.loads((out / "comparison.json").read_text(encoding="utf-8"))
    assert stored == table.model_dump(mode="json")
    assert stored["deltas"][0]["metrics"]["tokens_in"] == -6
    md = (out / "comparison.md").read_text(encoding="utf-8")
    assert "| answer_correctness | 0.5 | 0.75 | +25.0 pp |" in md
    assert "| tokens_in | 10 | 4 | -6 |" in md
    assert "| cost_usd | 1 | 1.5 | +0.5 |" in md  # as in the baseline diff (normalised)
    assert "`G02`" in md
    assert not (tmp_path / "reports" / "eval" / RUN_A / "comparison.json").exists()


def test_ut11_100_three_runs_and_newest_by_start(tmp_path: Path) -> None:
    """UT11-100 deltas and flips of every later run are against the first run; the newest
    run is the one that started last, whatever the argument order."""
    _pair(tmp_path)
    store_run(tmp_path, RUN_C, profile="local", depth="deep",
              results=[q_result("G01", checks=[numeric("7", "7")]), q_result("G02", "error")],
              started_at="2026-10-10T07:00:00Z")  # fmt: skip
    table = compare([RUN_B, RUN_C, RUN_A, RUN_B], data_root=tmp_path)  # duplicates dropped
    assert table.run_ids == [RUN_B, RUN_C, RUN_A]
    assert [d.run_id for d in table.deltas] == [RUN_C, RUN_A]
    assert [(f.from_run, f.to_run, f.after) for f in table.flips] == [
        (RUN_B, RUN_C, "error"), (RUN_B, RUN_A, "failed")]  # fmt: skip
    assert table.newest_run_id == RUN_B


def test_cv_t11_38_flips_only_over_shared_keys_and_suite_changed(tmp_path: Path) -> None:
    """CV-T11-38 runs of different suites: `suite_changed` and the banner; flips only over
    (question_id, repeat) present in both runs."""
    store_run(tmp_path, RUN_A, results=[q_result("G01"), q_result("G02", "failed")])
    store_run(tmp_path, RUN_B, suite_sha256="b" * 64, started_at="2026-10-10T09:00:00Z",
              results=[q_result("G01", "failed"), q_result("G03", "passed"),
                       q_result("G01", "passed", repeat=2)])  # fmt: skip
    table = compare([RUN_A, RUN_B], data_root=tmp_path)
    assert table.suite_changed is True
    assert [(f.question_id, f.repeat) for f in table.flips] == [("G01", 1)]
    md = (tmp_path / "reports" / "eval" / RUN_B / "comparison.md").read_text(encoding="utf-8")
    assert "suite changed" in md.lower()


# --- UT11-101 harness defects -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("a_check", "b_check", "defect"),
    [
        (numeric("7", "7"), numeric("7", "8", passed=False), True),  # matched value differs
        (numeric("7", "7"), numeric("8", "8"), True),  # reference value differs
        (numeric("7", "7"), numeric("7", "7.0"), False),  # the same number
        (numeric("7", "3", "7"), numeric("7", "7"), False),  # the matched ref, not every one
        (numeric("7", "9", passed=False), numeric("7", "8", passed=False), False),  # no match
        (numeric("7", "x", "7"), numeric("7", "7"), False),  # a value that is no number
        (
            GradeResult(
                check="numeric",
                status="passed",
                detail={"reference": None, "candidates": [{"value": True}]},
            ),
            numeric("7", "7"),
            True,
        ),  # an unusable stored reference differs
    ],
)
def test_ut11_101_harness_defect_names_the_question(
    a_check: Any, b_check: Any, defect: bool, tmp_path: Path
) -> None:
    """UT11-101 on the same build a numeric question whose reference value or matched
    NumberRef value differs between profiles is a harness defect, named by question id."""
    store_run(tmp_path, RUN_A, results=[q_result("G01", checks=[a_check]), q_result("G02")])
    store_run(tmp_path, RUN_B, profile="hybrid", started_at="2026-10-10T09:00:00Z",
              results=[q_result("G01", checks=[b_check]), q_result("G02")])  # fmt: skip
    table = compare([RUN_A, RUN_B], data_root=tmp_path)
    assert table.harness_defects == (["G01"] if defect else [])


def test_ut11_101_other_build_is_no_defect(tmp_path: Path) -> None:
    """UT11-101 runs on different builds may differ in numbers: no harness defect."""
    _pair(tmp_path, b_value="8", build_id="bld_other")
    assert compare([RUN_A, RUN_B], data_root=tmp_path).harness_defects == []
    _pair(tmp_path, b_value="8")
    table = compare([RUN_A, RUN_B], data_root=tmp_path)
    assert table.harness_defects == ["G01"]
    md = (tmp_path / "reports" / "eval" / RUN_B / "comparison.md").read_text(encoding="utf-8")
    assert "Harness defects" in md


# --- refusals ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ids", "message"),
    [
        (["../../etc", RUN_B], "run ids must be run_<ulid>"),
        ([RUN_A, "run_01ja0000000000000000000001"], "run ids must be run_<ulid>"),
        ([RUN_A], "at least two distinct runs"),
        ([RUN_A, RUN_A], "at least two distinct runs"),
        ([RUN_A, RUN_C], f"eval run not found: run_id={RUN_C}"),
    ],
)
def test_cv_t11_38_bad_run_ids_are_config_errors(
    ids: list[str], message: str, tmp_path: Path
) -> None:
    """CV-T11-38 a run id that is not `run_<ulid>` is refused before any path join; fewer
    than two distinct runs or an unknown run is `ConfigError` (exit 3)."""
    _pair(tmp_path)
    with pytest.raises(ConfigError, match=message.replace("(", r"\(").replace("[", r"\[")):
        compare(ids, data_root=tmp_path)


def test_cv_t11_38_broken_stored_runs_are_config_errors(tmp_path: Path) -> None:
    """CV-T11-38 an unreadable summary, a summary without metrics or a run directory without
    a summary is `ConfigError`."""
    _pair(tmp_path)
    summary = tmp_path / "reports" / "eval" / RUN_B / "summary.json"
    summary.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="summary is not valid"):
        compare([RUN_A, RUN_B], data_root=tmp_path)
    summary.write_text(json.dumps({"run_id": RUN_B, "metrics": {"bogus": 1}}), encoding="utf-8")
    with pytest.raises(ConfigError, match="summary is not valid"):
        compare([RUN_A, RUN_B], data_root=tmp_path)
    summary.write_text("[]", encoding="utf-8")
    with pytest.raises(ConfigError, match="summary is not valid"):
        compare([RUN_A, RUN_B], data_root=tmp_path)
    summary.unlink()
    with pytest.raises(ConfigError, match="eval run not found"):
        compare([RUN_A, RUN_B], data_root=tmp_path)


def test_cv_t11_38_mock_vs_real_is_refused(tmp_path: Path) -> None:
    """CV-T11-38 runs that disagree on `mock` are not compared (ConfigError, exit 3)."""
    _pair(tmp_path, mock=False)
    with pytest.raises(ConfigError, match="mock and real runs"):
        compare([RUN_A, RUN_B], data_root=tmp_path)
    assert not (tmp_path / "reports" / "eval" / RUN_B / "comparison.json").exists()


# --- rendering ---------------------------------------------------------------------------------


def test_cv_t11_38_markup_is_escaped(tmp_path: Path) -> None:
    """CV-T11-38 stored profile strings and question ids never reach the markup raw: code
    spans and escaped cells in Markdown, autoescaped HTML in the report section."""
    store_run(tmp_path, RUN_A, profile="<b>x|y</b>",
              results=[q_result("<img src=x>", "failed")])  # fmt: skip
    store_run(tmp_path, RUN_B, profile="lo`cal", started_at="2026-10-10T09:00:00Z",
              results=[q_result("<img src=x>", "passed")])  # fmt: skip
    table = compare([RUN_A, RUN_B], data_root=tmp_path)
    md = (tmp_path / "reports" / "eval" / RUN_B / "comparison.md").read_text(encoding="utf-8")
    assert "`<img src=x>`" in md
    assert "x\\|y" in md
    assert "`` lo`cal ``" in md
    out = tmp_path / "rendered"
    run = EvalRun(run_id=RUN_B, out_dir=out, summary=_summary(), passed=True)
    write_report(run, out, results=[], metrics=EvalMetrics(), thresholds=[], baseline=None,
                 classifier=None, comparison=table)  # fmt: skip
    html = (out / "report.html").read_text(encoding="utf-8")
    assert "<img src=x>" not in html
    assert "&lt;img src=x&gt;" in html
    assert "&lt;b&gt;x|y&lt;/b&gt;" in html
    report_md = (out / "report.md").read_text(encoding="utf-8")
    assert "## Comparison" in report_md
    assert "`<img src=x>`" in report_md


def test_cv_t11_38_report_without_comparison_has_no_section(tmp_path: Path) -> None:
    """CV-T11-38 `comparison` defaults to None: no comparison section (T11-31 behaviour)."""
    out = tmp_path / "rendered"
    run = EvalRun(run_id=RUN_B, out_dir=out, summary=_summary(), passed=True)
    write_report(run, out, results=[], metrics=EvalMetrics(), thresholds=[], baseline=None,
                 classifier=None)  # fmt: skip
    assert "Comparison" not in (out / "report.md").read_text(encoding="utf-8")
    assert "Comparison" not in (out / "report.html").read_text(encoding="utf-8")


def test_cv_t11_38_suite_changed_banner_in_html(tmp_path: Path) -> None:
    """CV-T11-38 the report section shows the harness defects and the suite-changed banner."""
    _pair(tmp_path, b_value="8", suite_sha256="b" * 64)
    table = compare([RUN_A, RUN_B], data_root=tmp_path)
    out = tmp_path / "rendered"
    run = EvalRun(run_id=RUN_B, out_dir=out, summary=_summary(), passed=True)
    write_report(run, out, results=[], metrics=EvalMetrics(), thresholds=[], baseline=None,
                 classifier=None, comparison=table)  # fmt: skip
    html = (out / "report.html").read_text(encoding="utf-8")
    assert "suite changed" in html.lower()
    assert "Harness defects" in html
    assert "+25.0 pp" in html


def test_cv_t11_38_public_names() -> None:
    """CV-T11-38 the report module exports `compare` and `ComparisonTable` (U11-71)."""
    assert {"compare", "ComparisonTable"} <= set(report.__all__)


def _summary() -> dict[str, Any]:
    return {
        "run_id": RUN_B, "profile": "hybrid", "depth": "standard", "build_id": "bld_x",
        "dataset_kind": "synthetic", "git_sha": "0" * 12, "config_hash": "c" * 16,
        "suite_sha256": "a" * 64, "judge_model": None, "judge_prompt_sha256": "d" * 16,
        "passed": True, "exit_code": 0,
    }  # fmt: skip


# --- U11-68 step 2: the --compare-runs job ------------------------------------------------------


@pytest.mark.parametrize(("b_value", "passed", "code"), [("7", True, 0), ("8", False, 14)])
def test_cv_t11_38_compare_runs_job_exit_codes(
    b_value: str, passed: bool, code: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CV-T11-38 `--compare-runs A,B` only compares (no run row, no question): job `done`
    with the newest run id; exit 0, or 14 when a harness defect is found."""
    root = tmp_path / "data"
    _pair(root, b_value=b_value)
    monkeypatch.setattr(runner._Binding, "factory", None)
    runner.set_deps_factory(lambda c: make_deps(root, None, job=c))  # type: ignore[arg-type]
    outcome = handle_eval(FakeCtx({"profile": "local", "repeat": 1,  # type: ignore[arg-type]
                                   "compare_runs": [RUN_A, RUN_B]}))  # fmt: skip
    assert outcome.status == "done"
    assert outcome.result == {"run_id": RUN_B, "passed": passed, "exit_code": code}
    assert exit_code(outcome.model_dump()) == code
    assert (root / "reports" / "eval" / RUN_B / "comparison.json").is_file()


def test_cv_t11_38_compare_runs_job_unknown_run_is_exit_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CV-T11-38 `--compare-runs` naming an unknown or malformed run is `ConfigError`."""
    monkeypatch.setattr(runner._Binding, "factory", None)
    runner.set_deps_factory(lambda c: make_deps(tmp_path, None, job=c))  # type: ignore[arg-type]
    for ids in ([RUN_A, RUN_B], ["run_x", RUN_B]):
        with pytest.raises(ConfigError):
            handle_eval(FakeCtx({"profile": "local", "repeat": 1,  # type: ignore[arg-type]
                                 "compare_runs": ids}))  # fmt: skip


def test_cv_t11_38_every_metric_is_rendered(tmp_path: Path) -> None:
    """CV-T11-38 the comparison shows every §5.3.4 metric: `skeptic_catch` (no delta) and the
    per-pipeline and per-tag correctness in pp; stored tag names are escaped (md code span,
    html autoescape)."""
    first = EvalMetrics(skeptic_catch=True, correctness_by_pipeline={"chat": 0.5},
                        correctness_by_tag={"<b>t|1</b>": 1.0})  # fmt: skip
    second = EvalMetrics(skeptic_catch=False, correctness_by_pipeline={"chat": 1.0})
    store_run(tmp_path, RUN_A, metrics=first, results=[q_result("G01")])
    store_run(tmp_path, RUN_B, metrics=second, results=[q_result("G01")],
              started_at="2026-10-10T09:00:00Z")  # fmt: skip
    table = compare([RUN_A, RUN_B], data_root=tmp_path)
    md = (tmp_path / "reports" / "eval" / RUN_B / "comparison.md").read_text(encoding="utf-8")
    assert "| skeptic_catch | true | false | - |" in md
    assert "| `correctness_by_pipeline.chat` | 0.5 | 1.0 | +50.0 pp |" in md
    assert "| `correctness_by_tag.<b>t\\|1</b>` | 1.0 | - | - |" in md
    out = tmp_path / "rendered"
    run = EvalRun(run_id=RUN_B, out_dir=out, summary=_summary(), passed=True)
    write_report(run, out, results=[], metrics=EvalMetrics(), thresholds=[], baseline=None,
                 classifier=None, comparison=table)  # fmt: skip
    html = (out / "report.html").read_text(encoding="utf-8")
    assert "correctness_by_tag.&lt;b&gt;t|1&lt;/b&gt;" in html
    assert "<b>t" not in html
    assert "skeptic_catch" in html
