"""Stages `enrich` and `score` of `build_pipeline` on `lake_small` (impl 02 T02-19).

IT02-23: `300_attach_decisions.sql` sets `core.incident.content_hash` and prunes enrich rows
of records that are not live. IT02-24: `310_attach_clusters.sql` deletes members of missing
clusters. IT02-25: U02-100 calls `run_enrichment` with the spec arguments, takes no GPU class
itself (R-43) and turns `YieldRequested` into `yield`. IT02-26: U02-101 runs the facts hook
and then scoring on the one build connection. ST02-16: a record under a done deletion
request is absent from `stg`, `core` and `enrich` of the next build (TH02-16).

The spec 03 / 04 hooks `run_enrichment` (T03-28) and `run_scoring` (T04-13) are not on this
tree: the tests replace the private loader seams of `herness.model._build_stages` with fakes.
"""

from __future__ import annotations

import dataclasses
import datetime
from collections.abc import Callable, Sequence
from pathlib import Path

import duckdb
import pytest
import structlog
from pydantic import BaseModel
from tests.support.build_harness import FakeJobContext
from tests.support.lake_small import install, load_config
from tests.support.ops_store import OpsStoreHandle

from herness.core.config import HernessConfig
from herness.core.errors import ConfigError, HernessError
from herness.core.jobs.handlers import register_handler, resolve_handler, run_handler
from herness.core.jobs.ports import JobContext
from herness.core.resilience import ProcessState
from herness.core.types import JobOutcome
from herness.enrich.gpu import YieldRequested
from herness.metrics import facts
from herness.model import _build_stages as stages
from herness.model import build
from herness.model.build import make_build_pipeline_handler
from herness.store import ops, warehouse
from herness.store._warehouse_rw import write_current
from herness.store.layout import DataLayout

pytestmark = pytest.mark.integration

LIVE = "servicenow:incident:i1"
DELETED = "servicenow:incident:i2"
GONE = "servicenow:incident:gone9"
GONE_CHANGE = "servicenow:change_request:gone9"
_ENRICH_IDS = (
    ("enrich.text_redacted", "record_id"),
    ("enrich.decision", "record_id"),
    ("enrich.cluster_member", "record_id"),
    ("enrich.incident_change_link", "incident_id"),
    ("enrich.decision_wide", "record_id"),
)


@dataclasses.dataclass(frozen=True)
class Env:
    layout: DataLayout
    cfg: HernessConfig


@pytest.fixture
def env(tmp_path: Path, ops_store: OpsStoreHandle, reset_process_state: ProcessState) -> Env:
    """`lake_small` under the ops store's data root and a loaded config pointing at it."""
    install(ops_store.data_root)
    cfg = load_config(tmp_path / "cfgroot", ops_store.data_root)
    return Env(DataLayout.from_root(ops_store.data_root), cfg)


class Report(BaseModel):
    """Stand-in for impl 03 `EnrichReport` / impl 04 `ScoringReport`."""

    name: str
    records: int = 0


@dataclasses.dataclass
class EnrichCall:
    con: duckdb.DuckDBPyConnection
    build_id: str
    depth: str
    ctx: JobContext
    prev_warehouse: Path | None
    stages: Sequence[str] | None
    llm_factory: object | None
    gpu_class: str
    finished_at: object


Statement = tuple[str, Sequence[object]]


class FakeEnrichment:
    """Recording `run_enrichment`: runs `statements`, or raises `YieldRequested`."""

    def __init__(
        self,
        statements: Sequence[Statement] = (),
        *,
        yield_at: str | None = None,
        writer: Callable[[duckdb.DuckDBPyConnection, Path | None], None] | None = None,
    ) -> None:
        self.statements = statements
        self.yield_at = yield_at
        self.writer = writer
        self.calls: list[EnrichCall] = []

    def __call__(  # noqa: PLR0913 - T03-28 signature
        self,
        wh: duckdb.DuckDBPyConnection,
        build_id: str,
        *,
        depth: str,
        ctx: JobContext,
        prev_warehouse: Path | None,
        stages: Sequence[str] | None = None,
        llm_factory: object | None = None,
    ) -> Report:
        assert isinstance(ctx, FakeJobContext)
        row = wh.execute("SELECT finished_at FROM meta.build").fetchone()
        finished = row[0] if row else "no row"
        call = EnrichCall(
            wh,
            build_id,
            depth,
            ctx,
            prev_warehouse,
            stages,
            llm_factory,
            ctx.current_class,
            finished,
        )
        self.calls.append(call)
        if self.yield_at is not None:
            raise YieldRequested(self.yield_at)
        for sql, params in self.statements:
            wh.execute(sql, list(params))
        if self.writer is not None:
            self.writer(wh, prev_warehouse)
        return Report(name="enrich", records=len(self.statements))


def _use(monkeypatch: pytest.MonkeyPatch, fake: FakeEnrichment) -> FakeEnrichment:
    monkeypatch.setattr(stages, "_load_run_enrichment", lambda: fake)
    return fake


def _run(ctx: FakeJobContext, llm_factory: object | None = None) -> JobOutcome | HernessError:
    return run_handler(ctx, make_build_pipeline_handler(llm_factory=llm_factory))


def _done(ctx: FakeJobContext, llm_factory: object | None = None) -> JobOutcome:
    outcome = _run(ctx, llm_factory)
    assert isinstance(outcome, JobOutcome), outcome
    return outcome


def _query(env: Env, build_id: str, sql: str) -> list[tuple[object, ...]]:
    with warehouse.open_readonly(build_id, layout=env.layout) as con:
        return con.execute(sql).fetchall()


def _count(env: Env, build_id: str, name: str) -> list[tuple[object, ...]]:
    return _query(env, build_id, f"SELECT value FROM stg.build_counts WHERE name = '{name}'")  # noqa: S608


def _ids(env: Env, build_id: str, table: str, column: str = "record_id") -> set[object]:
    return {r[0] for r in _query(env, build_id, f"SELECT {column} FROM {table}")}  # noqa: S608


_LINK = (
    "INSERT INTO enrich.incident_change_link SELECT ?, min(record_id), 'rule', 1.0 FROM core.change"
)


def _enrich_rows(*record_ids: str) -> list[Statement]:
    rows: list[Statement] = [("INSERT INTO enrich.cluster (cluster_id) VALUES ('c1')", ())]
    for rid in record_ids:
        rows += [
            (
                "INSERT INTO enrich.text_redacted VALUES (?, 'incident', '[t]', ?)",
                (rid, f"h-{rid}"),
            ),
            (
                "INSERT INTO enrich.decision (record_id, question, answer) VALUES (?, 'q1', 'y')",
                (rid,),
            ),
            ("INSERT INTO enrich.cluster_member VALUES (?, 'c1', 0.9)", (rid,)),
            (_LINK, (rid,)),
        ]
    return rows


def test_it02_23_attach_sets_hash_and_prunes_dead_records(
    env: Env, fake_job_context: Callable[..., FakeJobContext], monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT02-23 enrich rows of a live and a missing record: `content_hash` set for the live
    incident, every row of the missing record gone, `enrich_pruned` counted, the handler run
    through the job registry (T08-12)."""
    rows = [
        *_enrich_rows(LIVE, GONE),
        ("INSERT INTO enrich.incident_change_link VALUES (?, ?, 'rule', 0.5)", (LIVE, GONE_CHANGE)),
    ]
    fake = _use(monkeypatch, FakeEnrichment(rows))
    register_handler("build_pipeline", make_build_pipeline_handler(llm_factory=None))
    ctx = fake_job_context({"stages": ["build", "enrich"]})
    outcome = run_handler(ctx, resolve_handler("build_pipeline"))
    assert isinstance(outcome, JobOutcome), outcome
    build_id = str(outcome.result["build_id"])
    assert outcome.result["enrich"] == {"name": "enrich", "records": len(rows)}
    assert outcome.result["scoring"] is None
    assert ctx.load_state() == {"build_id": build_id, "stages_done": ["build", "enrich"]}
    assert list(outcome.result["durations_ms"]) == ["build", "enrich"]  # type: ignore[arg-type]
    assert len(fake.calls) == 1
    hashes = dict(_query(env, build_id, "SELECT record_id, content_hash FROM core.incident"))  # type: ignore[arg-type]
    assert hashes.pop(LIVE) == f"h-{LIVE}"
    assert set(hashes.values()) == {None}
    for table, column in _ENRICH_IDS:
        assert _ids(env, build_id, table, column) == {LIVE}, table
    links = _query(env, build_id, "SELECT change_id FROM enrich.incident_change_link")
    assert len(links) == 1
    assert links[0][0] != GONE_CHANGE
    # text 1 + decision 1 + member 1 + links 2 (dead incident; dead change)
    assert _count(env, build_id, "enrich_pruned") == [(5,)]
    assert _count(env, build_id, "cluster_member_orphans") == [(0,)]


def test_it02_23_attach_rerun_is_idempotent(
    env: Env, fake_job_context: Callable[..., FakeJobContext], monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT02-23 re-running stage `enrich` on the build (resume) keeps one count row per name
    and the same attached rows; a completed build is reopened (`finished_at` cleared)."""
    _use(monkeypatch, FakeEnrichment(_enrich_rows(GONE)))
    build_id = str(_done(fake_job_context({"stages": ["build", "enrich"]})).result["build_id"])
    again = _use(monkeypatch, FakeEnrichment())
    payload = {"stages": ["enrich"], "build_id": build_id}
    outcome = _done(fake_job_context(payload))
    assert outcome.result["build_id"] == build_id
    assert again.calls[0].finished_at is None  # cleared before enrichment runs
    assert again.calls[0].prev_warehouse is None  # no CURRENT
    assert _count(env, build_id, "enrich_pruned") == [(0,)]
    assert _count(env, build_id, "cluster_member_orphans") == [(0,)]
    assert _ids(env, build_id, "enrich.text_redacted") == set()
    [info] = warehouse.list_builds(layout=env.layout)
    assert (info.status, info.finished_at is not None) == ("building", True)


def test_it02_24_orphan_members_deleted_and_counted(
    env: Env, fake_job_context: Callable[..., FakeJobContext], monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT02-24 members of a missing cluster (and with no cluster) are deleted and counted in
    `cluster_member_orphans`; members of an existing cluster stay."""
    rows: list[Statement] = [
        ("INSERT INTO enrich.cluster (cluster_id) VALUES ('c1')", ()),
        ("INSERT INTO enrich.cluster_member VALUES (?, 'c1', 0.9)", (LIVE,)),
        ("INSERT INTO enrich.cluster_member VALUES (?, 'c-missing', 0.4)", (LIVE,)),
        ("INSERT INTO enrich.cluster_member VALUES (?, NULL, 0.1)", (LIVE,)),
    ]
    _use(monkeypatch, FakeEnrichment(rows))
    build_id = str(_done(fake_job_context({"stages": ["build", "enrich"]})).result["build_id"])
    members = _query(env, build_id, "SELECT record_id, cluster_id FROM enrich.cluster_member")
    assert members == [(LIVE, "c1")]
    assert _count(env, build_id, "cluster_member_orphans") == [(2,)]
    assert _count(env, build_id, "enrich_pruned") == [(0,)]


def _record_ranges(
    monkeypatch: pytest.MonkeyPatch, ctx: FakeJobContext
) -> list[tuple[int, int, str]]:
    seen: list[tuple[int, int, str]] = []
    original = build.run_sql_range

    def recording(con: duckdb.DuckDBPyConnection, files: object, lo: int, hi: int, **kw: object):  # type: ignore[no-untyped-def]
        seen.append((lo, hi, ctx.current_class))
        return original(con, files, lo, hi, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(build, "run_sql_range", recording)
    return seen


def test_it02_25_enrich_arguments_and_no_gpu_class(
    env: Env, fake_job_context: Callable[..., FakeJobContext], monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT02-25 with CURRENT set, `enrich_stage="link"` and a fake `llm_factory`: called with
    (con, build_id, depth, ctx, prev path, `stages=["link"]`, the same factory); no GPU
    request or scope from the stage (R-43); class `none` during `run_sql_range(300, 399)`."""
    _use(monkeypatch, FakeEnrichment())
    first = str(_done(fake_job_context({"stages": ["build"]})).result["build_id"])
    write_current(first, layout=env.layout)
    fake = _use(monkeypatch, FakeEnrichment())
    factory = object()
    payload = {"stages": ["build", "enrich"], "enrich_stage": "link", "depth": "deep"}
    ctx = fake_job_context(payload)
    ranges = _record_ranges(monkeypatch, ctx)
    outcome = _done(ctx, factory)
    build_id = str(outcome.result["build_id"])
    assert build_id != first
    [call] = fake.calls
    assert (call.build_id, call.depth, call.ctx, call.stages) == (build_id, "deep", ctx, ["link"])
    assert call.llm_factory is factory
    assert call.prev_warehouse == warehouse.build_path(first, layout=env.layout)
    assert isinstance(call.con, duckdb.DuckDBPyConnection)
    assert (ctx.gpu_requests, ctx.gpu_scopes, call.gpu_class) == ([], [], "none")
    assert (300, 399, "none") in ranges
    assert ctx.load_state()["stages_done"] == ["build", "enrich"]


def test_it02_25_current_is_this_build_gives_no_prev(
    env: Env, fake_job_context: Callable[..., FakeJobContext], monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT02-25 (U02-100 step 2) when CURRENT names this build, `prev_warehouse` is None and
    `stages` is None without `enrich_stage`."""
    _use(monkeypatch, FakeEnrichment())
    build_id = str(_done(fake_job_context({"stages": ["build"]})).result["build_id"])
    write_current(build_id, layout=env.layout)
    fake = _use(monkeypatch, FakeEnrichment())
    _done(fake_job_context({"stages": ["enrich"], "build_id": build_id}))
    assert (fake.calls[0].prev_warehouse, fake.calls[0].stages) == (None, None)


def test_it02_25_yield_then_resume(
    env: Env, fake_job_context: Callable[..., FakeJobContext], monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT02-25 `YieldRequested` from enrichment: the handler returns `yield`, `enrich` is not
    in `stages_done`, 300-399 did not run; the next run resumes at `enrich`."""
    _use(monkeypatch, FakeEnrichment(yield_at="link"))
    ctx = fake_job_context({"stages": ["build", "enrich"]})
    ranges = _record_ranges(monkeypatch, ctx)
    outcome = _done(ctx)
    build_id = str(outcome.result["build_id"])
    assert outcome.status == "yield"
    assert outcome.result == {"build_id": build_id}
    assert ctx.load_state() == {"build_id": build_id, "stages_done": ["build"]}
    assert [r[:2] for r in ranges] == [(0, 99), (100, 299)]
    fake = _use(monkeypatch, FakeEnrichment())
    resumed = fake_job_context({"stages": ["build", "enrich"]}, state=ctx.load_state())
    again = _done(resumed)
    assert (again.status, again.result["build_id"]) == ("done", build_id)
    assert [c.build_id for c in fake.calls] == [build_id]
    assert resumed.load_state()["stages_done"] == ["build", "enrich"]


def test_it02_25_yield_inside_attach_range(
    env: Env, fake_job_context: Callable[..., FakeJobContext], monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT02-25 a yield request between files 300 and 310 returns `yield` without `enrich`
    in `stages_done`."""
    _use(monkeypatch, FakeEnrichment())
    first = fake_job_context({"stages": ["build"]})
    build_id = str(_done(first).result["build_id"])
    payload = {"stages": ["enrich"], "build_id": build_id}
    ctx = fake_job_context(payload, yield_after=2, state=first.load_state())
    outcome = _done(ctx)  # checks: stage start, before 300, before 310 -> yield
    assert outcome.status == "yield"
    assert ctx.load_state() == {"build_id": build_id, "stages_done": ["build"]}


@pytest.mark.parametrize("stage", ["enrich", "score"])
def test_it02_25_missing_hook_fails_build(
    env: Env,
    fake_job_context: Callable[..., FakeJobContext],
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    """IT02-25 / IT02-26 a hook module that is not installed: ConfigError `build stage <name>
    is not available` through the failure path (status `failed`, `model.build.failed`)."""
    _use(monkeypatch, FakeEnrichment())
    absent = "herness.model._absent_hook"
    monkeypatch.setattr(stages, "_load_run_scoring", lambda: stages._hook(absent, "x", "score"))
    if stage == "enrich":
        monkeypatch.setattr(
            stages, "_load_run_enrichment", lambda: stages._hook(absent, "x", stage)
        )
    payload = {"stages": ["build", "enrich", "score"]}
    with structlog.testing.capture_logs() as logs:
        error = _run(fake_job_context(payload))
    assert isinstance(error, ConfigError)
    assert str(error) == f"build stage {stage} is not available"
    failed = [e for e in logs if e["event"] == "model.build.failed"]
    assert [(e["stage"], e["error_class"]) for e in failed] == [(stage, "ConfigError")]
    [info] = warehouse.list_builds(layout=env.layout)
    assert info.status == "failed"


@dataclasses.dataclass
class ScoreFakes:
    order: list[str] = dataclasses.field(default_factory=list)
    cons: list[duckdb.DuckDBPyConnection] = dataclasses.field(default_factory=list)
    scoring_args: list[tuple[str, object, JobContext]] = dataclasses.field(default_factory=list)


def _score_fakes(monkeypatch: pytest.MonkeyPatch, *, real_facts: bool = False) -> ScoreFakes:
    fakes = ScoreFakes()
    real = facts.materialize_facts

    def materialize(con: duckdb.DuckDBPyConnection, build_id: str, /) -> list[str]:
        fakes.order.append("facts")
        fakes.cons.append(con)
        if real_facts:
            return real(con, build_id)
        con.execute("BEGIN TRANSACTION")  # raises when a transaction is already open
        con.execute("ROLLBACK")
        return [f"q{i}" for i in range(5)]

    def scoring(
        build_id: str, *, steps: object, con: duckdb.DuckDBPyConnection, ctx: JobContext
    ) -> Report:
        fakes.order.append("scoring")
        fakes.cons.append(con)
        fakes.scoring_args.append((build_id, steps, ctx))
        con.execute("SELECT 1").fetchall()  # still open
        return Report(name="scoring", records=3)

    monkeypatch.setattr(facts, "materialize_facts", materialize)
    monkeypatch.setattr(stages, "_load_run_scoring", lambda: scoring)
    return fakes


def test_it02_26_score_uses_hooks_on_build_connection(
    env: Env, fake_job_context: Callable[..., FakeJobContext], monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT02-26 `materialize_facts` (no open transaction) then `run_scoring(build_id,
    steps=..., con=<the build connection>, ctx=ctx)`; no 400 file rendered by the runner;
    the build connection is opened once and never reopened."""
    enrich = _use(monkeypatch, FakeEnrichment())
    fakes = _score_fakes(monkeypatch)
    rendered: list[str] = []
    opened: list[bool] = []
    render, open_rw = build.render_sql, build.open_for_build

    def record_render(file: object, context: object) -> str:
        rendered.append(file.name)  # type: ignore[attr-defined]
        return render(file, context)  # type: ignore[arg-type]

    def record_open(build_id: str, *, create: bool, **kw: object) -> duckdb.DuckDBPyConnection:
        opened.append(create)
        return open_rw(build_id, create=create, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(build, "render_sql", record_render)
    monkeypatch.setattr(build, "open_for_build", record_open)
    payload = {"stages": ["build", "enrich", "score"], "score_steps": ["compute", "rank"]}
    ctx = fake_job_context(payload)
    outcome = _done(ctx)
    build_id = str(outcome.result["build_id"])
    assert fakes.order == ["facts", "scoring"]
    assert fakes.scoring_args == [(build_id, ["compute", "rank"], ctx)]
    assert all(con is enrich.calls[0].con for con in fakes.cons)
    assert opened == [True]
    assert "400_facts.sql" not in rendered
    assert {"300_attach_decisions.sql", "310_attach_clusters.sql"} <= set(rendered)
    assert outcome.result["facts_queries"] == 5
    assert outcome.result["scoring"] == {"name": "scoring", "records": 3}
    assert ctx.load_state()["stages_done"] == ["build", "enrich", "score"]


def test_it02_26_score_only_on_existing_build(
    env: Env, fake_job_context: Callable[..., FakeJobContext], monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT02-26 stage `score` alone on a completed build: one writable open (`create=False`),
    `steps=None` passed through, the connection kept for both hooks."""
    _use(monkeypatch, FakeEnrichment())
    build_id = str(_done(fake_job_context({"stages": ["build", "enrich"]})).result["build_id"])
    fakes = _score_fakes(monkeypatch)
    opened: list[bool] = []
    open_rw = build.open_for_build

    def record_open(bid: str, *, create: bool, **kw: object) -> duckdb.DuckDBPyConnection:
        opened.append(create)
        return open_rw(bid, create=create, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(build, "open_for_build", record_open)
    ctx = fake_job_context({"stages": ["score"], "build_id": build_id})
    outcome = _done(ctx)
    assert opened == [False]
    assert fakes.scoring_args == [(build_id, None, ctx)]
    assert fakes.cons[0] is fakes.cons[1]
    assert outcome.result["facts_queries"] == 5


def test_it02_26_real_facts_hook_on_lake_small(
    env: Env, fake_job_context: Callable[..., FakeJobContext], monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT02-26 (U02-126) the real `materialize_facts` runs 400_facts.sql on the build
    connection after enrich (no open transaction) and fills the five fact tables."""
    _use(monkeypatch, FakeEnrichment())
    _score_fakes(monkeypatch, real_facts=True)
    outcome = _done(fake_job_context({"stages": ["build", "enrich", "score"]}))
    build_id = str(outcome.result["build_id"])
    assert outcome.result["facts_queries"] == 5
    tables = _query(
        env,
        build_id,
        "SELECT table_name FROM duckdb_tables() WHERE schema_name = 'metrics' ORDER BY 1",
    )
    assert {"incident_fact", "change_fact", "work_item_fact", "org_closure"} <= {
        t[0] for t in tables
    }
    assert _query(env, build_id, "SELECT count(*) FROM metrics.incident_fact") == [(4,)]


def _copy_prev(wh: duckdb.DuckDBPyConnection, prev_id: str, layout: DataLayout) -> None:
    """Carry every enrich row of the previous build forward (what a cache hit would do)."""
    with warehouse.open_readonly(prev_id, layout=layout) as old:
        for table, _column in _ENRICH_IDS[:4]:
            rows = old.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608
            for row in rows:
                marks = ", ".join("?" * len(row))
                wh.execute(f"INSERT INTO {table} VALUES ({marks})", list(row))  # noqa: S608
        clusters = old.execute("SELECT cluster_id FROM enrich.cluster").fetchall()
    for (cluster_id,) in clusters:
        wh.execute("INSERT INTO enrich.cluster (cluster_id) VALUES (?)", [cluster_id])


def test_st02_16_deleted_record_absent_after_next_build(
    env: Env, fake_job_context: Callable[..., FakeJobContext], monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST02-16 a record with lake rows and enrich rows in the previous build, then a `done`
    deletion request: the next build has it in no `stg` data table, not in `core`, not in
    `enrich` (the vector store is impl 03's and not part of the build file)."""
    _use(monkeypatch, FakeEnrichment(_enrich_rows(LIVE, DELETED)))
    first = str(_done(fake_job_context({"stages": ["build", "enrich"]})).result["build_id"])
    assert DELETED in _ids(env, first, "enrich.decision")
    write_current(first, layout=env.layout)
    now = datetime.datetime(2026, 9, 2, tzinfo=datetime.UTC)
    request = ops.create_deletion_request(
        record_id=DELETED, requested_by="ab" * 16, reason_ref="REQ-2", now=now
    )
    ops.set_deletion_status(request.request_id, "running")
    ops.set_deletion_status(request.request_id, "done", completed_at=now)
    fake = _use(
        monkeypatch, FakeEnrichment(writer=lambda wh, _prev: _copy_prev(wh, first, env.layout))
    )
    second = str(_done(fake_job_context({"stages": ["build", "enrich"]})).result["build_id"])
    assert fake.calls[0].prev_warehouse == warehouse.build_path(first, layout=env.layout)
    assert DELETED not in _ids(env, second, "core.incident")
    for table, column in _ENRICH_IDS:
        found = _ids(env, second, table, column)
        assert DELETED not in found, table
        assert LIVE in found, table
    stg_tables = _query(
        env,
        second,
        "SELECT table_name FROM duckdb_columns() WHERE schema_name = 'stg'"
        " AND column_name = 'record_id' AND table_name <> 'deleted_record'",
    )
    assert stg_tables
    for (table,) in stg_tables:
        assert DELETED not in _ids(env, second, f"stg.{table}"), table
    assert _count(env, second, "enrich_pruned") == [(4,)]
