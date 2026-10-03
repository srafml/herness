"""Shared helpers for the outcome job tests (T07-18): planted warehouse, deps, seeds, job ctx.

Not a test module. Spec 11's `tiny_build` is not in the tree, so the warehouse is the
`metrics_tiny` DDL (T04 fixture support) without its rows, plus one planted incident per
service per week whose resolution hours come from a caller function; facts are materialized
with the real spec 04 code and `compute_metric` / `peer_group` read the shipped catalog (MTTR
minimum sample size lowered to 1). Ops rows go through the real closed-loop writers on the
migrated `ops_store` of the test; the writer is the real MemoryWriter of `_write_env`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import duckdb
import pytest
from freezegun import freeze_time
from pydantic import JsonValue
from tests.support.metrics_tiny import (
    BUILD_ID,
    build_metrics_tiny,
    patch_facts_config,
    shipped_catalog,
    tiny_weights,
)
from tests.unit.harness.memory._episodic_env import rec_row, seed_decision, seed_recs, seed_run
from tests.unit.harness.memory._write_env import Env, make_writer

from herness.core import time as clock
from herness.core.types import JobOutcome
from herness.harness.memory import outcome
from herness.harness.memory.outcome import OutcomeDeps
from herness.harness.memory.settings import MemoryConfig, OutcomeConfig
from herness.metrics import compute, peers
from herness.metrics.catalog import MetricCatalog
from herness.metrics.compute import compute_metric
from herness.metrics.facts import materialize_facts
from herness.store.ops import core

METRIC = "mttr_hours"
EFFECTIVE = datetime(2026, 1, 5, tzinfo=UTC)  # a Monday: weeks align with the windows
NOW = datetime(2026, 4, 1, 12, tzinfo=UTC)  # measurement 1 (12 weeks) is due, 2 is not
PRE = (date(2025, 10, 27), date(2026, 1, 5))
POST = (date(2026, 1, 19), date(2026, 3, 30))
FIRST_WEEK = date(2024, 9, 30)  # the prior-year windows are covered too
LAST_WEEK = date(2026, 3, 23)
CFG_HASH = "cfg_" + "0" * 16

type Hours = Callable[[str, date], float | None]


def flat(_sid: str, _week: date) -> float:
    """10 hours every week."""
    return 10.0


def improves(by: float = 0.2, *, start: date = FIRST_WEEK) -> Hours:
    """10 hours (+-0.25 alternating weekly), `10 * (1 - by)` from the effective week on;
    nothing before `start`."""

    def hours(_sid: str, week: date) -> float | None:
        if week < start:
            return None
        jitter = 0.25 if (week - FIRST_WEEK).days // 7 % 2 else -0.25
        return (10.0 * (1 - by) if week >= EFFECTIVE.date() else 10.0) + jitter

    return hours


def spy_compute(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record the entity ids of every outcome `compute_metric` call."""
    calls: list[list[str]] = []
    real = compute_metric

    def spy(*args: Any, **kw: Any) -> Any:
        calls.append(list(args[2]))
        return real(*args, **kw)

    monkeypatch.setattr(outcome, "compute_metric", spy)
    return calls


def weeks() -> Iterable[date]:
    week = FIRST_WEEK
    while week <= LAST_WEEK:
        yield week
        week += timedelta(weeks=1)


def plant(hours: dict[str, Hours], *, crit: int = 1, extra: Iterable[str] = ()) -> Any:
    """In-memory warehouse: services `hours` (criticality `crit`) with weekly incidents."""
    con = build_metrics_tiny(rows=False)
    for sid in [*hours, *extra]:
        con.execute("INSERT INTO core.service (service_id, criticality) VALUES (?, ?)",
                    [sid, crit])  # fmt: skip
    n = 0
    for sid, fn in hours.items():
        for week in weeks():
            value = fn(sid, week)
            if value is None:
                continue
            n += 1
            resolved = datetime.combine(week + timedelta(days=2), datetime.min.time(), UTC)
            resolved += timedelta(hours=15)
            con.execute(
                "INSERT INTO core.incident (record_id, number, opened_at, resolved_at, priority,"
                " state, service_id) VALUES (?, ?, ?, ?, 3, 'closed', ?)",
                [f"I{n}", f"INC{n:06d}", resolved - timedelta(hours=value), resolved, sid],
            )
    return con


def use_catalog(monkeypatch: pytest.MonkeyPatch, **min_service_peers: int) -> MetricCatalog:
    """Point spec 04 compute and peers at the shipped catalog (MTTR min sample 1)."""
    cfg = shipped_catalog(**{METRIC: 1}).config
    if min_service_peers:
        pg = cfg.scoring.peer_group.model_copy(update=min_service_peers)
        cfg = cfg.model_copy(update={"scoring": cfg.scoring.model_copy(update={"peer_group": pg})})
    catalog = MetricCatalog(cfg)
    weights = SimpleNamespace(weights=tiny_weights())
    for module in (compute, peers):
        monkeypatch.setattr(module, "catalog_from_config", lambda: catalog)
        monkeypatch.setattr(module, "get_config", lambda: weights)
    return catalog


def materialize(monkeypatch: pytest.MonkeyPatch, con: duckdb.DuckDBPyConnection) -> None:
    patch_facts_config(monkeypatch)
    with freeze_time("2026-04-01 06:30:00"):
        materialize_facts(con, BUILD_ID)


@dataclass
class OutcomeEnv:
    """Configured deps plus handles on the fakes and the warehouse."""

    deps: OutcomeDeps
    env: Env
    catalog: MetricCatalog
    con: duckdb.DuckDBPyConnection
    opened: int = 0
    evidence: list[str] = field(default_factory=list)


def make_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, con: duckdb.DuckDBPyConnection,
    cfg: OutcomeConfig | None = None, **min_service_peers: int,
) -> OutcomeEnv:  # fmt: skip
    """Deps on the test's ops store and `con`, configured into the module seam; time is NOW."""
    monkeypatch.setattr(clock, "now", lambda: NOW)
    catalog = use_catalog(monkeypatch, **min_service_peers)
    materialize(monkeypatch, con)
    env = make_writer(tmp_path)
    box: dict[str, OutcomeEnv] = {}

    def open_current() -> duckdb.DuckDBPyConnection:
        box["env"].opened += 1
        return con.cursor()

    def record(ev: Any) -> bool:
        box["env"].evidence.append(ev.query_id)
        from herness.store import ops  # noqa: PLC0415 - the real writer, recorded

        return ops.record_evidence(ev)

    deps = OutcomeDeps(
        writer=env.writer, outcome=cfg or MemoryConfig().outcome, config_hash=lambda: CFG_HASH,
        open_current=open_current, load_catalog=lambda: catalog, record_evidence=record,
    )  # fmt: skip
    box["env"] = OutcomeEnv(deps, env, catalog, con)
    outcome.configure_outcome(deps)
    return box["env"]


def accepted_rec(
    target_id: str, *, metric: str | None = METRIC, target_type: str = "service",
    decision: str = "accepted", effective: datetime = EFFECTIVE, **fields: Any,
) -> str:  # fmt: skip
    """One recommendation on `target_id` with a decision effective at `effective`."""
    run_id = seed_run(started=effective - timedelta(days=3))
    row = rec_row(run_id, effective - timedelta(days=2), target_id=target_id,
                  target_type=target_type, expected_metric=metric, **fields)  # fmt: skip
    [rec_id] = seed_recs([row])
    seed_decision(rec_id, decision, effective - timedelta(days=1), effective)
    return rec_id


@dataclass
class FakeJob:
    job_id: str = "job_outcome"
    payload: dict[str, JsonValue] = field(default_factory=lambda: {"sweep": True})


@dataclass
class FakeCtx:
    """A structural `JobContext`: records heartbeats; yields once `yield_after` were taken."""

    job: FakeJob = field(default_factory=FakeJob)
    yield_after: int | None = None
    notes: list[str | None] = field(default_factory=list)
    kind: str = "outcome_measure"

    @property
    def job_id(self) -> str:
        return self.job.job_id

    def should_yield(self) -> bool:
        return self.yield_after is not None and len(self.notes) >= self.yield_after

    def heartbeat(self, note: str | None = None) -> None:
        self.notes.append(note)


def run(payload: dict[str, JsonValue] | None = None, **kw: Any) -> tuple[JobOutcome, FakeCtx]:
    """Run the handler once with `payload` (default: a sweep)."""
    ctx = FakeCtx(job=FakeJob(payload=payload if payload is not None else {"sweep": True}), **kw)
    return outcome.outcome_measure_handler(ctx), ctx  # type: ignore[arg-type]


def outcomes() -> list[dict[str, Any]]:
    """Every `outcome` row with parsed details, by rec_id and measurement."""
    rows = core.read_all("SELECT * FROM outcome ORDER BY rec_id, measurement", ())
    return [dict(r) | {"details": core.load_json(r["details"], field="d")} for r in rows]


def summaries() -> list[dict[str, Any]]:
    """Every outcome_summary memory item with parsed data and provenance."""
    rows = core.read_all("SELECT * FROM memory_item WHERE kind = 'outcome_summary'", ())
    return [
        dict(r) | {"data": core.load_json(r["data"], field="d"),
                   "provenance": core.load_json(r["provenance"], field="p")}
        for r in rows
    ]  # fmt: skip
