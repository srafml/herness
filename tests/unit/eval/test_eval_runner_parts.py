"""Controller checks of the runner's private siblings (T11-30): the `open_session` hook, the
name index, traces, the `count_unsupported` evidence reader, rule placeholders, entity and
rubric grading, and the run row on failure."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import duckdb
import pytest
from tests.support.ops_store import OpsStoreHandle
from tests.unit.eval import _runner_env as env
from tests.unit.eval._golden_fixtures import ENTITIES_SQL, NUMERIC_SQL, T1_TEAM
from tests.unit.eval._runner_env import (
    AnswerChat,
    chat_q,
    lines,
    make_deps,
    make_root,
    runner_store,
    test_redactor,
)

from herness.core import time as clock
from herness.core.errors import FatalError, ModelUnavailable, NotFound
from herness.core.ids import IdKind, new_id
from herness.eval import _runner_support as support
from herness.eval import runner
from herness.eval.golden import load_suite
from herness.eval.runner import EvalOptions, run_golden
from herness.store.ops import get_run

pytestmark = pytest.mark.unit
__all__ = ["runner_store", "test_redactor"]  # fixtures


def _run(store: OpsStoreHandle, tmp_path: Path, questions: list[dict[str, Any]],
         **deps: Any) -> runner.EvalRun:  # fmt: skip
    con = deps.pop("con", None) or make_root(store.data_root)
    suite = env.suite_file(tmp_path / "suite.yaml", questions)
    eval_deps = make_deps(store.data_root, con, **deps)
    return run_golden(load_suite(suite), "local", "fast", deps=eval_deps, options=EvalOptions())


# --- open_session (U11-62 amendment) ------------------------------------------------------------


def test_cv_t11_30_open_session_fails_closed(
    runner_store: OpsStoreHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CV-T11-30 a question that cannot be redacted opens no session; a vanished session is
    `NotFound`."""
    opener = support.open_session_for(clock.now)
    monkeypatch.setattr(support, "redact_text", lambda text: None)
    with pytest.raises(FatalError, match="could not be redacted"):
        opener("How many incidents?")
    monkeypatch.setattr(support, "redact_text", lambda text: text)
    monkeypatch.setattr(support, "append_chat_message", lambda *a, **k: None)
    with pytest.raises(NotFound, match="vanished"):
        opener("How many incidents?")


# --- name index, traces, params ---------------------------------------------------------------


def test_cv_t11_30_name_index() -> None:
    """CV-T11-30 lower-cased display names map to entity ids; missing tables are skipped."""
    names = support.name_index(env.make_con())
    assert names["platform ops l2"] == T1_TEAM
    assert names["retail"] == "servicenow:cmn_department:1"
    assert names["chk-2001"] == "jira:issue:20010"
    assert support.name_index(duckdb.connect(":memory:")) == {}


def test_cv_t11_30_load_trace(tmp_path: Path) -> None:
    """CV-T11-30 trace lines that are not JSON objects are skipped; a missing file is None."""
    path = tmp_path / "t.jsonl"
    path.write_text('{"type": "retry"}\nnot json\n[1]\n{"type": "repair"}\n', encoding="utf-8")
    assert support.load_trace(path) == [{"type": "retry"}, {"type": "repair"}]
    assert support.load_trace(tmp_path / "missing.jsonl") is None


def test_cv_t11_30_bind_params() -> None:
    """CV-T11-30 recorded params: JSON text, impl 04 `{bind, template}`, plain, other."""
    assert support._bind('{"a": 1}') == {"a": 1}
    assert support._bind({"bind": {"a": 1}, "template": "t"}) == {"a": 1}
    assert support._bind({"a": 2}) == {"a": 2}
    assert support._bind(None) == {}


# --- evidence reader ----------------------------------------------------------------------------


def test_cv_t11_30_evidence_reader(
    runner_store: OpsStoreHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CV-T11-30 ops evidence of another build is not evidence; the build's `meta.evidence`
    is used when ops has none; a missing or oversized re-run raises."""
    con = make_root(runner_store.data_root)
    reader = support.EvidenceReader(con)
    assert reader.exists(env.NUMERIC_QID, env.BUILD_ID)
    assert not reader.exists(env.NUMERIC_QID, "20990101-000000-ABCDEF")
    assert not reader.exists("q_" + "0" * 16, env.BUILD_ID)  # no meta.evidence table yet
    con.execute("CREATE TABLE meta.evidence (query_id VARCHAR, sql VARCHAR, params VARCHAR)")
    meta_qid = "q_" + "1" * 16
    con.execute("INSERT INTO meta.evidence VALUES (?, ?, ?)",
                [meta_qid, "SELECT count(*) AS value FROM core.team WHERE $p = 1",
                 json.dumps({"bind": {"p": 1}, "template": "x"})])  # fmt: skip
    assert reader.exists(meta_qid, env.BUILD_ID)
    assert reader.rerun(meta_qid) == [{"value": 3}]
    assert reader.rerun(env.NUMERIC_QID) == [{"value": 2}]
    with pytest.raises(NotFound, match="no evidence"):
        reader.rerun("q_" + "2" * 16)
    monkeypatch.setattr(support, "RERUN_MAX_ROWS", 0)
    with pytest.raises(NotFound, match="too large"):
        reader.rerun(env.NUMERIC_QID)


# --- grading through run_golden -----------------------------------------------------------------


def test_cv_t11_30_rule_placeholders_and_entities(
    runner_store: OpsStoreHandle, tmp_path: Path
) -> None:
    """CV-T11-30 `{name}` placeholders from `placeholders_sql` are filled in rule strings
    (escaped in claim patterns); chat entity ids come from the build's name index."""
    expected = {
        "placeholders_sql": "SELECT name AS team_name FROM core.team ORDER BY name LIMIT 1",
        "must_mention": ["{team_name}", {"entity": "{team_name}", "with_any": ["incidents"]}],
        "must_not_claim": [{"entity": "{team_name}", "pattern": "{team_name} improved"}],
        "entities": {"reference_sql": ENTITIES_SQL, "check": "rank1"},
    }
    text = "Change Crew had [[n1]] incidents. Platform Ops L2 is fine."
    q = chat_q("G01", expected=expected)
    run = _run(runner_store, tmp_path, [q], chat=AnswerChat(text=text))
    (result,) = lines(run.out_dir / "results.jsonl")
    checks = {(c["check"], c["status"]) for c in result["checks"]}
    assert checks == {("must_mention", "passed"), ("must_not_claim", "passed"),
                      ("entities:rank1", "failed")}  # fmt: skip
    entity = next(c for c in result["checks"] if c["check"] == "entities:rank1")
    assert entity["detail"]["ranked"][:2] == ["servicenow:sys_user_group:9f2", T1_TEAM]


class _DownJudge:
    model = "judge-down"

    def score(self, *args: object) -> object:
        msg = "judge down"
        raise ModelUnavailable(msg)


def test_cv_t11_30_required_rubric_skipped_exit_14(
    runner_store: OpsStoreHandle, tmp_path: Path
) -> None:
    """CV-T11-30 a rubric the run asked the judge for and could not get is skipped and
    fails the run (exit 14) although every threshold passes (R-46)."""
    rubric = {
        "rubric": {"criteria": ["clear"], "min_score": 3},
        "numeric": {"reference_sql": NUMERIC_SQL, "unit": "count", "tolerance": {"abs": 0}},
    }
    run = _run(runner_store, tmp_path, [chat_q("G01", expected=rubric)],
               judge=_DownJudge)  # fmt: skip
    (result,) = lines(run.out_dir / "results.jsonl")
    assert result["status"] == "passed"
    assert all(row["passed"] for row in run.summary["thresholds"])
    assert (run.passed, run.summary["exit_code"]) == (False, 14)


def test_cv_t11_30_trace_counts(runner_store: OpsStoreHandle, tmp_path: Path) -> None:
    """CV-T11-30 retries, fallbacks and repairs come from the pipeline run's trace file."""
    run_id = new_id(IdKind.RUN)
    traces = runner_store.data_root / "traces"
    traces.mkdir()
    events = [{"type": t} for t in ("retry", "retry", "fallback", "repair", "tool_call")]
    (traces / f"{run_id}.jsonl").write_text(
        "\n".join(json.dumps(x) for x in events) + "\n", encoding="utf-8"
    )
    run = _run(runner_store, tmp_path, [chat_q("G01")], chat=AnswerChat(run_id=run_id))
    (result,) = lines(run.out_dir / "results.jsonl")
    assert (result["retries"], result["fallbacks"], result["repairs"]) == (2, 1, 1)
    assert result["trace_path"] == f"traces/{run_id}.jsonl"
    assert run.summary["metrics"]["retries_per_run"] == 2.0
    assert run.summary["metrics"]["notes"] == []


def test_cv_t11_30_unexpected_error_is_a_result(
    runner_store: OpsStoreHandle, tmp_path: Path
) -> None:
    """CV-T11-30 an exception outside the pipeline call (here: building the chat service)
    becomes an `error` result; a run with nothing graded has no correctness."""

    def no_chat(profile: str) -> Any:
        msg = "boom"
        raise RuntimeError(msg)

    con = make_root(runner_store.data_root)
    deps = dataclasses.replace(make_deps(runner_store.data_root, con), chat_for=no_chat)
    suite = env.suite_file(tmp_path / "suite.yaml", [chat_q("G01")])
    run = run_golden(load_suite(suite), "local", "fast", deps=deps, options=EvalOptions())
    (result,) = lines(run.out_dir / "results.jsonl")
    assert (result["status"], result["error"]) == ("error", "RuntimeError: boom")
    skipped = _run(runner_store, tmp_path, [chat_q("G02", datasets=["real"])], con=con)
    assert skipped.summary["metrics"]["answer_correctness"] is None


def test_cv_t11_30_failure_marks_run_failed(
    runner_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CV-T11-30 a failure after the run row exists closes it as `failed` and propagates."""

    def broken(*args: object, **kwargs: object) -> object:
        msg = "results not writable"
        raise OSError(msg)

    monkeypatch.setattr(runner, "finish_run", broken)
    with pytest.raises(OSError, match="not writable"):
        _run(runner_store, tmp_path, [chat_q("G01")])
    (run_dir,) = (runner_store.data_root / "reports" / "eval").iterdir()
    row = get_run(run_dir.name)
    assert row is not None
    assert row.status == "failed"
