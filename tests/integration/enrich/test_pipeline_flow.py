"""IT03-01, IT03-04, IT03-05, IT03-08: `run_enrichment` end to end through impl 02's
`build_pipeline` handler on a small build (F03-01, F03-07, F03-16; T03-28).

The environment (`_pipeline_env`) stands in for spec 11's `small_build` with a stub OpenJev
(hash mode), the fake Laya agent, a scripted LLM client and the tiny encoder on the CPU.
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from structlog.testing import capture_logs
from tests.integration.enrich._pipeline_env import EXTRA, LAYA_VERSION, PipelineEnv, pipeline_env
from tests.support.stub_http import StubFault

from herness.core.errors import ConfigError
from herness.core.types import JobOutcome
from herness.enrich.pipeline import STAGE_ORDER, EnrichReport
from herness.store._warehouse_rw import write_current

pytestmark = pytest.mark.integration

__all__ = ["pipeline_env"]  # the fixture is used by name

_ENRICH_TABLES = ("text_redacted", "decision", "cluster", "cluster_member",
                  "incident_change_link", "decision_wide")  # fmt: skip
_DECISIONS = "SELECT * FROM enrich.decision ORDER BY record_id, question"
_PAYLOAD: dict[str, Any] = {"stages": ["build", "enrich"], "depth": "standard"}


def _done(env: PipelineEnv, payload: dict[str, Any] = _PAYLOAD) -> tuple[str, EnrichReport]:
    outcome, ctx = env.job(payload)
    assert isinstance(outcome, JobOutcome), outcome
    assert outcome.status == "done"
    assert ctx.current_class == "none"
    return str(outcome.result["build_id"]), EnrichReport.model_validate(outcome.result["enrich"])


def test_it03_01_small_build_every_table_and_consistent_counts(pipeline_env: PipelineEnv) -> None:
    """IT03-01 small build, stub decider, fake LLM, fake Laya: every `enrich.*` table present,
    report counts consistent with the tables, no model output used as a metric number."""
    env = pipeline_env
    with capture_logs() as logs:
        build_id, report = _done(env)
    names = {r[0] for r in env.query(build_id, "SELECT table_name FROM duckdb_tables() "
             "WHERE schema_name = 'enrich' UNION SELECT view_name FROM duckdb_views() "
             "WHERE schema_name = 'enrich'")}  # fmt: skip
    assert set(_ENRICH_TABLES) <= names
    texts = env.query(build_id, "SELECT count(*) FROM enrich.text_redacted")[0][0]
    decided = env.query(build_id, "SELECT count(*) FROM enrich.decision")[0][0]
    escalated = env.query(build_id, "SELECT count(*) FROM enrich.decision WHERE escalated")[0][0]
    links = env.query(build_id, "SELECT count(*) FROM enrich.incident_change_link")[0][0]
    stages = report.stages
    assert list(stages) == list(STAGE_ORDER)
    assert stages["text"].rows == texts >= 4 + EXTRA
    hashes = env.query(build_id, "SELECT count(DISTINCT content_hash) FROM enrich.text_redacted")
    assert stages["embed"].embedded == hashes[0][0] > 0
    assert (stages["resolve"].decided, stages["resolve"].escalated) == (decided, escalated)
    assert decided > 0
    assert stages["link"].rows == links
    assert report.question_primary == {
        "root_cause": "laya", "change_caused": "openjev", "business_impact": "openjev",
    }  # fmt: skip
    assert report.decider_versions["laya"] == LAYA_VERSION
    assert set(report.decider_versions) == {"laya", "openjev", "llm"}
    assert report.escalation_share == pytest.approx(escalated / decided)
    assert set(report.coverage) <= {"incident", "change", "problem"}
    assert all(0.0 <= v <= 1.0 for v in report.coverage.values())
    assert report.stages["ensemble"].note == "not_deep"
    assert stages["cluster"].note == "too_few_vectors"  # 24 incidents < pca_dims 64
    assert (report.cluster_run, "too_few_vectors" in report.warnings) == ("skipped", True)
    assert not [n for n, s in stages.items() if s.status == "failed"]
    calls = env.calls()
    assert calls["laya"] > 0
    assert calls["openjev"] > 0
    assert calls["llm"] > 0  # the teacher's below-gate primary answers, the same night
    assert stages["reasoning"].decided > 0
    assert env.roles == ["enrich_decider"]  # no naming candidates: no namer client
    events = [e for e in logs if str(e.get("event", "")).startswith("enrich.stage.")]
    started = [e["stage"] for e in events if e["event"] == "enrich.stage.started"]
    assert started == [
        "text",
        "embed",
        "decide-primary",
        "decide-escalate",
        "cluster",
        "reasoning",
        "ensemble",
        "link",
        "suggest",
        "resolve",
    ]  # F03-01 order
    probs = env.query(build_id, "SELECT probability FROM enrich.decision")
    assert all(0.0 <= p[0] <= 1.0 for p in probs)
    dump = json.dumps(report.model_dump(mode="json"))
    sample = env.query(build_id, "SELECT text FROM enrich.text_redacted LIMIT 1")[0][0]
    assert sample not in dump
    assert "Users report" not in json.dumps(events, default=str)


def test_it03_01_openjev_down_defers_to_the_llm_in_the_reasoning_scope(
    pipeline_env: PipelineEnv,
) -> None:
    """IT03-01 OpenJev answering 529 only: chunk deferred, `openjev_unavailable`, the LLM
    answers the queue inside the nested `reasoning` scope; the build still completes."""
    env = pipeline_env
    env.stub.faults = (StubFault(0, "http_529", 10**6),)
    outcome, ctx = env.job(_PAYLOAD)
    assert isinstance(outcome, JobOutcome), outcome
    report = EnrichReport.model_validate(outcome.result["enrich"])
    build_id = str(outcome.result["build_id"])
    stages = report.stages
    assert (stages["decide-escalate"].status, stages["decide-escalate"].note) == (
        "degraded", "openjev_unavailable",
    )  # fmt: skip
    assert "openjev_unavailable" in report.warnings
    assert stages["reasoning"].status == "done"
    assert stages["reasoning"].decided > 0
    assert ctx.gpu_scopes == ["decider", "reasoning"]
    assert ctx.current_class == "none"
    assert env.calls()["openjev"] > 0
    assert env.calls()["llm"] > 0
    deciders = {r[0] for r in env.query(build_id, "SELECT DISTINCT decider FROM enrich.decision")}
    assert "llm" in deciders
    assert "openjev" not in deciders


def test_it03_04_second_run_unchanged_lake_makes_no_calls(pipeline_env: PipelineEnv) -> None:
    """IT03-04 twice on an unchanged lake: 0 decider calls, 0 embeddings, same decisions."""
    env = pipeline_env
    first, _ = _done(env)
    write_current(first, layout=env.layout)
    before = env.calls()
    encoded = len(env.encoder.inputs)
    second, report = _done(env)
    after = env.calls()
    assert {k: after[k] - before[k] for k in ("laya", "openjev", "llm")} == {
        "laya": 0, "openjev": 0, "llm": 0,
    }  # fmt: skip
    tickets = {r[0] for r in env.query(second, "SELECT text FROM enrich.text_redacted")}
    assert report.stages["embed"].embedded == 0
    assert not tickets & set(env.encoder.inputs[encoded:])  # only in-memory mapping texts
    assert report.stages["decide-primary"].decided == 0
    # decided_at is the cache row time, so every column matches (design 03 §10)
    assert env.query(second, _DECISIONS) == env.query(first, _DECISIONS)


def test_it03_05_new_question_set_version_reclassifies_only_the_changed_question(
    pipeline_env: PipelineEnv,
) -> None:
    """IT03-05 one question's instructions changed with a new qsv: only it is re-asked."""
    env = pipeline_env
    first, _ = _done(env)
    write_current(first, layout=env.layout)
    env.configure(qsv="qs-2026-10-02.1", edit="The text says the issue began after a change.")
    before = len(env.llm.requests)
    stub_before = env.stub.calls
    second, report = _done(env)
    schemas = [cast("dict[str, Any]", r.response_schema or {}) for r in env.llm.requests[before:]]
    asked = [set(schema.get("properties", {})) for schema in schemas]
    assert env.stub.calls > stub_before  # the changed question went back to the teacher
    assert all(qids <= {"change_caused"} for qids in asked)
    rows = env.query(second, "SELECT DISTINCT question_set_version FROM enrich.decision")
    assert rows == [("qs-2026-10-02.1",)]
    old = {r[:3] for r in env.query(first, "SELECT record_id, question, answer FROM "
           "enrich.decision WHERE question <> 'change_caused'")}  # fmt: skip
    new = {r[:3] for r in env.query(second, "SELECT record_id, question, answer FROM "
           "enrich.decision WHERE question <> 'change_caused'")}  # fmt: skip
    assert new == old  # migrated, not re-asked
    assert report.stages["decide-primary"].decided == 0  # Laya rows migrated too


def test_it03_08_question_edited_without_new_version_fails_before_gpu(
    pipeline_env: PipelineEnv,
) -> None:
    """IT03-08 qsv unchanged but a question edited: ConfigError before any GPU work."""
    env = pipeline_env
    _done(env)
    env.configure(edit="Edited without bumping the question set version.")
    before = env.calls()
    outcome, ctx = env.job(_PAYLOAD)
    assert isinstance(outcome, ConfigError)
    assert "changed without a new question_set_version" in str(outcome)
    assert (ctx.gpu_scopes, ctx.gpu_requests, ctx.services.calls) == ([], [], [])
    assert env.calls() == before
