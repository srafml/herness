"""SQL coverage gate tests (U11-33): tests/unit/test_sql_coverage.py."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.support.sql_coverage import tables_created, tables_referenced_by_tests

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def test_ut11_40_tables_created_excludes_temp_and_lowercases(tmp_path: Path) -> None:
    """UT11-40 CREATE OR REPLACE, IF NOT EXISTS and a view are found; TEMP is excluded."""
    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()
    (sql_dir / "001_stage.sql").write_text(
        "-- comment mentioning CREATE TABLE stg.ignored_in_comment\n"
        "CREATE OR REPLACE TABLE Stg.Foo (id INTEGER);\n"
        "CREATE TABLE IF NOT EXISTS Core.Bar (id INTEGER);\n"
        "CREATE TEMP TABLE tmp.baz (id INTEGER);\n"
        "CREATE TEMPORARY TABLE tmp.qux (id INTEGER);\n"
        "/* CREATE TABLE meta.blocked (id INTEGER); */\n"
        "CREATE VIEW Meta.Summary AS SELECT * FROM Core.Bar;\n",
        encoding="utf-8",
    )
    created = tables_created(sql_dir)
    assert created == {"stg.foo", "core.bar", "meta.summary"}


def test_ut11_40_tables_created_missing_dir_is_empty(tmp_path: Path) -> None:
    """UT11-40 a missing sql_dir yields the empty set rather than an error."""
    assert tables_created(tmp_path / "missing") == set()


def test_ut11_40_tables_referenced_by_tests_scans_test_files(tmp_path: Path) -> None:
    """UT11-40 references are collected only from test_*.py files, lower-cased."""
    tests_dir = tmp_path / "tests"
    unit_dir = tests_dir / "unit"
    unit_dir.mkdir(parents=True)
    (unit_dir / "test_stage.py").write_text(
        'QUERY = "SELECT * FROM stg.foo JOIN core.bar USING (id)"\n', encoding="utf-8"
    )
    (unit_dir / "helpers.py").write_text("NOT_A_TEST = 'meta.not_counted'\n", encoding="utf-8")
    referenced = tables_referenced_by_tests(tests_dir)
    assert referenced == {"stg.foo", "core.bar"}


def test_ut11_41_test_sql_coverage_every_created_table_is_referenced() -> None:
    """UT11-41 every table herness/model/sql/*.sql creates is named by some test."""
    created = tables_created(ROOT / "herness" / "model" / "sql")
    referenced = tables_referenced_by_tests(ROOT / "tests")
    missing = created - referenced
    assert missing == set(), f"tables created but never referenced by a test: {sorted(missing)}"
