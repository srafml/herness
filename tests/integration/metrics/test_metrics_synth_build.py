"""Integration tests of stage 400 and `run_scoring` on a scored synthetic build (impl 04 T04-22).

One `tiny` synthetic root (seed 42) is generated, built with the real `build_pipeline`
stage `build`, given the truth-planted enrichment and scored with default steps
(`tests.support.metrics_synth`). The scored build file `B` stays the build writer's file;
a byte copy is promoted as build `P` (`CURRENT`), so every read-only test reads `P` and
IT04-07 can write `B`.

IT04-01 re-runs every `meta.evidence` query; IT04-02 resolves every stored `query_id`;
IT04-03 `metric_series` vs `compute_metric`; IT04-04 `peer_group` vs `score.org`; IT04-05
five portfolio solves; IT04-06 `result_sample`; IT04-07 `persist=False` vs `persist=True`;
IT04-09 `compute_metric(con=None)` leaves the promoted file untouched (TH04-06 evidence).
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb
import pytest
from tests.support.metrics_synth import ScoredBuild, SynthEnv, generate_root, scored_build

from herness.core import time as clock
from herness.metrics.compute import compute_metric, metric_series
from herness.metrics.evidence import (
    RESULT_SAMPLE_LIMIT,
    result_hash,
    result_sample,
    rows_equivalent,
)
from herness.metrics.peers import peer_group
from herness.metrics.portfolio import optimize_portfolio
from herness.store import warehouse
from herness.store._warehouse_rw import write_current
from herness.store.ops import reset_connections

pytestmark = pytest.mark.integration

SEED = 42
SCENARIO = "base"
Columns = list[tuple[str, str]]


@dataclasses.dataclass(frozen=True)
class Scored:
    """The scored synthetic build `B` and its promoted copy `P`."""

    env: SynthEnv
    build: ScoredBuild
    promoted_id: str

    @property
    def build_id(self) -> str:
        return self.build.build_id

    def path(self, build_id: str) -> Path:
        return warehouse.build_path(build_id, layout=self.env.layout)


@dataclasses.dataclass(frozen=True)
class Rerun:
    """One `meta.evidence` row and the result of re-running its SQL with its binds."""

    query_id: str
    stored_hash: str
    stored_count: int
    stored_sample: list[dict[str, Any]]
    columns: Columns
    rows: list[tuple[Any, ...]]


@pytest.fixture(scope="module")
def scored(tmp_path_factory: pytest.TempPathFactory) -> Scored:
    """Generate, build, plant, score; promote a byte copy of the build file."""
    base = tmp_path_factory.mktemp("it04")
    root = generate_root(SEED, "tiny", base / "synth")
    env = SynthEnv.create(root, base / "work")
    build = scored_build(env, promote=False)
    promoted = warehouse.new_build_id(clock.now())
    shutil.copyfile(
        warehouse.build_path(build.build_id, layout=env.layout),
        warehouse.build_path(promoted, layout=env.layout),
    )
    write_current(promoted, layout=env.layout)
    reset_connections()
    return Scored(env, build, promoted)


@pytest.fixture
def active(scored: Scored) -> Iterator[Scored]:
    """`scored` with its config loaded and the ops store pointed at its work folder."""
    scored.env.activate()
    yield scored
    reset_connections()


@pytest.fixture
def ro(active: Scored) -> Iterator[duckdb.DuckDBPyConnection]:
    """A read-only connection of the promoted build `P`."""
    con = warehouse.open_readonly(active.promoted_id, layout=active.env.layout)
    yield con
    con.close()


def _columns(res: duckdb.DuckDBPyConnection) -> Columns:
    return [(str(d[0]), str(d[1])) for d in res.description or ()]


@pytest.fixture(scope="module")
def reruns(scored: Scored) -> list[Rerun]:
    """Every `meta.evidence` query of the promoted build re-run with its stored binds."""
    con = warehouse.open_readonly(scored.promoted_id, layout=scored.env.layout)
    try:
        stored = con.execute(
            "SELECT query_id, sql, params, result_hash, row_count, result_sample"
            " FROM meta.evidence ORDER BY query_id"
        ).fetchall()
        out = []
        for qid, sql, params, digest, count, sample in stored:
            res = con.execute(sql, json.loads(params)["bind"])
            columns = _columns(res)
            out.append(Rerun(qid, digest, count, json.loads(sample), columns, res.fetchall()))
    finally:
        con.close()
    assert out, "the scored build recorded no evidence"
    return out


def _stored_rows(con: duckdb.DuckDBPyConnection, qid: str) -> list[tuple[Any, ...]] | None:
    """Rows a materialized query stored (table with a `query_id` column), else None."""
    tables = con.execute(
        "SELECT schema_name || '.' || table_name FROM duckdb_columns()"
        " WHERE column_name = 'query_id' AND schema_name IN ('metrics', 'score')"
    ).fetchall()
    for (table,) in tables:
        rows = con.execute(
            f"SELECT * EXCLUDE (query_id) FROM {table} WHERE query_id = ?",  # noqa: S608 - catalog
            [qid],
        ).fetchall()
        if rows:
            return rows
    return None


def test_it04_01_every_evidence_query_reproduces(
    reruns: list[Rerun], ro: duckdb.DuckDBPyConnection
) -> None:
    """IT04-01 stage 400 + `run_scoring` on a synthetic build: re-running every
    `meta.evidence` query gives its recorded `result_hash`, or rows equivalent to the
    stored rows under the Verifier tolerance."""
    mismatched = []
    for rerun in reruns:
        if result_hash(rerun.columns, rerun.rows) == rerun.stored_hash:
            assert len(rerun.rows) == rerun.stored_count
            continue
        stored = _stored_rows(ro, rerun.query_id)
        if stored is None or not rows_equivalent(rerun.columns, rerun.rows, stored):
            mismatched.append(rerun.query_id)
    assert mismatched == []
    producers = {r[0] for r in ro.execute("SELECT DISTINCT producer FROM meta.evidence").fetchall()}
    assert {"facts", "metrics", "score"} <= producers


def _stored_query_ids(con: duckdb.DuckDBPyConnection) -> dict[str, set[str]]:
    cols = con.execute(
        "SELECT schema_name || '.' || table_name, column_name FROM duckdb_columns()"
        " WHERE schema_name IN ('metrics', 'score') AND column_name IN ('query_id', 'query_ids')"
        " ORDER BY ALL"
    ).fetchall()
    out: dict[str, set[str]] = {}
    for table, column in cols:
        expr = "unnest(query_ids)" if column == "query_ids" else "query_id"
        rows = con.execute(f"SELECT DISTINCT {expr} FROM {table}").fetchall()  # noqa: S608
        out[f"{table}.{column}"] = {str(r[0]) for r in rows if r[0] is not None}
    return out


def test_it04_02_every_stored_query_id_has_evidence(ro: duckdb.DuckDBPyConnection) -> None:
    """IT04-02 every `query_id` stored in a `metrics.*` / `score.*` table exists in
    `meta.evidence` of the same build."""
    evidence = {str(r[0]) for r in ro.execute("SELECT query_id FROM meta.evidence").fetchall()}
    stored = _stored_query_ids(ro)
    for name in ("metrics.incident_fact.query_id", "score.funding.query_ids"):
        assert stored.get(name), f"{name} holds no query id"
    missing = {name: sorted(ids - evidence) for name, ids in stored.items() if ids - evidence}
    assert missing == {}


_SERIES = (("mttr_hours", "team"), ("incident_count", "service"), ("change_failure_rate", "org"))


@pytest.mark.parametrize(("metric", "grain"), _SERIES)
def test_it04_03_metric_series_equals_compute_metric(
    active: Scored, ro: duckdb.DuckDBPyConnection, metric: str, grain: Any
) -> None:
    """IT04-03 `metric_series` returns exactly one `query_id`, and its rows equal
    `compute_metric` over the same window (same query, same id)."""
    end = active.env.truth.end + datetime.timedelta(days=1)
    start = active.env.truth.start
    seen: list[str] = []
    rows, qid = metric_series(
        metric,
        grain,
        None,
        start,
        end,
        "week",
        con=ro,
        on_evidence=lambda rq: seen.append(rq.query_id),
    )
    direct = compute_metric(metric, grain, None, "week", window=(start, end), con=ro)
    assert rows, f"{metric} by {grain} has no rows"
    assert rows == direct.rows
    assert qid == direct.query_id
    assert seen == [qid]


def test_it04_04_peer_group_matches_score_org(
    active: Scored, ro: duckdb.DuckDBPyConnection
) -> None:
    """IT04-04 for every team / org entity and scorecard metric of `score.org`,
    `peer_group(entity_type, entity_id, metric=m).key` equals `score.org.peer_group`."""
    rows = ro.execute(
        "SELECT entity_type, entity_id, metric, peer_group FROM score.org ORDER BY ALL"
    ).fetchall()
    assert {r[0] for r in rows} == {"team", "org"}
    diffs = []
    for entity_type, entity_id, metric, key in rows:
        info = peer_group(entity_type, entity_id, metric=metric, con=ro)
        if info.key != key:
            diffs.append((entity_type, entity_id, metric, key, info.key))
    assert diffs == []


def _dump(result: Any) -> str:
    return json.dumps(result.model_dump(mode="json"), sort_keys=True)


def test_it04_05_portfolio_five_runs_identical(active: Scored) -> None:
    """IT04-05 `optimize_portfolio` five times on the same build and scenario gives the
    identical result, and the planted candidates make the selection non-empty."""
    dumps = {_dump(optimize_portfolio(SCENARIO, persist=False)) for _ in range(5)}
    assert len(dumps) == 1
    assert json.loads(dumps.pop())["selected"]


def test_it04_06_result_sample_is_first_rows_in_hash_order(reruns: list[Rerun]) -> None:
    """IT04-06 every `meta.evidence.result_sample` holds at most 50 rows, equal to the
    first rows of the result in the design 00 §5.1 row-digest order."""
    for rerun in reruns:
        assert len(rerun.stored_sample) <= RESULT_SAMPLE_LIMIT
        expected = json.loads(json.dumps(result_sample(rerun.columns, rerun.rows)))
        assert rerun.stored_sample == expected, rerun.query_id
    assert any(len(r.stored_sample) == RESULT_SAMPLE_LIMIT for r in reruns)


def _fingerprint(path: Path) -> tuple[int, int, str]:
    stat = path.stat()
    return stat.st_mtime_ns, stat.st_size, hashlib.sha256(path.read_bytes()).hexdigest()


def test_it04_07_persist_false_on_promoted_equals_persist_true_on_build(
    active: Scored,
) -> None:
    """IT04-07 `persist=False` on the promoted copy and `persist=True` on the build file
    select the same candidates; the promoted file is not written, the build file's
    `score.portfolio` holds the persisted selection."""
    promoted = active.path(active.promoted_id)
    before = _fingerprint(promoted)
    read_only = optimize_portfolio(SCENARIO, persist=False)
    assert _fingerprint(promoted) == before
    assert not promoted.with_name(promoted.name + ".wal").exists()
    con = active.env.open_build(active.build_id)
    try:
        persisted = optimize_portfolio(SCENARIO, persist=True, build_id=active.build_id, con=con)
        stored = con.execute(
            "SELECT candidate_id FROM score.portfolio WHERE scenario = ? AND selected"
            " ORDER BY order_rank",
            [SCENARIO],
        ).fetchall()
    finally:
        con.close()
    assert read_only.selected
    assert persisted.selected == read_only.selected
    assert [r[0] for r in stored] == read_only.selected
    assert _fingerprint(promoted) == before


def test_it04_09_compute_metric_on_current_is_read_only(active: Scored) -> None:
    """IT04-09 `compute_metric` with `con=None` opens the CURRENT warehouse read-only: the
    promoted file's mtime, size and bytes are unchanged and no WAL appears."""
    promoted = active.path(active.promoted_id)
    assert warehouse.read_current(layout=active.env.layout) == active.promoted_id
    before = _fingerprint(promoted)
    result = compute_metric("incident_count", "service", None, "month")
    assert result.build_id == active.build_id  # meta.build of the promoted copy
    assert result.rows
    assert _fingerprint(promoted) == before
    assert not promoted.with_name(promoted.name + ".wal").exists()
