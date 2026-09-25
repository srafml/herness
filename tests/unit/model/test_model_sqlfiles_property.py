"""Property test for herness.model.sqlfiles filters (PT02-02)."""

import duckdb
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from herness.core.errors import ConfigError
from herness.model import sqlfiles

pytestmark = pytest.mark.unit

_SQLSTR_MAX = 1024
_FIRST_PRINTABLE = 0x20

_tricky = st.text(alphabet="'\\\"%_;-\n\t\x00 ab$", max_size=40)


@settings(max_examples=300, deadline=None)
@given(st.text(max_size=1100) | _tricky)
def test_pt02_02_sqlstr_roundtrip(value: str) -> None:
    """PT02-02 sqlstr output parsed by DuckDB as a literal equals the input, or is rejected."""
    try:
        literal = sqlfiles._sqlstr(value)
    except ConfigError:
        assert len(value) > _SQLSTR_MAX or any(ord(ch) < _FIRST_PRINTABLE for ch in value)
        return
    con = duckdb.connect(":memory:")
    try:
        assert len(con.extract_statements(f"SELECT {literal}")) == 1
        row = con.execute(f"SELECT {literal}").fetchone()
    finally:
        con.close()
    assert row == (value,)


@settings(max_examples=300, deadline=None)
@given(st.text(max_size=140) | st.from_regex(r"\A[a-z_][a-z0-9_]{0,127}\Z"))
def test_pt02_02_ident_never_embeds_quote(value: str) -> None:
    """PT02-02 ident never emits a quote inside the name; accepted names round-trip."""
    try:
        out = sqlfiles._ident(value)
    except ConfigError:
        return
    assert out.startswith('"')
    assert out.endswith('"')
    assert '"' not in out[1:-1]
    assert out[1:-1] == value
    con = duckdb.connect(":memory:")
    try:
        description = con.execute(f"SELECT 1 AS {out}").description
    finally:
        con.close()
    assert description is not None
    assert description[0][0] == value
