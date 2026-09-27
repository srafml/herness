"""The `build_pipeline` job with stage `build` (impl 02 T02-18: U02-97 … U02-99, flow F02-02).

IT02-21: `lake_small` built through the job handler registry (T08-12) matches the golden row
counts and `core.*` snapshot; `meta.build` is filled from config, watermarks and git.
IT02-22: a broken `230_incident.sql` fails the build without touching `CURRENT`.
IT02-27: a yield inside `build` leaves an orphan that the next run deletes; resume rules.
The handler is a one-argument wrapper of `run_build_pipeline` (the factory U02-134 is T02-19's).
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
import structlog
from tests.support.build_harness import FakeJobContext
from tests.support.lake_small import GOLDEN_CORE, GOLDEN_COUNTS, core_snapshot, install, load_config
from tests.support.ops_store import OpsStoreHandle

from herness.core.config import HernessConfig, config_hash
from herness.core.errors import ConfigError, HernessError
from herness.core.jobs.handlers import register_handler, resolve_handler, run_handler
from herness.core.jobs.ports import JobContext
from herness.core.resilience import ProcessState
from herness.core.types import JobOutcome
from herness.model import _build_support as support
from herness.model import build
from herness.model.build import run_build_pipeline, run_sql_range
from herness.model.errors import BuildSqlError
from herness.model.sqlfiles import discover_sql_files
from herness.store import ops, warehouse
from herness.store._warehouse_rw import open_for_build, write_current
from herness.store.errors import NotFoundError
from herness.store.layout import DataLayout

pytestmark = pytest.mark.integration

SQL_DIR = Path(build.__file__).parent / "sql"
LITERAL = "zq-literal-4471"
OTHER_ID = "20260901-070000-01ABCD"
T_WM = datetime.datetime(2026, 9, 1, 5, 0, tzinfo=datetime.UTC)
_META = (
    "SELECT build_id, started_at, finished_at, git_sha, config_hash, dataset_kind,"
    " CAST(source_watermarks AS VARCHAR), CAST(row_counts AS VARCHAR), status FROM meta.build"
)


@dataclasses.dataclass(frozen=True)
class Env:
    data_root: Path
    layout: DataLayout
    cfg: HernessConfig


@pytest.fixture
def env(tmp_path: Path, ops_store: OpsStoreHandle, reset_process_state: ProcessState) -> Env:
    """`lake_small` under the ops store's data root and a loaded config pointing at it."""
    install(ops_store.data_root)
    cfg = load_config(tmp_path / "cfgroot", ops_store.data_root)
    return Env(ops_store.data_root, DataLayout.from_root(ops_store.data_root), cfg)


def _handler(ctx: JobContext) -> JobOutcome:
    return run_build_pipeline(ctx)


def _run(ctx: FakeJobContext) -> JobOutcome | HernessError:
    """Run through the registry: register, resolve and call like the worker (T08-12)."""
    register_handler("build_pipeline", _handler)
    return run_handler(ctx, resolve_handler("build_pipeline"))


def _done(ctx: FakeJobContext) -> JobOutcome:
    outcome = _run(ctx)
    assert isinstance(outcome, JobOutcome), outcome
    return outcome


def _broken_sql(tmp_path: Path, number: str, text: str) -> Path:
    copy = tmp_path / "sql"
    shutil.copytree(SQL_DIR, copy)
    target = next(copy.glob(f"{number}_*.sql"))
    target.write_text(text, encoding="utf-8")
    return copy


def _use_sql(monkeypatch: pytest.MonkeyPatch, sql_dir: Path) -> None:
    monkeypatch.setattr(build, "discover_sql_files", lambda: discover_sql_files(sql_dir=sql_dir))


def _metrics() -> list[tuple[str, str]]:
    rows = ops.read_all("SELECT name, labels FROM metric_sample ORDER BY rowid")
    return [(str(r["name"]), str(r["labels"])) for r in rows]


def test_it02_21_lake_small_build(
    env: Env, fake_job_context: Callable[..., FakeJobContext]
) -> None:
    """IT02-21 pipeline `[build]` on `lake_small`: golden row counts and `core.*` snapshot,
    `meta.build` fields set, a completed unpromoted build (`building`, `finished_at` set)."""
    ops.set_watermark("servicenow", "incident", "sys_updated_on", T_WM, now=T_WM)
    ctx = fake_job_context({"stages": ["build"]})
    outcome = _done(ctx)
    assert outcome.status == "done"
    result = outcome.result
    build_id = str(result["build_id"])
    golden_counts = json.loads(GOLDEN_COUNTS.read_text(encoding="utf-8"))
    assert result["row_counts"] == golden_counts
    assert {k: result[k] for k in ("stages", "promoted", "dq", "enrich", "scoring")} == {
        "stages": ["build"],
        "promoted": False,
        "dq": None,
        "enrich": None,
        "scoring": None,
    }
    assert result["rekey_night"] is False
    assert list(result["durations_ms"]) == ["build"]  # type: ignore[arg-type]
    with warehouse.open_readonly(build_id, layout=env.layout) as con:
        assert core_snapshot(con) == json.loads(GOLDEN_CORE.read_text(encoding="utf-8"))
        rows = con.execute(_META).fetchall()
    assert len(rows) == 1
    bid, started, finished, sha, cfg_hash, kind, marks, counts, status = rows[0]
    assert (bid, cfg_hash, kind, status) == (build_id, config_hash(env.cfg), "real", "building")
    assert started is not None
    assert finished is not None
    assert finished >= started
    assert sha == "unknown" or len(str(sha)) == 12
    assert json.loads(str(marks)) == {"servicenow/incident": "2026-09-01T05:00:00.000000Z"}
    assert json.loads(str(counts)) == golden_counts
    assert ctx.saved_states == [{"build_id": build_id, "stages_done": ["build"]}]
    assert "sql 000_settings.sql" in ctx.heartbeats
    assert ctx.heartbeats[-1] == "stage build done"
    [info] = warehouse.list_builds(layout=env.layout)
    assert (info.build_id, info.status, info.is_current) == (build_id, "building", False)
    assert info.finished_at is not None
    assert warehouse.read_current(layout=env.layout) is None
    names = _metrics()
    assert ("herness_model_build_total", '{"status":"unpromoted"}') in names
    assert ("herness_model_build_stage_seconds", '{"stage":"build"}') in names
    assert ("herness_model_build_rows_total", '{"table":"core.incident"}') in names
    assert any(n == "herness_model_warehouse_bytes" for n, _ in names)
    assert sum(n == "herness_model_build_sql_file_seconds" for n, _ in names) == 17


def test_it02_21_deletions_are_honoured(
    env: Env, fake_job_context: Callable[..., FakeJobContext]
) -> None:
    """IT02-21 (ST02-12 dispatch note, TH02-16) a record with a running deletion request is
    registered before staging and never reaches `core.incident`."""
    now = datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC)
    request = ops.create_deletion_request(
        record_id="servicenow:incident:i2", requested_by="ab" * 16, reason_ref="REQ-1", now=now
    )
    ops.set_deletion_status(request.request_id, "running")
    outcome = _done(fake_job_context({"stages": ["build"]}))
    assert outcome.result["row_counts"]["core.incident"] == 3  # type: ignore[index]
    with warehouse.open_readonly(str(outcome.result["build_id"]), layout=env.layout) as con:
        ids = con.execute("SELECT record_id FROM core.incident ORDER BY 1").fetchall()
    assert ("servicenow:incident:i2",) not in ids


def test_it02_21_metric_failure_is_logged(
    env: Env, fake_job_context: Callable[..., FakeJobContext], monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT02-21 (F02-02 step 10) a metric write failure is logged and the outcome returned."""

    def fail(_samples: object) -> int:
        msg = "metric store down"
        raise ConfigError(msg)

    monkeypatch.setattr(ops, "record_metric_samples", fail)
    with structlog.testing.capture_logs() as logs:
        outcome = _done(fake_job_context({"stages": ["build"]}))
    assert outcome.status == "done"
    failed = [e for e in logs if e["event"] == "model.build.metrics_write_failed"]
    assert failed == [
        {
            "component": "model.build",
            "event": "model.build.metrics_write_failed",
            "error_class": "ConfigError",
            "log_level": "warning",
        }
    ]


def test_it02_22_broken_230_fails_build(
    env: Env,
    fake_job_context: Callable[..., FakeJobContext],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """IT02-22 a broken `230_incident.sql`: BuildSqlError naming the file, status `failed`,
    `CURRENT` unchanged, no literal value in the message or the logs."""
    first = str(_done(fake_job_context({"stages": ["build"]})).result["build_id"])
    write_current(first, layout=env.layout)
    broken = f"SELECT 1;\nSELECT CAST('{LITERAL}' AS INTEGER) AS n;\n"
    _use_sql(monkeypatch, _broken_sql(tmp_path, "230", broken))
    with structlog.testing.capture_logs() as logs:
        error = _run(fake_job_context({"stages": ["build"]}))
    assert isinstance(error, BuildSqlError)
    assert (error.file, error.statement_index) == ("230_incident.sql", 2)
    assert "230_incident.sql" in str(error)
    assert LITERAL not in str(error)
    assert LITERAL not in repr((dict(error.context), dict(error.details), error.db_error))
    assert error.__cause__ is None
    assert LITERAL not in json.dumps(logs, default=str)
    [failed] = [e for e in logs if e["event"] == "model.build.failed"]
    assert (failed["stage"], failed["error_class"]) == ("build", "BuildSqlError")
    assert warehouse.read_current(layout=env.layout) == first
    statuses = {b.build_id: b.status for b in warehouse.list_builds(layout=env.layout)}
    assert statuses.pop(first) == "building"
    assert list(statuses.values()) == ["failed"]
    assert ("herness_model_build_total", '{"status":"failed"}') in _metrics()


def test_it02_22_failure_before_meta_row(
    env: Env,
    fake_job_context: Callable[..., FakeJobContext],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """IT02-22 a failure before `meta.build` exists: marking it failed fails, is logged as
    `model.build.mark_failed_error` and suppressed; the original error propagates."""
    _use_sql(monkeypatch, _broken_sql(tmp_path, "010", "SELEC 1;\n"))
    with structlog.testing.capture_logs() as logs:
        error = _run(fake_job_context({"stages": ["build"]}))
    assert isinstance(error, BuildSqlError)
    assert error.file == "010_macros.sql"
    events = [e["event"] for e in logs if e["log_level"] == "error"]
    assert events == ["model.build.mark_failed_error", "model.build.failed"]
    [info] = warehouse.list_builds(layout=env.layout)
    assert info.status == "unreadable"  # no meta.build row


def test_it02_22_render_failure(
    env: Env,
    fake_job_context: Callable[..., FakeJobContext],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """IT02-22 a template error in 230 is ConfigError, logged `model.build.render_failed`."""
    _use_sql(monkeypatch, _broken_sql(tmp_path, "230", "SELECT {{ nope }};\n"))
    with structlog.testing.capture_logs() as logs:
        error = _run(fake_job_context({"stages": ["build"]}))
    assert isinstance(error, ConfigError)
    assert "230_incident.sql" in str(error)
    [event] = [e for e in logs if e["event"] == "model.build.render_failed"]
    assert event["file"] == "230_incident.sql"
    assert [b.status for b in warehouse.list_builds(layout=env.layout)] == ["failed"]


@pytest.mark.parametrize(
    ("lo", "hi"), [(-1, 10), (10, 5), (0, 1000), (300, 400), (450, 460), (499, 900)]
)
def test_it02_22_sql_range_bounds(lo: int, hi: int) -> None:
    """IT02-22 (U02-97) a range outside 0-999, reversed or touching 400-499 is ConfigError."""
    context = None
    with pytest.raises(ConfigError, match="400-499"):
        run_sql_range(
            None,  # type: ignore[arg-type]
            [],
            lo,
            hi,
            context=context,  # type: ignore[arg-type]
            should_yield=lambda: False,
            heartbeat=lambda _note: None,
        )


def test_it02_27_yield_leaves_orphan_next_run_deletes_it(
    env: Env, fake_job_context: Callable[..., FakeJobContext]
) -> None:
    """IT02-27 a yield after 3 SQL files returns `yield` with no `build` in state; the second
    run creates a new build and deletes the orphan."""
    payload = {"stages": ["build"]}
    first = fake_job_context(payload, yield_after=4)  # stage check + 3 files, then yield
    outcome = _done(first)
    assert outcome.status == "yield"
    ran = [h for h in first.heartbeats if h is not None and h.startswith("sql ")]
    assert ran == ["sql 000_settings.sql", "sql 010_macros.sql", "sql 110_stg_servicenow.sql"]
    state = first.load_state()
    assert state["stages_done"] == []
    orphan = str(state["build_id"])
    [info] = warehouse.list_builds(layout=env.layout)
    assert (info.build_id, info.status, info.finished_at) == (orphan, "building", None)
    second = fake_job_context(payload, state=state)
    with structlog.testing.capture_logs() as logs:
        done = _done(second)
    assert done.status == "done"
    assert done.result["build_id"] != orphan
    assert [b.build_id for b in warehouse.list_builds(layout=env.layout)] == [
        done.result["build_id"]
    ]
    deleted = [e for e in logs if e["event"] == "model.build.orphan_deleted"]
    assert [(e["build_id"], e["status"]) for e in deleted] == [(orphan, "building")]
    assert ("herness_model_build_total", '{"status":"yield"}') in _metrics()


def test_it02_27_yield_before_first_stage(
    env: Env, fake_job_context: Callable[..., FakeJobContext]
) -> None:
    """IT02-27 a yield before the first stage creates no file and saves empty progress."""
    ctx = fake_job_context({"stages": ["build"]}, yield_after=0)
    outcome = _done(ctx)
    assert outcome.status == "yield"
    assert warehouse.list_builds(layout=env.layout) == []
    assert ctx.load_state()["stages_done"] == []


def test_it02_27_yield_inside_setup_files(
    env: Env, fake_job_context: Callable[..., FakeJobContext]
) -> None:
    """IT02-27 a yield between the setup files (000-099) returns `yield` before `meta.build`
    exists: the partial file has no row yet."""
    ctx = fake_job_context({"stages": ["build"]}, yield_after=2)  # stage check + 000 only
    assert _done(ctx).status == "yield"
    [info] = warehouse.list_builds(layout=env.layout)
    assert info.status == "unreadable"
    assert ctx.load_state() == {"build_id": info.build_id, "stages_done": []}


def test_it02_27_resume_after_build_stage(
    env: Env, fake_job_context: Callable[..., FakeJobContext]
) -> None:
    """IT02-27 (U02-98 step 3) state with `build` done on a `building` build resumes it: no
    new file, nothing left to run, `finished_at` set again."""
    first = _done(fake_job_context({"stages": ["build"]}))
    build_id = str(first.result["build_id"])
    state = {"build_id": build_id, "stages_done": ["build"]}
    again = _done(fake_job_context({"stages": ["build"]}, state=state))
    assert again.result["build_id"] == build_id
    assert again.result["durations_ms"] == {}
    assert [b.build_id for b in warehouse.list_builds(layout=env.layout)] == [build_id]


def test_it02_27_resolve_build_rules(env: Env) -> None:
    """IT02-27 (U02-98 step 3) payload `build_id`: missing → NotFoundError, not `building` →
    ConfigError, `building` → its done stages from state; a stale state → a new build."""
    now = datetime.datetime(2026, 9, 1, 6, 0, tzinfo=datetime.UTC)
    with pytest.raises(NotFoundError):
        support.resolve_build(OTHER_ID, {}, env.layout, now)
    build_id = str(run_build_pipeline(FakeJobContext({"stages": ["build"]})).result["build_id"])
    state = {"build_id": build_id, "stages_done": ["build", "bogus"]}
    assert support.resolve_build(build_id, state, env.layout, now) == (build_id, ["build"])
    stale = {"build_id": "20200101-000000-01ABCD", "stages_done": ["build"]}
    new_id, done = support.resolve_build(None, stale, env.layout, now)
    assert new_id.startswith("20260901-060000-")
    assert done == []
    write_current(build_id, layout=env.layout)
    shutil.copyfile(
        warehouse.build_path(build_id, layout=env.layout),
        warehouse.build_path(OTHER_ID, layout=env.layout),
    )
    view = support.DbSettings(threads=1, memory_limit="512MiB")
    with open_for_build(OTHER_ID, create=False, cfg=view, layout=env.layout) as con:
        con.execute("UPDATE meta.build SET status = 'failed'")
    with pytest.raises(ConfigError, match="is failed"):
        support.resolve_build(OTHER_ID, {}, env.layout, now)


def test_it02_21_payload_errors(env: Env, fake_job_context: Callable[..., FakeJobContext]) -> None:
    """IT02-21 (U02-98 step 1) an invalid payload is ConfigError naming fields only, logged
    `model.build.payload_invalid`; a stage of a later card is rejected before any work."""
    with structlog.testing.capture_logs() as logs:
        error = _run(fake_job_context({"stages": ["build"], "build_id": LITERAL}))
    assert isinstance(error, ConfigError)
    assert LITERAL not in str(error)
    assert [e["event"] for e in logs] == ["model.build.payload_invalid"]
    missing = _run(fake_job_context({"stages": ["build", "enrich"]}))
    assert isinstance(missing, ConfigError)
    assert "enrich" in str(missing)
    assert warehouse.list_builds(layout=env.layout) == []
