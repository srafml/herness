"""Tests for herness.harness.sql_guard (U05-37): SqlGuard, GuardedQuery, rules 1-7."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import pytest
from sqlglot import expressions as exp
from structlog.testing import capture_logs
from tests.unit.harness._sql_guard_standin import (
    BLOCKED_COLUMNS,
    BUILD_ID,
    SCHEMA,
    make_warehouse,
)

from herness.core.errors import QueryError
from herness.harness import sql_guard as sg
from herness.harness.llm.settings import SqlSettings
from herness.harness.warehouse import open_warehouse

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def guard() -> sg.SqlGuard:
    return sg.SqlGuard(SCHEMA, BLOCKED_COLUMNS)


def _reject(guard: sg.SqlGuard, sql: str, **kw: bool) -> QueryError:
    with pytest.raises(QueryError) as info:
        guard.check(sql, **kw)
    return info.value


# --- UT05-51 --------------------------------------------------------------------------------


def test_ut05_51_accepts_select_union_recursive_on_warehouse_schema(tmp_path: Path) -> None:
    """UT05-51 SELECT, UNION and WITH RECURSIVE accepted on a warehouse schema; ordered flag."""
    make_warehouse(tmp_path)
    handle = open_warehouse(BUILD_ID, warehouse_dir=tmp_path, sql=SqlSettings())
    try:
        guard = sg.SqlGuard(handle.schema(), BLOCKED_COLUMNS)
    finally:
        handle.close()
    plain = guard.check("SELECT  priority, count(*) AS n FROM core.incident GROUP BY priority;")
    assert plain.ordered is False
    assert plain.sql == "SELECT  priority, count(*) AS n FROM core.incident GROUP BY priority;"
    assert (
        plain.normalized_sql
        == "SELECT priority, count(*) AS n FROM core.incident GROUP BY priority"
    )
    assert plain.tables == frozenset({"core.incident"})
    union = guard.check(
        "SELECT service FROM core.incident UNION SELECT service FROM core.change ORDER BY 1"
    )
    assert union.ordered is True
    assert union.tables == frozenset({"core.incident", "core.change"})
    rec = guard.check(
        "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 3) "
        "SELECT n FROM r ORDER BY n"
    )
    assert rec.ordered is True
    assert rec.tables == frozenset()


@pytest.mark.parametrize(
    ("sql", "ordered"),
    [
        ("SELECT number FROM core.incident ORDER BY opened_at", True),
        ("SELECT number FROM core.incident", False),
        ("(SELECT number FROM core.incident ORDER BY 1)", True),
        ("SELECT 1 INTERSECT SELECT 1", False),
        ("SELECT 1 EXCEPT SELECT 2", False),
        ("SELECT x FROM range(3) t(x)", False),
        ("SELECT u FROM unnest([1, 2]) t(u) ORDER BY u", True),
        ("SELECT g FROM generate_series(1, 3) s(g)", False),
    ],
)
def test_ut05_51_ordered_flag(guard: sg.SqlGuard, sql: str, *, ordered: bool) -> None:
    """UT05-51 accepted queries report `ordered` exactly when the root has ORDER BY."""
    assert guard.check(sql).ordered is ordered


def test_ut05_51_lineage_flags_untrusted_and_redact(guard: sg.SqlGuard) -> None:
    """UT05-51 lineage marks outputs reaching untrusted and redact-on-read columns."""
    g = guard.check(
        "WITH a AS (SELECT summary AS s, key FROM core.work_item) "
        "SELECT s AS out, upper(s) AS up, key, (SELECT max(text) FROM enrich.text_redacted) AS t "
        "FROM a"
    )
    assert g.untrusted_output_columns == frozenset({"out", "up", "t"})
    assert g.redact_output_columns == frozenset({"out", "up"})
    union = guard.check(
        "SELECT record_id AS r FROM core.event UNION ALL SELECT title FROM score.funding"
    )
    assert union.untrusted_output_columns == frozenset({"r"})
    assert union.redact_output_columns == frozenset({"r"})
    where_only = guard.check("SELECT count(*) AS n FROM core.event WHERE alert_name = 'x'")
    assert where_only.untrusted_output_columns == frozenset()
    clean = guard.check("SELECT number FROM core.incident")
    assert clean.untrusted_output_columns == frozenset()


def test_ut05_51_lineage_failure_marks_everything(
    guard: sg.SqlGuard, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT05-51 a lineage computation that raises marks every output untrusted and redact."""

    def boom(*_a: object, **_k: object) -> set[str]:
        raise ValueError

    monkeypatch.setattr(sg.SqlGuard, "_reach", boom)
    g = guard.check("SELECT key, summary FROM core.work_item")
    assert g.untrusted_output_columns == frozenset({"key", "summary"})
    assert g.redact_output_columns == frozenset({"key", "summary"})


def test_ut05_51_lineage_through_aliased_table_columns(guard: sg.SqlGuard) -> None:
    """UT05-51 a table alias column list is followed conservatively (all columns)."""
    g = guard.check("SELECT k FROM core.event e(r, k, s, o, v)")
    assert g.untrusted_output_columns == frozenset({"k"})


def test_ut05_51_unresolved_lineage_through_udtf_fails_closed(guard: sg.SqlGuard) -> None:
    """UT05-51 an output column reached through a UNNEST scope fails closed."""
    g = guard.check("SELECT u, w.summary FROM core.work_item w, unnest([1]) t(u)")
    assert "summary" in g.untrusted_output_columns


# --- UT05-52 --------------------------------------------------------------------------------


def test_ut05_52_two_statements_rejected(guard: sg.SqlGuard) -> None:
    """UT05-52 two statements are rejected by rule 1."""
    err = _reject(guard, "SELECT 1; SELECT 2")
    assert err.message == "SQL guard: single_statement"
    assert err.hint == "send exactly one statement"


def test_ut05_52_trailing_semicolons_accepted(guard: sg.SqlGuard) -> None:
    """UT05-52 trailing semicolons are dropped and the query accepted."""
    assert guard.check("SELECT 1;").normalized_sql == "SELECT 1"
    assert guard.check("SELECT 1;;").sql == "SELECT 1;;"


def test_ut05_52_empty_and_unparsable(guard: sg.SqlGuard) -> None:
    """UT05-52 empty input has no statement; garbage fails the parse step with a hint."""
    assert _reject(guard, ";").hint == "send exactly one statement"
    err = _reject(guard, "SELECT FROM WHERE (((")
    assert err.message == "SQL guard: parse"
    assert err.hint is not None
    assert err.hint.startswith("SQL could not be parsed: ")
    assert "\x1b" not in err.hint
    assert len(err.hint) <= len("SQL could not be parsed: ") + 200


def test_ut05_52_deep_nesting_rejected(guard: sg.SqlGuard) -> None:
    """UT05-52 pathological nesting is rejected, never a RecursionError."""
    sql = "SELECT " + "(" * 3000 + "1" + ")" * 3000
    assert _reject(guard, sql).message in {"SQL guard: parse", "SQL guard: second_parse"}


# --- UT05-53 --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    ["VALUES (1)", "DESCRIBE core.incident", "SHOW TABLES", "SUMMARIZE core.incident",
     "PIVOT core.incident ON state USING count(*)", "CHECKPOINT"],
)  # fmt: skip
def test_ut05_53_non_select_roots_rejected(guard: sg.SqlGuard, sql: str) -> None:
    """UT05-53 VALUES, DESCRIBE, SHOW and other non-SELECT roots fail rule 2."""
    err = _reject(guard, sql)
    assert err.message == "SQL guard: select_only"
    assert err.hint == "only SELECT queries are allowed"


# --- UT05-54 --------------------------------------------------------------------------------


def test_ut05_54_denied_node_names_resolve_on_pinned_sqlglot() -> None:
    """UT05-54 list unresolved denied class names: none on the pinned sqlglot 30.19.0."""
    unresolved = [n for n in sg.DENIED_NODE_NAMES if getattr(exp, n, None) is None]
    assert unresolved == []
    assert len(sg._DENIED_NODE_TYPES) == len(sg.DENIED_NODE_NAMES)


@pytest.mark.parametrize(
    ("sql", "node"),
    [
        ("INSERT INTO core.incident VALUES (1)", "Insert"),
        ("UPDATE core.incident SET priority = 1", "Update"),
        ("DELETE FROM core.incident", "Delete"),
        (
            "MERGE INTO core.incident t USING core.change s ON t.record_id = s.record_id "
            "WHEN MATCHED THEN DELETE",
            "Merge",
        ),
        ("CREATE TABLE core.x (a INT)", "Create"),
        ("DROP TABLE core.incident", "Drop"),
        ("ALTER TABLE core.incident ADD COLUMN x INT", "Alter"),
        ("LOAD httpfs", "Command"),
        ("PRAGMA version", "Pragma"),
        ("SET threads = 1", "Set"),
        ("USE core", "Use"),
        ("ATTACH 'x.db' AS x", "Attach"),
        ("DETACH x", "Detach"),
        ("COPY core.incident TO 'x.csv'", "Copy"),
        ("BEGIN TRANSACTION", "Transaction"),
        ("COMMIT", "Commit"),
        ("ROLLBACK", "Rollback"),
        ("INSTALL httpfs", "Install"),
    ],
)
def test_ut05_54_denied_statements_rejected(guard: sg.SqlGuard, sql: str, node: str) -> None:
    """UT05-54 each denied statement class is rejected (rule 2 at the root)."""
    err = _reject(guard, sql)
    assert err.message in {"SQL guard: select_only", "SQL guard: parse"}, node


@pytest.mark.parametrize(
    "node", ["Insert", "Delete", "Create", "Drop", "Set", "Attach", "Copy", "Pragma", "Command"]
)
def test_ut05_54_denied_node_nested_in_select_rejected(node: str) -> None:
    """UT05-54 a denied node anywhere inside a SELECT tree fails rule 3 with its name."""
    root = exp.select("1")
    root.set("where", getattr(exp, node)())
    with pytest.raises(sg._Rejected) as info:
        sg._check_nodes(root)
    assert info.value.args[1] == f"statement type {node} is not allowed"


def test_ut05_54_rule3_through_check(guard: sg.SqlGuard, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT05-54 rule 3 raises QueryError via check() when a denied node is in the tree."""
    real = sg._parse_one

    def with_drop(sql: str) -> exp.Expression:
        root = real(sql)
        root.set("where", exp.Drop())
        return root

    monkeypatch.setattr(sg, "_parse_one", with_drop)
    err = _reject(guard, "SELECT 1")
    assert err.message == "SQL guard: denied_node"
    assert err.hint == "statement type Drop is not allowed"


# --- UT05-55 --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "hint"),
    [
        ("SELECT * FROM stg.incident_raw", "schema stg is not available"),
        ("SELECT * FROM information_schema.tables", "schema information_schema is not available"),
        ("SELECT * FROM pg_catalog.pg_class", "schema pg_catalog is not available"),
        ("SELECT * FROM duckdb_internal.t", "schema duckdb_internal is not available"),
        ("SELECT * FROM other.t", "schema other is not available"),
        ("SELECT * FROM incident", "qualify as core.incident"),
        ("SELECT * FROM mem.core.incident", "schema mem.core is not available"),
        ("SELECT * FROM core.nope", "table core.nope does not exist; call list_tables"),
        ("SELECT * FROM 'C:/data/x.csv'", "qualify as core.c:/data/x.csv"),
        (
            "SELECT * FROM duckdb_tables()",
            "table function duckdb_tables is not allowed; use unnest, range or generate_series",
        ),
    ],
)
def test_ut05_55_table_rules_with_hints(guard: sg.SqlGuard, sql: str, hint: str) -> None:
    """UT05-55 denied schemas, unqualified, 3-part, unknown and catalog tables rejected."""
    assert _reject(guard, sql).hint == hint


def test_ut05_55_cte_name_allowed_but_not_with_schema(guard: sg.SqlGuard) -> None:
    """UT05-55 a CTE name is allowed unqualified; a schema-qualified CTE name is not."""
    guard.check("WITH x AS (SELECT 1 AS a) SELECT a FROM x")
    hint = _reject(guard, "WITH x AS (SELECT 1 AS a) SELECT a FROM core.x").hint
    assert hint == "table core.x does not exist; call list_tables"


def test_ut05_55_non_identifier_table_rejected(guard: sg.SqlGuard) -> None:
    """UT05-55 a table node whose `this` is neither identifier nor function is rejected."""
    root = exp.select("1").from_("core.incident")
    table = root.find(exp.Table)
    assert table is not None
    table.set("this", exp.Literal.string("x"))
    with pytest.raises(sg._Rejected) as info:
        guard._check_tables(root)
    assert info.value.args[1] == "qualify as core.<table>"


def test_ut05_55_cte_name_only_visible_in_its_scope(guard: sg.SqlGuard) -> None:
    """UT05-55 (I2) a CTE name used outside the scope that defines it fails rule 4."""
    sql = "SELECT * FROM (WITH t AS (SELECT 1 AS a) SELECT * FROM t), t"
    assert _reject(guard, sql).hint == "qualify as core.t"
    guard.check("WITH t AS (SELECT 1 AS a) SELECT a FROM (SELECT a FROM t) s")
    guard.check("WITH t AS (SELECT 1 AS a) SELECT a FROM t UNION SELECT a FROM t")
    guard.check("SELECT a FROM (WITH t AS (SELECT 1 AS a) SELECT a FROM t) s")


# --- UT05-56 --------------------------------------------------------------------------------

_DENIED_FUNCS = (
    "read_csv('x')", "read_parquet('x')", "read_text('x')", "sqlite_scan('a', 'b')",
    "postgres_scan('a', 'b', 'c')", "pragma_version()", "parquet_scan('x')", "glob('*')",
    "query('SELECT 1')", "query_table('t')", "getenv('HOME')", "current_setting('threads')",
    "json_serialize_sql('SELECT 1')", "load_extension('x')", "read_csv_auto('x')",
)  # fmt: skip


@pytest.mark.parametrize("func", _DENIED_FUNCS)
@pytest.mark.parametrize(
    "template",
    ["SELECT {f} AS x", "SELECT number FROM core.incident WHERE {f} IS NOT NULL",
     "SELECT * FROM {f}"],
    ids=["select", "where", "table"],
)  # fmt: skip
def test_ut05_56_denied_functions_rejected(guard: sg.SqlGuard, func: str, template: str) -> None:
    """UT05-56 each denied function is rejected in select, where and table position."""
    err = _reject(guard, template.format(f=func))
    assert err.message == "SQL guard: function"
    assert err.hint is not None
    assert err.hint.startswith("function ")


def test_ut05_56_columns_function_rejected(guard: sg.SqlGuard) -> None:
    """UT05-56 COLUMNS(...) is denied (it can select blocked columns by pattern)."""
    assert _reject(guard, "SELECT columns('.*') FROM core.incident").hint == (
        "function columns is not allowed"
    )


def test_ut05_56_other_table_functions_rejected(guard: sg.SqlGuard) -> None:
    """UT05-56 a table function outside the allowlist is rejected with the rule 5 hint."""
    err = _reject(guard, "SELECT * FROM repeat_row(1, num_rows := 3)")
    assert err.message == "SQL guard: table_function"
    err = _reject(guard, "SELECT * FROM core.incident, LATERAL upper('x')")
    assert err.message == "SQL guard: table_function"


def test_ut05_56_function_names_include_duckdb_spelling() -> None:
    """UT05-56 typed functions are matched by sqlglot and DuckDB names (range)."""
    root = exp.maybe_parse("SELECT * FROM range(3)", dialect="duckdb")
    funcs = list(sg._table_position_funcs(root))
    assert "range" in sg._func_names(funcs[0])
    assert "generate_series" in sg._func_names(funcs[0])


# --- UT05-57 --------------------------------------------------------------------------------


@pytest.mark.parametrize("column", BLOCKED_COLUMNS)
def test_ut05_57_blocked_column_direct(guard: sg.SqlGuard, column: str) -> None:
    """UT05-57 every blocked column selected directly is rejected with the text hint."""
    db, table, col = column.split(".")
    err = _reject(guard, f"SELECT {col} FROM {db}.{table}")  # noqa: S608 - test SQL
    assert err.message == "SQL guard: column"
    assert err.hint == "free text is not available; join enrich.text_redacted on record_id"


def test_ut05_57_misspelled_column_did_you_mean(guard: sg.SqlGuard) -> None:
    """UT05-57 a misspelled column gets the "did you mean" hint with close names."""
    err = _reject(guard, "SELECT prioritty FROM core.incident")
    assert err.message == "SQL guard: column"
    assert err.hint is not None
    assert err.hint.startswith("column not found — did you mean ")
    assert "priority" in err.hint


def test_ut05_57_unknown_column_no_close_match(guard: sg.SqlGuard) -> None:
    """UT05-57 a column with no close match gets the describe_table fallback hint."""
    err = _reject(guard, "SELECT zzqqxxw FROM meta.build_info")
    assert err.hint == "column not found; check names with describe_table"


def test_ut05_57_catalog_only_blocked_name_accepted_as_catalog(guard: sg.SqlGuard) -> None:
    """UT05-57 a catalog-only query naming a blocked column reaches no warehouse table."""
    guard.check("SELECT column_name FROM duckdb_columns()", allow_catalog=True)


# --- UT05-58 --------------------------------------------------------------------------------


def _joins(n: int) -> str:
    joins = " ".join(f"JOIN core.change c{i} ON c{i}.record_id = i.record_id" for i in range(n))
    return f"SELECT i.number FROM core.incident i {joins}"  # noqa: S608 - test SQL


def _ctes(n: int) -> str:
    body = ", ".join(f"c{i} AS (SELECT {i} AS v)" for i in range(n))
    return f"WITH {body} SELECT v FROM c0"  # noqa: S608 - test SQL


def test_ut05_58_limits(guard: sg.SqlGuard) -> None:
    """UT05-58 21 joins and 11 CTEs fail rule 7; 8,001 chars fails the size rule."""
    guard.check(_joins(20))
    guard.check(_ctes(10))
    assert _reject(guard, _joins(21)).hint == "too many joins (max 20)"
    assert _reject(guard, _ctes(11)).hint == "too many CTEs (max 10)"
    base = "SELECT 1 AS a"
    exact = base + " " * (8000 - len(base))
    guard.check(exact)
    err = _reject(guard, exact + " ")
    assert err.message == "SQL guard: size"
    assert err.hint == "query longer than 8000 characters; aggregate in fewer steps"


# --- UT05-59 --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    ["SELECT 1 INTO core.change", "SELECT number INTO core.change FROM core.incident",
     "SELECT number FROM core.incident FOR UPDATE", "SELECT 1 LIMIT 1, 2"],
)  # fmt: skip
def test_ut05_59_sqlglot_select_duckdb_non_select(guard: sg.SqlGuard, sql: str) -> None:
    """UT05-59 SQL sqlglot parses as SELECT but DuckDB refuses as a SELECT: second parser."""
    err = _reject(guard, sql)
    assert err.message == "SQL guard: second_parse"
    assert err.hint == "only SELECT queries are allowed"


@pytest.mark.parametrize(
    "sql", ["PRAGMA version", "SET threads = 1", "INSTALL httpfs", "SELECT 1; SELECT 2", "\ud800"]
)
def test_ut05_59_second_parser_directly(guard: sg.SqlGuard, sql: str) -> None:
    """UT05-59 the DuckDB parser rejects non-SELECT, multi-statement and undecodable input."""
    with pytest.raises(sg._Rejected) as info:
        guard._second_parse(sql)
    assert info.value.args[1] == "only SELECT queries are allowed"


def test_ut05_59_vi10_root_node_types(guard: sg.SqlGuard) -> None:
    """UT05-59 VI-10: DuckDB root node types of accepted shapes are in the allowed set."""
    for sql in ("SELECT 1", "SELECT 1 UNION SELECT 2", "WITH a AS (SELECT 1) SELECT * FROM a",
                "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 2) "
                "SELECT n FROM r"):  # fmt: skip
        guard._second_parse(sql)


def test_ut05_59_unexpected_root_type(guard: sg.SqlGuard, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT05-59 a root node type outside the VI-10 set is rejected."""
    monkeypatch.setattr(sg, "_VI10_ROOTS", frozenset({"CTE_NODE"}))
    with pytest.raises(sg._Rejected):
        guard._second_parse("SELECT 1")


def test_ut05_59_parser_connection_per_thread(guard: sg.SqlGuard) -> None:
    """UT05-59 the in-memory parser connection is created lazily, one per thread."""
    main = guard._parser()
    assert guard._parser() is main
    seen: list[object] = []
    worker = threading.Thread(target=lambda: seen.append(guard._parser()))
    worker.start()
    worker.join()
    assert seen
    assert seen[0] is not main


# --- UT05-60 --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    ["SELECT * FROM duckdb_columns()",
     "SELECT table_name, column_name FROM duckdb_columns() WHERE schema_name = 'core'",
     "SELECT table_name, comment FROM duckdb_tables() WHERE schema_name = $schema"],
)  # fmt: skip
def test_ut05_60_catalog_functions_only_with_flag(guard: sg.SqlGuard, sql: str) -> None:
    """UT05-60 duckdb_columns()/duckdb_tables() accepted only with allow_catalog."""
    err = _reject(guard, sql)
    assert err.message == "SQL guard: table_function"
    guard.check(sql, allow_catalog=True)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT i FROM core.incident i, duckdb_tables()",
        "SELECT to_json(i) FROM core.incident i, duckdb_tables() d",
        "SELECT i FROM core.incident i WHERE EXISTS (SELECT 1 FROM duckdb_tables())",
        "SELECT * FROM core.incident, duckdb_tables()",
        "SELECT short_description FROM duckdb_columns(), core.incident",
    ],
)
def test_ut05_60_catalog_functions_never_mix_with_tables(guard: sg.SqlGuard, sql: str) -> None:
    """UT05-60 (I1) with allow_catalog, a catalog function plus a warehouse table is rejected."""
    err = _reject(guard, sql, allow_catalog=True)
    assert err.message == "SQL guard: table_function"
    assert err.hint == "catalog functions cannot be combined with warehouse tables"


def test_ut05_51_star_without_warehouse_tables_accepted(guard: sg.SqlGuard) -> None:
    """UT05-51 an unexpanded `*` over table functions only reads no warehouse column."""
    guard.check("SELECT * FROM range(3)")
    guard.check("SELECT count(*) FROM core.incident, range(2)")


# --- rejection logging (U05-37 side effects) ------------------------------------------------


def test_ut05_52_rejection_logs_rule_and_hash_never_sql(guard: sg.SqlGuard) -> None:
    """UT05-52 a rejection logs INFO harness.sql_guard.rejected with rule and a 16-hex hash."""
    sql = "SELECT 1; SELECT 'secret-ticket-text'"
    with capture_logs() as logs:
        _reject(guard, sql)
    events = [e for e in logs if e["event"] == "harness.sql_guard.rejected"]
    assert len(events) == 1
    event = events[0]
    assert event["log_level"] == "info"
    assert event["rule"] == "single_statement"
    assert len(event["query_hash"]) == 16
    int(event["query_hash"], 16)
    assert "secret-ticket-text" not in repr(event)


def test_ut05_52_sqlglot_command_fallback_warning_suppressed() -> None:
    """UT05-52 (M1) sqlglot's Command-fallback warning, which quotes the SQL, is filtered."""
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    logger = logging.getLogger("sqlglot")
    logger.addHandler(handler)
    try:
        with pytest.raises(QueryError):
            sg.SqlGuard(SCHEMA, BLOCKED_COLUMNS).check("LOAD httpfs_secret_marker")
    finally:
        logger.removeHandler(handler)
    assert not [r for r in records if "httpfs_secret_marker" in r.getMessage()]
