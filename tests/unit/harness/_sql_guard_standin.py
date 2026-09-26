"""Test-local stand-in schema and accepted-query corpus for the SQL guard (T05-14).

Stand-in for spec 11's `tiny_build`/`small_build`/`full` schemas and `tests/fixtures/sql_ok/`
(owned by impl 11, not built yet). The schema holds every `UNTRUSTED_TEXT_COLUMNS` and
`REDACT_ON_READ_COLUMNS` column and the seven default blocked columns (U05-72).
"""

from __future__ import annotations

from pathlib import Path

import duckdb

BUILD_ID = "20260925-101500-ABCDEF"

BLOCKED_COLUMNS = (
    "core.incident.short_description",
    "core.incident.description",
    "core.incident.close_notes",
    "core.change.short_description",
    "core.change.description",
    "core.problem.root_cause_text",
    "core.work_item.description",
)

_V, _I, _D, _T, _TS = "varchar", "integer", "double", "date", "timestamp"

SCHEMA: dict[str, dict[str, dict[str, str]]] = {
    "core": {
        "incident": {
            "record_id": _V, "number": _V, "opened_at": _TS, "closed_at": _TS, "priority": _I,
            "state": _V, "assignment_group": _V, "service": _V, "short_description": _V,
            "description": _V, "close_notes": _V,
        },
        "change": {
            "record_id": _V, "number": _V, "start_at": _TS, "risk": _V, "state": _V,
            "service": _V, "short_description": _V, "description": _V,
        },
        "problem": {
            "record_id": _V, "number": _V, "opened_at": _TS, "state": _V, "service": _V,
            "root_cause_text": _V,
        },
        "work_item": {
            "record_id": _V, "key": _V, "created_at": _TS, "resolved_at": _TS, "status": _V,
            "project": _V, "service": _V, "summary": _V, "description": _V,
        },
        "event": {
            "record_id": _V, "alert_name": _V, "severity": _V, "occurred_at": _TS, "service": _V,
        },
    },
    "enrich": {
        "text_redacted": {"record_id": _V, "text": _V},
        "cluster": {"cluster_id": _V, "label": _V, "size": _I},
        "cluster_member": {"record_id": _V, "cluster_id": _V, "membership_prob": _D},
        "decision": {"record_id": _V, "question": _V, "answer": _V, "probability": _D},
    },
    "metrics": {
        "metric_value": {"metric": _V, "period": _T, "service": _V, "value": _D},
    },
    "score": {
        "funding": {"funding_id": _V, "title": _V, "amount": _D, "service": _V, "period": _T},
        "service_score": {"service": _V, "period": _T, "score": _D},
    },
    "meta": {"build_info": {"build_id": _V, "built_at": _TS}},
}  # fmt: skip


def make_warehouse(warehouse_dir: Path, build_id: str = BUILD_ID) -> Path:
    """Create an empty `wh-<build_id>.duckdb` with the stand-in schema."""
    warehouse_dir.mkdir(parents=True, exist_ok=True)
    path = warehouse_dir / f"wh-{build_id}.duckdb"
    con = duckdb.connect(str(path))
    try:
        for db, tables in SCHEMA.items():
            con.execute(f"CREATE SCHEMA {db}")
            for table, cols in tables.items():
                body = ", ".join(f'"{c}" {t}' for c, t in cols.items())
                con.execute(f"CREATE TABLE {db}.{table} ({body})")
    finally:
        con.close()
    return path


_HANDWRITTEN = (
    "SELECT count(*) AS n FROM core.incident",
    "SELECT priority, count(*) AS n FROM core.incident GROUP BY priority ORDER BY priority",
    "SELECT service, avg(value) AS v FROM metrics.metric_value WHERE metric = $metric "
    "GROUP BY service",
    "SELECT i.number, t.text FROM core.incident i JOIN enrich.text_redacted t "
    "USING (record_id) LIMIT 10",
    "WITH recent AS (SELECT * EXCLUDE (short_description, description, close_notes) "
    "FROM core.incident WHERE opened_at > DATE '2026-01-01') SELECT state, count(*) FROM recent "
    "GROUP BY ALL",
    "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 12) "
    "SELECT n FROM r ORDER BY n",
    "SELECT service FROM core.incident UNION SELECT service FROM core.change",
    "SELECT service FROM core.incident INTERSECT SELECT service FROM core.problem",
    "SELECT service FROM core.incident EXCEPT SELECT service FROM score.service_score",
    "SELECT x FROM range(10) t(x)",
    "SELECT g FROM generate_series(1, 5) s(g)",
    "SELECT u FROM unnest([1, 2, 3]) t(u)",
    "SELECT record_id, row_number() OVER (PARTITION BY service ORDER BY opened_at) AS rn "
    "FROM core.incident QUALIFY rn = 1",
    "SELECT c.label, count(*) AS members FROM enrich.cluster c JOIN enrich.cluster_member m "
    "ON m.cluster_id = c.cluster_id GROUP BY c.label ORDER BY members DESC",
    "SELECT w.key, w.summary FROM core.work_item w WHERE w.status = 'open'",
    "SELECT title, amount FROM score.funding ORDER BY amount DESC LIMIT 5",
    "SELECT alert_name, count(*) FROM core.event GROUP BY alert_name",
    "SELECT d.question, avg(d.probability) FROM enrich.decision d GROUP BY d.question",
    "SELECT * FROM meta.build_info",
    "SELECT (SELECT max(opened_at) FROM core.incident) AS latest",
    "SELECT number FROM core.incident i WHERE EXISTS (SELECT 1 FROM core.change c "
    "WHERE c.service = i.service)",
    "(SELECT count(*) FROM core.problem)",
    "SELECT date_trunc('month', opened_at) AS m, count(*) FROM core.incident GROUP BY 1 ORDER BY 1",
    "SELECT service, sum(amount) FILTER (WHERE amount > 0) AS pos FROM score.funding "
    "GROUP BY service",
    "SELECT CASE WHEN priority <= 2 THEN 'high' ELSE 'low' END AS band, count(*) "
    "FROM core.incident GROUP BY band",
    "SELECT record_id FROM core.incident WHERE record_id = $record_id",
    "SELECT list_transform([1, 2], x -> x * 2) AS doubled",
    "SELECT coalesce(resolved_at, created_at) AS ts FROM core.work_item",
    "SELECT service, quantile_cont(value, 0.95) AS p95 FROM metrics.metric_value GROUP BY service",
    "FROM core.change SELECT risk, count(*) GROUP BY risk",
)

_TABLE_FAMILY = (
    "SELECT count({c}) AS n FROM {db}.{t}",
    "SELECT {c}, count(*) AS n FROM {db}.{t} GROUP BY {c} ORDER BY n DESC",
    "SELECT DISTINCT {c} FROM {db}.{t} ORDER BY {c}",
    "SELECT {c} FROM {db}.{t} WHERE {c} IS NOT NULL LIMIT 20",
    "WITH x AS (SELECT {c} FROM {db}.{t}) SELECT count(DISTINCT {c}) FROM x",
    "SELECT q.{c} FROM (SELECT {c} FROM {db}.{t}) q ORDER BY q.{c} DESC",
)


def _safe_columns(db: str, table: str) -> list[str]:
    cols = [c for c in SCHEMA[db][table] if f"{db}.{table}.{c}" not in BLOCKED_COLUMNS]
    return cols[:3]


def accepted_corpus() -> list[str]:
    """At least 200 distinct queries the guard must accept on the stand-in schema."""
    out = list(_HANDWRITTEN)
    for db, tables in SCHEMA.items():
        for table in tables:
            for col in _safe_columns(db, table):
                out.extend(f.format(db=db, t=table, c=col) for f in _TABLE_FAMILY)
    return list(dict.fromkeys(out))
