"""Test-local stand-in build for the Verifier tests (T05-25).

Spec 11's `tiny_build`, `lake_small` and `full` fixtures do not exist yet: `make_build` writes
a minimal `wh-<build_id>.duckdb` (pattern of the T05-13 warehouse tests) with one metrics
table, and `record` plays `execute_recorded` (U05-35, not built) to produce ops `Evidence`.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
from pydantic import JsonValue

from herness.core.ids import normalize_sql
from herness.core.ids import query_id as compute_query_id
from herness.core.types import Evidence, NumberRef, VerifiableItem
from herness.harness import _verifier_rerun as rr
from herness.harness.warehouse import WarehousePool
from herness.metrics.evidence import result_hash

BUILD_ID = "20260925-101500-ABCDEF"
OTHER_BUILD_ID = "20260925-101500-BCDEFG"
EXECUTED_AT = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
PATTERNS = (
    r"\b(19|20)\d{2}\b",
    r"\d{4}-\d{2}-\d{2}",
    r"Q[1-4] \d{4}",
    r"(INC|CHG|PRB)\d+",
    r"[A-Z][A-Z0-9]+-\d+",
)

TEAM_ROWS: tuple[tuple[str, date, int, float, Decimal, str], ...] = (
    ("payments", date(2026, 9, 21), 12, 7.416666666666667, Decimal("1234.50"), "INC0012345"),
    ("search", date(2026, 9, 21), 5, 3.25, Decimal("99.99"), "INC0012346"),
    ("identity", date(2026, 9, 21), 9, 11.0, Decimal("250000.00"), "INC0012347"),
    ("payments", date(2026, 9, 14), 7, 2.5, Decimal("10.01"), "INC0012348"),
    ("mobile", date(2026, 9, 21), 3, 0.3, Decimal("0.00"), "INC0012349"),
)
TEAM_SQL = "SELECT team, week, incidents, mttr_hours, cost_usd FROM metrics.team_week"
TEAM_BY_WEEK_SQL = (
    "SELECT team, incidents, mttr_hours, cost_usd FROM metrics.team_week WHERE week = $week"
)
TOTALS_SQL = (
    "SELECT team, sum(incidents) AS incidents, sum(mttr_hours) AS mttr_hours"
    " FROM metrics.team_week GROUP BY team"
)
ONE_ROW_SQL = "SELECT count(*) AS n, sum(cost_usd) AS total_usd FROM metrics.team_week"
META_SQL = "SELECT team, incidents FROM metrics.team_week WHERE week = $week"
META_BIND: dict[str, JsonValue] = {"week": "2026-09-14"}


def meta_params() -> dict[str, JsonValue]:
    return {"bind": META_BIND, "template": {}}


def meta_query_id(build_id: str = BUILD_ID) -> str:
    return compute_query_id(normalize_sql(META_SQL), meta_params(), build_id)


def _columns(cur: duckdb.DuckDBPyConnection) -> tuple[list[str], list[str]]:
    description = cur.description or []
    return [str(d[0]) for d in description], [str(d[1]) for d in description]


def make_build(
    warehouse_dir: Path,
    build_id: str = BUILD_ID,
    *,
    rows: Iterable[tuple[object, ...]] = TEAM_ROWS,
    extra_rows: int = 0,
    meta: bool = True,
) -> Path:
    """Write `wh-<build_id>.duckdb` with `metrics.team_week` and a `meta.evidence` row."""
    warehouse_dir.mkdir(parents=True, exist_ok=True)
    path = warehouse_dir / f"wh-{build_id}.duckdb"
    con = duckdb.connect(str(path))
    try:
        for schema in ("core", "enrich", "metrics", "score", "meta"):
            con.execute(f"CREATE SCHEMA {schema}")
        con.execute(
            "CREATE TABLE metrics.team_week (team VARCHAR, week DATE, incidents INTEGER,"
            " mttr_hours DOUBLE, cost_usd DECIMAL(18,2), ticket VARCHAR)"
        )
        con.executemany("INSERT INTO metrics.team_week VALUES (?, ?, ?, ?, ?, ?)", list(rows))
        if extra_rows:
            con.execute(
                "INSERT INTO metrics.team_week SELECT 'bulk' || i, DATE '2026-01-05', 1, 1.0,"
                " 1.00, 'INC' || i FROM range(?) t(i)",
                [extra_rows],
            )
        con.execute(
            "CREATE TABLE meta.evidence (query_id VARCHAR PRIMARY KEY, sql VARCHAR, params JSON,"
            " result_hash VARCHAR, row_count BIGINT, result_sample JSON,"
            " executed_at TIMESTAMPTZ, producer VARCHAR)"
        )
        if meta:
            _insert_meta(con, build_id)
    finally:
        con.close()
    return path


def _insert_meta(con: duckdb.DuckDBPyConnection, build_id: str) -> None:
    cur = con.execute(META_SQL, META_BIND)
    names, types = _columns(cur)
    fetched = cur.fetchall()
    digest = result_hash(list(zip(names, types, strict=True)), fetched)
    con.execute(
        "INSERT INTO meta.evidence VALUES (?, ?, ?, ?, ?, ?, ?, 'test')",
        [
            meta_query_id(build_id),
            normalize_sql(META_SQL),
            json.dumps(meta_params()),
            digest,
            len(fetched),
            json.dumps(rr.sample_rows(names, fetched)),
            EXECUTED_AT,
        ],
    )


def record(
    pool: WarehousePool,
    sql: str,
    params: Mapping[str, JsonValue] | None = None,
    *,
    build_id: str = BUILD_ID,
    run_id: str | None = "run_test",
) -> Evidence:
    """Play `execute_recorded`: run `sql` on the pool's handle and build ops `Evidence`."""
    bind = dict(params or {})
    cur = pool.get(build_id).cursor()
    cur.execute(sql, bind)
    names, types = _columns(cur)
    fetched = cur.fetchall()
    norm = normalize_sql(sql)
    return Evidence(
        query_id=compute_query_id(norm, bind, build_id),
        run_id=run_id,
        build_id=build_id,
        sql=norm,
        params=bind,
        result_hash=result_hash(list(zip(names, types, strict=True)), fetched),
        row_count=len(fetched),
        result_sample=rr.sample_rows(names, fetched),
        executed_at=EXECUTED_AT,
        duration_ms=3,
    )


def ref(
    number_id: str,
    value: float | str,
    query_id: str,
    column: str,
    row_key: Mapping[str, Any] | None,
    *,
    unit: str = "count",
) -> NumberRef:
    return NumberRef.model_validate(
        {
            "id": number_id,
            "value": value,
            "unit": unit,
            "query_id": query_id,
            "column": column,
            "row_key": None if row_key is None else dict(row_key),
        }
    )


def item(
    text: str,
    numbers: Sequence[NumberRef],
    *,
    where: str = "item",
    finding_ids: Sequence[str] = (),
    refs: Mapping[str, str] | None = None,
) -> VerifiableItem:
    return VerifiableItem(
        where=where,
        text=text,
        numbers=list(numbers),
        finding_ids=list(finding_ids),
        refs=dict(refs or {}),
    )


# --- IT05-07 stand-in corpus (spec 11 `lake_small` corpus not built yet) ------------------

FND_VERIFIED = "fnd_" + "A" * 26
FND_CHALLENGED = "fnd_" + "B" * 26
WEEK_BIND: dict[str, JsonValue] = {"week": "2026-09-21"}
OTHER_BUILD_SQL = "SELECT 1 AS x"
REJECTED_SQL = "SELECT * FROM read_csv('x.csv')"
_ALLOWED_TAIL = " in Q3 2026 (INC0012345, as of 2026-09-24)."
_PAY = {"team": "payments", "week": "2026-09-21"}


def recorded_queries(pool: WarehousePool) -> dict[str, Evidence]:
    """Evidence for the corpus queries, keyed `team`, `by_week`, `totals`, `one_row`."""
    return {
        "team": record(pool, TEAM_SQL),
        "by_week": record(pool, TEAM_BY_WEEK_SQL, WEEK_BIND),
        "totals": record(pool, TOTALS_SQL),
        "one_row": record(pool, ONE_ROW_SQL),
    }


def foreign_evidence(sql: str, *, build_id: str = BUILD_ID) -> Evidence:
    """Ops evidence for `sql` that was never executed here (hash of zeros, empty sample)."""
    return Evidence(
        query_id=compute_query_id(sql, {}, build_id),
        run_id=None,
        build_id=build_id,
        sql=sql,
        params={},
        result_hash="0" * 64,
        row_count=1,
        result_sample=[],
        executed_at=EXECUTED_AT,
        duration_ms=1,
    )


def _one(text: str, number: NumberRef) -> VerifiableItem:
    return item(f"{text} [[{number.id}]]{_ALLOWED_TAIL}", [number])


def _row_items(team_qid: str) -> list[VerifiableItem]:
    items: list[VerifiableItem] = []
    for team, week, incidents, mttr, cost, _ticket in TEAM_ROWS:
        key = {"team": team, "week": week.isoformat()}
        inc = ref("n1", incidents, team_qid, "incidents", key)
        hours1 = ref("n2", round(mttr, 1), team_qid, "mttr_hours", key, unit="hours")
        hours2 = ref("n3", round(mttr, 2), team_qid, "mttr_hours", key, unit="hours")
        usd = ref("n4", str(cost), team_qid, "cost_usd", key, unit="usd")
        items += [
            _one(f"{team} incidents", inc),
            _one(f"{team} MTTR hours", hours1),
            _one(f"{team} MTTR hours", hours2),
            _one(f"{team} cost", usd),
            item(
                f"{team}: [[n1]] incidents, MTTR [[n2]] h, cost [[n4]]{_ALLOWED_TAIL}",
                [inc, hours1, usd],
                finding_ids=[FND_VERIFIED],
                refs={"expected_usd_ref": "n4", "expected_delta_ref": "n1"},
            ),
        ]
    return items


def _aggregate_items(qids: Mapping[str, str]) -> list[VerifiableItem]:
    totals: dict[str, tuple[int, float]] = {}
    for team, _week, incidents, mttr, _cost, _ticket in TEAM_ROWS:
        count, hours = totals.get(team, (0, 0.0))
        totals[team] = (count + incidents, hours + mttr)
    items: list[VerifiableItem] = []
    for team, (count, hours) in sorted(totals.items()):
        key = {"team": team}
        items.append(_one(f"{team} total", ref("n1", count, qids["totals"], "incidents", key)))
        hours_ref = ref("n1", round(hours, 2), qids["totals"], "mttr_hours", key, unit="hours")
        items.append(_one(f"{team} total MTTR", hours_ref))
    for team, week, incidents, _mttr, cost, _ticket in TEAM_ROWS:
        if week.isoformat() != WEEK_BIND["week"]:
            continue
        key = {"team": team}
        items.append(_one(team, ref("n1", incidents, qids["by_week"], "incidents", key)))
        usd = ref("n1", f"{cost:.2f}", qids["by_week"], "cost_usd", key, unit="usd")
        items.append(_one(f"{team} cost", usd))
    total_usd = sum((row[4] for row in TEAM_ROWS), Decimal(0))
    items.append(_one("rows", ref("n1", len(TEAM_ROWS), qids["one_row"], "n", None)))
    usd = ref("n1", f"{total_usd:.2f}", qids["one_row"], "total_usd", None, unit="usd")
    items.append(_one("total cost", usd))
    items.append(
        _one("prior week", ref("n1", 7, meta_query_id(), "incidents", {"team": "payments"}))
    )
    return items


def _precision_items(team_qid: str) -> list[VerifiableItem]:
    return [
        _one("MTTR", ref("n1", claimed, team_qid, "mttr_hours", _PAY, unit="hours"))
        for claimed in (7.4, 7.42, 7.417, 7.4167, 7.41667, 7)
    ]


def correct_items(qids: Mapping[str, str]) -> list[VerifiableItem]:
    """The 50 correct items: rounding (`7.4` for `7.41667`), USD strings, allowed numerals."""
    items = _row_items(qids["team"]) + _aggregate_items(qids) + _precision_items(qids["team"])
    return [one.model_copy(update={"where": f"correct[{i}]"}) for i, one in enumerate(items)]


def planted_items(team_qid: str) -> dict[str, tuple[VerifiableItem, str]]:
    """The 13 planted error kinds: kind -> (item, expected signal).

    The signal is a check result (`mismatch`, ...) or an item list name (`uncited`, ...).
    `other_build_query` and `failed_query` need `foreign_evidence(OTHER_BUILD_SQL,
    build_id=OTHER_BUILD_ID)` and `foreign_evidence(REJECTED_SQL)` in the ops handle.
    """
    other = compute_query_id(OTHER_BUILD_SQL, {}, OTHER_BUILD_ID)
    rejected = compute_query_id(REJECTED_SQL, {}, BUILD_ID)
    usd = ref("n1", "1234.50", team_qid, "cost_usd", _PAY, unit="usd")
    hours = ref("n2", 7.4, team_qid, "mttr_hours", _PAY, unit="hours")
    week_28 = {"team": "payments", "week": "2026-09-28"}
    planted = {
        "fabricated_number": (ref("n1", 13, team_qid, "incidents", _PAY), "mismatch"),
        "fabricated_float": (
            ref("n1", 7.5, team_qid, "mttr_hours", _PAY, unit="hours"),
            "mismatch",
        ),
        "off_by_one_cent_usd": (
            ref("n1", "1234.51", team_qid, "cost_usd", _PAY, unit="usd"),
            "mismatch",
        ),
        "wrong_column": (ref("n1", 12, team_qid, "cost", _PAY), "missing_column"),
        "wrong_row": (ref("n1", 12, team_qid, "incidents", week_28), "row_not_found"),
        "ambiguous_row": (
            ref("n1", 12, team_qid, "incidents", {"team": "payments"}),
            "row_ambiguous",
        ),
        "fabricated_query_id": (
            ref("n1", 12, "q_0123456789abcdef", "incidents", _PAY),
            "missing_query",
        ),
        "other_build_query": (ref("n1", 1, other, "x", None), "wrong_build"),
        "failed_query": (ref("n1", 1, rejected, "a", None), "query_failed"),
    }
    items = {kind: (_one("Claim", number), signal) for kind, (number, signal) in planted.items()}
    items["uncited_numeral"] = (item("Cost [[n1]], 3 more than search.", [usd]), "uncited")
    items["unknown_marker"] = (item("Cost [[n1]] and [[n7]].", [usd]), "unknown_markers")
    items["not_usd_ref"] = (
        item("Cost [[n1]], MTTR [[n2]].", [usd, hours], refs={"expected_usd_ref": "n2"}),
        "bad_refs",
    )
    items["unverified_finding"] = (
        item("Cost [[n1]].", [usd], finding_ids=[FND_CHALLENGED]),
        "unverified_findings",
    )
    return items
