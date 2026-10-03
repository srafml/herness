"""UT03-132, UT03-139: pipeline types, stage order, stage selection and GPU scopes (T03-28).

`run_enrichment` runs with stubbed stage bodies (the functions of `_pipeline_stages` and the
pipeline's reasoning and ensemble steps are replaced) and a `FakeJobContext` that records
GPU scopes, the current class and saved states; no warehouse is touched.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any, cast

import duckdb
import pytest
from pydantic import JsonValue, ValidationError
from structlog.testing import capture_logs
from tests.support.build_harness import FakeJobContext

from herness.core.errors import ConfigError, ModelUnavailable, SchemaViolation
from herness.core.types import GpuClass
from herness.enrich import _pipeline_stages as steps
from herness.enrich import pipeline
from herness.enrich.gpu import YieldRequested
from herness.enrich.pipeline import (
    STAGE_ORDER,
    EnrichReport,
    Run,
    StageName,
    StageReport,
    run_enrichment,
)
from herness.enrich.resolve import QueueItem

pytestmark = pytest.mark.unit

BUILD = "20261002-010000-01ABCD"
_BODIES: dict[StageName, str] = {
    "text": "text", "embed": "embed", "decide-primary": "decide_primary",
    "decide-escalate": "decide_escalate", "cluster": "cluster", "link": "link",
    "suggest": "suggest", "resolve": "resolve",
}  # fmt: skip
_ITEM = QueueItem("servicenow:incident:1", "incident", "0" * 32, "t", ("q_bool",))


class Recorder:
    """Stage calls with the GPU class current during each, and the prepared runs."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, GpuClass]] = []
        self.prepared: list[Run] = []
        self.raise_at: dict[str, BaseException] = {}
        self.degrade_at: set[str] = set()
        self.available: list[bool] = []

    def body(self, name: str) -> Callable[[Run, StageReport], None]:
        def run_stage(run: Run, report: StageReport, **kw: Any) -> None:
            ctx = cast("FakeJobContext", run.ctx)
            self.calls.append((name, ctx.current_class))
            if "available" in kw:
                self.available.append(kw["available"])
            if name in self.raise_at:
                raise self.raise_at[name]
            if name in self.degrade_at:  # set directly, as stage modules do (`_mark`)
                report.status, report.note = "degraded", f"{name.replace('-', '_')}_down"
            report.rows += 1

        return run_stage

    def prepare(self, run: Run) -> None:
        self.prepared.append(run)
        run.deciders["llm"] = cast("Any", object())
        run.deferred = [_ITEM]
        run.primaries = {"q_bool": "laya"}
        run.versions = {"laya": "laya-20260901-1"}


@pytest.fixture
def rec(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    recorder = Recorder()
    monkeypatch.setattr(steps, "prepare", recorder.prepare)
    for name, attr in _BODIES.items():
        monkeypatch.setattr(steps, attr, recorder.body(name))
    monkeypatch.setattr(pipeline, "_reasoning", recorder.body("reasoning"))
    monkeypatch.setattr(pipeline, "_ensemble", recorder.body("ensemble"))
    return recorder


def _run(ctx: FakeJobContext, **kw: Any) -> EnrichReport:
    wh = cast("duckdb.DuckDBPyConnection", object())
    args: dict[str, Any] = {"depth": "standard", "ctx": ctx, "prev_warehouse": None} | kw
    return run_enrichment(wh, BUILD, **args)


def _report(**stage: Any) -> EnrichReport:
    now = datetime(2026, 10, 2, 1, tzinfo=UTC)
    big = 10**9
    full = StageReport(rows=big, cache_hits=big, embedded=big, decided=big, escalated=big,
                       failed=big, duration_s=1e6, note="x" * 200, **stage)  # fmt: skip
    return EnrichReport(
        build_id=BUILD, depth="deep", started_at=now, finished_at=now,
        stages={name: full.model_copy() for name in reversed(STAGE_ORDER)},
        decider_versions=dict.fromkeys(("laya", "openjev", "jev", "llm", "ensemble"), "v" * 64),
        question_primary={f"q_{i:02d}": "openjev" for i in range(64)},
        coverage={"incident": 0.5, "change": 1.0, "problem": 0.0}, escalation_share=0.25,
        cluster_run="full", warnings=["laya_degraded", "openjev_unavailable"],
    )  # fmt: skip


# --- UT03-132 -----------------------------------------------------------------------------


def test_ut03_132_report_dump_fits_and_follows_stage_order() -> None:
    """UT03-132 a report with every stage: JSON <= 64 KB, stage keys in STAGE_ORDER."""
    report = _report()
    dumped = report.model_dump(mode="json")
    text = json.dumps(dumped)
    assert len(text.encode()) <= 64 * 1024
    assert list(dumped["stages"]) == list(STAGE_ORDER)  # pydantic keeps the Literal order
    assert STAGE_ORDER == (
        "text", "embed", "decide-primary", "decide-escalate", "ensemble", "cluster",
        "reasoning", "link", "suggest", "resolve",
    )  # fmt: skip
    assert EnrichReport.model_validate_json(text) == report


def test_ut03_132_stage_report_note_is_a_code() -> None:
    """UT03-132 `note` is a code of at most 200 chars: free text and overlong values fail."""
    with pytest.raises(ValidationError):
        StageReport(note="Payment API down for ACME")
    with pytest.raises(ValidationError):
        StageReport(note="x" * 201)
    report = StageReport()
    with pytest.raises(ValidationError):
        report.status = cast("Any", "unknown")
    report.status, report.note = "degraded", "laya_degraded"
    assert report.model_dump()["note"] == "laya_degraded"
    frozen = _report()
    with pytest.raises(ValidationError):
        frozen.build_id = "other"  # type: ignore[misc]


# --- UT03-139 -----------------------------------------------------------------------------


def test_ut03_139_subset_runs_in_stage_order_without_gpu_scope(rec: Recorder) -> None:
    """UT03-139 stages=["resolve","text"]: text then resolve, no GPU scope entered."""
    ctx = FakeJobContext()
    report = _run(ctx, stages=["resolve", "text"])
    assert [name for name, _ in rec.calls] == ["text", "resolve"]
    assert ctx.gpu_scopes == []
    assert {n: r.status for n, r in report.stages.items() if r.status != "skipped"} == {
        "text": "done", "resolve": "done",
    }  # fmt: skip
    assert report.stages["embed"].note == "not_selected"


def test_ut03_139_unknown_stage_fails_before_any_work(rec: Recorder) -> None:
    """UT03-139 stages=["text","bogus"]: ConfigError before config, any stage or state."""
    ctx = FakeJobContext()
    with pytest.raises(ConfigError, match="unknown enrichment stage bogus"):
        _run(ctx, stages=["text", "bogus"])
    assert (rec.prepared, rec.calls, ctx.saved_states, ctx.gpu_scopes) == ([], [], [], [])


def test_ut03_139_all_stages_decider_scope_with_nested_reasoning(rec: Recorder) -> None:
    """UT03-139 stages=None: `decider` once around the GPU stages, `reasoning` nested, exited."""
    ctx = FakeJobContext()
    report = _run(ctx)
    assert rec.calls == [
        ("text", "none"), ("embed", "decider"), ("decide-primary", "decider"),
        ("decide-escalate", "decider"), ("cluster", "decider"), ("reasoning", "reasoning"),
        ("ensemble", "decider"), ("link", "none"), ("suggest", "none"), ("resolve", "none"),
    ]  # fmt: skip
    assert ctx.gpu_scopes == ["decider", "reasoning"]
    assert ctx.current_class == "none"
    assert rec.available == [True]
    assert all(r.status == "done" and r.rows == 1 for r in report.stages.values())
    saved = ctx.saved_states[-1]["enrich"]
    assert saved["build_id"] == BUILD  # type: ignore[index, call-overload]
    assert saved["stages_done"] == list(STAGE_ORDER)  # type: ignore[index, call-overload]


def test_ut03_139_only_cpu_stages_after_gpu_ones_enter_no_scope(rec: Recorder) -> None:
    """UT03-139 a CPU-only selection after the GPU block (link, suggest) enters no scope."""
    ctx = FakeJobContext()
    _run(ctx, stages=["link", "suggest"])
    assert ctx.gpu_scopes == []
    assert [n for n, _ in rec.calls] == ["link", "suggest"]


def test_ut03_139_stage_failure_is_logged_reraised_and_scopes_left(rec: Recorder) -> None:
    """UT03-139 a stage error: `enrich.stage.failed` (class only), re-raised, class restored."""
    ctx = FakeJobContext()
    rec.raise_at["cluster"] = SchemaViolation("cluster stage write: CatalogException")
    with capture_logs() as logs, pytest.raises(SchemaViolation):
        _run(ctx)
    assert ctx.current_class == "none"
    failed = [e for e in logs if e["event"] == "enrich.stage.failed"]
    assert [(e["stage"], e["error_class"]) for e in failed] == [("cluster", "SchemaViolation")]
    done = ctx.saved_states[-1]["enrich"]["stages_done"]  # type: ignore[index, call-overload]
    assert done == ["text", "embed", "decide-primary", "decide-escalate"]


def test_ut03_139_yield_propagates_unchanged_after_scopes_exit(rec: Recorder) -> None:
    """UT03-139 `YieldRequested` from a stage propagates; `enrich.stage.yielded`; no class."""
    ctx = FakeJobContext()
    signal = YieldRequested("reasoning")
    rec.raise_at["reasoning"] = signal
    with capture_logs() as logs, pytest.raises(YieldRequested) as caught:
        _run(ctx)
    assert caught.value is signal
    assert ctx.current_class == "none"
    assert ctx.gpu_scopes == ["decider", "reasoning"]
    assert [e["stage"] for e in logs if e["event"] == "enrich.stage.yielded"] == ["reasoning"]
    assert not [e for e in logs if e["event"] == "enrich.stage.failed"]


def test_ut03_139_degraded_stage_note_becomes_report_warning(rec: Recorder) -> None:
    """UT03-139 a degraded stage keeps running the pipeline; its code joins `warnings`."""
    rec.degrade_at.add("suggest")
    with capture_logs() as logs:
        report = _run(FakeJobContext())
    assert report.stages["suggest"].status == "degraded"
    assert report.warnings == ["suggest_down"]
    assert report.stages["resolve"].status == "done"
    degraded = [e for e in logs if e["event"] == "enrich.stage.degraded"]
    fields = [(e["stage"], e["note"], e["build_id"], e["job_id"], e["log_level"]) for e in degraded]
    assert fields == [("suggest", "suggest_down", BUILD, "job-0001", "warning")]
    completed = [e for e in logs if e["event"] == "enrich.stage.completed"]
    assert len(completed) == len(STAGE_ORDER)
    assert all("note" not in e and isinstance(e["rows"], int) for e in completed)


@pytest.mark.parametrize(
    ("stages", "depth", "scopes"),
    [
        (["ensemble"], "standard", []),
        (["ensemble"], "deep", ["decider"]),
        (["reasoning"], "standard", []),
        (["reasoning", "link"], "deep", []),
        (["cluster", "reasoning"], "standard", ["decider", "reasoning"]),
    ],
)
def test_ut03_139_decider_scope_only_when_a_gpu_stage_can_work(
    rec: Recorder, stages: list[str], depth: str, scopes: list[str]
) -> None:
    """UT03-139 no class switch for a GPU stage without work: `ensemble` below `deep`, or
    `reasoning` without `decide-escalate` / `cluster` in the same call."""
    ctx = FakeJobContext()
    _run(ctx, stages=stages, depth=depth)
    assert ctx.gpu_scopes == scopes
    assert ctx.current_class == "none"


def test_ut03_139_metrics_per_stage(rec: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT03-139 stage duration histogram and records counter per completed stage (§8.2)."""
    seen: list[tuple[str, float, str, dict[str, str] | None]] = []

    def record(name: str, value: float, *, component: str, labels: Any = None) -> None:
        seen.append((name, value, component, labels))

    monkeypatch.setattr(pipeline, "record_counter", record)
    monkeypatch.setattr(pipeline, "record_histogram", record)
    _run(FakeJobContext(), stages=["text"])
    assert [(n, c, lab) for n, _, c, lab in seen] == [
        ("herness_enrich_stage_duration_seconds", "enrich", {"stage": "text"}),
        ("herness_enrich_records_total", "enrich", {"stage": "text"}),
    ]
    assert seen[1][1] == 1


def test_ut03_139_checkpoint_merges_into_the_entry_state(rec: Recorder) -> None:
    """UT03-139 the checkpoint keeps the keys the job state had on entry (impl 02's build)."""
    entry: dict[str, JsonValue] = {"build_id": BUILD, "stages_done": ["build"]}
    ctx = FakeJobContext(state=entry)
    _run(ctx, stages=["text"])
    saved = ctx.saved_states[-1]
    assert {k: saved[k] for k in entry} == entry
    assert saved["enrich"]["stages_done"] == ["text"]  # type: ignore[index, call-overload]


def test_ut03_139_resume_skips_done_stages_but_reruns_needed_producers(rec: Recorder) -> None:
    """UT03-139 a rerun of the build skips done stages unless a pending stage needs them."""
    first = "2026-10-02T00:30:00+00:00"
    done: list[JsonValue] = ["text", "embed", "decide-primary", "decide-escalate", "cluster"]
    state: dict[str, JsonValue] = {
        "enrich": {"build_id": BUILD, "stages_done": done, "started_at": first}
    }
    ctx = FakeJobContext(state=state)
    report = _run(ctx)
    ran = [n for n, _ in rec.calls]
    assert ran == ["embed", "decide-escalate", "cluster", "reasoning", "ensemble", "link",
                   "suggest", "resolve"]  # fmt: skip
    assert {n: report.stages[n].note for n in ("text", "decide-primary")} == {
        "text": "resumed", "decide-primary": "resumed",
    }  # fmt: skip
    assert rec.prepared[0].started_at == datetime.fromisoformat(first)
    assert ctx.saved_states[-1]["enrich"]["started_at"] == first  # type: ignore[index, call-overload]


def test_ut03_139_resume_of_a_finished_run_skips_everything(rec: Recorder) -> None:
    """UT03-139 every stage done: nothing runs again and no GPU scope is entered."""
    state: dict[str, JsonValue] = {"enrich": {"build_id": BUILD, "stages_done": [*STAGE_ORDER]}}
    ctx = FakeJobContext(state=state)
    report = _run(ctx)
    assert (rec.calls, ctx.gpu_scopes) == ([], [])
    assert {r.note for r in report.stages.values()} == {"resumed"}


def test_ut03_139_state_of_another_build_is_ignored(rec: Recorder) -> None:
    """UT03-139 a checkpoint of another build (or a malformed one) resumes nothing."""
    malformed: JsonValue = {"build_id": BUILD, "stages_done": "text", "started_at": "x"}
    saves: list[JsonValue] = [{"build_id": "other", "stages_done": ["text"]}, "garbage", malformed]
    for saved in saves:
        rec.calls.clear()
        _run(FakeJobContext(state={"enrich": saved}), stages=["text"])
        assert [n for n, _ in rec.calls] == ["text"]


class MergingContext(FakeJobContext):
    """Like impl 02's T02-19b job-context view: `save_state` merges into the build's state."""

    def save_state(self, state: dict[str, JsonValue]) -> None:
        super().save_state({**self.load_state(), **state})


def test_ut03_139_crash_then_resume_through_a_merging_context(rec: Recorder) -> None:
    """UT03-139 with a merging `save_state` (impl 02's job-context view): a crash in `cluster`
    keeps the build's keys; the rerun skips `text` and `decide-primary`, reruns the rest."""
    ctx = MergingContext(state={"build_id": BUILD, "stages_done": ["build"]})
    rec.raise_at["cluster"] = SchemaViolation("cluster stage write: IOException")
    with pytest.raises(SchemaViolation):
        _run(ctx)
    state = ctx.load_state()
    assert (state["build_id"], state["stages_done"]) == (BUILD, ["build"])
    rec.raise_at.clear()
    rec.calls.clear()
    again = MergingContext(state=state)
    report = _run(again)
    assert [n for n, _ in rec.calls] == ["embed", "decide-escalate", "cluster", "reasoning",
                                         "ensemble", "link", "suggest", "resolve"]  # fmt: skip
    assert report.stages["text"].note == "resumed"
    final = again.load_state()
    assert final["stages_done"] == ["build"]
    assert final["enrich"]["stages_done"] == list(STAGE_ORDER)  # type: ignore[index, call-overload]


class _NoReasoningCtx(FakeJobContext):
    @contextlib.contextmanager
    def gpu_scope(self, cls: GpuClass) -> Iterator[None]:
        if cls == "reasoning":
            msg = "reasoning class unavailable"
            raise ModelUnavailable(msg)
        with super().gpu_scope(cls):
            yield


def test_ut03_139_reasoning_switch_failure_runs_degraded(rec: Recorder) -> None:
    """UT03-139 `ModelUnavailable` entering `reasoning`: the step runs without the LLM."""
    ctx = _NoReasoningCtx()
    _run(ctx)
    assert rec.available == [False]
    assert ("reasoning", "decider") in rec.calls
    assert ctx.current_class == "none"


def test_ut03_139_reasoning_without_work_or_llm_enters_no_nested_scope(
    rec: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-139 no LLM work: reasoning `skipped`/`no_work`; no LLM: no nested scope either."""

    def idle(run: Run) -> None:
        Recorder.prepare(rec, run)
        run.deferred = []

    monkeypatch.setattr(steps, "prepare", idle)
    ctx = FakeJobContext()
    report = _run(ctx)
    assert (report.stages["reasoning"].status, report.stages["reasoning"].note) == (
        "skipped", "no_work",
    )  # fmt: skip
    assert ctx.gpu_scopes == ["decider"]

    def no_llm(run: Run) -> None:
        Recorder.prepare(rec, run)
        run.deciders.clear()

    monkeypatch.setattr(steps, "prepare", no_llm)
    ctx = FakeJobContext()
    _run(ctx)
    assert ctx.gpu_scopes == ["decider"]
    assert rec.available[-1] is False
