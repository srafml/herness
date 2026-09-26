"""Build-range harness and fake job context (impl 02 §11, created in T02-12).

`build_harness` renders and runs a range of build SQL files on a temp DuckDB build file
with a given lake inventory and reference data, the way the pipeline does: files below 100
first, then the reference tables (U02-87), then the rest (U02-97 step 5). The connection
comes from `open_for_build` with an explicit byte `memory_limit` (DuckDB 1.5 rejects the
`75%` default of `BuildSettings`) and two threads.

`fake_job_context` returns a factory of `FakeJobContext`, an in-memory fake of
`herness.core.jobs.JobContext` (T08-03) that records heartbeats, GPU requests, GPU scopes,
service calls and saved state, and yields on request or after a number of checks.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from pathlib import Path

import duckdb
import pytest
from pydantic import JsonValue

from herness.core.jobs import JobContext, JobRow
from herness.core.jobs.ports import StopReason
from herness.core.types import GpuClass, JobKind, ServiceName
from herness.model.lakeinfo import LakeInventory
from herness.model.refdata import register_prev_row_counts, register_reference_tables
from herness.model.render_context import RenderContext
from herness.model.settings import BuildSettings, CustomFieldsConfig, DqSettings, MappingsConfig
from herness.model.sqlfiles import SqlFile, discover_sql_files, render_sql
from herness.store._warehouse_rw import open_for_build
from herness.store.layout import DataLayout
from herness.store.ops import ReviewItem

HARNESS_BUILD_ID = "20260901-120000-01ABCD"
HARNESS_BUILD_SETTINGS = BuildSettings(memory_limit="1GB", threads=2)
_REFDATA_BEFORE = 100  # the pipeline registers reference tables after the setup stage


@dataclasses.dataclass(frozen=True)
class RefData:
    """Reference data handed to `register_reference_tables` (and prev row counts)."""

    mappings: MappingsConfig = dataclasses.field(default_factory=MappingsConfig)
    build_cfg: BuildSettings = HARNESS_BUILD_SETTINGS
    deleted_ids: Collection[str] = ()
    approved: Sequence[ReviewItem] = ()
    prev_row_counts: Mapping[str, int] | None = None


class BuildHarness:
    """One temp build file; `run(lo, hi)` renders and executes files numbered lo..hi."""

    def __init__(self, root: Path, *, build_id: str = HARNESS_BUILD_ID) -> None:
        self.layout = DataLayout.from_root(root)
        self.layout.raw.mkdir(parents=True, exist_ok=True)
        self.build_id = build_id
        self._con: duckdb.DuckDBPyConnection | None = None
        self.refdata_counts: dict[str, int] | None = None
        self.executed: list[str] = []

    @property
    def con(self) -> duckdb.DuckDBPyConnection:
        """The writable build connection (created on first use)."""
        if self._con is None:
            self._con = open_for_build(
                self.build_id, create=True, cfg=HARNESS_BUILD_SETTINGS, layout=self.layout
            )
        return self._con

    def context(
        self,
        inventory: LakeInventory | None = None,
        *,
        dq: DqSettings | None = None,
        custom_fields: CustomFieldsConfig | None = None,
        extra_entities: Mapping[str, tuple[str, ...]] | None = None,
    ) -> RenderContext:
        """A render context; the default inventory is an empty lake under the temp root."""
        return RenderContext(
            build_id=self.build_id,
            dq=dq if dq is not None else DqSettings(),
            custom_fields=custom_fields if custom_fields is not None else CustomFieldsConfig(),
            lake=inventory if inventory is not None else self.empty_lake(),
            extra_entities=extra_entities or {},
        )

    def empty_lake(self) -> LakeInventory:
        return LakeInventory(root=self.layout.raw, entities={})

    def files(self, lo: int, hi: int, *, sql_dir: Path | None = None) -> list[SqlFile]:
        return [f for f in discover_sql_files(sql_dir=sql_dir) if lo <= f.number <= hi]

    def render(
        self, lo: int, hi: int, *, context: RenderContext | None = None, sql_dir: Path | None = None
    ) -> dict[str, str]:
        """Rendered SQL of files lo..hi by file name, without running anything."""
        ctx = context if context is not None else self.context()
        return {f.name: render_sql(f, ctx) for f in self.files(lo, hi, sql_dir=sql_dir)}

    def register(self, refdata: RefData) -> dict[str, int]:
        """Register the reference tables (and `stg.prev_row_counts`) on the build file."""
        counts = register_reference_tables(
            self.con,
            mappings=refdata.mappings,
            build_cfg=refdata.build_cfg,
            deleted_ids=refdata.deleted_ids,
            approved=refdata.approved,
        )
        register_prev_row_counts(self.con, refdata.prev_row_counts)
        self.refdata_counts = counts
        return counts

    def execute_sql(self, sql: str) -> None:
        for statement in self.con.extract_statements(sql):
            self.con.execute(statement.query)

    def run(
        self,
        lo: int,
        hi: int,
        *,
        inventory: LakeInventory | None = None,
        refdata: RefData | None = None,
        context: RenderContext | None = None,
        sql_dir: Path | None = None,
    ) -> list[str]:
        """Render and run files lo..hi in order; `refdata` is registered before file 100.

        Returns the names of the files run (also appended to `executed`). Pass either
        `context` or `inventory`, not both (ValueError).
        """
        if context is not None and inventory is not None:
            msg = "pass either context or inventory, not both"
            raise ValueError(msg)
        ctx = context if context is not None else self.context(inventory)
        pending = refdata
        ran: list[str] = []
        for file in self.files(lo, hi, sql_dir=sql_dir):
            if pending is not None and file.number >= _REFDATA_BEFORE:
                self.register(pending)
                pending = None
            self.execute_sql(render_sql(file, ctx))
            ran.append(file.name)
        if pending is not None:
            self.register(pending)
        self.executed.extend(ran)
        return ran

    def query(self, sql: str, params: Sequence[object] | None = None) -> list[tuple[object, ...]]:
        return self.con.execute(sql, params).fetchall()

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None


@pytest.fixture
def build_harness(tmp_path: Path) -> Iterator[BuildHarness]:
    """A `BuildHarness` on `tmp_path/data` (impl 02 §11); closed after the test."""
    harness = BuildHarness(tmp_path / "data")
    try:
        yield harness
    finally:
        harness.close()


# --- fake job context (JobContext of T08-03) ---------------------------------------------


class FakeServiceControl:
    """`ServiceControl` recording start/stop calls; a started service is healthy."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ServiceName]] = []
        self.running: set[ServiceName] = set()

    def start(self, name: ServiceName, *, timeout_s: float | None = None) -> None:
        self.calls.append(("start", name))
        self.running.add(name)

    def stop(self, name: ServiceName) -> None:
        self.calls.append(("stop", name))
        self.running.discard(name)

    def healthy(self, name: ServiceName) -> bool:
        return name in self.running


class FakeJobContext:
    """In-memory `JobContext`: state round-trips through JSON like the real backend.

    `yield_after=n` makes `should_yield()` return False n times, then True (with
    `stop_reason` "preempt"); `request_yield()` makes it return True from now on.
    """

    def __init__(
        self,
        payload: Mapping[str, JsonValue] | None = None,
        *,
        kind: JobKind = "build_pipeline",
        job_id: str = "job-0001",
        attempt: int = 1,
        yield_after: int | None = None,
        state: Mapping[str, JsonValue] | None = None,
    ) -> None:
        self._job = JobRow(
            job_id=job_id,
            kind=kind,
            gpu_class="none",
            status="running",
            priority=50,
            attempts=attempt,
            max_attempts=3,
            payload=dict(payload or {}),
        )
        self._services = FakeServiceControl()
        self._stop_reason: StopReason | None = None
        self.yield_after = yield_after
        self.yield_checks = 0
        self.heartbeats: list[str | None] = []
        self.gpu_requests: list[GpuClass] = []
        self.gpu_scopes: list[GpuClass] = []
        self.current_class: GpuClass = "none"
        self.saved_states: list[dict[str, JsonValue]] = []
        self._state: str = json.dumps(dict(state or {}))

    @property
    def job(self) -> JobRow:
        return self._job

    @property
    def job_id(self) -> str:
        return self._job.job_id

    @property
    def kind(self) -> JobKind:
        return self._job.kind

    @property
    def attempt(self) -> int:
        return self._job.attempts

    @property
    def services(self) -> FakeServiceControl:
        return self._services

    @property
    def stop_reason(self) -> StopReason | None:
        return self._stop_reason

    def request_yield(self, reason: StopReason = "preempt") -> None:
        self._stop_reason = reason

    def should_yield(self) -> bool:
        self.yield_checks += 1
        if self.yield_after is not None and self.yield_checks > self.yield_after:
            self._stop_reason = self._stop_reason or "preempt"
        return self._stop_reason is not None

    def heartbeat(self, note: str | None = None) -> None:
        self.heartbeats.append(note)

    def require_gpu_class(self, cls: GpuClass, *, timeout_s: float | None = None) -> None:
        self.gpu_requests.append(cls)

    @contextlib.contextmanager
    def gpu_scope(self, cls: GpuClass) -> Iterator[None]:
        self.gpu_scopes.append(cls)
        previous, self.current_class = self.current_class, cls
        try:
            yield
        finally:
            self.current_class = previous

    def save_state(self, state: dict[str, JsonValue]) -> None:
        self._state = json.dumps(state)
        self.saved_states.append(json.loads(self._state))

    def load_state(self) -> dict[str, JsonValue]:
        loaded: dict[str, JsonValue] = json.loads(self._state)
        return loaded


def _conforms(ctx: FakeJobContext) -> JobContext:
    """Static check (mypy) that the fake satisfies the JobContext protocol."""
    return ctx


@pytest.fixture
def fake_job_context() -> Callable[..., FakeJobContext]:
    """Factory of `FakeJobContext` (impl 02 §11): `fake_job_context(payload, **options)`."""
    return FakeJobContext
