"""Stage 400 facts (impl 04 T04-06): `400_facts.sql` closures and `metrics.incident_fact`,
`herness.metrics.facts.split_statements` and `materialize_facts` on `metrics_tiny`."""

import datetime
import re
import time
from collections.abc import Iterator
from decimal import Decimal

import duckdb
import pytest
from freezegun import freeze_time
from structlog.testing import capture_logs
from tests.support.metrics_tiny import BUILD_ID, build_metrics_tiny, patch_facts_config

from herness.core.errors import ConfigError, SchemaViolation
from herness.metrics import facts
from herness.metrics.facts import FACT_TABLES, materialize_facts, split_statements

pytestmark = pytest.mark.unit

UTC = datetime.UTC
NOW = datetime.datetime(2026, 4, 1, 6, 30, tzinfo=UTC)
INCIDENT_COLUMNS = [
    ("record_id", "VARCHAR"),
    ("number", "VARCHAR"),
    ("opened_at", "TIMESTAMP WITH TIME ZONE"),
    ("resolved_at", "TIMESTAMP WITH TIME ZONE"),
    ("priority", "SMALLINT"),
    ("service_id", "VARCHAR"),
    ("team_id", "VARCHAR"),
    ("org_id", "VARCHAR"),
    ("criticality", "SMALLINT"),
    ("cluster_id", "VARCHAR"),
    ("membership_prob", "DOUBLE"),
    ("excluded", "BOOLEAN"),
    ("resolve_h", "DOUBLE"),
    ("resolve_bh", "DOUBLE"),
    ("impact_h", "DOUBLE"),
    ("impact_estimated", "BOOLEAN"),
    ("toil_h", "DOUBLE"),
    ("downtime_usd", "DECIMAL(18,2)"),
    ("toil_usd", "DECIMAL(18,2)"),
    ("total_usd", "DECIMAL(18,2)"),
    ("is_repeat", "BOOLEAN"),
    ("is_reopened", "BOOLEAN"),
    ("is_reassigned", "BOOLEAN"),
    ("sla_breached", "BOOLEAN"),
    ("change_caused", "BOOLEAN"),
    ("query_id", "VARCHAR"),
]
_INCIDENT_INSERT = (
    "INSERT INTO core.incident (record_id, number, opened_at, resolved_at, priority, state,"
    " service_id, team_id, close_code, caused_by_change_id, business_duration_s,"
    " customer_impact_minutes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


@pytest.fixture
def con(monkeypatch: pytest.MonkeyPatch) -> Iterator[duckdb.DuckDBPyConnection]:
    patch_facts_config(monkeypatch)
    c = build_metrics_tiny()
    yield c
    c.close()


def _materialize(con: duckdb.DuckDBPyConnection) -> list[str]:
    with freeze_time(NOW):
        return materialize_facts(con, BUILD_ID)


def _fact(con: duckdb.DuckDBPyConnection, *columns: str) -> dict[str, tuple[object, ...]]:
    rows = con.execute(
        f"SELECT record_id, {', '.join(columns)} FROM metrics.incident_fact"  # noqa: S608
    ).fetchall()
    return {str(r[0]): tuple(r[1:]) for r in rows}


def _ts(day: str, hour: int) -> datetime.datetime:
    return datetime.datetime.fromisoformat(f"2026-{day}T{hour:02d}:00:00+00:00")


# --- UT04-30 incident_fact on the design 04 §10.1 incidents ------------------------------------


def test_ut04_30_incident_fact_design_values(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-30 I1-I4 give resolve_h 2/4/6/NULL, I4 excluded, downtime 15000/0/5000, toil
    3.0/0.8/3.0 and total_usd 20680 (design 04 §10.1)."""
    _materialize(con)
    got = _fact(con, "resolve_h", "resolve_bh", "excluded", "downtime_usd", "toil_h", "toil_usd")
    assert got["I1"][:3] == (2.0, 2.0, False)
    assert got["I2"][:3] == (4.0, 4.0, False)
    assert got["I3"][:3] == (6.0, 6.0, False)
    assert got["I4"] == (None, None, True, None, None, None)
    assert [got[i][3] for i in ("I1", "I2", "I3")] == [
        Decimal("15000.00"),
        Decimal("0.00"),
        Decimal("5000.00"),
    ]
    assert [got[i][4] for i in ("I1", "I2", "I3")] == pytest.approx([3.0, 0.8, 3.0])
    assert [got[i][5] for i in ("I1", "I2", "I3")] == [
        Decimal("300.00"),
        Decimal("80.00"),
        Decimal("300.00"),
    ]
    total = con.execute("SELECT sum(total_usd) FROM metrics.incident_fact").fetchone()
    assert total == (Decimal("20680.00"),)


def test_ut04_30_incident_fact_flags_and_copies(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-30 copied columns, org of the resolving team, criticality, cluster, the §10.1 flags
    (I2 is a repeat 15 days after I1; I3 is 36 days after I2) and change_caused by link score."""
    _materialize(con)
    got = _fact(
        con,
        "number",
        "opened_at",
        "priority",
        "org_id",
        "criticality",
        "cluster_id",
        "membership_prob",
        "impact_h",
        "impact_estimated",
        "is_repeat",
        "is_reopened",
        "is_reassigned",
        "sla_breached",
        "change_caused",
    )
    assert got["I1"] == (
        *("INC0001", _ts("01-05", 15), 1, "O2", 1, "C1", 0.9, 1.5, False),
        *(False, False, True, True, False),
    )
    assert got["I2"][7:] == (0.0, False, True, True, False, False, False)
    assert got["I3"][7:] == (1.0, False, False, False, True, False, True)
    assert got["I4"][7:] == (None, False, False, False, False, None, False)


def test_ut04_30_incident_fact_shape_and_invariants(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-30 columns in design 04 §4.2 order and types; one row per core.incident; money NULL
    exactly when excluded; durations NULL or >= 0; every row carries the statement query_id."""
    query_ids = _materialize(con)
    res = con.execute("SELECT * FROM metrics.incident_fact")
    assert [(d[0], str(d[1])) for d in res.description] == INCIDENT_COLUMNS
    counts = con.execute(
        "SELECT (SELECT count(*) FROM core.incident), count(*),"
        " count(*) FILTER ((downtime_usd IS NULL) <> excluded OR (toil_usd IS NULL) <> excluded"
        "   OR (total_usd IS NULL) <> excluded OR (impact_h IS NULL) <> excluded),"
        " count(*) FILTER (resolve_h < 0 OR resolve_bh < 0 OR impact_h < 0 OR toil_h < 0),"
        " count(DISTINCT query_id), min(query_id)"
        " FROM metrics.incident_fact"
    ).fetchone()
    assert counts == (4, 4, 0, 0, 1, query_ids[2])


def test_ut04_30_incident_fact_edge_rules(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-30 the fallback impact (estimated, capped), invalid durations, unknown team and
    service, close-code exclusion, caused_by_change_id and a negative business duration."""
    late = datetime.datetime(2027, 1, 2, 1, tzinfo=UTC)  # past max_resolve_days
    con.execute("DELETE FROM core.incident")
    rows = [
        # P1 without impact, 30 h open: fallback min(30 x 1.0, cap 24) on unknown service.
        ("J1", "INC1", _ts("01-01", 0), _ts("01-02", 6), 1, "closed", "SX", "TX", None, "CH9"),
        # P2 without impact, 6 h: fallback 6 x 0.5 = 3 h on S1 (criticality 1).
        ("J2", "INC2", _ts("01-03", 0), _ts("01-03", 6), 2, "closed", "S1", "T1", None, None),
        # resolved before opened: resolve_h NULL, so no fallback and no toil.
        ("J3", "INC3", _ts("01-04", 6), _ts("01-04", 0), 1, "closed", "S1", "T1", None, None),
        # open longer than max_resolve_days (365): resolve_h NULL.
        ("J4", "INC4", _ts("01-01", 0), late, 1, "closed", "S1", "T1", None, None),
        # excluded by close code; priority NULL.
        ("J5", "INC5", _ts("01-05", 0), _ts("01-05", 1), None, "closed", "S1", "T1",
         "Duplicate", None),
    ]  # fmt: skip
    for rid, num, opened, resolved, prio, state, svc, team, code, cause in rows:
        bdur = -5 if rid == "J2" else None
        values = [rid, num, opened, resolved, prio, state, svc, team, code, cause]
        con.execute(_INCIDENT_INSERT, [*values, bdur, None])
    _materialize(con)
    got = _fact(
        con,
        "org_id",
        "criticality",
        "resolve_h",
        "resolve_bh",
        "impact_h",
        "impact_estimated",
        "downtime_usd",
        "toil_h",
        "excluded",
        "change_caused",
    )
    # J1: default cost 500/h x 24 h x multiplier 1.0; toil 30 x 0.33 x 1.5.
    assert got["J1"][:7] == (None, None, 30.0, None, 24.0, True, Decimal("12000.00"))
    assert got["J1"][7] == pytest.approx(30 * 0.33 * 1.5)
    assert got["J1"][8:] == (False, True)
    # J2: 3 h x 10000 x 0.5; negative business duration falls back to resolve_h x share.
    assert got["J2"][2:7] == (6.0, None, 3.0, True, Decimal("15000.00"))
    assert got["J2"][7] == pytest.approx(6 * 0.33 * 0.5)
    assert got["J3"][2:8] == (None, None, 0.0, False, Decimal("0.00"), None)
    assert got["J4"][2:8] == (None, None, 0.0, False, Decimal("0.00"), None)
    assert got["J5"][4:] == (None, False, None, None, True, False)


def test_ut04_30_repeat_window_skips_excluded(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-30 an excluded incident between two others is not their predecessor: I3 then
    compares with I2 (36 days, no repeat) although an excluded C1 incident lies in between."""
    con.execute(
        _INCIDENT_INSERT,
        ["I5", "INC5", _ts("02-20", 0), None, 3, "canceled", "S1", "T1", None, None, None, None],
    )
    con.execute("INSERT INTO enrich.cluster_member VALUES ('I5', 'C1', 0.9)")
    _materialize(con)
    got = _fact(con, "is_repeat", "excluded")
    assert got == {
        "I1": (False, False),
        "I2": (True, False),
        "I3": (False, False),
        "I4": (False, True),
        "I5": (False, True),
    }


# --- UT04-31 cluster membership ----------------------------------------------------------------


def test_ut04_31_membership_highest_qualifying_lowest_id_on_tie(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """UT04-31 memberships 0.4/0.6/0.6: the highest at or above 0.5 wins, the lowest cluster_id
    on a tie; a record with only 0.4 is a noise point (NULL cluster, never a repeat)."""
    con.execute("DELETE FROM enrich.cluster_member")
    con.execute(
        "INSERT INTO enrich.cluster_member VALUES"
        " ('I1', 'C9', 0.4), ('I1', 'C7', 0.6), ('I1', 'C5', 0.6),"
        " ('I2', 'C5', 0.4), ('I3', 'C5', 0.5), ('I3', 'C4', 0.49)"
    )
    _materialize(con)
    got = _fact(con, "cluster_id", "membership_prob", "is_repeat")
    assert got["I1"] == ("C5", 0.6, False)
    assert got["I2"] == (None, None, False)
    assert got["I3"] == ("C5", 0.5, False)
    assert got["I4"] == (None, None, False)


# --- UT04-34 closures ------------------------------------------------------------------------


def _closure(con: duckdb.DuckDBPyConnection, table: str) -> list[tuple[object, ...]]:
    return con.execute(
        f"SELECT * EXCLUDE (query_id) FROM {table} ORDER BY ALL"  # noqa: S608 - test names
    ).fetchall()


def test_ut04_34_org_closure_tree(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-34 ancestor-or-self pairs of the fixture org tree, self rows at depth 0."""
    _materialize(con)
    assert _closure(con, "metrics.org_closure") == [
        ("O1", "O1", 0),
        ("O2", "O1", 1),
        ("O2", "O2", 0),
        ("O3", "O1", 2),
        ("O3", "O2", 1),
        ("O3", "O3", 0),
    ]


def test_ut04_34_org_closure_cycle_and_depth_cap(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-34 a parent cycle keeps the minimum depth per pair; a 25-level chain stops at 20."""
    con.execute("INSERT INTO core.org (org_id, parent_org_id) VALUES ('X1', 'X2'), ('X2', 'X1')")
    chain = [(f"L{i:02d}", f"L{i + 1:02d}" if i < 24 else None) for i in range(25)]
    con.executemany("INSERT INTO core.org (org_id, parent_org_id) VALUES (?, ?)", chain)
    _materialize(con)
    rows = _closure(con, "metrics.org_closure")
    assert [r for r in rows if str(r[0]).startswith("X")] == [
        ("X1", "X1", 0),
        ("X1", "X2", 1),
        ("X2", "X1", 1),
        ("X2", "X2", 0),
    ]
    l00 = [r for r in rows if r[0] == "L00"]
    assert len(l00) == 21
    assert max(int(str(r[2])) for r in l00) == 20
    assert ("L00", "L21", 21) not in rows


def test_ut04_34_work_item_closure_and_candidate(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-34 pairs over parent_key; the nearest todo/in-progress initiative, epic or feature
    ancestor-or-self is every row's candidate; a done epic is no candidate."""
    _materialize(con)
    assert _closure(con, "metrics.work_item_closure") == [
        ("W1", "W1", "W1", 0),
        ("W2", "W1", "W2", 1),
        ("W2", "W2", "W2", 0),
        ("W3", "W1", "W2", 2),
        ("W3", "W2", "W2", 1),
        ("W3", "W3", "W2", 0),
        ("W4", "W1", "W2", 3),
        ("W4", "W2", "W2", 2),
        ("W4", "W3", "W2", 1),
        ("W4", "W4", "W2", 0),
        ("W5", "W5", None, 0),
        ("W6", "W5", None, 1),
        ("W6", "W6", None, 0),
    ]


def test_ut04_34_work_item_cycle_depth_cap_and_tie(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-34 a parent_key cycle is cut at depth 10 with minimum-depth pairs; two candidates at
    the same depth (duplicate parent key) resolve to the lowest ancestor_record_id."""
    con.execute("DELETE FROM core.work_item")
    items = [
        ("A", "K-A", "story", "K-B", "todo"),
        ("B", "K-B", "story", "K-A", "todo"),
        ("C", "K-C", "story", "K-P", "todo"),
        ("P2", "K-P", "feature", None, "in_progress"),
        ("P1", "K-P", "epic", None, "todo"),
    ]
    con.executemany(
        "INSERT INTO core.work_item (record_id, key, type, parent_key, status_category)"
        " VALUES (?, ?, ?, ?, ?)",
        items,
    )
    chain = [(f"N{i:02d}", f"K{i:02d}", "story", f"K{i + 1:02d}", "todo") for i in range(12)]
    con.executemany(
        "INSERT INTO core.work_item (record_id, key, type, parent_key, status_category)"
        " VALUES (?, ?, ?, ?, ?)",
        [*chain, ("N12", "K12", "initiative", None, "todo")],
    )
    _materialize(con)
    rows = _closure(con, "metrics.work_item_closure")
    by_record: dict[str, list[tuple[object, ...]]] = {}
    for row in rows:
        by_record.setdefault(str(row[0]), []).append(row[1:])
    assert by_record["A"] == [("A", None, 0), ("B", None, 1)]
    assert by_record["C"] == [("C", "P1", 0), ("P1", "P1", 1), ("P2", "P1", 1)]
    assert max(int(str(r[2])) for r in by_record["N00"]) == 10
    assert {r[1] for r in by_record["N00"]} == {None}  # N12 lies beyond the depth cap
    assert {r[1] for r in by_record["N02"]} == {"N12"}


# --- UT04-35 split_statements and materialize_facts ------------------------------------------


GOOD = (
    "{# header #}\n\n"
    "-- @statement metrics.org_closure\nSELECT 1 AS a\n"
    "-- @statement metrics.work_item_closure\nSELECT 2 AS a\n"
    "-- @statement metrics.incident_fact  \r\nSELECT 3 AS a\n"
    "-- @statement metrics.change_fact\nSELECT 4 AS a\n"
    "-- @statement metrics.work_item_fact\nSELECT 5 AS a\n"
)


def test_ut04_35_fact_tables_constant() -> None:
    """UT04-35 FACT_TABLES names the five fact tables in U04-46 order."""
    assert FACT_TABLES == (
        "metrics.org_closure",
        "metrics.work_item_closure",
        "metrics.incident_fact",
        "metrics.change_fact",
        "metrics.work_item_fact",
    )


def test_ut04_35_split_shipped_file() -> None:
    """UT04-35 the shipped stage file splits into the five fact statements in order, markers
    removed."""
    segments = split_statements(facts._stage_text())
    assert [t for t, _ in segments] == list(FACT_TABLES)
    assert all("@statement" not in body for _, body in segments)


def test_ut04_35_split_segments_in_order() -> None:
    """UT04-35 segments hold the lines after each marker; Jinja comments may precede them."""
    assert split_statements(GOOD) == [
        ("metrics.org_closure", "SELECT 1 AS a"),
        ("metrics.work_item_closure", "SELECT 2 AS a"),
        ("metrics.incident_fact", "SELECT 3 AS a"),
        ("metrics.change_fact", "SELECT 4 AS a"),
        ("metrics.work_item_fact", "SELECT 5 AS a"),
    ]


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        (GOOD.replace("metrics.incident_fact", "metrics.Incident_fact"), "malformed"),
        (GOOD.replace("metrics.incident_fact", "core.incident"), "malformed"),
        ("SELECT 0\n" + GOOD, "only whitespace and Jinja comments"),
        ("{% set x = 1 %}\n" + GOOD, "only whitespace and Jinja comments"),
        (GOOD.replace("SELECT 2 AS a\n", "  \n"), "empty statement body"),
        (
            GOOD.replace("metrics.incident_fact", "metrics.work_item_fact"),
            "statements must be exactly",
        ),
        ("{# only a comment #}\n", "statements must be exactly"),
        (
            GOOD.replace("work_item_closure", "tmp").replace("org_closure", "work_item_closure"),
            "statements must be exactly",
        ),
    ],
)
def test_ut04_35_split_bad_file_raises(text: str, reason: str) -> None:
    """UT04-35 bad markers, a non-comment prefix, a blank body or a wrong table list raise
    ConfigError("400_facts.sql: <reason>")."""
    with pytest.raises(ConfigError, match=rf"^400_facts\.sql: {reason}"):
        split_statements(text)


def test_ut04_35_materialize_records_evidence(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-35 one meta.evidence row per statement with producer facts and the stage template;
    every fact row's query_id has its evidence row; the log line per table carries no SQL."""
    with capture_logs() as logs:
        query_ids = _materialize(con)
    assert len(query_ids) == 5
    rows = con.execute(
        "SELECT query_id, producer, row_count, executed_at, params->>'$.template.name',"
        " params->>'$.template.statement' FROM meta.evidence ORDER BY executed_at, query_id"
    ).fetchall()
    assert sorted(r[0] for r in rows) == sorted(query_ids)
    assert {r[1] for r in rows} == {"facts"}
    assert {r[3] for r in rows} == {NOW}
    assert {r[4] for r in rows} == {"400_facts"}
    assert sorted(str(r[5]) for r in rows) == sorted(FACT_TABLES)
    for table, qid in zip(FACT_TABLES, query_ids, strict=True):
        found = con.execute(
            f"SELECT count(*) FILTER (f.query_id IS DISTINCT FROM $q),"  # noqa: S608
            " count(*) = (SELECT row_count FROM meta.evidence WHERE query_id = $q)"
            f" FROM {table} f",
            {"q": qid},
        ).fetchone()
        assert found == (0, True)
    events = [e for e in logs if e["event"] == "metrics.facts.materialized"]
    assert [e["table"] for e in events] == list(FACT_TABLES)
    assert all(e["build_id"] == BUILD_ID and "sql" not in e for e in events)
    assert events[2]["row_count"] == 4
    assert events[2]["query_id"] == query_ids[2]


def test_ut04_35_materialize_is_repeatable(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-35 a second run on the same build replaces the tables and keeps the evidence."""
    first = _materialize(con)
    assert _materialize(con) == first
    count = con.execute("SELECT count(*) FROM meta.evidence").fetchone()
    assert count == (5,)


def test_ut04_35_binds_are_only_the_used_ones(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-35 the recorded bind holds exactly the names each statement used."""
    query_ids = _materialize(con)
    rows = dict(
        con.execute("SELECT query_id, json_keys(params->'$.bind') FROM meta.evidence").fetchall()
    )
    assert rows[query_ids[0]] == []
    assert rows[query_ids[1]] == []
    assert "d_cluster_min_membership" in rows[query_ids[2]]
    assert "w_engineer_hour" in rows[query_ids[2]]
    assert "s_org_metrics" not in rows[query_ids[2]]
    assert sorted(rows[query_ids[3]]) == ["d_change_link_min_score", "d_failure_outcomes"]
    assert rows[query_ids[4]] == []


def _stage(monkeypatch: pytest.MonkeyPatch, third: str) -> None:
    text = (
        "-- @statement metrics.org_closure\nSELECT 'o' AS org_id\n"
        "-- @statement metrics.work_item_closure\nSELECT 'w' AS record_id\n"
        f"-- @statement metrics.incident_fact\n{third}\n"
        "-- @statement metrics.change_fact\nSELECT 'c' AS record_id\n"
        "-- @statement metrics.work_item_fact\nSELECT 'w' AS record_id\n"
    )
    monkeypatch.setattr(facts, "_stage_text", lambda: text)


class _RollbackFails:
    """Connection proxy whose ROLLBACK raises, as after a lost or closed transaction."""

    def __init__(self, con: duckdb.DuckDBPyConnection) -> None:
        self._con = con
        self.rollbacks = 0

    def execute(self, sql: str, *args: object) -> object:
        if sql == "ROLLBACK":
            self.rollbacks += 1
            msg = "rollback failed"
            raise duckdb.TransactionException(msg)
        return self._con.execute(sql, *args)

    def __getattr__(self, name: str) -> object:
        return getattr(self._con, name)


def test_ut04_35_rollback_error_keeps_original(
    con: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT04-35 when ROLLBACK itself fails, the mapped SchemaViolation of the failed statement
    still surfaces (the rollback error does not mask it)."""
    _stage(monkeypatch, "SELECT CAST('x' AS INTEGER) AS n FROM core.incident")
    proxy = _RollbackFails(con)
    with (
        freeze_time(NOW),
        pytest.raises(SchemaViolation, match=r"^400_facts\.sql statement metrics\.incident_fact"),
    ):
        materialize_facts(proxy, BUILD_ID)  # type: ignore[arg-type]
    assert proxy.rollbacks == 1
    con.execute("ROLLBACK")


def test_ut04_35_failed_statement_rolls_back(
    con: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT04-35 a SQL error becomes SchemaViolation naming the statement; the transaction rolls
    back, so neither earlier fact tables nor evidence rows remain."""
    _stage(monkeypatch, "SELECT CAST('x' AS INTEGER) AS n FROM core.incident")
    with pytest.raises(SchemaViolation, match=r"^400_facts\.sql statement metrics\.incident_fact"):
        _materialize(con)
    tables = con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'metrics'"
    ).fetchone()
    evidence = con.execute("SELECT count(*) FROM meta.evidence").fetchone()
    assert (tables, evidence) == ((0,), (0,))


@pytest.mark.parametrize(
    ("third", "message"),
    [
        ("SELECT {{ p('no_such_bind') }} AS n", "unknown parameter"),
        ("SELECT {{ p('window_start') }} AS n", "window_start with no candidate"),
        ("SELECT {{ undefined_name }} AS n", "template render failed"),
    ],
)
def test_ut04_35_render_errors_raise_config_error(
    con: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch, third: str, message: str
) -> None:
    """UT04-35 an unknown bind, a bind without a candidate or a render error is ConfigError."""
    _stage(monkeypatch, third)
    with pytest.raises(ConfigError, match=message):
        _materialize(con)
    evidence = con.execute("SELECT count(*) FROM meta.evidence").fetchone()
    assert evidence == (0,)


@pytest.mark.parametrize(
    "table", ["core.org", "core.change", "core.work_item_transition", "core.work_item_link"]
)
def test_ut04_35_missing_input_raises(con: duckdb.DuckDBPyConnection, table: str) -> None:
    """UT04-35 a missing input table (core.org and the change/work-item inputs) is
    SchemaViolation before any write."""
    con.execute(f"DROP TABLE {table}")
    with pytest.raises(SchemaViolation, match=rf"^stage 400 input {re.escape(table)} missing$"):
        _materialize(con)
    evidence = con.execute("SELECT count(*) FROM meta.evidence").fetchone()
    assert evidence == (0,)


# --- ST04-13 parent_key cycle ------------------------------------------------------------------


def test_st04_13_thousand_node_cycle_is_bounded(con: duckdb.DuckDBPyConnection) -> None:
    """ST04-13 a 1,000-node parent_key cycle: the closure stops at depth 10 (11 rows per item)
    and stage 400 completes in under 5 s (TH04-13)."""
    con.execute("DELETE FROM core.work_item")
    n = 1000
    con.executemany(
        "INSERT INTO core.work_item (record_id, key, type, parent_key, status_category)"
        " VALUES (?, ?, 'story', ?, 'todo')",
        [(f"R{i:04d}", f"K{i:04d}", f"K{(i + 1) % n:04d}") for i in range(n)],
    )
    start = time.perf_counter()
    _materialize(con)
    elapsed = time.perf_counter() - start
    stats = con.execute(
        "SELECT count(*), max(depth), count(DISTINCT record_id) FROM metrics.work_item_closure"
    ).fetchone()
    assert stats == (n * 11, 10, n)
    assert elapsed < 5.0
