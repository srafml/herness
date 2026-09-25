"""Tests for herness.metrics._encode (U04-01 … U04-04)."""

import datetime
import decimal
import hashlib
import io
import uuid
from collections.abc import Sequence
from typing import Any

import duckdb
import pyarrow as pa
import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from herness.core.errors import SchemaViolation
from herness.metrics import _encode
from herness.metrics._encode import HashAccumulator, encode_cell, hash_arrow_batch, row_digest
from herness.metrics.evidence import iter_batch_rows, result_hash

pytestmark = pytest.mark.unit

D = decimal.Decimal
UTC = datetime.UTC
PLUS2 = datetime.timezone(datetime.timedelta(hours=2))

CASES: list[tuple[object, str, str]] = [
    (None, "INTEGER", "null"),
    (None, "VARCHAR", "null"),
    (True, "BOOLEAN", "true"),
    (0, "BOOLEAN", "false"),
    (7, "TINYINT", "7"),
    (-3, "SMALLINT", "-3"),
    (2**40, "BIGINT", "1099511627776"),
    (
        D("170141183460469231731687303715884105727"),
        "HUGEINT",
        "170141183460469231731687303715884105727",
    ),
    (5, "ubigint", "5"),
    (D("3"), "UHUGEINT", "3"),
    (1.5, "DOUBLE", "1.5"),
    (0.1, "DOUBLE", "0.1"),
    (-0.0, "DOUBLE", "0"),
    (1e20, "DOUBLE", "1e+20"),
    (1e-7, "DOUBLE", "1e-07"),
    (1 / 3, "DOUBLE", "0.333333333"),
    (float("nan"), "DOUBLE", '"nan"'),
    (float("inf"), "REAL", '"inf"'),
    (float("-inf"), "FLOAT", '"-inf"'),
    (1.100000023841858, "FLOAT", "1.10000002"),
    (D("12.345"), "DECIMAL(18,2)", '"12.34"'),
    (D("12.355"), "DECIMAL(18,2)", '"12.36"'),
    (D("-0.00"), "DECIMAL(18,2)", '"0.00"'),
    (D("-0.001"), "DECIMAL(18,2)", '"0.00"'),
    (12, "DECIMAL(18,2)", '"12.00"'),
    (1.25, "DECIMAL(18,1)", '"1.2"'),
    (D("7"), "DECIMAL( 18, 0 )", '"7"'),
    (
        D("1234567890123456789012345678901234.5678"),
        "DECIMAL(38,4)",
        '"1234567890123456789012345678901234.5678"',
    ),
    (datetime.date(2026, 1, 2), "DATE", '"2026-01-02"'),
    (datetime.datetime(2026, 1, 2, 3, 4, 5), "TIMESTAMP", '"2026-01-02T03:04:05.000000Z"'),  # noqa: DTZ001 - naive DuckDB TIMESTAMP
    (datetime.datetime(2026, 1, 2, 3, 4, 5, 7), "TIMESTAMP_NS", '"2026-01-02T03:04:05.000007Z"'),  # noqa: DTZ001 - naive
    (
        datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=PLUS2),
        "TIMESTAMP WITH TIME ZONE",
        '"2026-01-02T01:04:05.000000Z"',
    ),
    (
        datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        "TIMESTAMPTZ",
        '"2026-01-02T03:04:05.000000Z"',
    ),
    (datetime.time(1, 2, 3), "TIME", '"01:02:03.000000"'),
    (datetime.timedelta(minutes=90), "INTERVAL", "5400"),
    (datetime.timedelta(days=-1, seconds=81000), "INTERVAL", "-5400"),
    ("x", "VARCHAR", '"x"'),
    ('é"\n', "VARCHAR", '"é\\"\\n"'),
    ("1", "VARCHAR", '"1"'),
    (uuid.UUID(int=1), "UUID", '"00000000-0000-0000-0000-000000000001"'),
    ('"x"', "JSON", '"\\"x\\""'),
    ("b", "ENUM('a', 'b')", '"b"'),
    (b"\x01\xff", "BLOB", '"Af8="'),
    (["a", None], "VARCHAR[]", '["a",null]'),
    ((1, 2), "INTEGER[2]", "[1,2]"),
    ([[1.5], []], "DOUBLE[][]", "[[1.5],[]]"),
    ([D("1.5")], "DECIMAL(10,1)[]", '["1.5"]'),
    ({"a": 1, "b": "x"}, "STRUCT(a INTEGER, b VARCHAR)", '{"a":1,"b":"x"}'),
    ({"b": 1, "a": 2}, "STRUCT(b INTEGER, a INTEGER)", '{"b":1,"a":2}'),
    ({1: "a"}, "MAP(INTEGER, VARCHAR)", '{"1":"a"}'),
    ([(1, "a")], "MAP(INTEGER, VARCHAR)", '{"1":"a"}'),
    ({"k": [1, None]}, "MAP(VARCHAR, INTEGER[])", '{"k":[1,null]}'),
    (2, "UNION(num INTEGER)", "2"),
    (1, "UNION(n INTEGER, s VARCHAR)", "1"),
    ({"h": 5}, "STRUCT(h HUGEINT)", '{"h":5}'),
    ({"h": D("5")}, "STRUCT(h HUGEINT)", '{"h":5}'),
    ({"m": {"k": 1}}, "STRUCT(m MAP(VARCHAR, INTEGER))", '{"m":{"k":1}}'),
    ({"m": [("k", 1)]}, "STRUCT(m MAP(VARCHAR, INTEGER))", '{"m":{"k":1}}'),
    ([("k", 1)], "UNION(m MAP(VARCHAR, INTEGER))", '{"k":1}'),
    ({"my field": 1, "x": 2}, 'STRUCT("my ""f"", x" INTEGER, x BIGINT)', '{"my field":1,"x":2}'),
    ({"e": "b"}, "STRUCT(e ENUM('a, b', 'b'))", '{"e":"b"}'),
    ({"key": [[1]], "value": ["a"]}, "MAP(INTEGER[], VARCHAR)", '{"[1]":"a"}'),
    ([([1], "a")], "MAP(INTEGER[], VARCHAR)", '{"[1]":"a"}'),
    (
        {uuid.UUID(int=1): D("2")},
        "MAP(UUID, HUGEINT)",
        '{"00000000-0000-0000-0000-000000000001":2}',
    ),
    ({"a": None}, "STRUCT(a INTEGER)", '{"a":null}'),
    (
        {
            "t": True,
            "f": -0.0,
            "d": D("-0.0"),
            "e": D("1E+2"),
            "ts": datetime.datetime(2026, 1, 1, tzinfo=UTC),
            "dt": datetime.date(2026, 1, 1),
            "tm": datetime.time(1, 0),
            "iv": datetime.timedelta(seconds=1),
            "u": uuid.UUID(int=0),
            "b": b"",
            "n": None,
            "a": (1, "z"),
        },
        "VARIANT",
        '{"t":true,"f":0,"d":"0.0","e":"100","ts":"2026-01-01T00:00:00.000000Z",'
        '"dt":"2026-01-01","tm":"01:00:00.000000","iv":1,'
        '"u":"00000000-0000-0000-0000-000000000000","b":"","n":null,"a":[1,"z"]}',
    ),
]

ALL_TYPES_SQL = """SELECT 1::TINYINT a, 3::HUGEINT c, 1.5::DOUBLE d, 1.1::FLOAT e,
 12.345::DECIMAL(18,2) f, DATE '2026-01-02' g, TIMESTAMP '2026-01-02 03:04:05.123456' h,
 TIMESTAMPTZ '2026-01-02 03:04:05+02' i, TIME '01:02:03' j,
 INTERVAL '1 month 2 days 3.5 seconds' k, 'é'::VARCHAR l, ['a', NULL] m, [1,2]::INTEGER[2] n,
 true o, '00000000-0000-0000-0000-000000000001'::UUID p, '\\x01'::BLOB q,
 {'a': 1, 'b': 'x', 'u': '00000000-0000-0000-0000-000000000002'::UUID, 'r': [1,2]::INTEGER[2]} r,
 MAP {'k': 1} s, '"x"'::JSON u, TIMESTAMP_S '2026-01-01 00:00:00' v,
 TIMESTAMP_NS '2026-01-01 00:00:00.123456789' w, [1.5::DECIMAL(10,1)] x, 1::UHUGEINT y,
 TIMESTAMP_MS '2026-01-01 00:00:00.123' z, 'b'::ENUM('a','b') en, -0.0::DOUBLE nz,
 'nan'::DOUBLE nn, union_value(num := 2) un, [MAP {'a': 1}] lm, -INTERVAL 90 MINUTE k2"""


def arrow_rows(con: duckdb.DuckDBPyConnection, sql: str) -> list[tuple[object, ...]]:
    result = con.sql(sql).arrow()
    batches = result.to_batches() if isinstance(result, pa.Table) else list(result)
    return list(iter_batch_rows(batches))


@pytest.mark.parametrize(("value", "duckdb_type", "expected"), CASES)
def test_ut04_01_encode_cell_tokens(value: object, duckdb_type: str, expected: str) -> None:
    """UT04-01 every type family encodes to its exact canonical token."""
    assert encode_cell(value, duckdb_type) == expected


def test_ut04_01_fetchall_and_arrow_encode_equally() -> None:
    """UT04-01 the same DuckDB result encodes equally from fetchall and from Arrow."""
    con = duckdb.connect()
    rel = con.sql(ALL_TYPES_SQL)
    types = [str(t) for t in rel.types]
    fetched = rel.fetchall()[0]
    arrowed = arrow_rows(con, ALL_TYPES_SQL)[0]
    for value_f, value_a, kind in zip(fetched, arrowed, types, strict=True):
        assert encode_cell(value_f, kind) == encode_cell(value_a, kind), kind


@pytest.mark.parametrize(
    ("value", "duckdb_type"),
    [
        (object(), "VARIANT"),
        ({"a": object()}, "VARIANT"),
        (5, "INTEGER[]"),
    ],
)
def test_ut04_01_unsupported_type_raises(value: object, duckdb_type: str) -> None:
    """UT04-01 unsupported Python types raise SchemaViolation naming type and column type."""
    with pytest.raises(SchemaViolation, match="unsupported result value type") as info:
        encode_cell(value, duckdb_type)
    assert duckdb_type in info.value.message


@pytest.mark.parametrize(
    ("value", "duckdb_type"),
    [
        ("abc", "INTEGER"),
        ("abc", "DECIMAL(18,2)"),
        (D("NaN"), "DECIMAL(18,2)"),
        ({"a": D("Infinity")}, "STRUCT(a DECIMAL(9,0))"),
        (object(), "STRUCT(a INTEGER)"),
        ({"a": 1, "b": 2}, "STRUCT(a INTEGER)"),
        (5, "MAP(VARCHAR, INTEGER)"),
        ({"k": [1]}, "MAP(INTEGER[], VARCHAR)"),
        ([(1, 2, 3)], "MAP(INTEGER, INTEGER)"),
        ({"a": 1}, 'STRUCT("a INTEGER)'),
        ("x", "DOUBLE"),
        ("2026-01-01", "TIMESTAMP"),
        (5, "BLOB"),
        (5, "INTERVAL"),
    ],
)
def test_ut04_01_conversion_failure_raises(value: object, duckdb_type: str) -> None:
    """UT04-01 conversion failures raise SchemaViolation from the original error."""
    with pytest.raises(SchemaViolation, match="column type") as info:
        encode_cell(value, duckdb_type)
    assert info.value.__cause__ is not None


@pytest.mark.parametrize("value", [{"k": 1}, [("k", 1)], (1,), D("1")])
def test_ut04_01_ambiguous_union_fails_closed(value: object) -> None:
    """UT04-01 multi-member UNION values that encode differently per path raise."""
    with pytest.raises(SchemaViolation, match="ambiguous UNION member value"):
        encode_cell(value, "UNION(a DECIMAL(9,0), m MAP(VARCHAR, INTEGER))")


NESTED_SQL = """SELECT {'h': 5::HUGEINT} h, {'u': 5::UHUGEINT} u, {'m': MAP {'k': 1}} m,
 union_value(m := MAP {'k': 1}) um, {'my field': 1, 'x': [MAP {1: 2::HUGEINT}]} q,
 MAP {[1]: 'a'} lk, MAP {{'a': 1}: 'b'} sk, MAP {'k': 5::HUGEINT} mh,
 [union_value(n := 1)::UNION(n INTEGER, s VARCHAR),
  union_value(s := 'x')::UNION(n INTEGER, s VARCHAR)] un,
 {'e': 'b'::ENUM('a, b', 'b'), 'd': 1.5::DECIMAL(9,2), 'i': INTERVAL 1 MONTH} st"""


def test_ut04_01_nested_fetchall_and_arrow_encode_equally() -> None:
    """UT04-01 HUGEINT and MAP nested in STRUCT, MAP, UNION and lists encode equally."""
    con = duckdb.connect()
    rel = con.sql(NESTED_SQL)
    types = [str(t) for t in rel.types]
    fetched = rel.fetchall()[0]
    arrowed = arrow_rows(con, NESTED_SQL)[0]
    tokens = [encode_cell(v, k) for v, k in zip(fetched, types, strict=True)]
    assert tokens == [encode_cell(v, k) for v, k in zip(arrowed, types, strict=True)]
    assert tokens[:4] == ['{"h":5}', '{"u":5}', '{"m":{"k":1}}', '{"k":1}']


def test_ut04_01_empty_type_raises() -> None:
    """UT04-01 an empty type string violates the precondition."""
    with pytest.raises(SchemaViolation, match="empty column type"):
        encode_cell(1, "")


def test_ut04_01_normalize_type() -> None:
    """UT04-01 type strings are upper-cased with spaces inside parentheses removed."""
    assert _encode.normalize_type(" decimal( 18,2 ) ") == "DECIMAL(18,2)"


def test_ut04_03_negative_zero_hashes_equal() -> None:
    """UT04-03 rows with -0.0 and 0.0 hash equal (DOUBLE and DECIMAL)."""
    cols = [("x", "DOUBLE"), ("m", "DECIMAL(18,2)")]
    assert result_hash(cols, [(-0.0, D("-0.00"))]) == result_hash(cols, [(0.0, D("0.00"))])


def test_ut04_02_row_digest_text_and_width() -> None:
    """UT04-02 row JSON keeps column order and duplicate names; widths must match."""
    digest, text = row_digest(["b", "a", "b"], ["INTEGER", "VARCHAR", "DOUBLE"], [1, "x", 2.0])
    assert text == '{"b":1,"a":"x","b":2}'
    assert digest == hashlib.sha256(text.encode("utf-8")).digest()
    with pytest.raises(SchemaViolation, match="row width 1 != column count 2"):
        row_digest(["a", "b"], ["INTEGER", "INTEGER"], [1])


def test_ut04_05_accumulator_sample_of_120_rows() -> None:
    """UT04-05 the accumulator samples 50 of 120 rows ascending; limits; single use (U04-03)."""
    big = HashAccumulator([("n", "BIGINT")], sample_limit=50)
    big.add_rows([(i,) for i in range(120)])
    _, big_count, big_sample = big.finish()
    digests = [row_digest(["n"], ["BIGINT"], (s["n"],))[0] for s in big_sample]
    assert big_count == 120
    assert digests == sorted(row_digest(["n"], ["BIGINT"], (i,))[0] for i in range(120))[:50]
    for bad in (-1, 1001, True):
        with pytest.raises(SchemaViolation, match="sample limit"):
            HashAccumulator([("a", "INTEGER")], sample_limit=bad)
    acc = HashAccumulator([("a", "INTEGER")], sample_limit=2)
    acc.add_rows([(3,), (1,), (2,), (1,)])
    digest, count, sample = acc.finish()
    assert count == 4
    assert len(sample) == 2
    assert digest == result_hash([("a", "INTEGER")], [(1,), (1,), (2,), (3,)])
    for call in (acc.finish, lambda: acc.add_rows([]), lambda: acc.add_digests([], [])):
        with pytest.raises(SchemaViolation, match="accumulator finished"):
            call()


def _ipc(batch: pa.RecordBatch) -> bytes:
    sink = io.BytesIO()
    with pa.ipc.new_stream(sink, batch.schema) as writer:
        writer.write_batch(batch)
    return sink.getvalue()


def test_ut04_12_arrow_ipc_and_lists_hash_identically() -> None:
    """UT04-12 the same rows via Arrow IPC and via lists give identical digests and hash."""
    con = duckdb.connect()
    sql = (
        "SELECT i::BIGINT AS n, i / 7.0 AS r, CAST(i * 1.005 AS DECIMAL(18,2)) AS m,"
        " TIMESTAMPTZ '2026-01-01 00:00:00+00' + to_minutes(i) AS t,"
        " CASE WHEN i % 3 = 0 THEN NULL ELSE 'v' || i END AS s FROM range(250) r(i)"
    )
    rel = con.sql(sql)
    columns = tuple((n, str(t)) for n, t in zip(rel.columns, rel.types, strict=True))
    rows = rel.fetchall()
    table = con.sql(sql).arrow()
    batches = table.to_batches(max_chunksize=100) if isinstance(table, pa.Table) else list(table)
    list_acc = HashAccumulator(columns, sample_limit=10)
    list_acc.add_rows(rows)
    ipc_acc = HashAccumulator(columns, sample_limit=10)
    all_digests: list[bytes] = []
    for batch in batches:
        digests, sample = hash_arrow_batch(columns, _ipc(batch), 10)
        assert len(sample) <= 10
        assert [d for d, _ in sample] == sorted(d for d, _ in sample)
        all_digests.extend(digests)
        ipc_acc.add_digests(digests, sample)
    expected = sorted(
        row_digest([c[0] for c in columns], [c[1] for c in columns], r)[0] for r in rows
    )
    assert sorted(all_digests) == expected
    assert ipc_acc.finish() == list_acc.finish()


def test_ut04_12_arrow_batch_width_mismatch() -> None:
    """UT04-12 a batch whose width differs from the columns raises SchemaViolation."""
    batch = pa.record_batch({"a": [1], "b": [2]})
    with pytest.raises(SchemaViolation, match="row width 2 != column count 1"):
        hash_arrow_batch((("a", "BIGINT"),), _ipc(batch), 5)


def test_st04_12_typed_header_and_duplicate_names() -> None:
    """ST04-12 "1" VARCHAR vs 1 INTEGER and duplicate column names hash differently."""
    assert result_hash([("c", "VARCHAR")], [("1",)]) != result_hash([("c", "INTEGER")], [(1,)])
    assert result_hash([("c", "VARCHAR")], [("1",)]) != result_hash([("c", "JSON")], [("1",)])
    dup = result_hash([("a", "INTEGER"), ("a", "INTEGER")], [(1, 2)])
    swapped = result_hash([("a", "INTEGER"), ("a", "INTEGER")], [(2, 1)])
    single = result_hash([("a", "INTEGER")], [(2,)])
    assert len({dup, swapped, single}) == 3


COLUMNS = [
    ("i", "BIGINT"),
    ("f", "DOUBLE"),
    ("s", "VARCHAR"),
    ("b", "BOOLEAN"),
    ("m", "DECIMAL(18,2)"),
]
CELL = {
    "BIGINT": st.one_of(st.none(), st.integers(-(2**63), 2**63 - 1)),
    "DOUBLE": st.one_of(st.none(), st.floats(allow_nan=True, allow_infinity=True)),
    "VARCHAR": st.one_of(st.none(), st.text(max_size=8)),
    "BOOLEAN": st.one_of(st.none(), st.booleans()),
    "DECIMAL(18,2)": st.one_of(
        st.none(), st.decimals(min_value=-(10**9), max_value=10**9, places=2, allow_nan=False)
    ),
}
ROW = st.tuples(*(CELL[t] for _, t in COLUMNS))


@given(st.lists(ROW, min_size=1, max_size=12), st.data())
def test_pt04_02_single_cell_change_changes_hash(rows: list[tuple[Any, ...]], data: Any) -> None:
    """PT04-02 changing any single cell changes the hash."""
    r = data.draw(st.integers(0, len(rows) - 1))
    c = data.draw(st.integers(0, len(COLUMNS) - 1))
    kind = COLUMNS[c][1]
    new = data.draw(CELL[kind])
    assume(encode_cell(new, kind) != encode_cell(rows[r][c], kind))
    changed: list[Sequence[object]] = list(rows)
    row = list(rows[r])
    row[c] = new
    changed[r] = tuple(row)
    assert result_hash(COLUMNS, changed) != result_hash(COLUMNS, rows)
