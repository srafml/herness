"""Tests for herness.model.errors (U02-76, U02-77), error-class part."""

import pytest

from herness.core import errors as e
from herness.model.errors import BuildSqlError, DqGateFailed

pytestmark = pytest.mark.unit


def test_st02_14_build_sql_error_sanitises_literals() -> None:
    """ST02-14 (error-class part) BuildSqlError drops quoted literals and later lines."""
    db_error = (
        "ConversionException: Could not convert string 'SENTINEL-1' to INT32 "
        "near 'it''s SENTINEL-2'\nLINE 1: SELECT 'SENTINEL-3'"
    )
    err = BuildSqlError("20260925-010203-ABCDEF", "230_incident.sql", 4, db_error)
    assert isinstance(err, e.SchemaViolation)
    text = str(err)
    assert "SENTINEL" not in text
    assert text == (
        "build 20260925-010203-ABCDEF: 230_incident.sql statement 4 failed: "
        "ConversionException: Could not convert string '?' to INT32 near '?'"
    )
    assert (err.build_id, err.file, err.statement_index) == (
        "20260925-010203-ABCDEF",
        "230_incident.sql",
        4,
    )
    assert err.db_error == "ConversionException: Could not convert string '?' to INT32 near '?'"


def test_st02_14_build_sql_error_truncates_and_closes_quotes() -> None:
    """ST02-14 (error-class part) message part is capped at 300; an open quote is masked."""
    long = "BinderException: " + "x" * 400
    assert BuildSqlError("b", "f.sql", 1, long).db_error == "BinderException: " + "x" * 300
    open_quote = "ParserException: syntax error at 'SENTINEL-4"
    err = BuildSqlError("b", "f.sql", 1, open_quote)
    assert "SENTINEL" not in str(err)
    assert err.db_error == "ParserException: syntax error at '?'"
    assert BuildSqlError("b", "f.sql", 1, "plain").db_error == "plain"


def test_it02_29_dq_gate_failed_message() -> None:
    """IT02-29 (error-class part) DqGateFailed lists at most 20 check names."""
    err = DqGateFailed("b1", ("row_count_drop:core.incident", "duplicate_key:core.change"))
    assert isinstance(err, e.SchemaViolation)
    assert str(err) == (
        "build b1 blocked by DQ: row_count_drop:core.incident, duplicate_key:core.change"
    )
    many = tuple(f"c{i}" for i in range(25))
    long = DqGateFailed("b2", many)
    assert str(long) == "build b2 blocked by DQ: " + ", ".join(many[:20])
    assert long.failed_checks == many
    assert long.build_id == "b2"
