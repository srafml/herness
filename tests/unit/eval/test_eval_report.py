"""Tests for herness.eval.report (U11-70, U11-72): UT11-110, UT11-111 and controller checks
of the runner wiring (T11-31): report files, baselines and the "suite changed" detail.

`write_report` and the baseline functions run on tmp directories; the runner rows use the
unit runner environment (`_runner_env`: fixture warehouse, tmp `CURRENT`, seed-42 truth,
migrated tmp ops store) with the baselines root (`EvalDeps.repo_root`) in a tmp directory.
"""

from __future__ import annotations

import json
from decimal import Decimal
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
from tests.support.ops_store import OpsStoreHandle
from tests.unit.eval import _runner_env as env
from tests.unit.eval._judge_fixtures import MODEL, FakeJudgeClient, make_judge
from tests.unit.eval._report_env import (
    PROVENANCE,
    ROWS,
    RUN_ID,
    SUITE_SHA,
    baseline_summary,
    eval_run,
    keys_anywhere,
    result,
    summary,
)
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
from herness.eval import report
from herness.eval.golden import load_suite
from herness.eval.metrics import EvalMetrics, ThresholdResult, check_thresholds
from herness.eval.report import load_baseline, save_baseline, write_report
from herness.eval.runner import EvalOptions, EvalRun, run_golden
from herness.store.ops import get_run
from herness.store.ops.core import read_all

pytestmark = pytest.mark.unit
__all__ = ["runner_store", "test_redactor"]  # fixtures


# --- UT11-110 write_report ----------------------------------------------------------------------


def test_ut11_110_three_files_and_provenance(tmp_path: Path) -> None:
    """UT11-110 `write_report` writes `summary.json`, `report.md`, `report.html`; the summary
    has `git_sha`, `config_hash`, `suite_sha256`, judge model and prompt hash, the metrics
    and the threshold rows; no temp file is left."""
    out = tmp_path / "run"
    metrics = EvalMetrics(answer_correctness=0.5, cost_usd=Decimal("1.25"))
    paths = write_report(
        eval_run(out), out, results=[result("G01", "passed"), result("G02", "failed")],
        metrics=metrics, thresholds=ROWS, baseline=None, classifier=None,
    )  # fmt: skip
    assert [p.name for p in paths] == ["summary.json", "report.md", "report.html"]
    assert sorted(p.name for p in out.iterdir()) == ["report.html", "report.md", "summary.json"]
    data = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    for key in PROVENANCE:
        assert data[key] == summary()[key]
        assert data[key]
    assert data["metrics"] == metrics.model_dump(mode="json")
    assert data["thresholds"] == [row.model_dump(mode="json") for row in ROWS]
    assert (data["profile"], data["depth"], data["build_id"]) == ("local", "standard", "bld_tiny")
    assert "suite_changed" not in data["metrics"]


def test_ut11_110_classifier_json_when_given(tmp_path: Path) -> None:
    """UT11-110 `classifier.json` is written when a classifier report is given (a mapping
    or a pydantic model)."""
    out = tmp_path / "run"
    paths = write_report(
        eval_run(out), out, results=[], metrics=EvalMetrics(), thresholds=[],
        baseline=None, classifier={"checks": [{"name": "gate", "passed": True}]},
    )  # fmt: skip
    assert [p.name for p in paths] == ["summary.json", "report.md", "report.html",
                                       "classifier.json"]  # fmt: skip
    body = json.loads((out / "classifier.json").read_text(encoding="utf-8"))
    assert body == {"checks": [{"name": "gate", "passed": True}]}
    write_report(eval_run(out), out, results=[], metrics=EvalMetrics(), thresholds=[],
                 baseline=None, classifier=ROWS[0])  # fmt: skip
    assert json.loads((out / "classifier.json").read_text(encoding="utf-8"))["check"] == (
        "unsupported_numbers"
    )


def test_ut11_110_failures_first_and_baseline_diff(tmp_path: Path) -> None:
    """UT11-110 failures come first (thresholds and questions), then passes; a baseline adds
    the diff table; the final text sits in a code fence (md) and `<pre>` (html)."""
    out = tmp_path / "run"
    results = [result("G01", "passed", "Passing answer."), result("G02", "error", "",
               error="ModelUnavailable: down"), result("G03", "skipped")]  # fmt: skip
    base = EvalMetrics(answer_correctness=0.9, chat_latency_p95_s=4.0)
    write_report(
        eval_run(out), out, results=results, metrics=EvalMetrics(answer_correctness=0.5),
        thresholds=ROWS, baseline=base, classifier=None,
    )  # fmt: skip
    md = (out / "report.md").read_text(encoding="utf-8")
    assert md.index("G02") < md.index("G03") < md.index("G01")
    assert md.index("| correctness") < md.index("| unsupported_numbers")
    assert "## Baseline diff" in md
    assert "| answer_correctness | 0.9 | 0.5 | -40.0 pp |" in md
    assert "| chat_latency_p95_s | 4.0 | - | - |" in md
    assert "```text\nPassing answer.\n```" in md
    html = (out / "report.html").read_text(encoding="utf-8")
    assert html.index("G02") < html.index("G01")
    assert "<pre>Passing answer.</pre>" in html
    assert "Baseline diff" in html


def test_ut11_110_missing_provenance_is_config_error(tmp_path: Path) -> None:
    """UT11-110 a summary without a provenance key is refused; nothing is written."""
    out = tmp_path / "run"
    run = EvalRun(run_id=RUN_ID, out_dir=out, summary={"run_id": RUN_ID}, passed=True)
    with pytest.raises(ConfigError, match="git_sha"):
        write_report(run, out, results=[], metrics=EvalMetrics(), thresholds=[],
                     baseline=None, classifier=None)  # fmt: skip
    assert not out.exists()


# --- UT11-111 save_baseline, load_baseline ------------------------------------------------------


def test_ut11_111_aggregates_only_round_trip(tmp_path: Path) -> None:
    """UT11-111 `save_baseline` writes aggregates only (no `final_text` key anywhere, no run
    text); `load_baseline` returns the metrics; a missing name → None."""
    src = baseline_summary(final_text="The secret answer", results=[{"final_text": "x"}])
    path = save_baseline("synthetic-42-small", src, repo_root=tmp_path)
    assert path == tmp_path / "tests" / "eval" / "baselines" / "synthetic-42-small.json"
    raw = path.read_text(encoding="utf-8")
    stored = json.loads(raw)
    assert "final_text" not in set(keys_anywhere(stored))
    assert "secret answer" not in raw
    assert set(stored) == {"schema", "name", "metrics", "profile", "depth", "suite_version",
                           "suite_sha256", "dataset_seed", "dataset_scale"}  # fmt: skip
    assert (stored["dataset_seed"], stored["dataset_scale"]) == (42, "small")
    loaded = load_baseline("synthetic-42-small", repo_root=tmp_path, suite_sha256=SUITE_SHA)
    assert loaded == EvalMetrics.model_validate(src["metrics"])
    assert loaded is not None
    assert loaded.suite_changed is False
    assert load_baseline("no-such-baseline", repo_root=tmp_path) is None
    assert [p.name for p in path.parent.iterdir()] == ["synthetic-42-small.json"]


def test_ut11_111_name_and_content_validation(tmp_path: Path) -> None:
    """UT11-111 the name pattern `^[a-z0-9][a-z0-9-]{0,63}$` applies to both functions;
    metrics carrying text keys or a corrupt file are `ConfigError`."""
    for bad in ("Bad", "-lead", "a" * 65, "x/../y", "ok\n"):
        with pytest.raises(ConfigError, match="baseline name"):
            save_baseline(bad, baseline_summary(), repo_root=tmp_path)
        with pytest.raises(ConfigError, match="baseline name"):
            load_baseline(bad, repo_root=tmp_path)
    leaky = baseline_summary(metrics={"answer_correctness": 0.5, "final_text": "x"})
    with pytest.raises(ConfigError, match="metrics"):
        save_baseline("leaky", leaky, repo_root=tmp_path)
    folder = tmp_path / "tests" / "eval" / "baselines"
    folder.mkdir(parents=True)
    for i, body in enumerate(("{", "[]", '{"metrics": {"tokens_in": "many"}}')):
        (folder / f"broken-{i}.json").write_text(body, encoding="utf-8")
        with pytest.raises(ConfigError, match="baseline is not valid"):
            load_baseline(f"broken-{i}", repo_root=tmp_path)


def test_ut11_111_suite_changed_flag(tmp_path: Path) -> None:
    """UT11-111 a baseline of another suite is returned with `suite_changed` set (TH11-03);
    without a current suite SHA the flag stays off."""
    save_baseline("synthetic-42-small", baseline_summary(), repo_root=tmp_path)
    changed = load_baseline("synthetic-42-small", repo_root=tmp_path, suite_sha256="b" * 64)
    assert changed is not None
    assert changed.suite_changed is True
    assert changed.answer_correctness == 0.9
    plain = load_baseline("synthetic-42-small", repo_root=tmp_path)
    assert plain is not None
    assert plain.suite_changed is False


def test_cv_t11_31_mock_run_needs_mock_name(tmp_path: Path) -> None:
    """CV-T11-31 a mock run may only save a baseline whose name ends in `-mock` (TH11-10);
    a real build is refused (TH11-14); nothing is written."""
    with pytest.raises(ConfigError, match="-mock"):
        save_baseline("synthetic-42-small", baseline_summary(mock=True), repo_root=tmp_path)
    with pytest.raises(ConfigError, match="synthetic"):
        save_baseline("real-mock", baseline_summary(dataset_kind="real"), repo_root=tmp_path)
    assert not (tmp_path / "tests").exists()
    path = save_baseline("synthetic-42-small-mock", baseline_summary(mock=True),
                         repo_root=tmp_path)  # fmt: skip
    assert path.is_file()


def test_cv_t11_31_suite_changed_detail_in_thresholds(tmp_path: Path) -> None:
    """CV-T11-31 a suite-changed baseline disables the correctness drop check with the detail
    `suite changed`; the floor still applies."""
    save_baseline("synthetic-42-small", baseline_summary(), repo_root=tmp_path)
    base = load_baseline("synthetic-42-small", repo_root=tmp_path, suite_sha256="b" * 64)

    def correctness(value: float) -> ThresholdResult:
        rows = check_thresholds(
            EvalMetrics(answer_correctness=value), profile="local", depth="standard",
            baseline=base, results=[], dataset_kind="synthetic", dataset_scale="small",
            settings=env.settings().thresholds,
        )  # fmt: skip
        return next(r for r in rows if r.check == "correctness")

    ok = correctness(0.81)  # 9 pp below the baseline, above the floor
    assert (ok.passed, ok.detail) == (True, "suite changed")
    low = correctness(0.5)
    assert (low.passed, low.detail) == (False, "0.500 below floor 0.80; suite changed")


def test_cv_t11_31_markdown_fence_and_text_cap(tmp_path: Path) -> None:
    """CV-T11-31 model text with backtick runs gets a longer fence; very long text is cut
    with a marker; the html carries no external resource or script."""
    out = tmp_path / "run"
    text = "before ```` inside\n" + "x" * 30_000
    results = [result("G01", "failed", text)]
    write_report(eval_run(out), out, results=results, metrics=EvalMetrics(), thresholds=ROWS,
                 baseline=None, classifier=None)  # fmt: skip
    md = (out / "report.md").read_text(encoding="utf-8")
    assert "`````text\nbefore ```` inside" in md
    assert "[truncated]" in md
    assert "x" * 20_001 not in md
    html = (out / "report.html").read_text(encoding="utf-8").lower()
    for banned in ("http://", "https://", "<script", "<link", "<img", "<iframe", "@import"):
        assert banned not in html


# --- runner wiring (F11-07 steps 11-12) ---------------------------------------------------------


@pytest.fixture
def con(runner_store: OpsStoreHandle) -> Any:
    return make_root(runner_store.data_root)


def _run(  # noqa: PLR0913 - test helper with keyword-only knobs
    store: OpsStoreHandle, con: Any, suite: Path, *, chat: AnswerChat | None = None,
         profile: str = "local", resume: str | None = None, job: Any = None,
         **options: Any) -> EvalRun:  # fmt: skip
    deps = make_deps(store.data_root, con, chat=chat, job=job)
    return run_golden(load_suite(suite), profile, "fast", deps=deps, resume_run_id=resume,
                      options=EvalOptions(**options))  # fmt: skip


def _repo(store: OpsStoreHandle) -> Path:
    return store.data_root.parent  # make_deps' baselines root


def test_cv_t11_31_all_error_run_fails_once_a_baseline_exists(
    runner_store: OpsStoreHandle, con: Any, tmp_path: Path
) -> None:
    """CV-T11-31 an all-`error` local-fast run passes every threshold without a baseline (no
    local-fast floor) but fails the correctness drop once the default baseline exists."""
    suite = env.suite_file(tmp_path / "suite.yaml", [chat_q("G01"), chat_q("G02")])
    chat = AnswerChat(errors=frozenset({"How many priority one incidents?"}))
    first = _run(runner_store, con, suite, chat=chat)
    assert first.passed is True  # the gap this card closes
    correctness = next(r for r in first.summary["thresholds"] if r["check"] == "correctness")
    assert correctness["detail"] == "no baseline"
    row = get_run(first.run_id)
    assert row is not None
    assert row.meta["baseline"] is None

    sha = load_suite(suite).sha256
    save_baseline("synthetic-42-small", baseline_summary(suite_sha256=sha, depth="fast"),
                  repo_root=_repo(runner_store))  # fmt: skip
    second = _run(runner_store, con, suite, chat=chat)
    assert second.passed is False
    assert second.summary["exit_code"] == 14
    correctness = next(r for r in second.summary["thresholds"] if r["check"] == "correctness")
    assert (correctness["passed"], correctness["detail"]) == (False, "dropped 90.0 pp vs baseline")
    row = get_run(second.run_id)
    assert row is not None
    assert row.meta["baseline"] == "synthetic-42-small"
    assert second.summary["baseline"] == "synthetic-42-small"
    files = {p.name for p in second.out_dir.iterdir()}
    assert files >= {"summary.json", "report.md", "report.html", "results.jsonl"}


def test_cv_t11_31_explicit_baseline_and_set_baseline(
    runner_store: OpsStoreHandle, con: Any, tmp_path: Path
) -> None:
    """CV-T11-31 `--baseline NAME` that does not exist is `ConfigError` before any run row;
    `--set-baseline` saves the aggregates after the run; a changed suite reports
    `suite changed` on the next run."""
    suite = env.suite_file(tmp_path / "suite.yaml", [chat_q("G01")])
    with pytest.raises(ConfigError, match="baseline not found: name=other"):
        _run(runner_store, con, suite, baseline="other")
    run = _run(runner_store, con, suite, set_baseline="local-fast")
    path = _repo(runner_store) / "tests" / "eval" / "baselines" / "local-fast.json"
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["metrics"] == run.summary["metrics"]
    assert stored["suite_sha256"] == load_suite(suite).sha256
    assert stored["dataset_seed"] == 42

    changed = env.suite_file(tmp_path / "suite2.yaml", [chat_q("G01", tags=["new"])])
    again = _run(runner_store, con, changed, baseline="local-fast")
    correctness = next(r for r in again.summary["thresholds"] if r["check"] == "correctness")
    assert correctness["detail"] == "suite changed"
    row = get_run(again.run_id)
    assert row is not None
    assert row.meta["baseline"] == "local-fast"


def test_cv_t11_31_set_baseline_refused_before_the_run(
    runner_store: OpsStoreHandle, con: Any, tmp_path: Path
) -> None:
    """CV-T11-31 `--set-baseline` on a mock run with a non-`-mock` name is refused before the
    run starts (no run row, no question asked)."""
    suite = env.suite_file(tmp_path / "suite.yaml", [chat_q("G01")])
    chat = AnswerChat()
    with pytest.raises(ConfigError, match="-mock"):
        _run(runner_store, con, suite, chat=chat, mock=True, set_baseline="synthetic-42-small")
    assert chat.calls == []
    assert not (_repo(runner_store) / "tests").exists()


def test_cv_t11_31_judge_model_and_seed_in_summary(
    runner_store: OpsStoreHandle, con: Any, tmp_path: Path
) -> None:
    """CV-T11-31 the summary names the real judge's model (`RubricJudge.model`) and the
    dataset seed; without a judge `judge_model` is null."""
    suite = env.suite_file(tmp_path / "suite.yaml", [chat_q("G01")])
    judge = make_judge(FakeJudgeClient(), tmp_path / "judge-cache")
    assert judge.model == MODEL
    deps = make_deps(runner_store.data_root, con, judge=lambda: judge)
    run = run_golden(load_suite(suite), "local", "fast", deps=deps, options=EvalOptions())
    assert (run.summary["judge_model"], run.summary["dataset_seed"]) == (MODEL, 42)
    bare = _run(runner_store, con, suite)
    assert bare.summary["judge_model"] is None


def test_cv_t11_31_template_ships_as_package_data() -> None:
    """CV-T11-31 the HTML template is package data next to the module (PackageLoader)."""
    template = resources.files("herness.eval").joinpath("templates/report.html.j2")
    assert template.is_file()
    body = template.read_text(encoding="utf-8").lower()
    assert "<script" not in body
    assert "http" not in body.replace("http-equiv", "")


def test_cv_t11_31_report_module_public_names() -> None:
    """CV-T11-31 the report module exports the U11-70/U11-72 functions."""
    assert {"write_report", "save_baseline", "load_baseline"} <= set(report.__all__)
    assert report.BASELINE_NAME_RE.pattern == "^[a-z0-9][a-z0-9-]{0,63}$"


# --- fix round 1: comparability (I1), M1-M4 -----------------------------------------------------


def _row(run: EvalRun, check: str) -> dict[str, Any]:
    return next(r for r in run.summary["thresholds"] if r["check"] == check)


def _save_default(store: OpsStoreHandle, suite: Path, **over: Any) -> None:
    body = baseline_summary(suite_sha256=load_suite(suite).sha256, **over)
    save_baseline(over.pop("name", "synthetic-42-small"), body, repo_root=_repo(store))


def test_cv_t11_31_premium_run_ignores_local_default(
    runner_store: OpsStoreHandle, con: Any, tmp_path: Path
) -> None:
    """CV-T11-31 a premium run with a local-standard default baseline (cost about 0) is not
    compared: no cost failure, a visible `not comparable` detail, no `run.meta.baseline`."""
    suite = env.suite_file(tmp_path / "suite.yaml", [chat_q("G01")])
    _save_default(runner_store, suite)  # local-standard, cost 0
    run = _run(runner_store, con, suite, profile="premium")
    note = (
        "baseline synthetic-42-small not comparable: taken at local-standard, run is premium-fast"
    )
    assert _row(run, "cost") == {**_row(run, "cost"), "passed": True, "detail": note}
    assert note in _row(run, "correctness")["detail"]
    assert note in _row(run, "latency")["detail"]
    assert run.passed is True
    assert run.summary["baseline"] is None
    row = get_run(run.run_id)
    assert row is not None
    assert row.meta["baseline"] is None


def test_cv_t11_31_real_build_ignores_synthetic_default(
    runner_store: OpsStoreHandle, tmp_path: Path
) -> None:
    """CV-T11-31 a real build is never compared with a (synthetic) default baseline."""
    real = make_root(runner_store.data_root, kind="real")
    suite = env.suite_file(tmp_path / "suite.yaml", [chat_q("G01")])
    _save_default(runner_store, suite, depth="fast")
    run = _run(runner_store, real, suite)
    assert _row(run, "correctness")["detail"] == (
        "baseline synthetic-42-small not comparable: run is not synthetic"
    )
    assert run.summary["baseline"] is None


def test_cv_t11_31_mock_and_non_mock_baselines(
    runner_store: OpsStoreHandle, con: Any, tmp_path: Path
) -> None:
    """CV-T11-31 a mock run ignores a non-`-mock` default; an explicit `-mock` baseline on a
    non-mock run, or an explicit baseline of another profile or depth, is `ConfigError`."""
    suite = env.suite_file(tmp_path / "suite.yaml", [chat_q("G01")])
    _save_default(runner_store, suite, depth="fast")
    run = _run(runner_store, con, suite, mock=True)
    assert _row(run, "correctness")["detail"] == (
        "baseline synthetic-42-small not comparable: mock runs use -mock baselines only"
    )
    _save_default(runner_store, suite, depth="fast", name="local-fast-mock", mock=True)
    with pytest.raises(ConfigError, match="a -mock baseline needs a mock run"):
        _run(runner_store, con, suite, baseline="local-fast-mock")
    mocked = _run(runner_store, con, suite, mock=True, baseline="local-fast-mock")
    assert mocked.summary["baseline"] == "local-fast-mock"
    _save_default(runner_store, suite, name="local-standard")
    with pytest.raises(ConfigError, match="not comparable: taken at local-standard"):
        _run(runner_store, con, suite, baseline="local-standard")
    rows = read_all("SELECT run_id FROM run WHERE kind = 'eval'", ())
    assert sorted(r["run_id"] for r in rows) == sorted([run.run_id, mocked.run_id])


def test_cv_t11_31_suite_changed_without_graded_questions(tmp_path: Path) -> None:
    """CV-T11-31 (M1) with no graded question the correctness row still says `suite changed`."""
    save_baseline("synthetic-42-small", baseline_summary(), repo_root=tmp_path)
    base = load_baseline("synthetic-42-small", repo_root=tmp_path, suite_sha256="b" * 64)
    rows = check_thresholds(
        EvalMetrics(), profile="local", depth="standard", baseline=base, results=[],
        dataset_kind="synthetic", dataset_scale="small", settings=env.settings().thresholds,
    )  # fmt: skip
    correctness = next(r for r in rows if r.check == "correctness")
    assert correctness.detail == "no graded questions; suite changed"


def test_cv_t11_31_stored_suite_changed_key_refused(tmp_path: Path) -> None:
    """CV-T11-31 (M2) a baseline file cannot carry `suite_changed` to switch the drop check
    off; `read_baseline` exposes the stored profile, depth and scale."""
    path = save_baseline("synthetic-42-small", baseline_summary(), repo_root=tmp_path)
    found = report.read_baseline("synthetic-42-small", repo_root=tmp_path)
    assert found is not None
    assert (found.profile, found.depth, found.dataset_scale) == ("local", "standard", "small")
    body = json.loads(path.read_text(encoding="utf-8"))
    body["metrics"]["suite_changed"] = True
    path.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(ConfigError, match="baseline is not valid"):
        load_baseline("synthetic-42-small", repo_root=tmp_path)


def test_cv_t11_31_resume_records_the_baseline_compared(
    runner_store: OpsStoreHandle, con: Any, tmp_path: Path
) -> None:
    """CV-T11-31 (M3) a default baseline that appears between a yield and the resume: the
    run row's `meta.baseline` is patched at the end to match `summary.json`."""
    suite = env.suite_file(tmp_path / "suite.yaml", [chat_q("G01"), chat_q("G02")])
    ctx = FakeCtx({"profile": "local", "repeat": 1}, yield_after=1)
    first = _run(runner_store, con, suite, job=ctx)
    assert first.yielded is True
    row = get_run(first.run_id)
    assert row is not None
    assert row.meta["baseline"] is None
    _save_default(runner_store, suite, depth="fast")
    done = _run(runner_store, con, suite, resume=first.run_id)
    assert done.summary["baseline"] == "synthetic-42-small"
    row = get_run(first.run_id)
    assert row is not None
    assert row.meta["baseline"] == "synthetic-42-small"


def test_cv_t11_31_write_closes_fd_when_fdopen_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CV-T11-31 (M4) when `os.fdopen` fails the temp file's descriptor is closed and the
    temp file removed."""
    closed: list[int] = []
    real_close = report.os.close

    def fail(*args: object, **kwargs: object) -> Any:
        msg = "no fdopen"
        raise OSError(msg)

    def close(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    monkeypatch.setattr(report.os, "fdopen", fail)
    monkeypatch.setattr(report.os, "close", close)
    with pytest.raises(OSError, match="no fdopen"):
        report._write(tmp_path / "x.json", "{}")
    assert len(closed) == 1
    assert list(tmp_path.iterdir()) == []
