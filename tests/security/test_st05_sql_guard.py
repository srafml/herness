"""Security tests for herness.harness.sql_guard (ST05-03, ST05-04, ST05-05; TH05-03/04/05).

Property tests run under the active hypothesis profile (`commit` 200 examples by default,
`nightly` >= 10,000 with `--hypothesis-profile nightly`); no `max_examples` is hard-coded.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.unit.harness._sql_guard_standin import BLOCKED_COLUMNS, SCHEMA

from herness.core.errors import QueryError
from herness.harness.sql_guard import SqlGuard

pytestmark = pytest.mark.unit

GUARD = SqlGuard(SCHEMA, BLOCKED_COLUMNS)

_SEPARATORS = (" ", "  ", "\n", "\t", " /* note */ ", "/**/", " -- note\n", "\n/* ; */\n")


def _rejected(sql: str, *, allow_catalog: bool = False) -> bool:
    try:
        GUARD.check(sql, allow_catalog=allow_catalog)
    except QueryError:
        return True
    return False


@st.composite
def _recase(draw: st.DrawFn, text: str) -> str:
    flips = draw(st.lists(st.booleans(), min_size=len(text), max_size=len(text)))
    return "".join(c.swapcase() if f else c for c, f in zip(text, flips, strict=True))


@st.composite
def _with_comments(draw: st.DrawFn, text: str) -> str:
    """Re-join the tokens of `text` with random whitespace and comment separators."""
    words = text.split(" ")
    seps = draw(st.lists(st.sampled_from(_SEPARATORS), min_size=len(words), max_size=len(words)))
    return "".join(w + s for w, s in zip(words, seps, strict=True)).rstrip()


# --- ST05-03: DDL/DML/PRAGMA/SET/ATTACH/COPY/INSTALL/LOAD alone and stacked -----------------

_STATEMENTS = (
    "INSERT INTO core.incident (record_id) VALUES ('x')",
    "INSERT INTO core.incident SELECT * FROM core.incident",
    "UPDATE core.incident SET priority = 1",
    "DELETE FROM core.incident WHERE priority = 1",
    "MERGE INTO core.incident t USING core.change s ON t.record_id = s.record_id "
    "WHEN MATCHED THEN DELETE",
    "CREATE TABLE core.evil AS SELECT 1 AS a",
    "CREATE VIEW core.v AS SELECT 1 AS a",
    "CREATE MACRO m() AS 1",
    "CREATE OR REPLACE TEMP TABLE t AS SELECT 1 AS a",
    "DROP TABLE core.incident",
    "DROP SCHEMA core CASCADE",
    "ALTER TABLE core.incident RENAME TO x",
    "TRUNCATE core.incident",
    "PRAGMA enable_profiling",
    "PRAGMA database_list",
    "SET enable_external_access = true",
    "SET lock_configuration = false",
    "RESET threads",
    "ATTACH 'C:/tmp/evil.db' AS evil",
    "ATTACH ':memory:' AS m",
    "DETACH evil",
    "USE core",
    "COPY core.incident TO 'C:/tmp/out.csv'",
    "COPY core.incident FROM 'C:/tmp/in.csv'",
    "COPY (SELECT 1) TO 'out.parquet' (FORMAT parquet)",
    "EXPORT DATABASE 'C:/tmp/dump'",
    "IMPORT DATABASE 'C:/tmp/dump'",
    "INSTALL httpfs",
    "FORCE INSTALL httpfs",
    "LOAD httpfs",
    "LOAD 'C:/tmp/evil.duckdb_extension'",
    "BEGIN TRANSACTION",
    "COMMIT",
    "ROLLBACK",
    "CHECKPOINT",
    "VACUUM",
    "CALL pragma_version()",
    "DESCRIBE core.incident",
    "SHOW TABLES",
    "SUMMARIZE core.incident",
    "CREATE SECRET s (TYPE s3, KEY_ID 'k', SECRET 'v')",
)
_PREFIXES = ("", "SELECT 1;", "SELECT number FROM core.incident;", "SELECT 1 ; ", "(SELECT 1);")


@given(
    statement=st.sampled_from(_STATEMENTS),
    prefix=st.sampled_from(_PREFIXES),
    suffix=st.sampled_from(("", ";", "; SELECT 1", " -- trailing")),
    data=st.data(),
)
def test_st05_03_ddl_dml_statements_rejected(
    statement: str, prefix: str, suffix: str, data: st.DataObject
) -> None:
    """ST05-03 every DDL/DML/PRAGMA/SET/ATTACH/COPY/INSTALL/LOAD statement is rejected."""
    body = data.draw(_with_comments(data.draw(_recase(statement))))
    assert _rejected(f"{prefix}{body}{suffix}")


@pytest.mark.parametrize("statement", _STATEMENTS)
def test_st05_03_every_statement_rejected_plain(statement: str) -> None:
    """ST05-03 each listed statement is rejected alone and stacked after a SELECT."""
    assert _rejected(statement)
    assert _rejected(f"SELECT 1; {statement}")
    assert _rejected(statement, allow_catalog=True)


# --- ST05-04: file, network and settings functions anywhere in the query --------------------

_FUNCS = (
    "read_csv('C:/data/x.csv')", "read_csv_auto('x.csv')", "read_parquet('s3://b/x.parquet')",
    "read_json('https://example.invalid/x.json')", "read_json_auto('x')", "read_ndjson('x')",
    "read_text('C:/Windows/win.ini')", "read_blob('x')", "parquet_scan('x')",
    "sqlite_scan('x.db', 't')", "postgres_scan('dsn', 'public', 't')", "iceberg_scan('x')",
    "delta_scan('x')", "glob('C:/*')", "query('SELECT 1')", "query_table('core.incident')",
    "getenv('HOME')", "current_setting('enable_external_access')", "pragma_version()",
    "json_serialize_sql('SELECT 1')", "load_extension('x')",
)  # fmt: skip
_POSITIONS = (
    "SELECT {f} AS x",
    "SELECT * FROM {f}",
    "SELECT * FROM {f} AS t",
    "SELECT i.number FROM core.incident i JOIN {f} f ON true",
    "SELECT number FROM core.incident WHERE number IN (SELECT * FROM {f})",
    "SELECT number FROM core.incident WHERE {f} IS NOT NULL",
    "WITH a AS (SELECT * FROM {f}) SELECT * FROM a",
    "SELECT number FROM core.incident ORDER BY {f}",
    "SELECT count(*) FROM core.incident GROUP BY {f}",
    "SELECT count(*) FROM core.incident HAVING max({f}) IS NULL",
    "SELECT upper(cast({f} AS VARCHAR)) AS x",
    "SELECT * FROM core.incident, LATERAL (SELECT {f}) l",
    "SELECT 1 UNION ALL SELECT {f}",
    "SELECT (SELECT {f}) AS x",
    "SELECT list_transform([1], x -> {f}) AS y",
)
_FILE_TABLES = (
    "'C:/data/x.csv'", "'/etc/passwd'", "'x.parquet'", "\"C:/data/x.csv\"",
    "'https://example.invalid/x.csv'", "'s3://bucket/key.parquet'",
)  # fmt: skip


@given(
    func=st.sampled_from(_FUNCS),
    position=st.sampled_from(_POSITIONS),
    catalog=st.booleans(),
    data=st.data(),
)
def test_st05_04_denied_functions_anywhere_rejected(
    func: str, position: str, *, catalog: bool, data: st.DataObject
) -> None:
    """ST05-04 read_*, *_scan, glob, query, getenv, current_setting anywhere are rejected."""
    name, _, rest = func.partition("(")
    recased = data.draw(_recase(name)) + "(" + rest
    assert _rejected(position.format(f=recased), allow_catalog=catalog)


@given(
    path=st.sampled_from(_FILE_TABLES),
    template=st.sampled_from(
        (
            "SELECT * FROM {p}",
            "SELECT count(*) FROM {p} AS t",
            "SELECT i.number FROM core.incident i JOIN {p} x ON true",
            "WITH a AS (SELECT * FROM {p}) SELECT * FROM a",
            "SELECT number FROM core.incident WHERE number IN (SELECT * FROM {p})",
        )
    ),
    catalog=st.booleans(),
)
def test_st05_04_file_path_tables_rejected(path: str, template: str, *, catalog: bool) -> None:
    """ST05-04 file-path string tables are rejected in every table position."""
    assert _rejected(template.format(p=path), allow_catalog=catalog)


# --- ST05-05: blocked columns through every disguise ----------------------------------------

_TEMPLATES = (
    "SELECT {c} FROM {t}",
    "SELECT x.{c} AS y FROM {t} x",
    "SELECT {c} AS record_id FROM {t}",
    "WITH a AS (SELECT {c} AS z FROM {t}) SELECT z FROM a",
    "WITH a AS (SELECT * FROM {t}) SELECT record_id FROM a",
    "SELECT z FROM (SELECT {c} AS z FROM {t}) s",
    "SELECT * FROM {t}",
    "SELECT x.* FROM {t} x",
    "SELECT * EXCLUDE (record_id) FROM {t}",
    "SELECT record_id FROM {t} WHERE {c} LIKE '%a%'",
    "SELECT record_id FROM {t} ORDER BY {c}",
    "SELECT length({c}) AS n FROM {t}",
    "SELECT (SELECT i.{c}) AS z FROM {t} i",
    "SELECT 1 FROM {t} i, LATERAL (SELECT i.{c} AS p) l",
    "SELECT b FROM {t} x(a, b)",
    "SELECT record_id FROM {t} UNION ALL SELECT {c} FROM {t}",
    "SELECT string_agg({c}, ',') FROM {t}",
    "SELECT record_id FROM {t} i WHERE EXISTS (SELECT 1 WHERE i.{c} = 'x')",
    "SELECT list_transform([1], v -> {c}) AS z FROM {t}",
)


def _fullwidth(text: str, mask: list[bool]) -> str:
    """Map chosen ASCII letters and `_` to fullwidth compatibility forms (NFKC folds back)."""
    return "".join(
        chr(ord(c) + 0xFEE0) if m and c.isascii() else c for c, m in zip(text, mask, strict=True)
    )


@st.composite
def _disguise(draw: st.DrawFn, ident: str) -> str:
    style = draw(st.sampled_from(("plain", "quoted", "recase", "fullwidth", "fw_quoted")))
    if style == "plain":
        return ident
    if style in {"recase", "quoted"}:
        text = draw(_recase(ident))
        return f'"{text}"' if style == "quoted" else text
    mask = draw(st.lists(st.booleans(), min_size=len(ident), max_size=len(ident)))
    return f'"{_fullwidth(ident, mask)}"'


@given(
    column=st.sampled_from(BLOCKED_COLUMNS),
    template=st.sampled_from(_TEMPLATES),
    catalog=st.booleans(),
    data=st.data(),
)
def test_st05_05_blocked_columns_rejected_through_disguises(
    column: str, template: str, *, catalog: bool, data: st.DataObject
) -> None:
    """ST05-05 blocked columns via aliases, CTEs, subqueries, *, t.*, quoting, lookalikes."""
    db, table, col = column.split(".")
    disguised = data.draw(_disguise(col))
    qualified = f"{data.draw(_disguise(db))}.{data.draw(_disguise(table))}"
    sql = data.draw(_with_comments(template.format(c=disguised, t=qualified)))
    assert _rejected(sql, allow_catalog=catalog)


@pytest.mark.parametrize("column", BLOCKED_COLUMNS)
@pytest.mark.parametrize("template", _TEMPLATES)
def test_st05_05_every_template_rejected_plain(column: str, template: str) -> None:
    """ST05-05 every disguise template is rejected for every blocked column, with the hint."""
    db, table, col = column.split(".")
    with pytest.raises(QueryError) as info:
        GUARD.check(template.format(c=col, t=f"{db}.{table}"))
    assert info.value.hint == "free text is not available; join enrich.text_redacted on record_id"


def test_st05_05_fullwidth_identifier_resolves_to_blocked() -> None:
    """ST05-05 a fullwidth-quoted blocked column is caught by rule 6, not by a parse error."""
    col = _fullwidth("short_description", [True] * 17)
    sql = 'SELECT "' + col + '" FROM core.incident'  # noqa: S608 - test SQL
    with pytest.raises(QueryError) as info:
        GUARD.check(sql)
    assert info.value.hint == "free text is not available; join enrich.text_redacted on record_id"
