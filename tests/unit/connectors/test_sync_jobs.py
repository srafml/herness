"""Tests for herness.connectors.jobs: the `sync` and `reconcile` handlers and their
registration (impl 01 U01-51 to U01-53; T01-11). The runner, the connector factory and the
config are scripted fakes (`_jobs_data`); the handlers' own control flow is under test."""

from __future__ import annotations

import datetime
import threading
from typing import Any

import pytest
from structlog.testing import capture_logs
from tests.unit.connectors._jobs_data import (
    DATA,
    NOW,
    circuit_open,
    ctx,
    files,
    install,
    result,
    sources,
    two_sources,
)
from tests.unit.connectors._settings_data import adapter, jira, monitoring, servicenow

from herness.connectors import jobs
from herness.core.errors import ConfigError, SchemaViolation, SourceUnavailable

pytestmark = pytest.mark.unit


def _events(logs: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [line for line in logs if line["event"] == name]


# --- UT01-54: open breaker ends the job done ---------------------------------------------


def test_ut01_54_open_circuit_skips_source_and_job_is_done(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT01-54 two sources, the first raises CircuitOpen: `done`, skipped = [first], second ran."""
    calls = install(
        monkeypatch, two_sources(), {("servicenow", "incident"): circuit_open("servicenow")}
    )
    with capture_logs() as logs:
        outcome = jobs.handle_sync(ctx())
    assert outcome.status == "done"
    assert outcome.result["skipped_open_circuit"] == ["servicenow"]
    assert outcome.result["partial"] is True
    assert outcome.result["failed"] == []
    assert calls.runs == [
        ("servicenow", "incremental", "incident"),
        ("jira", "incremental", "issue"),
    ]
    results = outcome.result["results"]
    assert isinstance(results, list)
    assert [r["source"] for r in results] == ["jira"]  # type: ignore[index]
    (line,) = _events(logs, "connectors.sync.skipped_open_circuit")
    assert line["source"] == "servicenow"
    assert line["stream"] == "servicenow"
    assert line["retry_at"] == "2026-03-01T12:05:00.000000Z"
    assert line["log_level"] == "warning"


def test_ut01_54_open_circuit_skips_rest_of_that_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT01-54 an open breaker on entity 1 skips the source's later entities."""
    cfg = sources(
        files=files(entities={e: {"pattern": "*.csv", "key_field": ["id"]} for e in "ab"})
    )
    calls = install(
        monkeypatch,
        cfg,
        {("files", "a"): circuit_open("files")},
        entities={"files": ("a", "b")},
    )
    outcome = jobs.handle_sync(ctx({"source": "files"}))
    assert outcome.status == "done"
    assert calls.runs == [("files", "incremental", "a")]


def test_ut01_54_tool_streams_skipped_by_the_runner_are_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT01-54 keys the runner skipped (`skipped_open`) join the job's skipped list, sorted."""

    def partly_open(runner: Any) -> Any:
        runner.skipped_open = ("monitoring:splunk", "monitoring:datadog")
        return result("monitoring", "event")

    cfg = sources(monitoring=monitoring(prometheus=adapter("prometheus")))
    install(
        monkeypatch,
        cfg,
        {("monitoring", "event"): partly_open},
        entities={"monitoring": ("event",)},
    )
    outcome = jobs.handle_sync(ctx())
    assert outcome.status == "done"
    assert outcome.result["skipped_open_circuit"] == ["monitoring:datadog", "monitoring:splunk"]
    assert outcome.result["partial"] is True


def test_ut01_54_clean_run_is_not_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT01-54 every entity ran: `partial` false, results hold each `SyncResult.to_dict()`."""
    install(monkeypatch, two_sources())
    outcome = jobs.handle_sync(ctx())
    assert outcome.status == "done"
    assert outcome.result["partial"] is False
    assert outcome.result["skipped_open_circuit"] == []
    assert outcome.result["results"] == [
        result("servicenow", "incident").to_dict(data_root=DATA),
        result("jira", "issue").to_dict(data_root=DATA),
    ]


# --- UT01-55: entity failures ------------------------------------------------------------


def _three(monkeypatch: pytest.MonkeyPatch, script: dict[tuple[str, str], Any]) -> Any:
    ents = {e: {"pattern": "*.csv", "key_field": ["id"]} for e in ("a", "b", "c")}
    return install(
        monkeypatch,
        sources(files=files(entities=ents)),
        script,
        entities={"files": ("a", "b", "c")},
    )


def test_ut01_55_fatal_error_raised_first_after_all_entities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT01-55 A SourceUnavailable, B SchemaViolation, C ok: C ran; SchemaViolation raised."""
    unavailable = SourceUnavailable("source down")
    violation = SchemaViolation("bad shape")
    calls = _three(monkeypatch, {("files", "a"): unavailable, ("files", "b"): violation})
    with capture_logs() as logs, pytest.raises(SchemaViolation) as info:
        jobs.handle_sync(ctx({"source": "files"}))
    assert info.value is violation
    assert [run[2] for run in calls.runs] == ["a", "b", "c"]
    failed = _events(logs, "connectors.sync.failed")
    assert [(f["entity"], f["error_class"]) for f in failed] == [
        ("a", "SourceUnavailable"),
        ("b", "SchemaViolation"),
    ]
    assert all(f["source"] == f["stream"] == "files" for f in failed)
    assert all(f["mode"] == "incremental" and f["log_level"] == "error" for f in failed)
    assert all("source down" not in str(f) and "bad shape" not in str(f) for f in failed)


def test_ut01_55_first_retryable_raised_without_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT01-55 only retryable failures: the first recorded one is raised."""
    first, second = SourceUnavailable("one"), SourceUnavailable("two")
    _three(monkeypatch, {("files", "b"): first, ("files", "c"): second})
    with pytest.raises(SourceUnavailable) as info:
        jobs.handle_sync(ctx({"source": "files"}))
    assert info.value is first


def test_ut01_55_reconcile_fatal_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT01-55 reconcile: A retryable, B SchemaViolation (valve), C ok → C ran; valve raised."""
    ents = {e: {"pattern": "*.csv", "key_field": ["id"], "mode": "snapshot"} for e in "abc"}
    violation = SchemaViolation("reconcile safety valve")
    calls = install(
        monkeypatch,
        sources(files=files(entities=ents)),
        {("files", "a"): SourceUnavailable("x"), ("files", "b"): violation},
        entities={"files": ("a", "b", "c")},
    )
    with capture_logs() as logs, pytest.raises(SchemaViolation):
        jobs.handle_reconcile(ctx({"source": "files"}, kind="reconcile"))
    assert calls.runs == [("files", "reconcile", e) for e in "abc"]
    assert {f["mode"] for f in _events(logs, "connectors.sync.failed")} == {"reconcile"}


# --- UT01-56: cooperative yield ------------------------------------------------------------


def test_ut01_56_yield_before_second_entity(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT01-56 `should_yield` true before entity 2 → JobOutcome(status="yield"), partial."""
    calls = install(monkeypatch, two_sources())
    outcome = jobs.handle_sync(ctx(yield_after=1))
    assert outcome.status == "yield"
    assert calls.runs == [("servicenow", "incremental", "incident")]
    assert outcome.result["results"] == [result("servicenow", "incident").to_dict(data_root=DATA)]


def test_ut01_56_runner_stopped_yields(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT01-56 a backfill that left slices pending (`runner.stopped`) yields the job."""

    def stopped(runner: Any) -> Any:
        runner.stopped = True
        return result("servicenow", "incident", "backfill")

    calls = install(monkeypatch, two_sources(), {("servicenow", "incident"): stopped})
    outcome = jobs.handle_sync(ctx())
    assert outcome.status == "yield"
    assert len(calls.runs) == 1


def test_ut01_56_reconcile_yields(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT01-56 reconcile honours `should_yield` before the first entity too."""
    calls = install(monkeypatch, sources(servicenow=servicenow()))
    outcome = jobs.handle_reconcile(ctx({"source": "servicenow"}, kind="reconcile", yield_after=0))
    assert outcome.status == "yield"
    assert outcome.result["results"] == []
    assert calls.runs == []


# --- U01-51 algorithm details ----------------------------------------------------------------


def test_ut01_54_runner_wiring(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT01-54 the runner gets the section, a per-source connector factory, progress and stop."""
    calls = install(monkeypatch, two_sources())
    context = ctx()
    jobs.handle_sync(context)
    first, second = calls.runners
    assert first.cfg.SOURCE == "servicenow"
    assert first.connector_factory().name == "servicenow"
    assert second.connector_factory().name == "jira"  # no late-binding of the loop variable
    assert first.should_stop == context.should_yield
    first.progress("servicenow/incident rows=1")
    assert context.heartbeats == ["servicenow/incident rows=1"]


def test_ut01_54_progress_is_thread_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT01-54 the progress callable serialises heartbeats from backfill slice threads."""
    calls = install(monkeypatch, sources(servicenow=servicenow()))
    context = ctx()
    inside = [0]
    overlaps = [0]

    def beat(note: str | None = None) -> None:
        inside[0] += 1
        if inside[0] > 1:
            overlaps[0] += 1
        threading.Event().wait(0.0005)
        context.heartbeats.append(note)
        inside[0] -= 1

    monkeypatch.setattr(context, "heartbeat", beat)
    jobs.handle_sync(context)
    progress = calls.runners[0].progress
    threads = [
        threading.Thread(target=lambda n=n: [progress(f"t{n}") for _ in range(20)])
        for n in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(context.heartbeats) == 160
    assert overlaps[0] == 0


def test_ut01_54_named_entities_and_backfill_range(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT01-54 `--backfill --from --to`: dates at 00:00 UTC; entities as requested."""
    calls = install(monkeypatch, two_sources())
    payload = {
        "source": "servicenow",
        "entities": ["incident"],
        "mode": "backfill",
        "start": "2026-01-01",
        "end": "2026-02-01",
    }
    outcome = jobs.handle_sync(ctx(payload))
    assert outcome.status == "done"
    day = datetime.datetime
    assert calls.runs == [
        (
            "servicenow",
            "backfill",
            "incident",
            day(2026, 1, 1, tzinfo=datetime.UTC),
            day(2026, 2, 1, tzinfo=datetime.UTC),
        )
    ]


def test_ut01_54_full_backfill_uses_backfill_start_and_now(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT01-54 `--full` (backfill without dates): `backfill.start` at 00:00 UTC to now (D-6)."""
    cfg = sources(servicenow=servicenow(backfill={"start": datetime.date(2025, 6, 1)}))
    calls = install(monkeypatch, cfg)
    jobs.handle_sync(ctx({"source": "servicenow", "mode": "backfill"}))
    start = datetime.datetime(2025, 6, 1, tzinfo=datetime.UTC)
    assert calls.runs == [("servicenow", "backfill", "incident", start, NOW)]


@pytest.mark.parametrize(
    "payload",
    [
        {"source": "servicenow", "extra": 1},
        {"entities": ["incident"]},
        {"source": "servicenow", "start": "2026-01-01"},
        {"source": "servicenow", "mode": "backfill", "start": "2026-02-01", "end": "2026-01-01"},
        {"source": "servicenow", "mode": "full"},
        {"source": "Service Now"},
        {"source": "servicenow", "entities": []},
        {"source": "servicenow", "mode": "backfill", "start": "yesterday"},
        {"source": "servicenow", "mode": "backfill", "start": 20260101},
    ],
)
def test_ut01_54_invalid_payload_is_config_error(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]
) -> None:
    """UT01-54 an invalid payload → ConfigError("invalid sync payload") before any build."""
    calls = install(monkeypatch, two_sources())
    with pytest.raises(ConfigError) as info:
        jobs.handle_sync(ctx(payload))
    assert info.value.message == "invalid sync payload"
    assert info.value.__cause__ is None
    assert calls.runners == []


def test_ut01_54_disabled_or_unknown_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT01-54 a named disabled source or an unknown entity → ConfigError."""
    install(monkeypatch, two_sources())
    with pytest.raises(ConfigError, match="source files is disabled"):
        jobs.handle_sync(ctx({"source": "files"}))
    with pytest.raises(ConfigError, match="unknown entity problem for source servicenow"):
        jobs.handle_sync(ctx({"source": "servicenow", "entities": ["problem"]}))


# --- U01-52 reconcile ------------------------------------------------------------------------


def test_ut01_55_reconcile_skips_delta_files_entities(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT01-55 files entities in `delta` mode are skipped with `connectors.reconcile.skipped`."""
    ents = {
        "d": {"pattern": "*.csv", "key_field": ["id"]},
        "s": {"pattern": "*.csv", "key_field": ["id"], "mode": "snapshot"},
    }
    calls = install(
        monkeypatch, sources(files=files(entities=ents)), entities={"files": ("d", "s")}
    )
    with capture_logs() as logs:
        outcome = jobs.handle_reconcile(ctx({"source": "files"}, kind="reconcile"))
    assert outcome.status == "done"
    assert calls.runs == [("files", "reconcile", "s")]
    (line,) = _events(logs, "connectors.reconcile.skipped")
    assert (line["source"], line["entity"], line["reason"]) == ("files", "d", "delta_mode")
    assert line["log_level"] == "info"


@pytest.mark.parametrize(
    "payload", [{}, {"source": "servicenow", "mode": "backfill"}, {"source": 3}]
)
def test_ut01_55_invalid_reconcile_payload(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]
) -> None:
    """UT01-55 reconcile needs `source`; other keys are refused."""
    install(monkeypatch, two_sources())
    with pytest.raises(ConfigError) as info:
        jobs.handle_reconcile(ctx(payload, kind="reconcile"))
    assert info.value.message == "invalid reconcile payload"


def test_ut01_55_monitoring_is_not_reconciled(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT01-55 a reconcile job for `monitoring` → ConfigError before any runner is built."""
    calls = install(monkeypatch, sources(monitoring=monitoring(), jira=jira()))
    with pytest.raises(ConfigError, match="monitoring is not reconciled"):
        jobs.handle_reconcile(ctx({"source": "monitoring"}, kind="reconcile"))
    assert calls.runners == []
