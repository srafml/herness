"""Stage 400 delivery facts (impl 04 T04-07): `metrics.change_fact` (U04-44) and
`metrics.work_item_fact` (U04-45) from `400_facts.sql` on `metrics_tiny` plus inserted rows."""

import datetime
from collections.abc import Iterator

import duckdb
import pytest
from freezegun import freeze_time
from tests.support.metrics_tiny import BUILD_ID, build_metrics_tiny, patch_facts_config

from herness.metrics.facts import materialize_facts

pytestmark = pytest.mark.unit

UTC = datetime.UTC
NOW = datetime.datetime(2026, 4, 1, 6, 30, tzinfo=UTC)
_TSTZ = "TIMESTAMP WITH TIME ZONE"
CHANGE_COLUMNS = [
    *(("record_id", "VARCHAR"), ("type", "VARCHAR"), ("service_id", "VARCHAR")),
    *(("team_id", "VARCHAR"), ("org_id", "VARCHAR"), ("actual_end", _TSTZ)),
    *(("deployed", "BOOLEAN"), ("failed", "BOOLEAN"), ("linked_incident_count", "INTEGER")),
    *(("lead_time_h", "DOUBLE"), ("query_id", "VARCHAR")),
]
WORK_ITEM_COLUMNS = [
    *(("record_id", "VARCHAR"), ("key", "VARCHAR"), ("type", "VARCHAR")),
    *(("parent_key", "VARCHAR"), ("status_category", "VARCHAR"), ("service_id", "VARCHAR")),
    *(("team_id", "VARCHAR"), ("org_id", "VARCHAR"), ("story_points", "DOUBLE")),
    *(("created_at", _TSTZ), ("first_in_progress_at", _TSTZ), ("done_at", _TSTZ)),
    *(("cycle_days", "DOUBLE"), ("is_unplanned", "BOOLEAN"), ("query_id", "VARCHAR")),
]


def _ts(day: str, hour: int = 10) -> datetime.datetime:
    return datetime.datetime.fromisoformat(f"2026-{day}T{hour:02d}:00:00+00:00")


# record_id, type, opened_at, actual_end, service_id, team_id, outcome
CHANGES = [
    ("C1", "normal", _ts("03-01"), _ts("03-01", 16), "S1", "T1", "successful"),
    ("C2", "emergency", None, _ts("03-02"), "S1", "T1", "unsuccessful"),
    ("C3", "normal", _ts("03-04"), _ts("03-03"), "S2", "T1", "canceled"),
    ("C4", "standard", _ts("03-04"), None, None, None, None),
    ("C5", "normal", _ts("03-05"), _ts("03-05"), "S2", "T2", "backed_out"),
    ("C6", "normal", _ts("03-06"), _ts("03-07"), "S2", "T2", None),
]
# record_id, state, caused_by_change_id (incident "ID" is excluded by its canceled state)
INCIDENTS = [
    ("IA", "closed", "C1"),
    ("IB", "closed", None),
    ("IC", "closed", None),
    ("ID", "canceled", "C1"),
    ("IE", "closed", "C3"),
]
# incident_id, change_id, score (change_link_min_score is 0.7; "IX" is no core.incident row)
LINKS = [
    ("IA", "C1", 0.95),
    ("IB", "C1", 0.7),
    ("IC", "C1", 0.5),
    ("ID", "C5", 0.9),
    ("IX", "C6", 0.9),
]
# record_id, key, type, status_category, resolved_at, team_id, story_points
WORK_ITEMS = [
    ("WA", "K-A", "story", "done", None, "T1", 3.0),
    ("WB", "K-B", "bug", "done", _ts("03-02", 12), "T1", None),
    ("WC", "K-C", "story", "in_progress", _ts("03-02"), "T2", 5.0),
    ("WD", "K-D", "task", "done", None, None, 1.0),
    ("WE", "K-E", "story", "done", _ts("03-02", 22), "T2", 2.0),
    ("WF", "K-F", None, "todo", None, None, None),
]
# record_id, to_category, at
TRANSITIONS = [
    ("WA", "in_progress", _ts("03-01")),
    ("WA", "done", _ts("03-03")),
    ("WA", "in_progress", _ts("03-04")),
    ("WA", "done", _ts("03-05")),
    ("WC", "in_progress", _ts("03-01")),
    ("WC", "done", _ts("03-02")),
    ("WC", "in_progress", _ts("03-03")),
    ("WD", "done", _ts("03-01")),
    ("WD", "in_progress", _ts("03-02")),
    ("WE", "in_progress", _ts("03-01")),
    ("WE", "todo", _ts("03-01", 12)),
]
# from_key, to_key, link_type
WORK_ITEM_LINKS = [
    ("K-C", "INC0042", "mentions_incident"),
    ("INC0043", "K-D", "mentions_incident"),
    ("K-A", "K-E", "blocks"),
    ("K-E", "K-A", "relates"),
]


@pytest.fixture
def con(monkeypatch: pytest.MonkeyPatch) -> Iterator[duckdb.DuckDBPyConnection]:
    patch_facts_config(monkeypatch)
    c = build_metrics_tiny()
    for table in ("core.incident", "enrich.incident_change_link", "core.work_item", "core.change"):
        c.execute(f"DELETE FROM {table}")  # noqa: S608 - fixed table names
    c.executemany(
        "INSERT INTO core.change (record_id, number, type, opened_at, actual_end, service_id,"
        " team_id, outcome, short_description, description)"
        " VALUES (?, 'CHG-' || ?, ?, ?, ?, ?, ?, ?, 'free text', 'free text')",
        [(r[0], r[0], *r[1:]) for r in CHANGES],
    )
    c.executemany(
        "INSERT INTO core.incident (record_id, number, opened_at, state, caused_by_change_id)"
        " VALUES (?, 'INC-' || ?, TIMESTAMPTZ '2026-03-01 00:00:00+00', ?, ?)",
        [(r[0], r[0], *r[1:]) for r in INCIDENTS],
    )
    c.executemany(
        "INSERT INTO enrich.incident_change_link (incident_id, change_id, method, score)"
        " VALUES (?, ?, 'time_window', ?)",
        LINKS,
    )
    c.executemany(
        "INSERT INTO core.work_item (record_id, key, type, parent_key, status_category,"
        " resolved_at, team_id, service_id, story_points, created_at, summary, description)"
        " VALUES (?, ?, ?, 'K-P', ?, ?, ?, 'S1', ?, TIMESTAMPTZ '2026-02-01 00:00:00+00',"
        " 'free text', 'free text')",
        WORK_ITEMS,
    )
    c.executemany(
        'INSERT INTO core.work_item_transition (record_id, to_category, "at") VALUES (?, ?, ?)',
        TRANSITIONS,
    )
    c.executemany(
        "INSERT INTO core.work_item_link (from_key, to_key, link_type) VALUES (?, ?, ?)",
        WORK_ITEM_LINKS,
    )
    yield c
    c.close()


def _materialize(con: duckdb.DuckDBPyConnection) -> list[str]:
    with freeze_time(NOW):
        return materialize_facts(con, BUILD_ID)


def _rows(
    con: duckdb.DuckDBPyConnection, table: str, columns: str
) -> dict[str, tuple[object, ...]]:
    rows = con.execute(f"SELECT record_id, {columns} FROM {table}").fetchall()  # noqa: S608
    return {str(r[0]): tuple(r[1:]) for r in rows}


# --- UT04-32 metrics.change_fact ---------------------------------------------------------------


def test_ut04_32_change_fact_deployed_failed_and_counts(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-32 deployed needs actual_end and an outcome other than canceled; failed needs
    deployed and a failure outcome or a linked incident; linked_incident_count counts distinct
    non-excluded incidents by caused_by_change_id or a link with score >= 0.7."""
    _materialize(con)
    got = _rows(con, "metrics.change_fact", "deployed, failed, linked_incident_count")
    assert got == {
        "C1": (True, True, 2),  # IA (cause and link), IB (link at 0.7); IC 0.5, ID excluded
        "C2": (True, True, 0),  # outcome unsuccessful
        "C3": (False, False, 1),  # canceled: never deployed, so never failed
        "C4": (False, False, 0),  # no actual_end
        "C5": (True, True, 0),  # outcome backed_out; ID link is an excluded incident
        "C6": (True, False, 0),  # IX is no incident
    }


def test_ut04_32_change_fact_copies_and_lead_time(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-32 copied columns, org of the team (NULL without team), and lead_time_h in hours;
    NULL when opened_at is NULL, actual_end is NULL or opened_at > actual_end."""
    _materialize(con)
    got = _rows(con, "metrics.change_fact", "type, service_id, team_id, org_id, actual_end")
    assert got["C1"] == ("normal", "S1", "T1", "O2", _ts("03-01", 16))
    assert got["C4"] == ("standard", None, None, None, None)
    assert got["C5"] == ("normal", "S2", "T2", "O3", _ts("03-05"))
    lead = _rows(con, "metrics.change_fact", "lead_time_h")
    assert lead == {
        "C1": (6.0,),
        "C2": (None,),
        "C3": (None,),
        "C4": (None,),
        "C5": (0.0,),
        "C6": (24.0,),
    }


def test_ut04_32_change_fact_shape_and_invariants(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-32 design 04 §4.2 column order and types (no text column, TH04-04); one row per
    core.change; failed => deployed; lead_time_h NULL or >= 0; counts >= 0; one query_id."""
    query_ids = _materialize(con)
    res = con.execute("SELECT * FROM metrics.change_fact")
    assert [(d[0], str(d[1])) for d in res.description] == CHANGE_COLUMNS
    stats = con.execute(
        "SELECT (SELECT count(*) FROM core.change), count(*),"
        " count(*) FILTER (failed AND NOT deployed),"
        " count(*) FILTER (lead_time_h < 0 OR linked_incident_count < 0"
        "   OR linked_incident_count IS NULL OR deployed IS NULL OR failed IS NULL),"
        " count(DISTINCT query_id), min(query_id)"
        " FROM metrics.change_fact"
    ).fetchone()
    assert stats == (6, 6, 0, 0, 1, query_ids[3])


def test_ut04_32_change_fact_empty_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT04-32 the metrics_tiny fixture with its core.change rows removed gives an empty
    change_fact and a work_item_fact row per fixture work item."""
    patch_facts_config(monkeypatch)
    c = build_metrics_tiny()
    c.execute("DELETE FROM core.change")
    try:
        _materialize(c)
        counts = c.execute(
            "SELECT (SELECT count(*) FROM metrics.change_fact),"
            " (SELECT count(*) FROM metrics.work_item_fact),"
            " (SELECT count(*) FROM core.work_item)"
        ).fetchone()
    finally:
        c.close()
    assert counts is not None
    assert counts[0] == 0
    assert counts[1] == counts[2] > 0


# --- UT04-33 metrics.work_item_fact ------------------------------------------------------------


def test_ut04_33_work_item_fact_timestamps_and_cycle(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-33 first_in_progress_at = first in_progress transition; done_at only for done items:
    the last done transition, else resolved_at; cycle_days NULL when either is missing or
    done_at precedes first_in_progress_at."""
    _materialize(con)
    got = _rows(con, "metrics.work_item_fact", "first_in_progress_at, done_at, cycle_days")
    assert got == {
        "WA": (_ts("03-01"), _ts("03-05"), 4.0),
        "WB": (None, _ts("03-02", 12), None),  # no transitions: resolved_at fallback
        "WC": (_ts("03-01"), None, None),  # not currently done (OI04-07)
        "WD": (_ts("03-02"), _ts("03-01"), None),  # negative cycle
        "WE": (_ts("03-01"), _ts("03-02", 22), 1.5),  # resolved_at fallback
        "WF": (None, None, None),
    }


def test_ut04_33_work_item_fact_unplanned_and_copies(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-33 is_unplanned for a bug or a key on either end of a mentions_incident link (never
    NULL); copied columns; org of the team, NULL without team."""
    _materialize(con)
    unplanned = _rows(con, "metrics.work_item_fact", "is_unplanned")
    assert unplanned == {
        "WA": (False,),
        "WB": (True,),
        "WC": (True,),
        "WD": (True,),
        "WE": (False,),
        "WF": (False,),
    }
    got = _rows(
        con,
        "metrics.work_item_fact",
        "key, type, parent_key, status_category, service_id, team_id, org_id, story_points,"
        " created_at",
    )
    created = datetime.datetime(2026, 2, 1, tzinfo=UTC)
    assert got["WA"] == ("K-A", "story", "K-P", "done", "S1", "T1", "O2", 3.0, created)
    assert got["WD"] == ("K-D", "task", "K-P", "done", "S1", None, None, 1.0, created)
    assert got["WE"][5:7] == ("T2", "O3")


def test_ut04_33_work_item_fact_shape_and_invariants(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-33 design 04 §4.2 column order and types (no summary or description, TH04-04);
    one row per core.work_item; cycle_days NULL or >= 0; one query_id."""
    query_ids = _materialize(con)
    res = con.execute("SELECT * FROM metrics.work_item_fact")
    assert [(d[0], str(d[1])) for d in res.description] == WORK_ITEM_COLUMNS
    stats = con.execute(
        "SELECT (SELECT count(*) FROM core.work_item), count(*),"
        " count(*) FILTER (cycle_days < 0 OR is_unplanned IS NULL"
        "   OR (done_at IS NOT NULL AND status_category <> 'done')),"
        " count(DISTINCT query_id), min(query_id)"
        " FROM metrics.work_item_fact"
    ).fetchone()
    assert stats == (6, 6, 0, 1, query_ids[4])
