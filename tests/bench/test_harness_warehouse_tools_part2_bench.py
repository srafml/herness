"""Warehouse tool benchmarks, part 2 (BT05-06 `semantic_search`, BT05-07 `get_metric`).

Spec 11's `full` synthetic build does not exist yet; both run on local "full-like" data
(re-point to `full` when spec 11 lands; carry-over). BT05-06: a real `herness.store.vectors`
`ticket_embedding` table of `VECTORS` random unit vectors searched by a LanceDB adapter
(`LanceVectors`, test-side: no production `VectorHandle` adapter exists yet), the real
`embed_query` on the CPU with the tiny sentence-transformers model of `tests/fixtures/models/
tiny-st` (bge-m3 is not in the test environment), and a stand-in warehouse whose
`enrich.text_redacted` holds a snippet for every vector. BT05-07: every shipped catalog metric
at team grain over 13 weeks on a `metrics_tiny`-schema build with `TEAMS` teams, `SERVICES`
services (owner and support `service_map` rows) and about two years of synthetic `INCIDENTS`
incidents, `EVENTS` alert events, `CHANGES` changes (+ `LINKS` incident-change links) and
`ITEMS` work items (epics with child stories, bugs and tasks, their transitions and
incident-mention links), so every timed metric computes over real rows. Run:
pytest -m "integration and slow" tests/bench.

Spec targets (impl 05 §10.1): "BT05-06 | `semantic_search` k = 20 | 100 queries on `full`
vectors, CPU embedding | p95 < 300 ms"; "BT05-07 | `get_metric` | every catalog metric, team
grain, 13 weeks, on `full` | p95 < 2 s". Method: warm-up calls, then the stated calls measured
`REPEATS` times; the gate is the median of the per-repeat p95s.
"""

from __future__ import annotations

import datetime
import statistics
import sys
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import duckdb
import numpy as np
import pyarrow as pa
import pytest
from freezegun import freeze_time
from lancedb.query import LanceVectorQueryBuilder
from lancedb.table import Table
from tests.support import tools_standin as sd
from tests.support import warehouse_read_build as rb
from tests.support.bench_stats import REPEATS, report
from tests.support.dispatch_standin import use_test_config
from tests.support.harness_fakes import FakeOps
from tests.support.make_tiny_st import TINY_ST
from tests.support.metrics_tiny import (
    BUILD_ID as TINY_BUILD_ID,
)
from tests.support.metrics_tiny import (
    build_metrics_tiny,
    patch_facts_config,
    shipped_catalog,
    tiny_weights,
)

from herness.core import config as c
from herness.core.resilience import ProcessState
from herness.core.types import SqlLimits, ToolContext, VectorHit
from herness.enrich import embed
from herness.harness import _warehouse_tools_metric as wm
from herness.harness import warehouse_tools as wt
from herness.harness.llm.settings import SqlSettings
from herness.harness.warehouse import DuckWarehouse, open_warehouse
from herness.metrics import compute
from herness.metrics.facts import materialize_facts
from herness.store.vectors import EMBEDDING_DIM, TICKET_EMBEDDING_SCHEMA, VectorStore

pytestmark = [pytest.mark.integration, pytest.mark.slow]

VECTORS = 100_000
QUERIES = 100
K = 20
INCIDENTS = 200_000
TEAMS = 50
SERVICES = 200
CALLS_PER_METRIC = 25
EVENTS = 400_000  # ~80 per team-week, 4/5 noise severities (alert_noise_ratio min sample 50)
CHANGES = 100_000  # ~20 deployed per team-week (change metrics min sample 10)
LINKS = 20_000
ITEMS = 160_000  # 1 epic per 8 items: ~4 epics and ~22 done items per team-week
_CHUNK = 20_000


def _p95(samples: list[float]) -> float:
    return statistics.quantiles(samples, n=20)[18]


# --- BT05-06 semantic_search -----------------------------------------------------------------


class LanceVectors:
    """`VectorHandle` over a LanceDB `ticket_embedding` table (cosine; test-side adapter)."""

    def __init__(self, table: Table) -> None:
        self._table = table

    def search_tickets(
        self, vector: Sequence[float], k: int, *, entity: str | None, service_id: str | None
    ) -> list[VectorHit]:
        builder = cast("LanceVectorQueryBuilder", self._table.search(list(vector)))
        query = builder.distance_type("cosine").limit(k)
        where = [f"{col} = '{val}'" for col, val in (("entity", entity), ("service_id", service_id))
                 if val is not None]  # fmt: skip
        if where:
            query = query.where(" AND ".join(where), prefilter=True)
        rows = query.select(["record_id", "entity", "service_id", "opened_at"]).to_list()
        return [
            VectorHit(
                record_id=r["record_id"],
                entity=r["entity"],
                service_id=r["service_id"],
                opened_at=r["opened_at"],
                similarity=1.0 - float(r["_distance"]),
            )
            for r in rows
        ]


def _write_vectors(store: VectorStore, model: str) -> Table:
    store.ensure_tables()
    table = store.table("ticket_embedding")
    rng = np.random.default_rng(7)
    opened = datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC)
    for start in range(0, VECTORS, _CHUNK):
        n = min(_CHUNK, VECTORS - start)
        vecs = rng.standard_normal((n, EMBEDDING_DIM), dtype=np.float32)
        vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
        ids = [f"sn:incident:{i}" for i in range(start, start + n)]
        batch = pa.table(
            {
                "record_id": ids,
                "entity": ["incident"] * n,
                "service_id": [f"svc_{i % SERVICES}" for i in range(start, start + n)],
                "opened_at": [
                    opened + datetime.timedelta(hours=i) for i in range(start, start + n)
                ],
                "content_hash": ["h"] * n,
                "model": [model] * n,
                "vector": pa.FixedSizeListArray.from_arrays(vecs.reshape(-1), EMBEDDING_DIM),
            },
            schema=TICKET_EMBEDDING_SCHEMA,
        )
        table.add(batch)
    return table


@pytest.fixture(scope="module")
def vector_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("bt05_06")
    encoder = embed.Encoder(TINY_ST, model_name="bge-m3")
    _write_vectors(VectorStore(root / "vectors"), encoder.model_id)
    path = rb.make_build(root / "wh")
    con = duckdb.connect(str(path))
    try:
        con.execute(
            "INSERT INTO enrich.text_redacted SELECT 'sn:incident:' || i,"
            " 'redacted ticket text ' || i || ' disk full on host' FROM range(10, $n) t(i)",
            {"n": VECTORS},
        )
    finally:
        con.close()
    sys.stderr.write(f"BT05-06 data: {VECTORS} vectors x {EMBEDDING_DIM}, {VECTORS} snippets\n")
    return root


@pytest.fixture
def search_ctx(
    vector_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reset_process_state: ProcessState,
) -> Iterator[ToolContext]:
    del reset_process_state
    use_test_config(tmp_path / "cfg")
    import torch  # noqa: PLC0415 - only this benchmark needs torch

    store = VectorStore(vector_dir / "vectors")
    encoder = embed.Encoder(TINY_ST, model_name="bge-m3")
    monkeypatch.setattr(embed, "get_encoder", lambda: encoder)
    monkeypatch.setattr(embed, "VectorStore", lambda: store)
    monkeypatch.setattr(embed._QueryState, "model_check", None)
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    monkeypatch.setattr(embed, "release_cuda", lambda: None)
    rb.patch_redaction(monkeypatch)
    handle = open_warehouse(rb.BUILD_ID, warehouse_dir=vector_dir / "wh", sql=SqlSettings())
    ctx = sd.make_ctx(handle, FakeOps())
    try:
        yield ctx.model_copy(update={"vectors": LanceVectors(store.table("ticket_embedding"))})
    finally:
        handle.close()
        c.reset_config()


def test_bt05_06_semantic_search_k20_p95(search_ctx: ToolContext) -> None:
    """BT05-06 100 semantic_search queries, k = 20, CPU embedding, on full-like vectors:
    p95 < 300 ms."""
    tool = wt.SemanticSearch()
    tool(search_ctx, text="warm up query", entity=None, service_id=None, k=K)  # model load
    p95s: list[float] = []
    for _ in range(REPEATS):
        seconds: list[float] = []
        for i in range(QUERIES):
            entity = "incident" if i % 2 else None
            start = time.perf_counter()
            result = tool(
                search_ctx, text=f"disk {i} full on host {i % 17}", entity=entity,
                service_id=None, k=K,
            )  # fmt: skip
            seconds.append(time.perf_counter() - start)
            assert result.ok
            assert result.row_count == K
        p95s.append(_p95(seconds) * 1000)
    p95_ms = report("BT05-06", "p95_semantic_search_k20", p95s, "ms", "< 300 ms")
    assert p95_ms < 300, f"semantic_search p95 {p95_ms:.1f} ms"


# --- BT05-07 get_metric ----------------------------------------------------------------------

_SYNTHETIC = (
    "INSERT INTO core.org VALUES ('O1', 'Root', NULL, NULL, 'servicenow'),"
    " ('O2', 'Ops', 'O1', NULL, 'servicenow')",
    "INSERT INTO core.team SELECT 'T' || i, 'Team ' || i, 'O2', 'servicenow', true"
    " FROM range($teams) t(i)",
    "INSERT INTO core.service (service_id, name, ci_class, criticality, org_id, source)"
    " SELECT 'S' || i, 'Service ' || i, 'cmdb_ci_service', 1 + i % 4, 'O2', 'servicenow'"
    " FROM range($services) t(i)",
    "INSERT INTO core.incident (record_id, number, opened_at, resolved_at, priority, state,"
    " service_id, team_id, reassignment_count, reopen_count, sla_breached, business_duration_s,"
    " customer_impact_minutes) SELECT 'I' || i, 'INC' || i,"
    " TIMESTAMPTZ '2024-04-01 00:00:00+00' + to_minutes(CAST(i * 5 AS BIGINT)),"
    " TIMESTAMPTZ '2024-04-01 00:00:00+00' + to_minutes(CAST(i * 5 + 30 + i % 600 AS BIGINT)),"
    " 1 + i % 5, CASE WHEN i % 50 = 0 THEN 'canceled' ELSE 'closed' END, 'S' || (i % $services),"
    " 'T' || (i % $teams), i % 3, i % 7 = 0, i % 11 = 0, 1800 + i % 600, i % 90"
    " FROM range($incidents) t(i)",
    "INSERT INTO core.service_map SELECT 'S' || i, 'T' || ((i + r) % $teams), NULL, NULL, 'O2',"
    " CASE r WHEN 0 THEN 'owner' ELSE 'support' END, 'cmdb', 1.0 - r * 0.5"
    " FROM range($services) t(i), range(2) u(r)",
    "INSERT INTO core.event SELECT 'E' || i, 'prometheus',"
    " TIMESTAMPTZ '2024-04-01 00:00:00+00' + to_seconds(CAST(i * 150 AS BIGINT)),"
    " 'S' || (i % $services), 'h' || (i % 997),"
    " ['critical', 'major', 'minor', 'warning', 'info'][1 + i % 5], 'alert ' || (i % 40),"
    " 'resolved', 'd' || i, 60 + i % 900, CASE WHEN i % 4 = 0 THEN 'I' || (i % $incidents) END"
    " FROM range($events) t(i)",
    "INSERT INTO core.change (record_id, number, state, risk, type, opened_at, actual_end,"
    " service_id, team_id, outcome) SELECT 'C' || i, 'CHG' || i, 'closed', 'moderate',"
    " CASE WHEN i % 10 = 0 THEN 'emergency' WHEN i % 3 = 0 THEN 'standard' ELSE 'normal' END,"
    " ts - to_hours(CAST(4 + i % 200 AS BIGINT)), ts, 'S' || (i % $services), 'T' || (i % $teams),"
    " CASE WHEN i % 20 = 0 THEN 'unsuccessful' WHEN i % 37 = 0 THEN 'backed_out'"
    " WHEN i % 50 = 1 THEN 'canceled' ELSE 'successful' END FROM (SELECT i,"
    " TIMESTAMPTZ '2024-04-01 00:00:00+00' + to_minutes(CAST(i * 10 AS BIGINT)) AS ts"
    " FROM range($changes) t(i))",
    "INSERT INTO enrich.incident_change_link SELECT 'I' || (i * 10), 'C' || (i * 5),"
    " 'time_window', CASE WHEN i % 4 = 0 THEN 0.5 ELSE 0.9 END FROM range($links) t(i)",
    "INSERT INTO core.work_item (record_id, key, type, parent_key, project, status,"
    " status_category, created_at, resolved_at, story_points, team_id, service_id)"
    " SELECT 'W' || i, 'K' || i, CASE WHEN i % 8 = 0 THEN 'epic'"
    " ELSE ['story', 'bug', 'task'][1 + i % 3] END,"
    " CASE WHEN i % 8 <> 0 THEN 'K' || (i - i % 8) END, 'P' || (i // 8 % $teams), cat, cat, ts,"
    " CASE WHEN cat = 'done' THEN ts + to_hours(done_h) END, 1 + i % 8,"
    " 'T' || (i // 8 % $teams), 'S' || (i // 8 % $services) FROM (SELECT i,"
    " TIMESTAMPTZ '2024-04-01 00:00:00+00' + to_seconds(CAST(i * 375 AS BIGINT)) AS ts,"
    " CAST(CASE WHEN i % 8 = 0 THEN 480 ELSE 72 + i % 13 * 24 END AS BIGINT) AS done_h,"
    " CASE WHEN i % 10 = 9 THEN 'in_progress' WHEN i % 10 = 7 THEN 'todo' ELSE 'done' END AS cat"
    " FROM range($items) t(i))",
    "INSERT INTO core.work_item_transition SELECT w.record_id, 'Open', 'In Progress', 'todo',"
    " 'in_progress', w.created_at + to_hours(CAST(CASE WHEN w.type = 'epic' THEN 96"
    " ELSE 24 + abs(hash(w.key)) % 48 END AS BIGINT)) FROM core.work_item w"
    " WHERE w.status_category <> 'todo' UNION ALL SELECT w.record_id, 'In Progress', 'Done',"
    " 'in_progress', 'done', w.resolved_at FROM core.work_item w WHERE w.status_category = 'done'",
    "INSERT INTO core.work_item_link SELECT 'K' || i, 'INC' || i, 'mentions_incident'"
    " FROM range($items) t(i) WHERE i % 15 = 1",
)


@pytest.fixture(scope="module")
def metrics_con() -> Iterator[duckdb.DuckDBPyConnection]:
    with pytest.MonkeyPatch.context() as mp:
        patch_facts_config(mp)
        con = build_metrics_tiny(rows=False)
        values = {
            "teams": TEAMS, "services": SERVICES, "incidents": INCIDENTS, "events": EVENTS,
            "changes": CHANGES, "links": LINKS, "items": ITEMS,
        }  # fmt: skip
        for statement in _SYNTHETIC:
            con.execute(statement, {k: v for k, v in values.items() if f"${k}" in statement})
        with freeze_time("2026-04-01 06:30:00"):
            materialize_facts(con, TINY_BUILD_ID)
    facts = cast(
        "tuple[int, int, int, int]",
        con.execute(
            "SELECT (SELECT count(*) FROM metrics.incident_fact),"
            " (SELECT count(*) FROM metrics.change_fact),"
            " (SELECT count(*) FROM metrics.work_item_fact),"
            " (SELECT count(*) FROM core.work_item_transition)"
        ).fetchone(),
    )
    sys.stderr.write(
        f"BT05-07 data: {TEAMS} teams, {SERVICES} services, {EVENTS} events, {LINKS} links;"
        f" facts: {facts[0]} incident, {facts[1]} change, {facts[2]} work_item;"
        f" {facts[3]} transitions\n"
    )
    yield con
    con.close()


def test_bt05_07_get_metric_team_13_weeks_p95(
    metrics_con: duckdb.DuckDBPyConnection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reset_process_state: ProcessState,
) -> None:
    """BT05-07 every catalog metric, team grain, 13 weeks: p95 < 2 s."""
    del reset_process_state
    use_test_config(tmp_path / "cfg")
    catalog = shipped_catalog()
    monkeypatch.setattr(compute, "get_config", lambda: SimpleNamespace(weights=tiny_weights()))
    monkeypatch.setattr(compute, "catalog_from_config", lambda: catalog)
    monkeypatch.setattr(wm, "catalog_from_config", lambda: catalog)
    handle = cast("DuckWarehouse", rb.MemoryWarehouse(metrics_con, TINY_BUILD_ID))
    ctx = sd.make_ctx(handle, FakeOps(), limits=SqlLimits(timeout_s=30.0))
    tool = wt.GetMetric()
    first_monday = datetime.date(2024, 7, 1)
    # "every catalog metric, team grain": every metric with the team grain. Two shipped metrics,
    # availability_pct and error_rate, have grains [service, org] (config/metrics.yaml, their
    # entries) and refuse team grain, so they are not timed.
    names = [name for name in catalog.names() if "team" in catalog.get(name).grains]
    assert set(catalog.names()) - set(names) == {"availability_pct", "error_rate"}
    # Every timed call must return rows (an empty result is cheaper than a real one and would
    # understate the p95); metrics with an empty result are listed on stderr, then fail.
    empty: set[str] = set()

    def one_pass() -> list[float]:
        seconds: list[float] = []
        for name in names:
            for i in range(CALLS_PER_METRIC):
                start = first_monday + datetime.timedelta(weeks=2 * i)
                args: dict[str, Any] = {
                    "name": name, "entity_type": "team", "entity_ids": None, "period": "week",
                    "start": start.isoformat(),
                    "end": (start + datetime.timedelta(weeks=13)).isoformat(), "filters": None,
                }  # fmt: skip
                began = time.perf_counter()
                result = tool(ctx, **args)
                seconds.append(time.perf_counter() - began)
                assert result.ok, result.content
                if not result.row_count:
                    empty.add(name)
        return seconds

    p95s: list[float] = []
    try:
        for name in names:  # warm-up: one call per metric, outside the samples
            warm = tool(ctx, name=name, entity_type="team", entity_ids=None, period="week",
                        start="2024-04-01", end="2024-07-01", filters=None)  # fmt: skip
            assert warm.ok, warm.content
        p95s.extend(_p95(one_pass()) * 1000 for _ in range(REPEATS))
    finally:
        c.reset_config()
    sys.stderr.write(
        f"BT05-07 {len(names)} metrics x {CALLS_PER_METRIC} calls per repeat;"
        f" metrics with an empty result: {sorted(empty)}\n"
    )
    assert not empty, f"metrics with an empty result: {sorted(empty)}"
    p95_ms = report("BT05-07", "p95_get_metric", p95s, "ms", "< 2000 ms")
    assert p95_ms < 2000, f"get_metric p95 {p95_ms:.1f} ms"
