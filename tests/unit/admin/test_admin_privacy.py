"""Unit tests for ``run_privacy_delete`` and its ``maintenance`` dispatch (impl 10 T10-29).

UT10-69 (U10-99, U10-104): the impl 02, 03, 05, 06 and 08 functions are recording fakes
patched on ``herness.admin.privacy``; ``deletion_request`` rows live in the real migrated ops
store (``ops_store``), so step records and status transitions are the real U10-105 ones.
"""

from __future__ import annotations

import datetime
import functools
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast

import pytest
from structlog.testing import capture_logs
from tests.support.ops_store import OpsStoreHandle
from tests.support.worker_env import WorkerEnv, worker_env  # noqa: F401 - fixture import

from herness import admin
from herness.admin import _privacy_steps as steps
from herness.admin import privacy
from herness.admin.maintenance import handle_maintenance
from herness.core.config import get_config
from herness.core.errors import ConfigError, FatalError, ModelUnavailable, StoreBusy
from herness.core.jobs import resolve_handler
from herness.core.jobs.ports import JobContext, require_jobs_backend
from herness.core.resilience._state import process_state
from herness.core.types import JobOutcome
from herness.model.promote import CleanupReport
from herness.store.lake_purge import LakePurgeResult
from herness.store.ops import privacy as ops_privacy
from herness.store.ops.core import _drop_cached, read_all
from herness.store.ops.core import connection as core_connection

pytestmark = pytest.mark.unit

NOW = datetime.datetime(2026, 9, 24, 12, 0, tzinfo=datetime.UTC)
RID = "servicenow:incident:INC0042"
ACTOR = "a" * 32
BASE: dict[str, Any] = {
    "action": "privacy_delete",
    "record_id": RID,
    "reason_ref": "DSR-7",
    "requested_by": ACTOR,
}
NEWER_BUILD = "20260924-123000-ABCDEF"  # 30 minutes after NOW
OLDER_BUILD = "20260920-020000-ABCDEF"


def _ctx(payload: dict[str, Any]) -> JobContext:
    return cast("JobContext", SimpleNamespace(job=SimpleNamespace(payload=payload)))


@dataclass
class Fakes:
    """Recording stand-ins for every store primitive the deletion calls."""

    events: list[str] = field(default_factory=list)
    lake_rows: list[int] = field(default_factory=list)  # rows_removed per lake pass, in order
    fail: dict[str, list[BaseException]] = field(default_factory=dict)  # name -> raise once each
    enqueued: list[tuple[tuple[Any, ...], dict[str, Any]]] = field(default_factory=list)
    job_status: dict[str, str] = field(default_factory=dict)
    current: str | None = NEWER_BUILD
    deferred: tuple[str, ...] = ()
    audits: list[tuple[tuple[Any, ...], dict[str, Any]]] = field(default_factory=list)
    memory_calls: list[str] = field(default_factory=list)
    cleanup_now: list[datetime.datetime] = field(default_factory=list)

    def _maybe_fail(self, name: str) -> None:
        pending = self.fail.get(name)
        if pending:
            raise pending.pop(0)

    def lake(self, ids: Any) -> LakePurgeResult:
        assert list(ids) == [RID]
        self.events.append("lake")
        self._maybe_fail("lake")
        removed = self.lake_rows.pop(0) if self.lake_rows else 0
        return LakePurgeResult(3, 1 if removed else 0, 0, removed)

    def enrich(self, record_id: str) -> dict[str, int]:
        assert record_id == RID
        self.events.append("enrich")
        self._maybe_fail("enrich")
        return {"embeddings_deleted": 2, "labels_deleted": 1}

    def memory(self, record_id: str) -> int:
        self.memory_calls.append(record_id)
        self.events.append("memory")
        self._maybe_fail("memory")
        return 4

    def evidence(self, record_id: str) -> int:
        assert record_id == RID
        self.events.append("evidence")
        self._maybe_fail("evidence")
        return 1

    def findings(self, record_id: str, *, conn: Any) -> int:
        assert record_id == RID
        assert conn is not None
        self.events.append("findings")
        return 2

    def enqueue(self, *args: Any, **kwargs: Any) -> str:
        self.enqueued.append((args, kwargs))
        return f"job_{len(self.enqueued):026d}"

    def get_job(self, job_id: str) -> Any:
        return SimpleNamespace(status=self.job_status.get(job_id, "queued"))

    def read_current(self, *, layout: Any = None) -> str | None:
        del layout
        return self.current

    def cleanup(self, **kwargs: Any) -> CleanupReport:
        assert kwargs["mode"] == "post"
        assert kwargs["keep_last"] == 1
        assert kwargs["protect"] == frozenset()
        assert kwargs["layout"].warehouse.name == "warehouse"
        self.cleanup_now.append(kwargs["now"])
        self.events.append("cleanup")
        return CleanupReport(deleted=(OLDER_BUILD,), deferred=self.deferred, retired=())

    def audit(self, *args: Any, **kwargs: Any) -> None:
        self.audits.append((args, kwargs))
        self.events.append("audit")

    def list_builds(self, *, layout: Any = None) -> list[Any]:
        """Build files on disk after cleanup: CURRENT plus whatever ``left`` names."""
        del layout
        current = [SimpleNamespace(build_id=self.current, is_current=True)] if self.current else []
        return current + [SimpleNamespace(build_id=b, is_current=False) for b in self.left]

    left: list[str] = field(default_factory=list)  # build files cleanup_builds kept


def _patch(monkeypatch: pytest.MonkeyPatch, f: Fakes) -> None:
    """Step bodies live in ``_privacy_steps``; jobs, builds and audit in ``privacy``."""
    for name, value in {
        "purge_record_ids": f.lake,
        "purge_record": f.enrich,
        "scrub_record_from_evidence": f.evidence,
        "scrub_record_from_findings": f.findings,
        "list_builds": f.list_builds,
    }.items():
        monkeypatch.setattr(steps, name, value)
    for name, value in {
        "enqueue": f.enqueue,
        "get_job": f.get_job,
        "read_current": f.read_current,
        "cleanup_builds": f.cleanup,
        "audit": f.audit,
    }.items():
        monkeypatch.setattr(privacy, name, value)


@pytest.fixture
def fakes(ops_store: OpsStoreHandle, monkeypatch: pytest.MonkeyPatch) -> Fakes:
    del ops_store
    f = Fakes()
    _patch(monkeypatch, f)
    return f


def _run(f: Fakes, payload: dict[str, Any], *, now: datetime.datetime = NOW) -> JobOutcome:
    from herness.core.config import get_config  # noqa: PLC0415 - after ops_store set paths

    return privacy.run_privacy_delete(get_config(), _ctx(payload), now=now, memory_purge=f.memory)


def _request() -> ops_privacy.DeletionRequest:
    found = ops_privacy.open_deletion_request(RID)
    if found is not None:
        return found
    rows = read_all("SELECT request_id FROM deletion_request", ())
    return ops_privacy.get_deletion_request(str(rows[-1]["request_id"]))


def _status(steps: dict[str, dict[str, Any]]) -> dict[str, str]:
    return {k: v["status"] for k, v in steps.items()}


def _first_visit(f: Fakes) -> tuple[JobOutcome, dict[str, Any]]:
    """Run steps 1-5 (first visit); return the outcome and the continuation payload."""
    outcome = _run(f, dict(BASE))
    args, _ = f.enqueued[-1]
    assert args[0] == "maintenance"
    return outcome, cast("dict[str, Any]", args[1])


# --- UT10-69: failure, rerun, step 5, continuation -----------------------------------------


def test_ut10_69_step4_failure_then_rerun_skips_done_steps(fakes: Fakes) -> None:
    """UT10-69 step 4 raising once leaves the request running with step 4 failed (error class
    only); the rerun skips steps 1-3b, completes step 4 and enqueues the rebuild and the
    continuation (GPU class none, +30 min, wait key 1)."""
    fakes.fail["evidence"] = [StoreBusy("ops store busy")]
    with pytest.raises(StoreBusy):
        _run(fakes, dict(BASE))
    req = _request()
    assert req.status == "running"
    assert _status(req.steps) == {
        "1": "done",
        "2": "done",
        "3": "done",
        "3b": "done",
        "4": "failed",
    }
    assert req.steps["4"]["error"] == "StoreBusy"
    assert fakes.events == ["lake", "enrich", "memory", "evidence"]
    assert fakes.memory_calls == [RID]
    assert req.steps["3b"]["counts"] == {"memory_items": 4}
    assert req.steps["2"]["counts"]["rows_removed"] == 0

    outcome = _run(fakes, dict(BASE))
    assert fakes.events == ["lake", "enrich", "memory", "evidence", "evidence", "findings"]
    req = _request()
    assert req.status == "running"
    assert req.steps["4"]["status"] == "done"
    assert req.steps["4"]["counts"] == {"evidence_rows": 1, "finding_rows": 2, "trace_files": 0}
    assert "5" not in req.steps
    build_args, build_kwargs = fakes.enqueued[0]
    assert build_args[0] == "build_pipeline"
    assert build_args[2] == "none"
    assert build_args[1]["stages"] == ["build", "enrich", "score", "dq", "promote"]
    assert build_kwargs == {"priority": 60, "idem_key": "privacy_rebuild:" + req.request_id}
    cont_args, cont_kwargs = fakes.enqueued[1]
    assert cont_args[0] == "maintenance"
    assert cont_args[2] == "none"
    assert cont_args[1] == {
        **BASE,
        "request_id": req.request_id,
        "build_job_id": "job_" + "1".zfill(26),
        "wait": 1,
        "rebuilds": 0,
        "rebuild_at": "2026-09-24T12:00:00.000000Z",
    }
    assert cont_kwargs["scheduled_for"] == NOW + datetime.timedelta(minutes=30)
    assert cont_kwargs["idem_key"] == f"privacy_delete:{req.request_id}:wait:1"
    assert outcome == JobOutcome(status="done", result={"waiting_for": "job_" + "1".zfill(26)})


def test_ut10_69_continuation_completes_and_step7_repeats_3_3b_and_4(fakes: Fakes) -> None:
    """UT10-69 a done build with a newer CURRENT runs cleanup_builds(post, keep_last=1); the
    request is done, audited with the record_id only; step 7 runs lake pass 2 and then steps
    3, 3b and 4 again (SN-T10-29-a): memory_purge is called after step 3 and again in step 7."""
    fakes.lake_rows = [2, 1]
    _, cont = _first_visit(fakes)
    fakes.job_status[cont["build_job_id"]] = "done"
    later = NOW + datetime.timedelta(minutes=31)
    outcome = _run(fakes, cont, now=later)

    assert fakes.events == [
        "lake", "enrich", "memory", "evidence", "findings",
        "cleanup", "audit", "lake", "enrich", "memory", "evidence", "findings",
    ]  # fmt: skip
    assert fakes.memory_calls == [RID, RID]
    assert fakes.cleanup_now == [later]
    req = _request()
    assert req.status == "done"
    assert req.completed_at == later
    assert _status(req.steps) == dict.fromkeys(["1", "2", "3", "3b", "4", "5", "6", "7"], "done")
    assert req.steps["5"]["counts"] == {"builds_deleted": 1, "builds_retired": 0, "builds_left": 0}
    seven = req.steps["7"]["counts"]
    assert (seven["rows_removed"], seven["memory_items"], seven["evidence_rows"]) == (1, 4, 1)
    assert (seven["finding_rows"], seven["trace_files"]) == (2, 0)
    assert fakes.audits == [
        (("admin_action", "system"), {"action": "privacy_delete", "target": RID})
    ]
    assert outcome.status == "done"
    assert outcome.result["request_id"] == req.request_id


def test_ut10_69_step7_repeats_even_without_lake_rows(fakes: Fakes) -> None:
    """UT10-69 / SN-T10-29-a lake pass 2 removing nothing still repeats steps 3, 3b and 4:
    memory, evidence, findings and traces can cite the record again during the rebuild wait."""
    _, cont = _first_visit(fakes)
    fakes.job_status[cont["build_job_id"]] = "done"
    _run(fakes, cont)
    assert fakes.memory_calls == [RID, RID]
    assert fakes.events[-6:] == ["audit", "lake", "enrich", "memory", "evidence", "findings"]
    assert _request().steps["7"]["counts"]["rows_removed"] == 0


def test_ut10_69_step7_failure_then_retry_completes(fakes: Fakes) -> None:
    """UT10-69 step 7 failing in its repeated 3b: the request is already done (step 6), step 7
    is recorded failed with the error class; the retry repeats 3, 3b and 4 and completes."""
    fakes.lake_rows = [0, 1]
    _, cont = _first_visit(fakes)
    fakes.job_status[cont["build_job_id"]] = "done"
    fakes.fail["memory"] = [ModelUnavailable("vector store unavailable")]
    with pytest.raises(ModelUnavailable):
        _run(fakes, cont)
    req = _request()
    assert req.status == "done"  # step 6 already ran
    assert (req.steps["7"]["status"], req.steps["7"]["error"]) == ("failed", "ModelUnavailable")
    assert req.steps["7"]["counts"]["rows_removed"] == 1
    _run(fakes, cont)
    req = _request()
    assert req.steps["7"]["status"] == "done"
    assert fakes.memory_calls == [RID, RID, RID]
    assert fakes.events.count("audit") == 1


@pytest.mark.parametrize("why", ["pinned by a run", "kept failed build", "retired, not deleted"])
def test_ut10_69_older_build_left_fails_step5_retryably(fakes: Fakes, why: str) -> None:
    """UT10-69 / I1 a build file older than the step-5 time that cleanup_builds kept (pinned by
    a run or job, the newest failed build, a retired build) fails step 5 with StoreBusy and
    the count only; the retry once it is gone completes. ``deferred`` stays empty."""
    del why
    _, cont = _first_visit(fakes)
    fakes.job_status[cont["build_job_id"]] = "done"
    fakes.left = [OLDER_BUILD]
    with capture_logs() as logs, pytest.raises(StoreBusy, match="older warehouse builds remain"):
        _run(fakes, cont)
    req = _request()
    assert (req.status, req.steps["5"]["status"], req.steps["5"]["error"]) == (
        "running",
        "failed",
        "StoreBusy",
    )
    assert req.steps["5"]["counts"]["builds_left"] == 1
    assert "6" not in req.steps
    assert all(RID not in repr(entry) for entry in logs)
    fakes.left = ["20260924-125900-ABCDEF"]  # a build newer than the step-5 time is no leftover
    _run(fakes, cont)
    assert _request().status == "done"


def test_ut10_69_done_build_without_current_is_not_accepted(fakes: Fakes) -> None:
    """UT10-69 a done build job while no build is CURRENT counts as a failed rebuild."""
    _, cont = _first_visit(fakes)
    fakes.job_status[cont["build_job_id"]] = "done"
    fakes.current = None
    _run(fakes, cont)
    assert fakes.enqueued[-2][0][0] == "build_pipeline"
    assert "cleanup" not in fakes.events


def test_ut10_69_busy_trace_file_fails_step4_retryably(
    fakes: Fakes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-69 a trace file held open (PermissionError on unlink) fails step 4 as StoreBusy;
    a directory named like a trace file is skipped."""
    from herness.core.config import get_config  # noqa: PLC0415

    traces = get_config().paths.data / "traces"
    (traces / "dir.jsonl").mkdir(parents=True)
    held = traces / "run_a.jsonl"
    held.write_bytes(RID.encode())
    real_unlink = type(held).unlink

    def unlink(self: Any, missing_ok: bool = False) -> None:
        if self.name == "run_a.jsonl":
            raise PermissionError(13, "in use")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(type(held), "unlink", unlink)
    with pytest.raises(StoreBusy, match="trace file busy"):
        _run(fakes, dict(BASE))
    assert _request().steps["4"]["error"] == "StoreBusy"
    assert held.exists()


def test_ut10_69_build_still_running_schedules_next_continuation(fakes: Fakes) -> None:
    """UT10-69 a running build: next continuation in 30 minutes with wait key n+1."""
    _, cont = _first_visit(fakes)
    fakes.job_status[cont["build_job_id"]] = "running"
    later = NOW + datetime.timedelta(minutes=30)
    outcome = _run(fakes, cont, now=later)
    args, kwargs = fakes.enqueued[-1]
    assert args[1]["wait"] == 2
    assert args[1]["build_job_id"] == cont["build_job_id"]
    assert kwargs["idem_key"] == f"privacy_delete:{cont['request_id']}:wait:2"
    assert kwargs["scheduled_for"] == later + datetime.timedelta(minutes=30)
    assert outcome.result == {"waiting_for": cont["build_job_id"]}
    assert "5" not in _request().steps


def test_ut10_69_done_build_without_newer_current_is_not_accepted(fakes: Fakes) -> None:
    """UT10-69 a done build job whose build is not CURRENT counts as a failed rebuild."""
    _, cont = _first_visit(fakes)
    fakes.job_status[cont["build_job_id"]] = "done"
    fakes.current = OLDER_BUILD
    _run(fakes, cont)
    args, _ = fakes.enqueued[-2]
    assert args[0] == "build_pipeline"
    assert fakes.enqueued[-1][0][1]["rebuilds"] == 1
    assert "cleanup" not in fakes.events


def test_ut10_69_failed_build_reenqueued_three_times_then_request_failed(fakes: Fakes) -> None:
    """UT10-69 a failed build is re-enqueued at most 3 times, then the request is failed."""
    _, cont = _first_visit(fakes)
    for n in range(1, 4):
        fakes.job_status[cont["build_job_id"]] = "failed"
        _run(fakes, cont)
        cont = fakes.enqueued[-1][0][1]
        assert cont["rebuilds"] == n
        assert fakes.enqueued[-2][0][0] == "build_pipeline"
    fakes.job_status[cont["build_job_id"]] = "failed"
    with pytest.raises(FatalError) as caught:
        _run(fakes, cont)
    assert RID not in str(caught.value)
    req = _request()
    assert req.status == "failed"
    assert req.steps["5"]["status"] == "failed"
    builds = [a for a, _ in fakes.enqueued if a[0] == "build_pipeline"]
    assert len(builds) == 4  # the first build plus three re-enqueues


def test_ut10_69_deferred_cleanup_fails_step5_and_retries(fakes: Fakes) -> None:
    """UT10-69 non-empty ``deferred`` marks step 5 failed (retryable StoreBusy); the retry
    with nothing deferred completes."""
    _, cont = _first_visit(fakes)
    fakes.job_status[cont["build_job_id"]] = "done"
    fakes.deferred = (OLDER_BUILD,)
    with pytest.raises(StoreBusy):
        _run(fakes, cont)
    req = _request()
    assert (req.status, req.steps["5"]["status"], req.steps["5"]["error"]) == (
        "running",
        "failed",
        "StoreBusy",
    )
    fakes.deferred = ()
    _run(fakes, cont)
    assert _request().status == "done"


# --- UT10-69: fail closed, error classes, no PII -------------------------------------------


@pytest.mark.parametrize(
    ("target", "exc", "step", "raised"),
    [
        ("memory", ModelUnavailable("vector store down"), "3b", ModelUnavailable),
        ("lake", StoreBusy("lake busy"), "2", StoreBusy),
        ("enrich", ValueError(RID + " description text"), "3", FatalError),
        ("lake", ConfigError("bad"), "2", ConfigError),
    ],
)
def test_ut10_69_failing_step_records_class_and_reraises(
    fakes: Fakes, target: str, exc: BaseException, step: str, raised: type[BaseException]
) -> None:
    """UT10-69 a failing step records ``failed`` with the error class only and leaves the
    request running; StoreBusy and ModelUnavailable are re-raised, others become FatalError
    (a FatalError subclass passes through); no log line or message carries the record_id."""
    fakes.fail[target] = [exc]
    with capture_logs() as logs, pytest.raises(raised) as caught:
        _run(fakes, dict(BASE))
    req = _request()
    assert req.status == "running"
    assert req.steps[step] == {
        "status": "failed",
        "at": "2026-09-24T12:00:00.000000Z",
        "counts": {},
        "error": type(exc).__name__,
    }
    if raised is FatalError:
        assert type(caught.value) is FatalError
        assert RID not in str(caught.value)
        assert "description" not in str(caught.value)
    assert all(RID not in repr(entry) for entry in logs)
    assert any(e.get("event") == "admin.privacy.step_failed" for e in logs)


def test_ut10_69_payload_is_revalidated(fakes: Fakes) -> None:
    """UT10-69 the payload is re-validated as U10-73: a bad record_id or reason_ref raises
    ConfigError before any request or store is touched; values are never echoed."""
    bad_rid = {**BASE, "record_id": "../../etc:x:y"}
    bad_reason = {**BASE, "reason_ref": "has space"}
    for payload in (bad_rid, bad_reason, {**BASE, "requested_by": 7}):
        with pytest.raises(ConfigError) as caught:
            _run(fakes, payload)
        assert "../" not in str(caught.value)
    assert fakes.events == []
    assert read_all("SELECT * FROM deletion_request", ()) == []


def test_ut10_69_continuation_request_must_match_record(fakes: Fakes) -> None:
    """UT10-69 a continuation whose request_id names another record's request is rejected."""
    _, cont = _first_visit(fakes)
    other = {**cont, "record_id": "servicenow:incident:INC0099"}
    with pytest.raises(ConfigError, match="does not match"):
        _run(fakes, other)
    bad = (("wait", "x"), ("rebuilds", -1), ("build_job_id", "../x"), ("request_id", "del_x"))
    for key, value in bad:
        with pytest.raises(ConfigError):
            _run(fakes, {**cont, key: value})


def test_ut10_69_failed_request_continuation_is_fatal(fakes: Fakes) -> None:
    """UT10-69 a continuation for a request already ``failed`` does no work."""
    _, cont = _first_visit(fakes)
    ops_privacy.set_deletion_status(cont["request_id"], "failed")
    before = list(fakes.events)
    with pytest.raises(FatalError, match="privacy deletion request failed"):
        _run(fakes, cont)
    assert fakes.events == before


def test_ut10_69_trace_files_citing_record_are_deleted(fakes: Fakes) -> None:
    """UT10-69 step 4 deletes ``data/traces/*.jsonl`` files whose bytes contain the record_id
    (also across a read-chunk boundary) and keeps the others."""
    traces = get_config().paths.data / "traces"
    traces.mkdir(parents=True)
    pad = b"x" * (steps.TRACE_CHUNK - 5)
    (traces / "run_a.jsonl").write_bytes(b'{"sql":"' + RID.encode() + b'"}\n')
    (traces / "run_b.jsonl").write_bytes(pad + RID.encode() + b"\n")
    (traces / "run_c.jsonl").write_bytes(b'{"sql":"servicenow:incident:INC0043"}\n')
    (traces / "notes.txt").write_bytes(RID.encode())
    _run(fakes, dict(BASE))
    assert sorted(p.name for p in traces.iterdir()) == ["notes.txt", "run_c.jsonl"]
    assert _request().steps["4"]["counts"]["trace_files"] == 2


def test_ut10_69_trace_with_longer_id_sharing_the_prefix_is_deleted_whole(fakes: Fakes) -> None:
    """UT10-69 (review A1, spec-mandated) a trace citing only a longer ID that has the record_id
    as a prefix is deleted whole: U10-99 matches bytes, and boundary matching would fail open
    (the record-id charset includes ``.``, ``:``, ``/`` and ``+``)."""
    traces = get_config().paths.data / "traces"
    traces.mkdir(parents=True)
    (traces / "run_other.jsonl").write_bytes(b'{"row":"' + RID.encode() + b'1"}\n')
    (traces / "run_dot.jsonl").write_bytes(b"see " + RID.encode() + b".\n")
    _run(fakes, dict(BASE))
    assert list(traces.iterdir()) == []
    assert _request().steps["4"]["counts"]["trace_files"] == 2


class _Spy:
    """One ops connection stand-in that records statements and forwards them."""

    def __init__(self, real: Any, events: list[str], *, busy: bool = False) -> None:
        self.real, self.events, self.busy = real, events, busy
        self.issued: list[str] = []

    def execute(self, sql: str, *args: Any) -> Any:
        self.issued.append(sql)
        if "wal_checkpoint" in sql:
            self.events.append("checkpoint")
            if self.busy:
                return SimpleNamespace(fetchone=lambda: (1, 5, 2))
        return self.real.execute(sql, *args)


def test_ut10_69_steps_run_with_secure_delete_and_checkpoint(
    fakes: Fakes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-69 / TH10-42 every step runs with ``secure_delete`` on the ops connection and step
    4 ends with a WAL truncate, after its scrubs; without a reopen there is no VACUUM."""
    real = core_connection()
    spy = _Spy(real, fakes.events)
    monkeypatch.setattr(steps, "connection", lambda: spy)
    _run(fakes, dict(BASE))
    assert real.execute("PRAGMA secure_delete").fetchone()[0] == 1
    assert spy.issued.count("PRAGMA secure_delete = ON") == 6  # steps 1, 2, 3, 3b, 4 and 5
    assert spy.issued.count("PRAGMA wal_checkpoint(TRUNCATE)") == 1
    assert "VACUUM" not in spy.issued
    assert fakes.events[-2:] == ["findings", "checkpoint"]


def test_ut10_69_busy_checkpoint_fails_step4_retryably(
    fakes: Fakes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-69 a checkpoint that cannot complete fails step 4 with StoreBusy (retried) instead
    of reporting the scrub done while deleted bytes remain in the WAL."""
    real = core_connection()
    busy = _Spy(real, fakes.events, busy=True)
    monkeypatch.setattr(steps, "connection", lambda: busy)
    with pytest.raises(StoreBusy, match="checkpoint busy"):
        _run(fakes, dict(BASE))
    req = _request()
    assert (req.status, req.steps["4"]["status"], req.steps["4"]["error"]) == (
        "running",
        "failed",
        "StoreBusy",
    )
    assert req.steps["4"]["counts"]["evidence_rows"] == 1  # partial counts are kept
    assert "vacuum_owed" not in req.steps["4"]["counts"]
    monkeypatch.setattr(steps, "connection", core_connection)
    _run(fakes, dict(BASE))
    assert _request().steps["4"]["status"] == "done"


def _raiser(exc: BaseException) -> Callable[..., Any]:
    def call(*_args: Any) -> Any:
        raise exc

    return call


def _sqlite_error(text: str, code: int) -> sqlite3.OperationalError:
    exc = sqlite3.OperationalError(text)
    exc.sqlite_errorcode = code
    return exc


@pytest.mark.parametrize("code", [5, 6, 517])  # BUSY, LOCKED, BUSY_SNAPSHOT (extended)
def test_ut10_69_busy_or_locked_pragma_is_store_busy(
    fakes: Fakes, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    """UT10-69 (review minor 1) SQLITE_BUSY / SQLITE_LOCKED (also extended codes) from the
    maintenance PRAGMAs, which run outside run_write, are StoreBusy (retried), not FatalError;
    another sqlite3 error becomes FatalError naming its class only."""
    locked = SimpleNamespace(execute=_raiser(_sqlite_error("database is locked", code)))
    monkeypatch.setattr(steps, "connection", lambda: locked)
    with pytest.raises(StoreBusy):
        _run(fakes, dict(BASE))
    assert _request().steps["1"]["error"] == "StoreBusy"
    broken = SimpleNamespace(execute=_raiser(_sqlite_error("disk I/O error", 10)))
    monkeypatch.setattr(steps, "connection", lambda: broken)
    with pytest.raises(FatalError, match="step 1 failed: OperationalError"):
        _run(fakes, dict(BASE))


def test_ut10_69_connection_reopened_mid_step_is_vacuumed(
    fakes: Fakes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-69 / I3 run_write dropping the thread's connection mid-step (failed ROLLBACK) loses
    ``secure_delete``: the step ends with VACUUM on the new connection (secure_delete on again);
    when the step fails after such a reopen, VACUUM is owed and runs first on the retry."""
    vacuums: list[int] = []
    real_vacuum = steps.vacuum

    def counted_vacuum() -> None:
        vacuums.append(1)
        real_vacuum()

    monkeypatch.setattr(steps, "vacuum", counted_vacuum)
    calls: list[str] = []

    def reopening_memory(record_id: str) -> int:
        calls.append(record_id)
        _drop_cached(close=True)  # what run_write does when ROLLBACK fails
        if len(calls) == 1:
            msg = "memory store busy"
            raise StoreBusy(msg)
        return 2

    def run() -> JobOutcome:
        ctx = _ctx(dict(BASE))
        return privacy.run_privacy_delete(get_config(), ctx, now=NOW, memory_purge=reopening_memory)

    with pytest.raises(StoreBusy):
        run()
    req = _request()
    assert (req.steps["3b"]["status"], req.steps["3b"]["counts"]) == ("failed", {"vacuum_owed": 1})
    assert vacuums == []
    run()
    req = _request()
    assert req.steps["3b"]["status"] == "done"
    assert req.steps["3b"]["counts"] == {"vacuumed": 1, "memory_items": 2}
    assert vacuums == [1, 1]  # the owed one first, then the one for this attempt's reopen
    assert core_connection().execute("PRAGMA secure_delete").fetchone()[0] == 1


def test_ut10_69_reopen_check_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT10-69 / I3 when the connection cannot be checked, it counts as reopened (VACUUM)."""
    monkeypatch.setattr(steps, "connection", _raiser(StoreBusy("ops store busy")))
    assert steps.reopened(cast("sqlite3.Connection", object())) is True


def test_ut10_69_failure_to_record_failure_keeps_step_error(
    fakes: Fakes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-69 (review minor 2) when recording the failed step itself raises, the step error
    still propagates and the secondary error is logged by class only."""
    fakes.fail["evidence"] = [ModelUnavailable("down")]
    real_record = ops_privacy.record_deletion_step

    def record(*args: Any, **kwargs: Any) -> None:
        if kwargs.get("status") == "failed":
            msg = "ops store busy"
            raise StoreBusy(msg)
        real_record(*args, **kwargs)

    monkeypatch.setattr(privacy, "record_deletion_step", record)
    with capture_logs() as logs, pytest.raises(ModelUnavailable):
        _run(fakes, dict(BASE))
    secondary = [e for e in logs if e.get("event") == "admin.privacy.step_record_failed"]
    assert [(e["step"], e["error_type"]) for e in secondary] == [("4", "StoreBusy")]
    assert all(RID not in repr(entry) for entry in logs)
    assert "4" not in _request().steps


# --- UT10-69: dispatch and the memory_purge binding (U10-104, U10-75) ----------------------


def test_ut10_69_handle_maintenance_dispatches_privacy_delete(
    fakes: Fakes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-69 / U10-104 ``privacy_delete`` reaches run_privacy_delete with memory_purge; its
    own JobOutcome is returned unchanged."""
    seen: list[Any] = []
    own = JobOutcome(status="done", result={"waiting_for": "job_x"})

    def fake_run(cfg: Any, ctx: JobContext, *, now: Any, memory_purge: Any) -> JobOutcome:
        seen.append((cfg, ctx.job.payload["action"], now.tzinfo, memory_purge))
        return own

    monkeypatch.setattr(privacy, "run_privacy_delete", fake_run)
    assert handle_maintenance(_ctx(dict(BASE)), memory_purge=fakes.memory) is own
    assert seen[0][1:] == ("privacy_delete", datetime.UTC, fakes.memory)


class FakeMemoryStore:
    """The ``MemoryStore.purge`` signature (T07-26); records how it was called."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def purge(
        self,
        record_id: str | None = None,
        *,
        author_ref: str | None = None,
        now: datetime.datetime | None = None,
    ) -> int:
        self.calls.append(((), {"record_id": record_id, "author_ref": author_ref, "now": now}))
        return 3


def test_ut10_69_registered_binding_routes_step_3b_to_memory_store_purge(
    worker_env: WorkerEnv,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT10-69 / U10-75 the composition-root binding ``lambda rid: store.purge(record_id=rid)``
    registered through register_handlers routes step 3b to MemoryStore.purge(record_id=...)
    when impl 08 calls the handler with ctx alone (carry-over of the herness.cli binding)."""
    del worker_env
    f = Fakes()
    for name, value in {
        "purge_record_ids": f.lake,
        "purge_record": f.enrich,
        "scrub_record_from_evidence": f.evidence,
    }.items():
        monkeypatch.setattr(steps, name, value)
    store = FakeMemoryStore()
    monkeypatch.delitem(process_state().handlers, "maintenance")
    binding: Callable[[str], int] = lambda rid: store.purge(record_id=rid)  # noqa: E731
    admin.register_handlers(memory_purge=binding)
    handler = resolve_handler("maintenance")
    assert isinstance(handler, functools.partial)
    outcome = handler(_ctx(dict(BASE)))
    assert store.calls == [((), {"record_id": RID, "author_ref": None, "now": None})]
    assert outcome.status == "done"
    assert set(outcome.result) == {"waiting_for"}
    queued = require_jobs_backend().list_jobs(status="queued", kind=None, limit=10)
    kinds = sorted(r.kind for r in queued)
    assert kinds == ["build_pipeline", "maintenance"]
    build = require_jobs_backend().list_jobs(kind="build_pipeline", status="queued", limit=1)[0]
    assert build.gpu_class == "none"
