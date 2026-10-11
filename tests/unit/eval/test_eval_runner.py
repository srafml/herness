"""Tests for herness.eval.runner (U11-66, U11-68, U11-69): UT11-70, UT11-109, UT11-116 and
controller checks (T11-30).

`run_golden` runs against the fixture warehouse of `_runner_env` (in-memory DuckDB, tmp
`CURRENT`, seed-42 truth) and the migrated tmp ops store (`runner_store`); chat answers come
from `AnswerChat`, reviews from a fake swarm.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

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
from herness.core.jobs.handlers import resolve_handler
from herness.core.types import ReportDraft
from herness.eval import runner
from herness.eval.golden import load_suite
from herness.eval.runner import (
    EvalOptions,
    build_eval_payload,
    eval_gpu_class,
    eval_idem_key,
    exit_code,
    handle_eval,
    run_golden,
)
from herness.harness.swarm import RunRequest, RunResult
from herness.store.ops import get_chat_session, get_run, list_chat_messages
from herness.store.ops.core import read_all

pytestmark = pytest.mark.unit
__all__ = ["runner_store", "test_redactor"]  # fixtures

_RESUME = "run_01J9ZQ4Y8M6V3K2N1P0R5T7W9X"  # pragma: allowlist secret - test ULID


@pytest.fixture
def unbound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner._Binding, "factory", None)


# --- UT11-70 build_eval_payload, eval_gpu_class -------------------------------------------------


def test_ut11_70_payload_repeat_from_config_and_no_priority() -> None:
    """UT11-70 `repeat` defaults to `eval.yaml: repeat[depth]`; `priority` is absent (None)."""
    cfg = env.settings()
    deep = build_eval_payload(profile="local", depth="deep", settings=cfg)
    standard = build_eval_payload(profile="local", settings=cfg, ids=("G01",))
    assert deep["repeat"] == 3
    assert standard["repeat"] == 1
    assert standard["ids"] == ["G01"]
    assert "priority" not in deep
    assert deep.get("priority") is None
    assert build_eval_payload(profile="local", depth="deep", repeat=2, settings=cfg)["repeat"] == 2
    assert set(deep) == set(runner.EvalPayload.model_fields)


def test_ut11_70_gpu_class() -> None:
    """UT11-70 `none` for mock and compare-runs; `decider` for the classifier suite (R-43);
    `reasoning` otherwise."""
    cfg = env.settings()
    mock = build_eval_payload(profile="local", mock_llm_dir="tests/eval/mock_scripts", settings=cfg)
    compare = build_eval_payload(profile="local", compare_runs=[_RESUME], settings=cfg)
    classifier = build_eval_payload(profile="local", suite="classifier", settings=cfg)
    golden = build_eval_payload(profile="premium", settings=cfg)
    assert eval_gpu_class(mock) == "none"
    assert eval_gpu_class(compare) == "none"
    assert eval_gpu_class(classifier) == "decider"
    assert eval_gpu_class(golden) == "reasoning"


def test_ut11_70_idem_key_and_validation() -> None:
    """UT11-70 `idem_key` is the spec 08 default (None) or `resume:eval:<run_id>`; unknown
    options and out-of-range values are `ConfigError`."""
    cfg = env.settings()
    assert eval_idem_key(build_eval_payload(profile="local", settings=cfg)) is None
    resumed = build_eval_payload(profile="local", resume_run_id=_RESUME, settings=cfg)
    assert eval_idem_key(resumed) == f"resume:eval:{_RESUME}"
    with pytest.raises(ConfigError, match="unknown eval options: colour"):
        build_eval_payload(profile="local", colour="red", settings=cfg)
    with pytest.raises(ConfigError, match="profile"):
        build_eval_payload(profile="cloudy", settings=cfg)


def test_ut11_70_repeat_from_loaded_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT11-70 without `settings=` the loaded config's `eval` section supplies `repeat`; no
    section → `ConfigError`."""

    class _Cfg:
        eval: Any = env.settings()

    monkeypatch.setattr("herness.eval._runner_payload.get_config", _Cfg)
    assert build_eval_payload(profile="local", depth="deep")["repeat"] == 3
    _Cfg.eval = None
    with pytest.raises(ConfigError, match="not configured"):
        build_eval_payload(profile="local")


# --- UT11-109 exit_code -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "code"),
    [
        ({"status": "done", "result": {"run_id": _RESUME, "passed": True, "exit_code": 0}}, 0),
        ({"run_id": _RESUME, "passed": True, "exit_code": 0}, 0),
        ({"status": "done", "result": {"passed": False, "exit_code": 14}}, 14),  # threshold
        ({"status": "done", "result": {"passed": False}}, 14),  # required rubric skipped
        ({"status": "done", "result": {"passed": False, "classifier": "failed"}}, 14),
        ({"status": "failed", "last_error": {"class": "ConfigError", "message": "x"}}, 3),
        ({"error_type": "ConfigError"}, 3),
        ({"status": "failed", "last_error": {"class": "FatalError"}}, 1),
        ({"status": "failed"}, 1),
        ({"status": "yield", "result": {"run_id": _RESUME}}, 1),
        ({"status": "done", "result": "garbled"}, 1),
    ],
)
def test_ut11_109_exit_codes(outcome: dict[str, Any], code: int) -> None:
    """UT11-109 0 pass; 14 threshold fail, required rubric skipped, classifier check failed;
    3 config error; 1 any other job failure; never 2 or 4 (R-46)."""
    assert exit_code(outcome) == code
    assert exit_code(outcome) not in {2, 4}


# --- UT11-116 handle_eval -----------------------------------------------------------------------


def _mock_dir(tmp_path: Path) -> Path:
    scripts = tmp_path / "mock"
    scripts.mkdir()
    (scripts / "judge.yaml").write_text(
        "- match: {model_role: eval_judge}\n  turns:\n    - final: {text: ok}\n", encoding="utf-8"
    )
    return scripts


def _payload(tmp_path: Path, suite: Path, **extra: Any) -> dict[str, Any]:
    return build_eval_payload(
        suite=str(suite), profile="local", depth="fast", ids=["G01"], no_judge=True,
        mock_llm_dir=str(_mock_dir(tmp_path)), settings=env.settings(), **extra,
    )  # fmt: skip


def test_ut11_116_handle_eval_reads_ctx_payload(
    runner_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT11-116 the payload is read from `ctx.job.payload` (R-42) and the bound factory gets
    the context; `JobOutcome("done", ...)` carries `run_id`, `passed`, `exit_code`."""
    con = make_root(runner_store.data_root)
    suite = env.suite_file(tmp_path / "suite.yaml", [chat_q("G01"), chat_q("G02")])
    ctx = FakeCtx(_payload(tmp_path, suite))
    seen: list[object] = []

    def factory(c: Any) -> runner.EvalDeps:
        seen.append(c)
        return make_deps(runner_store.data_root, con, job=c)

    monkeypatch.setattr(runner._Binding, "factory", None)
    runner.set_deps_factory(factory)
    outcome = handle_eval(ctx)  # type: ignore[arg-type]

    assert seen == [ctx]
    assert outcome.status == "done"
    run_id = outcome.result["run_id"]
    assert outcome.result == {"run_id": run_id, "passed": True, "exit_code": 0}
    assert exit_code(outcome.model_dump()) == 0
    run = get_run(str(run_id))
    assert run is not None
    assert (run.kind, run.status, run.build_id, run.meta["mock"]) == (
        "eval", "done", env.BUILD_ID, True,
    )  # fmt: skip
    out = runner_store.data_root / "reports" / "eval" / str(run_id)
    assert [r["id"] for r in lines(out / "results.jsonl")] == ["G01"]  # --ids
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert (summary["mock"], summary["passed"], summary["exit_code"]) == (True, True, 0)
    assert ctx.state == {"eval_run_id": run_id}


def test_ut11_116_unbound_factory(unbound: None, tmp_path: Path) -> None:
    """UT11-116 without `set_deps_factory` → `ConfigError("eval dependencies not bound")`."""
    ctx = FakeCtx({"profile": "local", "repeat": 1})
    with pytest.raises(ConfigError, match=r"^eval dependencies not bound$"):
        handle_eval(ctx)  # type: ignore[arg-type]


def test_ut11_116_register_handlers_idempotent() -> None:
    """UT11-116 `register_handlers` binds `handle_eval` to kind `eval`; again is a no-op."""
    runner.register_handlers()
    runner.register_handlers()
    assert resolve_handler("eval") is handle_eval


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"repeat": 11}, "repeat"),  # TH11-13
        ({"compare_runs": [_RESUME]}, "run comparison is not available until T11-38"),
        ({"suite": "classifier"}, "classifier eval is not available until T11-35"),
        ({"compare_profile": "premium"}, "profile comparison is not available until T11-38"),
        ({"baseline": "synthetic-42-small"}, "baseline comparison is not available until T11-31"),
        ({"set_baseline": "x-mock"}, "baseline comparison is not available until T11-31"),
        ({"ids": ["G77"]}, "unknown question ids: G77"),
        ({"mock_llm_dir": "no/such/dir"}, ""),
    ],
)
def test_cv_t11_30_config_errors(
    extra: dict[str, Any], message: str, runner_store: OpsStoreHandle, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    """CV-T11-30 payload, unbuilt features (T11-31/35/38), unknown ids and a bad mock dir
    are `ConfigError` (exit 3)."""
    con = make_root(runner_store.data_root)
    suite = env.suite_file(tmp_path / "suite.yaml", [chat_q("G01")])
    payload = {"suite": str(suite), "profile": "local", "depth": "fast", "repeat": 1} | extra
    monkeypatch.setattr(runner._Binding, "factory", None)
    runner.set_deps_factory(lambda c: make_deps(runner_store.data_root, con, job=c))
    with pytest.raises(ConfigError, match=message):
        handle_eval(FakeCtx(payload))  # type: ignore[arg-type]
    assert read_all("SELECT run_id FROM run WHERE kind = 'eval'", ()) == []


def test_cv_t11_30_suite_path_golden_uses_settings(
    runner_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CV-T11-30 suite `golden` resolves to `eval.yaml: suite`; a missing file is exit 3."""
    con = make_root(runner_store.data_root)
    deps = make_deps(runner_store.data_root, con)
    missing = tmp_path / "none.yaml"
    deps = runner.EvalDeps(**(vars(deps) | {"settings": deps.settings.model_copy(
        update={"suite": str(missing)})}))  # fmt: skip
    monkeypatch.setattr(runner._Binding, "factory", None)
    runner.set_deps_factory(lambda c: deps)
    with pytest.raises(ConfigError, match="cannot read golden suite"):
        handle_eval(FakeCtx({"profile": "local", "repeat": 1}))  # type: ignore[arg-type]


# --- run_golden ---------------------------------------------------------------------------------


def _run(data_root: Path, suite: Path, *, profile: str = "local", **kw: Any) -> runner.EvalRun:
    deps = kw.pop("deps")
    options = kw.pop("options", EvalOptions())
    return run_golden(load_suite(suite), profile, kw.pop("depth", "fast"), deps=deps,
                      options=options, **kw)  # fmt: skip


def test_cv_t11_30_statuses_and_open_session(runner_store: OpsStoreHandle, tmp_path: Path) -> None:
    """CV-T11-30 one run grades passed, failed, error, suite_error and skipped results; the
    real `open_session` hook stores the redacted question as the session's user message."""
    con = make_root(runner_store.data_root)
    questions = [
        chat_q("G01"),
        chat_q("G02", question="Count of failing priority one incidents?"),
        chat_q(
            "G03",
            expected={"numeric": {"reference_sql": "SELECT * FROM core.nope", "unit": "count"}},
        ),
        chat_q("G04", datasets=["real"]),
        chat_q("G05", setup="seed_prior_run"),
        chat_q("G06", expected={"rubric": {"criteria": ["clear"], "min_score": 3}}),
    ]
    suite = env.suite_file(tmp_path / "suite.yaml", questions)
    chat = AnswerChat(errors=frozenset({"Count of failing priority one incidents?"}))
    run = _run(
        runner_store.data_root, suite, deps=make_deps(runner_store.data_root, con, chat=chat)
    )
    statuses = {r["id"]: r["status"] for r in lines(run.out_dir / "results.jsonl")}
    assert statuses == {"G01": "passed", "G02": "error", "G03": "suite_error",
                        "G04": "skipped", "G05": "suite_error", "G06": "skipped"}  # fmt: skip
    by_id = {r["id"]: r for r in lines(run.out_dir / "results.jsonl")}
    assert by_id["G05"]["error"] == (  # no memory in the deps (T11-33 hook)
        "SuiteError: setup seed_prior_run needs EvalDeps.memory and EvalDeps.seed_prior_run"
    )
    assert by_id["G03"]["error"] == "SuiteError: reference SQL rejected"
    assert by_id["G01"]["unsupported"]["unsupported"] == 0
    assert by_id["G02"]["error"].startswith("ModelUnavailable")
    assert not run.passed
    assert run.summary["exit_code"] == 14
    failed = {row["check"] for row in run.summary["thresholds"] if not row["passed"]}
    assert failed == {"suite_errors", "planted_truth"}  # G03 is a planted id
    session_id = chat.calls[0][0]
    assert session_id.startswith("ses_")
    session = get_chat_session(session_id)
    assert session is not None
    assert [m["role"] for m in list_chat_messages(session_id)] == ["user"]
    assert "question_status" in run.summary["metrics"]
    assert "How many" not in json.dumps(run.summary)  # no question or answer text


def test_cv_t11_30_unsupported_and_wrong_value(
    runner_store: OpsStoreHandle, tmp_path: Path
) -> None:
    """CV-T11-30 a cited number that differs from its re-run is unsupported and fails
    `unsupported_numbers` (TH11-12); numeric grading fails the question."""
    con = make_root(runner_store.data_root)
    suite = env.suite_file(tmp_path / "suite.yaml", [chat_q("G01")])
    deps = make_deps(runner_store.data_root, con, chat=AnswerChat(value=5))
    run = _run(runner_store.data_root, suite, deps=deps)
    (result,) = lines(run.out_dir / "results.jsonl")
    assert result["status"] == "failed"
    assert result["unsupported"]["stale_refs"] == ["n1"]
    failed = {row["check"] for row in run.summary["thresholds"] if not row["passed"]}
    assert "unsupported_numbers" in failed
    assert run.summary["exit_code"] == 14


def test_cv_t11_30_repeats_and_tags(runner_store: OpsStoreHandle, tmp_path: Path) -> None:
    """CV-T11-30 every (id, repeat) is recorded once; `--tags` filters the questions."""
    con = make_root(runner_store.data_root)
    questions = [chat_q("G01", tags=["a"]), chat_q("G02", tags=["b"])]
    suite = env.suite_file(tmp_path / "suite.yaml", questions)
    run = _run(runner_store.data_root, suite, repeat=3, deps=make_deps(runner_store.data_root, con),
               options=EvalOptions(tags=frozenset({"a"})))  # fmt: skip
    keys = [(r["id"], r["repeat"]) for r in lines(run.out_dir / "results.jsonl")]
    assert keys == [("G01", 1), ("G01", 2), ("G01", 3)]
    assert run.passed
    with pytest.raises(ConfigError, match=r"repeat must be 1\.\.10"):
        _run(runner_store.data_root, suite, repeat=0, deps=make_deps(runner_store.data_root, con))
    with pytest.raises(ConfigError, match="depth"):
        _run(
            runner_store.data_root, suite, depth="slow", deps=make_deps(runner_store.data_root, con)
        )


@pytest.mark.parametrize("after", [0, 1])
def test_cv_t11_30_yield_then_resume(
    after: int, runner_store: OpsStoreHandle, tmp_path: Path
) -> None:
    """CV-T11-30 `should_yield` between questions → `yield` with the run id; the requeued job
    resumes from `results.jsonl` (saved state) and reuses the run row (F11-10), also when
    it yielded before its first result."""
    con = make_root(runner_store.data_root)
    questions = [chat_q("G01"), chat_q("G02"), chat_q("G03")]
    suite = env.suite_file(tmp_path / "suite.yaml", questions)
    payload = {"suite": str(suite), "profile": "local", "depth": "fast", "repeat": 1}
    ctx = FakeCtx(payload, yield_after=after)
    chat = AnswerChat()
    runner._Binding.factory = None
    runner.set_deps_factory(lambda c: make_deps(runner_store.data_root, con, chat=chat, job=c))
    try:
        first = handle_eval(ctx)  # type: ignore[arg-type]
        assert first.status == "yield"
        run_id = str(first.result["run_id"])
        out = runner_store.data_root / "reports" / "eval" / run_id
        assert out.is_dir()
        done = lines(out / "results.jsonl") if after else []
        assert [r["id"] for r in done] == ["G01"][:after]
        ctx.yield_after = None
        second = handle_eval(ctx)  # type: ignore[arg-type]
    finally:
        runner._Binding.factory = None
    assert second.status == "done"
    assert second.result["run_id"] == run_id
    assert [r["id"] for r in lines(out / "results.jsonl")] == ["G01", "G02", "G03"]
    assert len(chat.calls) == 3
    rows = read_all("SELECT run_id, status FROM run WHERE kind = 'eval'", ())
    assert [(r["run_id"], r["status"]) for r in rows] == [(run_id, "done")]


def test_cv_t11_30_resume_errors(runner_store: OpsStoreHandle, tmp_path: Path) -> None:
    """CV-T11-30 resuming a run without its directory or row is `ConfigError` (F11-10)."""
    con = make_root(runner_store.data_root)
    suite = env.suite_file(tmp_path / "suite.yaml", [chat_q("G01")])
    deps = make_deps(runner_store.data_root, con)
    with pytest.raises(ConfigError, match="run directory not found"):
        _run(runner_store.data_root, suite, resume_run_id=_RESUME, deps=deps)
    (runner_store.data_root / "reports" / "eval" / _RESUME).mkdir(parents=True)
    with pytest.raises(ConfigError, match="eval run not found"):
        _run(runner_store.data_root, suite, resume_run_id=_RESUME, deps=deps)


def test_cv_t11_30_build_errors(runner_store: OpsStoreHandle, tmp_path: Path) -> None:
    """CV-T11-30 no `CURRENT`, a bad `CURRENT` or a build without `meta.build` is exit 3."""
    suite = env.suite_file(tmp_path / "suite.yaml", [chat_q("G01")])
    con = make_root(runner_store.data_root)
    current = runner_store.data_root / "warehouse" / "CURRENT"
    current.write_text("garbage", encoding="ascii")
    with pytest.raises(ConfigError, match="cannot read the current build"):
        _run(runner_store.data_root, suite, deps=make_deps(runner_store.data_root, con))
    current.unlink()
    with pytest.raises(ConfigError, match="no current build"):
        _run(runner_store.data_root, suite, deps=make_deps(runner_store.data_root, con))
    current.write_text(env.BUILD_ID, encoding="ascii")
    con.execute("UPDATE meta.build SET dataset_kind = 'odd'")
    with pytest.raises(ConfigError, match="dataset_kind is unknown"):
        _run(runner_store.data_root, suite, deps=make_deps(runner_store.data_root, con))
    con.execute("DROP TABLE meta.build")
    with pytest.raises(ConfigError, match=r"no meta\.build row"):
        _run(runner_store.data_root, suite, deps=make_deps(runner_store.data_root, con))


def test_cv_t11_30_real_build_has_no_truth(runner_store: OpsStoreHandle, tmp_path: Path) -> None:
    """CV-T11-30 a real build loads no truth: planted checks do not apply, scale is full."""
    con = make_root(runner_store.data_root, kind="real")
    suite = env.suite_file(tmp_path / "suite.yaml", [chat_q("G01")])
    run = _run(runner_store.data_root, suite, deps=make_deps(runner_store.data_root, con))
    assert run.summary["dataset_kind"] == "real"
    assert run.summary["dataset_scale"] == "full"
    planted = next(r for r in run.summary["thresholds"] if r["check"] == "planted_truth")
    assert planted["detail"] == "not synthetic"


class _Swarm:
    """Fake swarm: each `start` writes a full draft citing the numeric evidence."""

    def __init__(self, tmp: Path) -> None:
        self.tmp, self.requests = tmp, []

    async def start(self, req: RunRequest, job: object = None) -> RunResult:
        from tests.support.report_drafts import draft_dict, paragraph  # noqa: PLC0415

        self.requests.append(req)
        ref = {"id": "n1", "value": 2, "unit": "count", "query_id": env.NUMERIC_QID,
               "column": "value", "row_key": None}  # fmt: skip
        para = paragraph("Platform Ops had [[n1]] priority one incidents.", numbers=[ref])
        draft = ReportDraft.model_validate(draft_dict(
            sections=[{"id": "executive_summary", "title": "Summary", "paragraphs": [para]}],
            recommendations=[], caveats=[],
            ranked_entities=[{"rank": 1, "entity_type": "team", "entity_id": "t1"}],
        ))  # fmt: skip
        path = self.tmp / f"draft{len(self.requests)}.json"
        path.write_text(draft.model_dump_json(), encoding="utf-8")
        return RunResult(run_id=env.final().run_id, status="done", draft_path=str(path),
                         coverage=draft.coverage, dead_task_ids=[], token_usage={},
                         cost_usd=Decimal(0))  # fmt: skip


def test_cv_t11_30_reviews_share_one_run_per_repeat(
    runner_store: OpsStoreHandle, tmp_path: Path
) -> None:
    """CV-T11-30 review questions share one run per (pipeline, profile, depth) and repeat;
    a framing question gets its own run with the resolved question text."""
    con = make_root(runner_store.data_root)
    review = {"numeric": {"reference_sql": env.NUMERIC_SQL, "unit": "count",
                          "tolerance": {"abs": 0}}}  # fmt: skip
    questions = [
        {"id": "O01", "pipeline": "org", "question": "Org review", "expected": review},
        {"id": "O02", "pipeline": "org", "question": "Org review", "expected": review},
        {"id": "F01", "pipeline": "funding", "question": "Fund what?", "framing": True,
         "expected": review},
    ]  # fmt: skip
    suite = env.suite_file(tmp_path / "suite.yaml", questions)
    swarm = _Swarm(tmp_path)
    run = _run(runner_store.data_root, suite, repeat=2,
               deps=make_deps(runner_store.data_root, con, swarm=swarm))  # fmt: skip
    kinds = [(r.kind, r.question) for r in swarm.requests]
    assert kinds.count(("org_review", None)) == 2  # one per repeat
    assert kinds.count(("funding_review", "Fund what?")) == 2
    assert all(r.build_id == env.BUILD_ID and r.profile == "local" for r in swarm.requests)
    statuses = {(r["id"], r["repeat"]): r["status"] for r in lines(run.out_dir / "results.jsonl")}
    assert set(statuses.values()) == {"passed"}
    assert run.summary["metrics"]["skeptic_catch"] is True
