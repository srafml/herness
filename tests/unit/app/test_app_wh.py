"""UT09-57 - UT09-60: warehouse pool, named queries, raw-text guard, query (U09-60 - U09-63)."""

from __future__ import annotations

import datetime
import json
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest
from structlog.testing import capture_logs
from tests.support.app_env import (
    QUERIES_FILE,
    plant_scored_build,
    preserved_main,
    wh_script,
    wh_services,
)
from tests.support.fake_clock import FIXTURE_START, FakeClock
from tests.support.promote_builds import build_id, make_build

from app.common import bootstrap, wh
from app.common.wh import NamedQuery, ParamSpec
from herness.core import time as clock
from herness.core.errors import ConfigError, QueryError
from herness.reports.rules import UserInputError
from herness.store._warehouse_rw import write_current
from herness.store.layout import DataLayout
from herness.store.warehouse import open_readonly

pytestmark = pytest.mark.unit

A, B = build_id(1), build_id(2)


@pytest.fixture(autouse=True)
def _isolated() -> Iterator[None]:
    """Fresh caches per test; AppTest leaves ``__main__`` alone afterwards."""
    st.cache_data.clear()
    wh._pool.clear()
    with preserved_main():
        yield
    st.cache_data.clear()
    wh._pool.clear()


@pytest.fixture
def fc(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """A fake clock behind ``herness.core.time.now`` (no freezegun: AppTest needs real time)."""
    fake = FakeClock(FIXTURE_START)
    monkeypatch.setattr(clock, "now", fake.now)
    return fake


@pytest.fixture
def layout(tmp_path: Path) -> DataLayout:
    lay = DataLayout.from_root(tmp_path / "data")
    for build in (A, B):
        make_build(lay, build, "promoted")
    write_current(A, layout=lay)
    return lay


def _services(monkeypatch: pytest.MonkeyPatch, data: Path, **kw: Any) -> None:
    services = wh_services(data, **kw)
    monkeypatch.setattr(bootstrap, "get_services", lambda: services)


def _texts(at: AppTest) -> list[str]:
    return [t.value for t in at.text]


def _toasts(at: AppTest) -> list[str]:
    return [t.value for t in at.toast]


def _write_yaml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "q.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


# --- UT09-57 get_conn, pool, current_build_id ------------------------------------------------


def test_ut09_57_recheck_interval_and_switch(
    monkeypatch: pytest.MonkeyPatch, layout: DataLayout, fc: FakeClock
) -> None:
    """UT09-57 no recheck before 60 s; switch after, with a toast."""
    _services(monkeypatch, layout.root)
    at = AppTest.from_function(wh_script).run()
    assert _texts(at) == [A]
    write_current(B, layout=layout)
    fc.advance(59)
    at.run()
    assert _texts(at) == [A]
    assert _toasts(at) == []
    fc.advance(1)
    at.run()
    assert _texts(at) == [B]
    assert _toasts(at) == [f"Data refreshed to build {B}"]


def test_ut09_57_connection_is_hardened(
    monkeypatch: pytest.MonkeyPatch, layout: DataLayout, fc: FakeClock
) -> None:
    """UT09-57 the pooled connection is read-only with external access off and config locked."""
    del fc
    _services(monkeypatch, layout.root)
    conn = wh._pool().get(A, layout)
    row = conn.execute(
        "SELECT current_setting('enable_external_access'), current_setting('lock_configuration')"
    ).fetchone()
    assert row == (False, True)
    with pytest.raises(duckdb.Error):
        conn.execute("CREATE TABLE meta.x (a INTEGER)")
    assert wh._pool().get(A, layout) is conn  # one connection per build per process


def test_ut09_57_stale_connection_closed_after_grace(layout: DataLayout, fc: FakeClock) -> None:
    """UT09-57 a connection to a non-current build closes 300 s after it stopped being current."""
    pool = wh._Pool()
    conn_a = pool.get(A, layout)  # A last seen current now
    fc.advance(10)
    pool.get(B, layout)
    fc.advance(wh.STALE_CONN_GRACE_S - 10)
    pool.get(B, layout)
    assert conn_a.execute("SELECT 1").fetchone() == (1,)  # exactly 300 s: still open
    fc.advance(1)
    pool.get(B, layout)
    with pytest.raises(duckdb.ConnectionException):
        conn_a.execute("SELECT 1")
    pool.close_all()


def test_ut09_57_no_current_build(tmp_path: Path) -> None:
    """UT09-57 missing or invalid CURRENT -> NoCurrentBuild with its fix."""
    data = tmp_path / "data"
    with pytest.raises(wh.NoCurrentBuild) as missing:
        wh.current_build_id(data)
    assert missing.value.message == "No promoted warehouse yet."
    assert missing.value.hint == "Run `herness pipeline`."
    assert isinstance(missing.value, ConfigError)
    (data / "warehouse").mkdir(parents=True)
    (data / "warehouse" / "CURRENT").write_text("../../etc/passwd\n", encoding="ascii")
    with pytest.raises(wh.NoCurrentBuild):
        wh.current_build_id(data)
    (data / "warehouse" / "CURRENT").write_text(f"{A}\n", encoding="ascii")  # file missing
    with pytest.raises(wh.NoCurrentBuild):
        wh.current_build_id(data)


def test_ut09_57_current_build_id_reads_pointer(layout: DataLayout) -> None:
    """UT09-57 the pointer is read through read_current and stripped."""
    assert wh.current_build_id(layout.root) == A


def test_ut09_57_retired_build_rereads_current(
    monkeypatch: pytest.MonkeyPatch, layout: DataLayout, fc: FakeClock
) -> None:
    """UT09-57 open failure (build retired after the check) -> one re-read of CURRENT, recorded
    like a normal recheck: wh_checked_at updated, toast and `app.warehouse.switched` log."""
    _services(monkeypatch, layout.root)
    gone = build_id(3)
    at = AppTest.from_function(wh_script)
    at.session_state["wh_build_id"] = gone  # checked recently, file since retired
    at.session_state["wh_checked_at"] = clock.now()
    fc.advance(10)  # inside the 60 s window: no normal recheck
    with capture_logs() as logs:
        at.run()
    assert _texts(at) == [A]
    assert not at.exception
    assert at.session_state["wh_checked_at"] == clock.now()
    assert _toasts(at) == [f"Data refreshed to build {A}"]
    switched = [e for e in logs if e["event"] == "app.warehouse.switched"]
    assert [(e["old_build_id"], e["new_build_id"]) for e in switched] == [(gone, A)]


class _FakeConn:
    """Stands in for an opened reader: records SETs and close; scripted settings/failures."""

    def __init__(self, settings: tuple[bool, bool], *, set_fails: bool = False) -> None:
        self.settings, self.set_fails = settings, set_fails
        self.sets: list[str] = []
        self.closed = False

    def execute(self, sql: str) -> _FakeConn:
        if sql.startswith("SET"):
            if self.set_fails:
                msg = "Invalid Input Error: configuration locked"
                raise duckdb.InvalidInputException(msg)
            self.sets.append(sql)
        return self

    def fetchone(self) -> tuple[bool, bool]:
        return self.settings

    def close(self) -> None:
        self.closed = True


def test_ut09_57_unhardened_open_sets_settings(
    monkeypatch: pytest.MonkeyPatch, layout: DataLayout
) -> None:
    """UT09-57 a reader opened without hardening gets both SETs and is then verified."""
    raw = duckdb.connect(":memory:")
    monkeypatch.setattr(wh, "open_readonly", lambda *_a, **_k: raw)
    conn = wh._open(A, layout)
    assert conn is raw
    row = conn.execute(wh._SETTINGS_SQL).fetchone()
    assert row == (False, True)
    conn.close()


@pytest.mark.parametrize(
    "fake",
    [
        _FakeConn((True, False), set_fails=True),  # SET fails
        _FakeConn((True, False)),  # SETs accepted but the values stay wrong
    ],
    ids=["set_fails", "still_wrong"],
)
def test_ut09_57_hardening_failure_fails_closed(
    monkeypatch: pytest.MonkeyPatch, layout: DataLayout, fake: _FakeConn
) -> None:
    """UT09-57 hardening that cannot be verified -> connection closed, ConfigError."""
    monkeypatch.setattr(wh, "open_readonly", lambda *_a, **_k: fake)
    with pytest.raises(ConfigError, match="could not be hardened"):
        wh._open(A, layout)
    assert fake.closed is True


def test_ut09_57_no_current_build_in_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fc: FakeClock
) -> None:
    """UT09-57 a session without a promoted build sees NoCurrentBuild."""
    del fc
    _services(monkeypatch, tmp_path / "data")
    at = AppTest.from_function(wh_script).run()
    assert [e.message for e in at.exception] == ["No promoted warehouse yet."]


# --- UT09-58 load_named_queries ----------------------------------------------------------------

_BAD_YAML = {
    "two_statements": """
        version: 1
        queries:
          a.b: {sql: "SELECT 1 AS x; SELECT 2 AS y"}
        """,
    "undeclared_param": """
        version: 1
        queries:
          a.b: {sql: "SELECT 1 AS x WHERE $p = 1"}
        """,
    "unused_param": """
        version: 1
        queries:
          a.b: {sql: "SELECT 1 AS x", params: {p: {type: str}}}
        """,
    "delete": """
        version: 1
        queries:
          a.b: {sql: "DELETE FROM meta.build"}
        """,
    "union": """
        version: 1
        queries:
          a.b: {sql: "SELECT 1 AS x UNION SELECT 2"}
        """,
    "unparsable": """
        version: 1
        queries:
          a.b: {sql: "SELEC FROM WHERE"}
        """,
    "bad_name": """
        version: 1
        queries:
          Ab: {sql: "SELECT 1 AS x"}
        """,
    "row_cap_over_limit": """
        version: 1
        queries:
          a.b: {sql: "SELECT 1 AS x", row_cap: 5001}
        """,
    "bad_type": """
        version: 1
        queries:
          a.b: {sql: "SELECT $p AS x", params: {p: {type: blob}}}
        """,
    "extra_key": """
        version: 1
        queries:
          a.b: {sql: "SELECT 1 AS x", cache: false}
        """,
    "bad_version": """
        version: 2
        queries: {}
        """,
    "not_mapping": """
        - 1
        """,
}


@pytest.mark.parametrize("case", sorted(_BAD_YAML))
def test_ut09_58_invalid_file_rejected(tmp_path: Path, case: str) -> None:
    """UT09-58 two statements, undeclared param, DELETE, ... -> ConfigError each."""
    path = _write_yaml(tmp_path, _BAD_YAML[case])
    with pytest.raises(ConfigError, match=r"^named query "):
        wh.load_named_queries(path, page_row_limit=5000)


def test_ut09_58_valid_file_loads(tmp_path: Path) -> None:
    """UT09-58 a valid file: frozen NamedQuery with ParamSpec, row cap defaults to the limit."""
    path = _write_yaml(
        tmp_path,
        """
        version: 1
        queries:
          a.b:
            sql: "WITH c AS (SELECT 1 AS x) SELECT x FROM c WHERE ($p IS NULL OR x = $p)"
            params: {p: {type: int, default: 1}}
          a.c: {sql: "SELECT 2 AS y", row_cap: 7}
        """,
    )
    queries = wh.load_named_queries(path, page_row_limit=900)
    assert queries["a.b"] == NamedQuery(
        "a.b", queries["a.b"].sql, {"p": ParamSpec("int", required=False, default=1)}, 900
    )
    assert queries["a.c"].row_cap == 7
    with pytest.raises(AttributeError):
        queries["a.c"].row_cap = 1  # type: ignore[misc]


@pytest.mark.parametrize(
    ("ptype", "default"),
    [("int", "5"), ("int", True), ("str", 3), ("float", "x"), ("bool", 1), ("list_str", [1])],
)
def test_ut09_58_default_type_checked_at_load(tmp_path: Path, ptype: str, default: object) -> None:
    """UT09-58 a param default that does not match its declared type -> ConfigError at load."""
    body = (
        "version: 1\nqueries:\n  a.b:\n    sql: 'SELECT $p AS x'\n"
        f"    params: {{p: {{type: {ptype}, default: {json.dumps(default)}}}}}\n"
    )
    path = _write_yaml(tmp_path, body)
    with pytest.raises(ConfigError, match=r"^named query a\.b: default of p"):
        wh.load_named_queries(path, page_row_limit=5000)


def test_ut09_58_unreadable_file(tmp_path: Path) -> None:
    """UT09-58 a missing file or bad YAML -> ConfigError."""
    with pytest.raises(ConfigError, match="named query"):
        wh.load_named_queries(tmp_path / "missing.yaml", page_row_limit=5000)
    path = _write_yaml(tmp_path, "version: [1\n")
    with pytest.raises(ConfigError, match="named query"):
        wh.load_named_queries(path, page_row_limit=5000)


def test_ut09_58_shipped_file_loads() -> None:
    """UT09-58 the shipped queries.sql.yaml loads with the U09-63 names."""
    queries = wh.load_named_queries(QUERIES_FILE, page_row_limit=5000)
    assert {"build.meta", "funding.ranking_by_wsjf", "evidence.meta", "evidence.meta_v1"} <= set(
        queries
    )
    assert len(queries) == 27
    assert queries["org.list"].row_cap == 5000
    assert queries["clusters.members"].row_cap == 20


_SAMPLE: dict[str, object] = {
    "str": "x",
    "int": 5,
    "float": 0.5,
    "bool": True,
    "date": datetime.date(2026, 1, 1),
    "list_str": ["x"],
}


@pytest.fixture(scope="module")
def scored(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The scored tiny build (data root), planted once per module."""
    data = tmp_path_factory.mktemp("scored") / "data"
    plant_scored_build(data / "warehouse")
    return data


def _bind_conn(monkeypatch: pytest.MonkeyPatch, data: Path, queries: dict[str, NamedQuery]) -> str:
    """Services over ``data`` and ``get_conn`` returning a hardened reader of CURRENT."""
    _services(monkeypatch, data, queries=queries)
    current = wh.current_build_id(data)
    conn = open_readonly(current, layout=DataLayout.from_root(data))
    monkeypatch.setattr(wh, "get_conn", lambda: (current, conn))
    return current


_SHIPPED = wh.load_named_queries(QUERIES_FILE, page_row_limit=5000)


@pytest.mark.parametrize("name", sorted(_SHIPPED))
def test_ut09_58_every_named_query_executes(
    monkeypatch: pytest.MonkeyPatch, scored: Path, name: str
) -> None:
    """UT09-58 smoke: every shipped named query executes on the tiny scored build."""
    current = _bind_conn(monkeypatch, scored, _SHIPPED)
    nq = _SHIPPED[name]
    params = {p: _SAMPLE[spec.type] for p, spec in nq.params.items() if spec.required}
    if name == "build.meta":
        params["build_id"] = current
    result = wh.query(name, **params)
    assert isinstance(result.table, pa.Table)
    assert result.build_id == current
    assert result.table.num_rows <= nq.row_cap
    if name in {"build.meta", "home.builds", "org.list", "entities.teams", "portfolio.scenarios"}:
        assert result.table.num_rows >= 1


def test_ut09_58_real_rows_through_filters(monkeypatch: pytest.MonkeyPatch, scored: Path) -> None:
    """UT09-58 NULL-tolerant filters and bound values return the planted rows."""
    _bind_conn(monkeypatch, scored, _SHIPPED)
    scenarios = wh.query("portfolio.scenarios").table.column("scenario").to_pylist()
    rows = wh.query("portfolio.rows", scenario=scenarios[0]).table
    assert rows.num_rows >= 1
    ranking = wh.query("funding.ranking_by_priority", top_n=1).table
    assert ranking.num_rows == 1
    metrics = wh.query("org.metrics", entity_type="team").table.column("metric").to_pylist()
    assert wh.query("org.scores", entity_type="team", metric=metrics[0]).table.num_rows >= 1


def test_ut09_58_evidence_variant_on_old_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """UT09-58 builds without meta.evidence.result_sample use evidence.meta_v1."""
    lay = DataLayout.from_root(tmp_path / "data")
    make_build(lay, A, "promoted")
    con = duckdb.connect(str(lay.warehouse / f"wh-{A}.duckdb"))
    con.execute(
        "CREATE TABLE meta.evidence (query_id VARCHAR, sql VARCHAR, params JSON,"
        " row_count BIGINT, executed_at TIMESTAMPTZ)"
    )
    con.execute("INSERT INTO meta.evidence VALUES ('q_1', 'SELECT 1', '{}', 1, now())")
    con.close()
    write_current(A, layout=lay)
    _bind_conn(monkeypatch, lay.root, _SHIPPED)
    table = wh.query("evidence.meta", ids=["q_1"]).table
    assert table.column_names == ["query_id", "sql", "params", "row_count", "executed_at"]
    assert table.num_rows == 1


# --- UT09-59 check_forbidden_columns -----------------------------------------------------------


def _nq(name: str, sql: str) -> NamedQuery:
    return NamedQuery(name, sql, {}, 10)


def test_ut09_59_alias_and_star_reported() -> None:
    """UT09-59 `core.incident.description` via an alias and `SELECT i.*` are violations."""
    queries = {
        "a.alias": _nq("a.alias", "SELECT i.description FROM core.incident i"),
        "a.star": _nq("a.star", "SELECT i.* FROM core.incident AS i"),
        "a.bare": _nq("a.bare", "SELECT * FROM core.problem"),
        "a.unqual": _nq(
            "a.unqual",
            "SELECT close_notes FROM core.incident i JOIN enrich.cluster_member m"
            " ON m.record_id = i.record_id",
        ),
        "a.sub": _nq(
            "a.sub",
            "SELECT x FROM (SELECT p.root_cause_text AS x FROM core.problem p) s",
        ),
        "a.ok": _nq(
            "a.ok",
            "SELECT i.number, t.text, m.description FROM core.incident i"
            " JOIN enrich.text_redacted t ON t.record_id = i.record_id"
            " JOIN metrics.thing m ON m.id = i.record_id",
        ),
        "a.outer_star": _nq("a.outer_star", "SELECT * FROM (SELECT i.number FROM core.incident i)"),
    }
    assert wh.check_forbidden_columns(queries) == [
        "a.alias: core.incident.description",
        "a.bare: core.problem.*",
        "a.star: core.incident.*",
        "a.sub: core.problem.root_cause_text",
        "a.unqual: core.incident.close_notes",
    ]


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT i.description FROM core.incident I", "core.incident.description"),
        ("SELECT description FROM CORE.incident", "core.incident.description"),
        ('SELECT "I"."DESCRIPTION" FROM "Core"."Incident" "I"', "core.incident.DESCRIPTION"),
        ("SELECT I.* FROM core.incident i", "core.incident.*"),
        ("SELECT i FROM core.incident i", "core.incident.*"),
        ("SELECT to_json(i) FROM core.incident i", "core.incident.*"),
        ("SELECT i.I FROM core.incident i", "core.incident.*"),
        ("SELECT incident FROM core.incident", "core.incident.*"),
        ("SELECT to_json(i.*) FROM core.incident i", "core.incident.*"),
        ("SELECT COLUMNS('desc.*') FROM core.incident", "core.incident.*"),
        ("SELECT COLUMNS(*) FROM core.incident", "core.incident.*"),
    ],
)
def test_ut09_59_case_whole_row_and_columns_bypasses(sql: str, expected: str) -> None:
    """UT09-59 case-insensitive identifiers, whole-row references and COLUMNS() are caught."""
    assert wh.check_forbidden_columns({"a.b": _nq("a.b", sql)}) == [f"a.b: {expected}"]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count(*) AS n FROM core.incident",
        "SELECT i.number FROM core.incident i WHERE i.number IN (SELECT number FROM core.change)",
        "SELECT COLUMNS('n.*') FROM metrics.thing",
    ],
)
def test_ut09_59_allowed_shapes(sql: str) -> None:
    """UT09-59 count(*), allowed columns and COLUMNS() over non-core tables pass the guard."""
    assert wh.check_forbidden_columns({"a.b": _nq("a.b", sql)}) == []


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        (
            "SELECT l.d FROM core.incident i, LATERAL (SELECT i.description AS d) l",
            "core.incident.description",
        ),
        ("SELECT (SELECT i.description) AS d FROM core.incident i", "core.incident.description"),
        (
            "SELECT 1 FROM core.incident i WHERE EXISTS"
            " (SELECT 1 FROM meta.build b WHERE b.status = i.description)",
            "core.incident.description",
        ),
        (
            "SELECT 1 FROM core.incident i WHERE EXISTS"
            " (SELECT 1 FROM meta.build b WHERE b.status = description)",
            "core.incident.description",
        ),
        ("SELECT #2 FROM core.incident", "positional column reference #2"),
        ("SELECT description FROM query_table('core.incident')", "function query_table"),
        ("SELECT * FROM query('SELECT description FROM core.incident')", "function query"),
        ("SELECT * FROM read_csv('x.csv')", "function read_csv"),
        ("SELECT 1 AS x WHERE EXISTS (SELECT * FROM read_parquet('x'))", "function read_parquet"),
        ("SELECT x FROM range(10) t(x)", "source range(0, 10) is not a table, CTE or subquery"),
        ("SELECT a FROM incident", "source incident is not a table, CTE or subquery"),
        (
            "SELECT value FROM (UNPIVOT core.incident ON COLUMNS(* EXCLUDE number))",
            "PIVOT/UNPIVOT",
        ),
        ("PIVOT core.incident ON number USING first(description)", "PIVOT/UNPIVOT"),
        ("SELECT i.b FROM core.incident AS i(a, b)", "alias column list on i"),
        ("SELECT b FROM core.incident i(a, b)", "alias column list on i"),
        ("SELECT q.x FROM (SELECT i.number FROM core.incident i) q(x)", "alias column list on q"),
        (
            "WITH c(x) AS (SELECT i.number FROM core.incident i) SELECT c.x FROM c",
            "alias column list on c",
        ),
        ("SELECT * LIKE 'desc%' FROM core.incident", "core.incident.*"),
        ("SELECT * SIMILAR TO 'desc.*' FROM core.incident", "core.incident.*"),
        ("SELECT * ILIKE 'DESC%' FROM core.incident", "core.incident.*"),
        ("SELECT i.* LIKE 'desc%' FROM core.incident i", "core.incident.*"),
        ("SELECT i.* SIMILAR TO 'desc.*' FROM core.incident i", "core.incident.*"),
    ],
)
def test_ut09_59_outer_scopes_and_fail_closed_shapes(sql: str, expected: str) -> None:
    """UT09-59 outer-scope references, positional refs, table functions, SQL-from-string,
    file reads, non-table sources and PIVOT/UNPIVOT are violations (fail-closed posture)."""
    assert f"a.b: {expected}" in wh.check_forbidden_columns({"a.b": _nq("a.b", sql)})


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT f.team FROM score.funding f LEFT JOIN core.team t ON t.team_id = f.x",
        "SELECT x.i FROM core.incident i, metrics.thing x",
        "WITH c AS (SELECT i.number FROM core.incident i) SELECT c.number FROM c",
        "SELECT q.n FROM (SELECT i.number AS n FROM core.incident i) q",
        "SELECT 1 FROM core.incident i WHERE EXISTS (SELECT * FROM meta.build b)",
    ],
)
def test_ut09_59_qualified_non_core_and_allowed_sources(sql: str) -> None:
    """UT09-59 a non-core qualifier is not a whole-row read; CTEs and subqueries are allowed."""
    assert wh.check_forbidden_columns({"a.b": _nq("a.b", sql)}) == []


def test_ut09_59_constants() -> None:
    """UT09-59 the forbidden set is the design §9.3 raw-text columns."""
    assert (
        frozenset({"short_description", "description", "close_notes", "root_cause_text"})
        == wh.FORBIDDEN_TEXT_COLUMNS
    )


# --- UT09-60 query -----------------------------------------------------------------------------

_SPEC_QUERIES = {
    "t.big": NamedQuery("t.big", "SELECT i FROM range(6000) t(i)", {}, 5000),
    "t.typed": NamedQuery(
        "t.typed",
        "SELECT $n AS n, $s AS s, $l AS l, $d AS d, $f AS f, $b AS b",
        {
            "n": ParamSpec("int", required=True, default=None),
            "s": ParamSpec("str", required=False, default="dflt"),
            "l": ParamSpec("list_str", required=False, default=None),
            "d": ParamSpec("date", required=False, default=None),
            "f": ParamSpec("float", required=False, default=None),
            "b": ParamSpec("bool", required=False, default=None),
        },
        10,
    ),
    "t.slow": NamedQuery(
        "t.slow", "SELECT count(*) AS n FROM range(100000000000) a WHERE a.range % 7 = 3", {}, 1
    ),
    "t.broken": NamedQuery("t.broken", "SELECT * FROM no_such_table", {}, 1),
}


@pytest.fixture
def mem_conn(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    """``get_conn`` -> an in-memory connection; returns the log of get_conn calls."""
    _services(monkeypatch, tmp_path, queries=_SPEC_QUERIES)
    conn = duckdb.connect(":memory:")
    calls: list[str] = []

    def get_conn() -> tuple[str, duckdb.DuckDBPyConnection]:
        calls.append("get_conn")
        return A, conn

    monkeypatch.setattr(wh, "get_conn", get_conn)
    return calls


def test_ut09_60_row_cap_truncates(mem_conn: list[str]) -> None:
    """UT09-60 a query over 6,000 rows -> 5000 rows and truncated."""
    result = wh.query("t.big")
    assert result.table.num_rows == 5000
    assert result.truncated is True
    assert result.build_id == A


def test_ut09_60_params_bound_and_cached(mem_conn: list[str]) -> None:
    """UT09-60 params bound by name, defaults applied, None allowed; cached per build+params."""
    day = datetime.date(2026, 3, 1)
    first = wh.query("t.typed", n=3, l=["a", "b"], d=day, f=2, b=False)
    assert first.table.to_pylist() == [
        {"n": 3, "s": "dflt", "l": ["a", "b"], "d": day, "f": 2.0, "b": False}
    ]
    assert first.truncated is False
    again = wh.query("t.typed", n=3, l=["a", "b"], d=day, f=2, b=False)
    assert again.table.equals(first.table)
    assert wh.query("t.typed", n=4, s=None).table.to_pylist()[0]["s"] is None
    assert mem_conn == ["get_conn"] * 3


@pytest.mark.parametrize(
    "params",
    [
        {"n": "3"},
        {"n": True},
        {"n": 1.5},
        {"n": 1, "s": 5},
        {"n": 1, "s": "x" * 201},
        {"n": 1, "l": "abc"},
        {"n": 1, "l": ["x"] * 501},
        {"n": 1, "l": ["x" * 201]},
        {"n": 1, "l": [1]},
        {"n": 1, "d": datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)},
        {"n": 1, "d": "2026-01-01"},
        {"n": 1, "f": True},
        {"n": 1, "f": "1"},
        {"n": 1, "b": 1},
        {"n": None},
        {},
        {"n": 1, "zzz": 1},
    ],
)
def test_ut09_60_wrong_params(mem_conn: list[str], params: dict[str, object]) -> None:
    """UT09-60 wrong type, unknown key or missing required param -> UserInputError."""
    with pytest.raises(UserInputError):
        wh.query("t.typed", **params)
    assert mem_conn == []  # rejected before any warehouse access


def test_ut09_60_unknown_query(mem_conn: list[str]) -> None:
    """UT09-60 an unknown name -> ConfigError."""
    with pytest.raises(ConfigError, match="unknown named query"):
        wh.query("t.nope")


def test_ut09_60_timeout_interrupts(mem_conn: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """UT09-60 interrupt by the timer -> QueryError naming the timeout."""
    monkeypatch.setattr(wh, "QUERY_TIMEOUT_S", 0.2)
    with pytest.raises(QueryError, match=r"^query t\.slow timed out after "):
        wh.query("t.slow")


def test_ut09_60_failure_is_query_error(mem_conn: list[str]) -> None:
    """UT09-60 other DuckDB errors -> QueryError("query <name> failed")."""
    with pytest.raises(QueryError, match=r"^query t\.broken failed$"):
        wh.query("t.broken")
