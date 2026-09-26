"""Unit tests for the setup SQL and DuckDB macros (UT02-60 … UT02-64; U02-107, U02-108)."""

import datetime
import re
from pathlib import Path

import duckdb
import pytest
from tests.support.build_harness import BuildHarness

from herness.model import sqlfiles

pytestmark = pytest.mark.unit

UTC = datetime.UTC
SQL_DIR = Path(sqlfiles.__file__).parent / "sql"


@pytest.fixture
def harness(build_harness: BuildHarness) -> BuildHarness:
    assert build_harness.run(0, 99) == ["000_settings.sql", "010_macros.sql"]
    return build_harness


def _select(harness: BuildHarness, expr: str, values: list[object]) -> list[object]:
    """`expr` (over column x) for each value, in input order."""
    rows = harness.query(
        f"SELECT {expr} FROM (SELECT unnest(?) AS x, generate_subscripts(?, 1) AS i) ORDER BY i",  # noqa: S608
        [values, values],
    )
    return [r[0] for r in rows]


def _ts(*parts: int) -> datetime.datetime:
    return datetime.datetime(*parts, tzinfo=UTC)  # type: ignore[misc]


TS_CASES: list[tuple[str | None, datetime.datetime | None]] = [
    ("2024-03-01 10:20:30", _ts(2024, 3, 1, 10, 20, 30)),
    ("2024-03-01 10:20:30.5", _ts(2024, 3, 1, 10, 20, 30, 500000)),
    ("2024-03-01 10:20:30.123456", _ts(2024, 3, 1, 10, 20, 30, 123456)),
    ("2024-03-01T10:20:30", _ts(2024, 3, 1, 10, 20, 30)),
    ("2024-03-01T10:20:30.250", _ts(2024, 3, 1, 10, 20, 30, 250000)),
    ("2024-03-01T10:20:30+0000", _ts(2024, 3, 1, 10, 20, 30)),
    ("2024-03-01T10:20:30+0200", _ts(2024, 3, 1, 8, 20, 30)),
    ("2024-03-01T10:20:30.123+0000", _ts(2024, 3, 1, 10, 20, 30, 123000)),
    ("2024-03-01T10:20:30.123-0530", _ts(2024, 3, 1, 15, 50, 30, 123000)),
    ("2024-03-01T10:20:30Z", _ts(2024, 3, 1, 10, 20, 30)),
    ("2024-03-01T10:20:30.5Z", _ts(2024, 3, 1, 10, 20, 30, 500000)),
    ("2024-02-29 23:59:59", _ts(2024, 2, 29, 23, 59, 59)),
    ("31/02/2024", None),
    ("1717000000000", None),
    ("", None),
    (None, None),
    ("2024-02-30 10:00:00", None),
    ("2024-03-01", None),
    ("2024-03-01 10:20", None),
    ("not a time", None),
]


def test_ut02_60_ts_utc_twenty_strings(harness: BuildHarness) -> None:
    """UT02-60 ts_utc parses every listed format to UTC; other strings and NULL are NULL."""
    assert len(TS_CASES) == 20
    values = [v for v, _ in TS_CASES]
    assert _select(harness, "ts_utc(x)", values) == [want for _, want in TS_CASES]
    typed = harness.query("SELECT typeof(ts_utc('2024-03-01 10:20:30'))")
    assert typed == [("TIMESTAMP WITH TIME ZONE",)]


def test_ut02_60_ts_utc_ignores_session_zone(harness: BuildHarness) -> None:
    """UT02-60 naive strings read as UTC even when the session time zone differs."""
    harness.con.execute("SET TimeZone = 'America/New_York'")
    try:
        got = harness.query("SELECT epoch(ts_utc('1970-01-01 01:00:00'))")
    finally:
        harness.con.execute("SET TimeZone = 'UTC'")
    assert got == [(3600.0,)]


def test_ut02_60_to_date(harness: BuildHarness) -> None:
    """UT02-60 to_date parses `%Y-%m-%d` only."""
    got = _select(harness, "to_date(x)", ["2024-02-29", "2024-02-30", "29/02/2024", None])
    assert got == [datetime.date(2024, 2, 29), None, None, None]


def test_ut02_60_settings_schemas_and_tables(harness: BuildHarness) -> None:
    """UT02-60 000_settings.sql creates the six schemas and the fixed tables; re-run is a no-op."""
    schemas = {r[0] for r in harness.query("SELECT schema_name FROM information_schema.schemata")}
    assert {"stg", "core", "enrich", "metrics", "score", "meta"} <= schemas
    harness.con.execute("INSERT INTO stg.build_counts VALUES ('n', 1)")
    harness.run(0, 99)
    assert harness.query("SELECT * FROM stg.build_counts") == [("n", 1)]
    tables = {
        f"{r[0]}.{r[1]}": r[2]
        for r in harness.query(
            "SELECT table_schema, table_name, count(*) FROM information_schema.columns"
            " WHERE table_schema IN ('meta', 'enrich', 'stg') GROUP BY ALL"
        )
    }
    assert tables == {
        "meta.build": 9,
        "meta.evidence": 8,
        "meta.dq_result": 6,
        "enrich.text_redacted": 4,
        "enrich.decision": 12,
        "enrich.cluster": 9,
        "enrich.cluster_member": 3,
        "enrich.incident_change_link": 4,
        "stg.cast_stats": 4,
        "stg.build_counts": 2,
    }
    harness.con.execute("INSERT INTO meta.evidence (query_id) VALUES ('q_1')")
    with pytest.raises(duckdb.ConstraintException):
        harness.con.execute("INSERT INTO meta.evidence (query_id) VALUES ('q_1')")
    cluster = harness.query(
        "SELECT data_type FROM information_schema.columns"
        " WHERE table_schema = 'enrich' AND table_name = 'cluster' AND column_name = 'top_terms'"
    )
    assert cluster == [("VARCHAR[]",)]


def test_ut02_60_macros_live_in_main_not_temp(harness: BuildHarness) -> None:
    """UT02-60 the macros are persistent in schema main of the build file."""
    names = {
        r[0]
        for r in harness.query(
            "SELECT function_name FROM duckdb_functions()"
            " WHERE schema_name = 'main' AND NOT internal AND database_name <> 'temp'"
        )
    }
    assert {
        "ts_utc",
        "to_date",
        "to_bool",
        "lead_int",
        "to_double",
        "to_int",
        "sn_duration_s",
        "jstr",
        "json_names",
        "json_str_list",
        "jira_text",
        "team_value",
    } <= names


def test_ut02_61_lead_int(harness: BuildHarness) -> None:
    """UT02-61 lead_int reads a leading integer inside [lo, hi]."""
    values = ["1 - Critical", "2", "P2-ish", "9", "3-High", "0", None]
    assert _select(harness, "lead_int(x, 1, 5)", values) == [1, 2, None, None, 3, None, None]
    assert harness.query("SELECT typeof(lead_int('1', 1, 5))") == [("INTEGER",)]


def test_ut02_62_to_bool(harness: BuildHarness) -> None:
    """UT02-62 to_bool maps the listed spellings case-insensitively; anything else is NULL."""
    values = ["true", "1", "YES", "y", "T", "false", "0", "No", "n", "F", "maybe", "", None]
    want = [True] * 5 + [False] * 5 + [None] * 3
    assert _select(harness, "to_bool(x)", values) == want


def test_ut02_62_sn_duration_s(harness: BuildHarness) -> None:
    """UT02-62 sn_duration_s: digits are seconds, glide `1970-01-01 HH:MM:SS` via ts_utc."""
    values = ["3600", "1970-01-01 02:00:00", "1970-01-03 00:00:01", "1 day", "", None]
    assert _select(harness, "sn_duration_s(x)", values) == [3600, 7200, 172801, None, None, None]
    assert harness.query("SELECT typeof(sn_duration_s('5'))") == [("BIGINT",)]


def test_ut02_62_to_double_and_to_int(harness: BuildHarness) -> None:
    """UT02-62 to_double drops non-finite values; to_int is a TRY_CAST."""
    values = ["1.5", "inf", "NaN", "-2", "x", None]
    assert _select(harness, "to_double(x)", values) == [1.5, None, None, -2.0, None, None]
    assert _select(harness, "to_int(x)", ["42", "-7", "x", None]) == [42, -7, None, None]


ADF = (
    '{"type":"doc","version":1,"content":[{"type":"paragraph","content":['
    '{"type":"text","text":"Disk \\"full\\""},{"type":"text","text":"on db-1\\nretry"}]}]}'
)


def test_ut02_63_jira_text(harness: BuildHarness) -> None:
    """UT02-63 jira_text joins ADF text values; wiki text, other JSON and NULL pass through."""
    wiki = "h1. Outage\n*db-1* is {color:red}down{color}"
    other = '{"type":"paragraph","text":"x"}'
    got = _select(harness, "jira_text(x)", [ADF, wiki, None, other, "broken {"])
    assert got == ['Disk "full" on db-1\nretry', wiki, None, other, "broken {"]


def test_ut02_64_json_macros(harness: BuildHarness) -> None:
    """UT02-64 json_names, json_str_list and jstr read valid JSON; invalid JSON is NULL."""
    values = ['[{"name":"a"},{"name":"b"}]', '["x","y"]', '{"a":"1"}', "not json", "", None]
    assert _select(harness, "json_names(x)", values) == [["a", "b"], [], [], None, None, None]
    got = _select(harness, "json_str_list(x)", values)
    assert got == [['{"name":"a"}', '{"name":"b"}'], ["x", "y"], [], None, None, None]
    assert _select(harness, "jstr(x, '$.a')", values) == [None, None, "1", None, None, None]


def test_ut02_64_team_value(harness: BuildHarness) -> None:
    """UT02-64 team_value: object name/title/value, JSON string value, else the input."""
    values = [
        '{"name":"Payments","value":"v"}',
        '{"title":"Core"}',
        '{"value":"Ops"}',
        '{"id":1}',
        '"Quoted"',
        "Plain team",
        "123",
        None,
    ]
    want = ["Payments", "Core", "Ops", None, "Quoted", "Plain team", "123", None]
    assert _select(harness, "team_value(x)", values) == want


def test_st02_12_setup_sql_never_installs_or_loads() -> None:
    """ST02-12 no build SQL file or macro file installs or loads a DuckDB extension."""
    pattern = re.compile(r"\b(INSTALL|LOAD)\b", re.IGNORECASE)
    for path in sorted(SQL_DIR.iterdir()):
        text = re.sub(r"--[^\n]*", "", path.read_text(encoding="utf-8"))
        assert pattern.search(text) is None, path.name
