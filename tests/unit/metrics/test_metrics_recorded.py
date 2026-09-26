"""Tests for recorded execution in herness.metrics.evidence (U04-10 … U04-12, T04-05)."""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb
import pytest
from structlog.testing import capture_logs

from herness.core import ids
from herness.core.errors import ConfigError, QueryError, SchemaViolation
from herness.core.types import Evidence
from herness.metrics import evidence
from herness.metrics.evidence import IntoSpec, RecordedQuery, result_hash, run_recorded

pytestmark = pytest.mark.unit

UTC = datetime.UTC
BUILD = "20260924-211403-ABCDEF"
OTHER_BUILD = "20260925-080000-ZZZZZZ"
SETTINGS_SQL = (
    Path(__file__).resolve().parents[3] / "herness" / "model" / "sql" / "000_settings.sql"
)
SELECT = "SELECT k, v, d FROM core.t WHERE v >= $min_v"
PARAMS: dict[str, Any] = {"bind": {"min_v": 1.0}, "template": {"name": "t_test"}}
T_ROWS = [("a", 1.0, "1.50"), ("b", 2.5, "2.25"), ("c", 0.5, "3.00"), ("d", 4.0, None)]
_QID_LEN = 18


@pytest.fixture
def con() -> Iterator[duckdb.DuckDBPyConnection]:
    c = duckdb.connect()
    c.execute(SETTINGS_SQL.read_text(encoding="utf-8"))
    c.execute("INSERT INTO meta.build (build_id, status) VALUES (?, 'building')", [BUILD])
    c.execute("CREATE TABLE core.t (k VARCHAR, v DOUBLE, d DECIMAL(10, 2))")
    c.executemany("INSERT INTO core.t VALUES (?, ?, ?)", T_ROWS)
    yield c
    c.close()


@pytest.fixture
def samples(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, float, dict[str, str]]]:
    seen: list[tuple[str, str, float, dict[str, str]]] = []

    def histogram(name: str, value: float, *, component: str, labels: dict[str, str]) -> None:
        assert component == "metrics"
        seen.append(("histogram", name, value, dict(labels)))

    def counter(name: str, value: float = 1.0, *, component: str, labels: dict[str, str]) -> None:
        assert component == "metrics"
        seen.append(("counter", name, value, dict(labels)))

    monkeypatch.setattr(evidence, "record_histogram", histogram)
    monkeypatch.setattr(evidence, "record_counter", counter)
    return seen


def _evidence_rows(c: duckdb.DuckDBPyConnection) -> list[tuple[Any, ...]]:
    return c.execute(
        "SELECT query_id, sql, params, result_hash, row_count, result_sample, producer"
        " FROM meta.evidence ORDER BY query_id"
    ).fetchall()


def _stored_hash(c: duckdb.DuckDBPyConnection, table: str, id_column: str) -> str:
    res = c.execute(f"SELECT * EXCLUDE ({id_column}) FROM {table}")  # noqa: S608 - test table
    columns = [(d[0], str(d[1])) for d in res.description]
    return result_hash(columns, res.fetchall())


def test_ut04_07_second_run_keeps_one_evidence_row(
    con: duckdb.DuckDBPyConnection, samples: list[Any]
) -> None:
    """UT04-07 producer="metrics" twice: one meta.evidence row, the second call keeps it."""
    first = run_recorded(con, SELECT, PARAMS, "metrics")
    second = run_recorded(con, SELECT, PARAMS, "metrics")
    assert first.query_id == second.query_id
    assert first.result_hash == second.result_hash
    rows = _evidence_rows(con)
    assert len(rows) == 1
    qid, sql, params, rhash, count, sample, producer = rows[0]
    assert (qid, sql, rhash, count, producer) == (
        first.query_id,
        ids.normalize_sql(SELECT),
        first.result_hash,
        3,
        "metrics",
    )
    assert params == ids.canonical_json(first.params)
    assert json.loads(sample) == first.result_sample
    assert first.build_id == BUILD
    assert first.row_count == 3
    assert samples[0][:2] == ("histogram", "herness_metrics_query_duration_seconds")
    assert samples[0][3] == {"producer": "metrics"}
    assert samples[1] == ("counter", "herness_metrics_query_rows_total", 3, {"producer": "metrics"})


def test_ut04_07_logs_query_recorded(con: duckdb.DuckDBPyConnection, samples: list[Any]) -> None:
    """UT04-07 success logs metrics.query.recorded with the §8.1 fields and no SQL text."""
    with capture_logs() as logs:
        rq = run_recorded(con, SELECT, PARAMS, "metrics")
    events = [e for e in logs if e["event"] == "metrics.query.recorded"]
    assert len(events) == 1
    event = events[0]
    assert event["log_level"] == "debug"
    assert event["query_id"] == rq.query_id
    assert event["producer"] == "metrics"
    assert event["template"] == "t_test"
    assert event["row_count"] == 3
    assert event["build_id"] == BUILD
    assert event["duration_ms"] == rq.duration_ms
    assert "sql" not in event


def test_ut04_08_no_producer_returns_rows_and_maps_evidence(
    con: duckdb.DuckDBPyConnection, samples: list[Any]
) -> None:
    """UT04-08 producer=None: no evidence row; rows returned; to_evidence maps fields 1:1."""
    rq = run_recorded(con, SELECT, PARAMS, None)
    assert _evidence_rows(con) == []
    assert isinstance(rq, RecordedQuery)
    assert rq.columns == (("k", "VARCHAR"), ("v", "DOUBLE"), ("d", "DECIMAL(10,2)"))
    assert rq.rows is not None
    assert sorted(rq.rows, key=lambda r: str(r[0])) == [
        ("a", 1.0, decimal.Decimal("1.50")),
        ("b", 2.5, decimal.Decimal("2.25")),
        ("d", 4.0, None),
    ]
    assert rq.row_count == len(rq.rows) == 3
    assert rq.result_hash == result_hash(rq.columns, rq.rows)
    assert len(rq.query_id) == _QID_LEN
    assert rq.query_id.startswith("q_")
    assert rq.executed_at.tzinfo is not None
    assert rq.duration_ms >= 0
    assert samples[0][3] == {"producer": "none"}
    ev = rq.to_evidence("run_1")
    assert isinstance(ev, Evidence)
    assert ev.run_id == "run_1"
    assert (ev.query_id, ev.build_id, ev.sql, ev.params) == (
        rq.query_id,
        rq.build_id,
        rq.sql,
        rq.params,
    )
    assert (ev.result_hash, ev.row_count, ev.result_sample) == (
        rq.result_hash,
        rq.row_count,
        rq.result_sample,
    )
    assert (ev.executed_at, ev.duration_ms) == (rq.executed_at, rq.duration_ms)
    assert rq.to_evidence(None).run_id is None


def test_ut04_08_max_rows_and_sample_limit(
    con: duckdb.DuckDBPyConnection, samples: list[Any]
) -> None:
    """UT04-08 more than max_rows rows is a QueryError with query_id; sample capped at 50."""
    with pytest.raises(QueryError, match="result too large: more than 2 rows") as info:
        run_recorded(con, SELECT, PARAMS, None, max_rows=2)
    assert str(info.value.context["query_id"]).startswith("q_")
    big = run_recorded(con, "SELECT range AS x FROM range(25000)", _params(), None)
    assert big.row_count == 25_000
    assert len(big.result_sample) == evidence.RESULT_SAMPLE_LIMIT


def _params(**bind: object) -> dict[str, Any]:
    return {"bind": bind, "template": {"name": "t_test"}}


def test_ut04_09_into_replace_hash_equals_rerun(
    con: duckdb.DuckDBPyConnection, samples: list[Any]
) -> None:
    """UT04-09 into replace: stored hash equals a re-run of the SELECT; rows is None."""
    into = IntoSpec("metrics.metric_value", "replace", "query_id")
    stored = run_recorded(con, SELECT, PARAMS, "metrics", into=into)
    rerun = run_recorded(con, SELECT, PARAMS, None)
    assert stored.rows is None
    assert stored.result_hash == rerun.result_hash
    assert stored.row_count == 3
    assert stored.query_id == rerun.query_id
    qids = con.execute("SELECT DISTINCT query_id FROM metrics.metric_value").fetchall()
    assert qids == [(stored.query_id,)]
    assert _stored_hash(con, "metrics.metric_value", "query_id") == stored.result_hash
    assert len(_evidence_rows(con)) == 1


def test_ut04_09_into_append_and_query_ids(
    con: duckdb.DuckDBPyConnection, samples: list[Any]
) -> None:
    """UT04-09 into append (query_id and query_ids): hash equals a re-run of the SELECT."""
    replace = IntoSpec("metrics.change_fact", "replace", "query_id")
    first = run_recorded(con, SELECT, PARAMS, "facts", into=replace)
    append = IntoSpec("metrics.change_fact", "append", "query_id")
    other = _params(min_v=3.0)
    second = run_recorded(con, SELECT, other, "facts", into=append)
    assert second.row_count == 1
    assert second.result_hash == run_recorded(con, SELECT, other, None).result_hash
    assert con.execute("SELECT count(*) FROM metrics.change_fact").fetchone() == (4,)
    upstream = (second.query_id,)
    lists = IntoSpec("score.funding", "replace", "query_ids", upstream)
    scored = run_recorded(con, SELECT, PARAMS, "score", into=lists)
    got = con.execute("SELECT DISTINCT query_ids FROM score.funding").fetchall()
    assert got == [([scored.query_id, second.query_id],)]
    assert scored.result_hash == first.result_hash
    appended = IntoSpec("score.funding", "append", "query_ids", upstream)
    again = run_recorded(con, SELECT, other, "score", into=appended)
    assert again.result_hash == second.result_hash
    assert again.row_count == 1


def test_ut04_09_parallel_hash_equals_in_process(
    con: duckdb.DuckDBPyConnection, samples: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT04-09 read-back at or above HASH_PARALLEL_MIN_ROWS hashes in worker processes equally."""
    sql = "SELECT range AS x, range * 0.5 AS y, 'r' || range AS z FROM range($n)"
    into = IntoSpec("metrics.incident_fact", "replace", "query_id")
    local = run_recorded(con, sql, _params(n=25_000), "facts", into=into)
    monkeypatch.setattr(evidence, "HASH_PARALLEL_MIN_ROWS", 10)
    monkeypatch.setattr(evidence, "HASH_WORKERS", 2)
    parallel = run_recorded(con, sql, _params(n=25_000), "facts", into=into)
    assert parallel.result_hash == local.result_hash
    assert parallel.result_sample == local.result_sample
    assert parallel.row_count == 25_000
    plain = run_recorded(con, sql, _params(n=25_000), None)
    assert plain.result_hash == local.result_hash


def test_ut04_09_parallel_path_is_taken(
    con: duckdb.DuckDBPyConnection, samples: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT04-09 the worker pool is used exactly when the read-back count reaches the threshold."""
    from herness.metrics import _recorded  # noqa: PLC0415 - private module under test

    calls: list[int] = []
    real = _recorded.hash_in_workers

    def spy(*args: Any, **kwargs: Any) -> None:
        calls.append(1)
        real(*args, **kwargs)

    monkeypatch.setattr(_recorded, "hash_in_workers", spy)
    into = IntoSpec("metrics.incident_fact", "replace", "query_id")
    run_recorded(con, SELECT, PARAMS, "facts", into=into)
    assert calls == []
    monkeypatch.setattr(evidence, "HASH_PARALLEL_MIN_ROWS", 3)
    monkeypatch.setattr(evidence, "HASH_WORKERS", 1)
    run_recorded(con, SELECT, PARAMS, "facts", into=into)
    assert calls == [1]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("core.incident", "replace", "query_id"), "table core.incident is not writable"),
        (("meta.evidence", "append", "query_id"), "not writable by metrics"),
        (("metrics.metric_value; DROP", "replace", "query_id"), "not writable by metrics"),
        (("score.funding", "replace", "query_id", ("q_0123456789abcdef",)), "bad upstream"),
        (("score.funding", "replace", "query_ids", ("q_XYZ",)), "bad upstream query id"),
        (("score.funding", "upsert", "query_ids"), "bad into mode"),
        (("score.funding", "replace", "ids"), "bad into id column"),
    ],
)
def test_ut04_09_intospec_rejects(args: tuple[Any, ...], message: str) -> None:
    """UT04-09 IntoSpec refuses tables outside WRITABLE_TABLES and bad upstream IDs."""
    with pytest.raises(ConfigError, match=message):
        IntoSpec(*args)


def test_ut04_09_into_requires_producer(con: duckdb.DuckDBPyConnection) -> None:
    """UT04-09 materializing without a producer is refused (stored results are recorded)."""
    into = IntoSpec("metrics.metric_value", "replace", "query_id")
    with pytest.raises(ConfigError, match="producer"):
        run_recorded(con, SELECT, PARAMS, None, into=into)
    assert con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = 'metric_value'"
    ).fetchone() == (0,)


def test_st04_09_intospec_core_incident_refused() -> None:
    """ST04-09 IntoSpec("core.incident") is refused: writes stay inside metrics.* and score.*."""
    with pytest.raises(ConfigError, match=r"table core\.incident is not writable by metrics"):
        IntoSpec("core.incident", "replace", "query_id")
    assert all(t.split(".")[0] in {"metrics", "score"} for t in evidence.WRITABLE_TABLES)


def test_ut04_10_params_canonical_and_query_id(
    con: duckdb.DuckDBPyConnection, samples: list[Any]
) -> None:
    """UT04-10 Decimal/date/datetime params: JSON types, sorted keys, ids.query_id output."""
    sql = (
        "SELECT CAST($amount AS DECIMAL(10, 2)) AS a, CAST($day AS DATE) AS d,"
        " CAST($ts AS TIMESTAMPTZ) AS t"
    )
    bind = {
        "ts": datetime.datetime(2026, 9, 1, 12, 30, tzinfo=UTC),
        "day": datetime.date(2026, 9, 1),
        "amount": decimal.Decimal("12.50"),
    }
    rq = run_recorded(con, sql, {"bind": bind, "template": {"z": 1, "a": "x"}}, "metrics")
    expected = {
        "bind": {"amount": "12.50", "day": "2026-09-01", "ts": "2026-09-01T12:30:00.000000Z"},
        "template": {"a": "x", "z": 1},
    }
    assert rq.params == expected
    assert list(rq.params["bind"]) == sorted(bind)  # type: ignore[call-overload]
    assert rq.query_id == ids.query_id(ids.normalize_sql(sql), expected, BUILD)
    stored = con.execute("SELECT params FROM meta.evidence").fetchone()
    assert stored == (ids.canonical_json(expected),)
    assert rq.rows == [
        (decimal.Decimal("12.50"), datetime.date(2026, 9, 1), bind["ts"]),
    ]


def test_ut04_10_build_id_argument_and_meta_build(
    con: duckdb.DuckDBPyConnection, samples: list[Any]
) -> None:
    """UT04-10 the build_id argument feeds query_id; without it meta.build must hold one row."""
    norm = ids.normalize_sql(SELECT)
    rq = run_recorded(con, SELECT, PARAMS, None, build_id=OTHER_BUILD)
    assert rq.build_id == OTHER_BUILD
    assert rq.query_id == ids.query_id(norm, rq.params, OTHER_BUILD)
    con.execute("INSERT INTO meta.build (build_id) VALUES (?)", [OTHER_BUILD])
    with pytest.raises(SchemaViolation, match=r"meta.build must hold one row"):
        run_recorded(con, SELECT, PARAMS, None)
    con.execute("DELETE FROM meta.build")
    with pytest.raises(SchemaViolation, match=r"meta.build must hold one row"):
        run_recorded(con, SELECT, PARAMS, None)
    con.execute("DROP TABLE meta.build")
    with pytest.raises(SchemaViolation, match=r"meta.build must hold one row"):
        run_recorded(con, SELECT, PARAMS, None)


@pytest.mark.parametrize(
    ("sql", "params", "message"),
    [
        ("SELECT 1 -- note", _params(), "comments or semicolons"),
        ("SELECT /* x */ 1", _params(), "comments or semicolons"),
        ("SELECT 1; SELECT 2", _params(), "comments or semicolons"),
        ("SELECT $x AS x", {"bind": {"x": "a" * 70_000}, "template": {}}, "params too large"),
        ("SELECT 1", {"bind": {}}, "bind"),
        ("SELECT 1", {"bind": {}, "template": {}, "extra": 1}, "bind"),
        ("SELECT $X", {"bind": {"X": 1}, "template": {}}, "bad bind name"),
    ],
)
def test_ut04_10_rejected_inputs(
    con: duckdb.DuckDBPyConnection, sql: str, params: dict[str, Any], message: str
) -> None:
    """UT04-10 comments, semicolons, oversized or malformed params are ConfigError."""
    with pytest.raises(ConfigError, match=message):
        run_recorded(con, sql, params, "metrics")
    assert _evidence_rows(con) == []


def test_ut04_10_trailing_semicolon_is_normalized(
    con: duckdb.DuckDBPyConnection, samples: list[Any]
) -> None:
    """UT04-10 normalize_sql strips a trailing semicolon; the stored SQL is the normalized text."""
    rq = run_recorded(con, "SELECT   1 AS one ;", _params(), "metrics")
    assert rq.sql == "SELECT 1 AS one"
    assert rq.query_id == run_recorded(con, "SELECT 1 AS one", _params(), None).query_id


def test_ut04_66_timeout_interrupts(con: duckdb.DuckDBPyConnection, samples: list[Any]) -> None:
    """UT04-66 a slow query with timeout_s=0.1 is interrupted: QueryError timeout with query_id."""
    slow = "SELECT count(*) AS n FROM range(100000000000) a"
    with capture_logs() as logs, pytest.raises(QueryError, match=r"timeout after 0\.1s") as info:
        run_recorded(con, slow, _params(), "metrics", timeout_s=0.1)
    assert str(info.value.context["query_id"]).startswith("q_")
    failed = [e for e in logs if e["event"] == "metrics.query.failed"]
    assert failed[0]["error_class"] == "QueryError"
    assert failed[0]["template"] == "t_test"
    assert con.execute("SELECT 42").fetchone() == (42,)
    fast = run_recorded(con, SELECT, PARAMS, None, timeout_s=30.0)
    assert fast.row_count == 3


def test_ut04_66_duckdb_error_is_query_error(
    con: duckdb.DuckDBPyConnection, samples: list[Any]
) -> None:
    """UT04-66 a DuckDB error becomes QueryError with the DuckDB message and query_id."""
    with pytest.raises(QueryError, match="no_such") as info:
        run_recorded(con, "SELECT * FROM core.no_such", _params(), "metrics")
    assert str(info.value.context["query_id"]).startswith("q_")
    into = IntoSpec("metrics.metric_value", "append", "query_id")
    with pytest.raises(QueryError):
        run_recorded(con, SELECT, PARAMS, "metrics", into=into)
    assert _evidence_rows(con) == []


def test_st04_06_edited_evidence_hash_is_nondeterministic(
    con: duckdb.DuckDBPyConnection, samples: list[Any]
) -> None:
    """ST04-06 (unit) a tampered meta.evidence hash makes the re-run fail as nondeterministic."""
    into = IntoSpec("score.funding", "replace", "query_id")
    rq = run_recorded(con, SELECT, PARAMS, "score", into=into)
    con.execute(
        "UPDATE meta.evidence SET result_hash = ? WHERE query_id = ?", ["0" * 64, rq.query_id]
    )
    with (
        capture_logs() as logs,
        pytest.raises(SchemaViolation, match="nondeterministic result for"),
    ):
        run_recorded(con, SELECT, PARAMS, "score", into=into)
    conflict = [e for e in logs if e["event"] == "metrics.evidence.hash_conflict"]
    assert conflict[0]["query_id"] == rq.query_id
    assert conflict[0]["build_id"] == BUILD


def test_st04_06_edited_stored_value_detected(
    con: duckdb.DuckDBPyConnection, samples: list[Any]
) -> None:
    """ST04-06 (unit) an edited score.funding value no longer matches its evidence hash."""
    into = IntoSpec("score.funding", "replace", "query_id")
    rq = run_recorded(con, SELECT, PARAMS, "score", into=into)
    assert _stored_hash(con, "score.funding", "query_id") == rq.result_hash
    con.execute("UPDATE score.funding SET v = v + 1000 WHERE k = 'a'")
    stored = con.execute("SELECT result_hash FROM meta.evidence").fetchone()
    assert stored == (rq.result_hash,)
    assert _stored_hash(con, "score.funding", "query_id") != rq.result_hash
    # The same query over changed inputs on the same build is a hash conflict, not a new row.
    con.execute("UPDATE core.t SET v = v + 1 WHERE k = 'b'")
    with pytest.raises(SchemaViolation, match="nondeterministic result"):
        run_recorded(con, SELECT, PARAMS, "score")
    assert len(_evidence_rows(con)) == 1


def test_ut04_08_rows_invariant(con: duckdb.DuckDBPyConnection, samples: list[Any]) -> None:
    """UT04-08 RecordedQuery refuses rows whose length differs from row_count; None is allowed."""
    rq = run_recorded(con, SELECT, PARAMS, None)
    assert rq.rows is not None
    with pytest.raises(SchemaViolation, match="recorded rows do not match row_count"):
        dataclasses.replace(rq, rows=rq.rows[:1])
    assert dataclasses.replace(rq, rows=None).rows is None
