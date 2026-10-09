"""Procedural benchmarks: `promote_procedural` and the LoRA export (BT07-09, BT07-10).

Spec targets (impl 07 §10, §11): "BT07-09 `promote_procedural` | 500 queries | < 10 s";
"BT07-10 LoRA export | 50k pairs | < 2 min". Run: pytest -m "integration and slow" tests/bench
-k memory. Seeding is bulk and never timed; the gate is the median of `REPEATS` timed runs.

BT07-09: per repeat one fresh `done` run of 50 tasks (a distinct objective each) with 10
verified findings per task, one evidence query each: 500 queries in 25 structural variants
(aggregate, service filter, ordering, limit, join), so promotion groups them into 25
fingerprints and writes a template per fingerprint plus a `qa_pair` per distinct
(question, query). The deps are the unit env's (real writer, redactor, real `SqlGuard`) on a
fresh LanceDB table in tmp per repeat; every repeat must see 500 queries and create 25
templates and 500 qa pairs. BT07-09 is a non-strict xfail: the 525 per-item vector upserts
alone take about 22 s.

BT07-10: 50 `sql_template` items (30 passes, no fails, Wilson lower bound about 0.89) and
50,000 active `qa_pair` items of distinct questions are inserted directly; the export runs
through the real redactor, injection scanner, SQL parse and golden-question check (hash-seeded
fake embedder), the digest warehouse is the in-memory DuckDB of the LoRA unit env. Every repeat
must write all 50,000 pairs to the train and validation files.
"""

from __future__ import annotations

import itertools
import json
import sqlite3
import time
from pathlib import Path

import pytest
from tests.support.bench_stats import REPEATS, report
from tests.support.ops_store import OpsStoreHandle
from tests.unit.harness.memory import _lora_env as le
from tests.unit.harness.memory._procedural_env import BUILD, make_deps, seed_run
from tests.unit.harness.memory._write_env import NOW

from herness.core import time as clock
from herness.core.ids import new_ulid
from herness.harness.memory import lora, procedural
from herness.store.ops import core

pytestmark = [pytest.mark.integration, pytest.mark.slow]

QUERIES = 500
TASKS = 50
PROMOTE_LIMIT_S = 10.0
PAIRS = 50_000
TEMPLATES = 50
EXPORT_LIMIT_S = 120.0
_AT = clock.format_utc(NOW)
_AGGS = ("SUM", "AVG", "MAX", "MIN", "COUNT")
_LETTERS = "abcdefghijklmnopqrstuvwxyz"


def _word(n: int) -> str:
    """A purely alphabetic distinct word for ordinal `n` (no digits in question text)."""
    out = ""
    n += 1
    while n:
        n, rest = divmod(n - 1, 26)
        out = _LETTERS[rest] + out
    return out


def _variants(tag: str) -> list[str]:
    """25 SQL shapes over the unit env's two-table schema; `tag` names the output column, so
    every repeat has its own fingerprints (literals are filled per query)."""
    shapes: list[str] = []
    for agg, filtered, ordered in itertools.product(_AGGS, (True, False), (True, False)):
        where = " AND service_id = '{svc}'" if filtered else ""
        tail = f" ORDER BY {tag} DESC LIMIT 5" if ordered else ""
        shapes.append(
            f"SELECT service_id, {agg}(incidents) AS {tag} FROM metrics.daily_incidents"  # noqa: S608 - fixed text
            f" WHERE day >= '{{d1}}' AND day < '{{d2}}'{where} GROUP BY service_id{tail}"
        )  # fmt: skip
    shapes += [
        f"SELECT s.team_id, {agg}(d.incidents) AS {tag} FROM metrics.daily_incidents d"  # noqa: S608 - fixed text
        " JOIN core.service s ON s.service_id = d.service_id"
        " WHERE d.day >= '{d1}' AND d.day < '{d2}' GROUP BY s.team_id"
        for agg in _AGGS
    ]
    return shapes[:25]


def _seed_run_with_queries(repeat: int) -> str:
    """One run: TASKS tasks, QUERIES verified findings each citing one evidence query."""
    run_id = seed_run()
    tag = f"n_{_word(repeat)}"
    shapes = _variants(tag)
    tasks, evidence, findings = [], [], []
    for i in range(QUERIES):
        if i % (QUERIES // TASKS) == 0:
            task_id = "task_" + new_ulid()
            objective = (
                f"How many incidents did team {_word(i + repeat * QUERIES)} have last month?"
            )
            spec = core.dump_json({"objective": objective}, field="spec")
            tasks.append((task_id, run_id, spec, _AT, _AT))
        sql = shapes[i % len(shapes)].format(svc=f"svc_{i}", d1="2026-01-01", d2="2026-02-01")
        query_id = f"q_{repeat * QUERIES + i:016x}"
        evidence.append((query_id, BUILD, sql, _AT))
        findings.append(
            ("fnd_" + new_ulid(), run_id, task_id, core.dump_json([query_id], field="q"), _AT)
        )

    def insert(conn: sqlite3.Connection) -> None:
        conn.executemany(
            "INSERT INTO task (task_id, run_id, role, spec, status, created_at, updated_at)"
            " VALUES (?, ?, 'analyst', ?, 'done', ?, ?)",
            tasks,
        )
        conn.executemany(
            "INSERT INTO evidence (query_id, build_id, sql, result_hash, params, row_count,"
            " duration_ms, executed_at) VALUES (?, ?, ?, 'h', '{}', 1, 1, ?)",
            evidence,
        )
        conn.executemany(
            "INSERT INTO finding (finding_id, run_id, task_id, author_role, claim, query_ids,"
            " status, verification, created_at)"
            " VALUES (?, ?, ?, 'analyst', 'c', ?, 'verified', '{\"passed\": true}', ?)",
            findings,
        )

    core.run_write(insert, op="bench_seed")
    return run_id


@pytest.mark.timeout(900)
def test_bt07_09_promote_procedural_500_queries_under_10s(
    ops_store: OpsStoreHandle, tmp_path: Path
) -> None:
    """BT07-09 `promote_procedural` over a run of 500 verified queries: < 10 s (median of 3)."""
    del ops_store
    seconds: list[float] = []
    for repeat in range(REPEATS):
        env = make_deps(tmp_path / f"repeat{repeat}")  # a fresh LanceDB table per repeat
        run_id = _seed_run_with_queries(repeat)
        start = time.perf_counter()
        result = procedural.promote_procedural(run_id, deps=env.deps)
        seconds.append(time.perf_counter() - start)
        assert result.queries_seen == QUERIES
        assert result.templates_created == len(_variants("n"))
        assert result.qa_pairs_created == QUERIES
    median = report("BT07-09", "median_promote", seconds, "s", f"< {PROMOTE_LIMIT_S:g} s")
    assert median < PROMOTE_LIMIT_S, f"promote_procedural {median:.2f} s"


_QA_INSERT = (
    "INSERT INTO memory_item (memory_id, layer, kind, content, data, provenance, confidence,"
    " status, created_at, expires_at, last_used_at, use_count)"
    " VALUES (?, 'procedural', ?, ?, ?, ?, 0.9, 'active', ?, NULL, NULL, 0)"
)


def _seed_pairs() -> None:
    """TEMPLATES passing templates and PAIRS qa_pairs spread over them, in one transaction."""
    prov = json.dumps({"author_type": "system", "via": "promotion"})
    rows: list[tuple[object, ...]] = []
    for t, fp in enumerate(le.fingerprints(TEMPLATES)):
        data = {"fingerprint": fp, "sql_template": le.SQL, "params": [], "question_examples": [],
                "passes": 30, "fails": 0, "run_ids": [], "build_id_last_ok": le.BUILD,
                "metrics_used": [], "qa_ids": []}  # fmt: skip
        rows.append((le.mid(t + 1), "sql_template", f"template {fp}", json.dumps(data), prov, _AT))
    for n in range(PAIRS):
        question = f"How many incidents did team {_word(n)} have in region {_word(n // 7)}?"
        sql = f"SELECT service_id, COUNT(*) AS n FROM core.incident WHERE service_id = 'svc_{n}'"  # noqa: S608 - integer only
        data = {"question": question, "sql": sql, "query_id": f"q_{n:016d}",
                "template_id": le.mid(n % TEMPLATES + 1)}  # fmt: skip
        rows.append((le.mid(TEMPLATES + 1 + n), "qa_pair", question, json.dumps(data), prov, _AT))
    core.run_write(lambda conn: conn.executemany(_QA_INSERT, rows), op="bench_seed")


@pytest.mark.timeout(600)
def test_bt07_10_lora_export_50k_pairs_under_2min(
    ops_store: OpsStoreHandle, tmp_path: Path
) -> None:
    """BT07-10 LoRA export of 50,000 qa_pairs: < 2 min (median of 3 runs)."""
    del ops_store
    env = le.make_deps(tmp_path)
    _seed_pairs()
    seconds: list[float] = []
    for _ in range(REPEATS):
        start = time.perf_counter()
        result = lora.export_lora(env.root, 0.8, golden_questions=le.golden(), deps=env.deps)
        seconds.append(time.perf_counter() - start)
        assert result.train_count + result.val_count == PAIRS
        assert result.excluded_golden == 0
    median = report("BT07-10", "median_export", seconds, "s", f"< {EXPORT_LIMIT_S:g} s")
    assert median < EXPORT_LIMIT_S, f"LoRA export {median:.1f} s"
