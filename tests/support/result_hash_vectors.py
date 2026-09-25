"""Generator of tests/fixtures/result_hash_vectors.json (T04-01, VI04-02).

Run from the repository root: ``uv run python -m tests.support.result_hash_vectors``.
UT04-02 checks that the fixture's IDs and SQL equal ``VECTORS`` below.
"""

# ruff: noqa: E501 - the SQL texts are frozen byte-for-byte in the committed fixture

import json
import sys
from pathlib import Path

import duckdb

from herness.metrics._encode import row_digest
from herness.metrics.evidence import result_hash

VECTORS = [
    ("V01", "empty result", "SELECT 1::INTEGER AS a, 'x' AS b WHERE false"),
    (
        "V02",
        "integer family with negatives and NULL",
        "SELECT * FROM (VALUES (1::TINYINT, -2::SMALLINT, 3::INTEGER, -9223372036854775807::BIGINT,"
        " 170141183460469231731687303715884105727::HUGEINT, 255::UTINYINT, 18446744073709551615::UBIGINT,"
        " 1::UHUGEINT), (NULL, 0, -3, 42, -1, 0, 0, NULL)) t(ti, si, i, bi, hi, uti, ubi, uhi)",
    ),
    (
        "V03",
        "DOUBLE specials: -0.0, nan, inf, tiny, huge, NULL",
        "SELECT * FROM (VALUES (1.5::DOUBLE), (-0.0::DOUBLE), (0.1::DOUBLE), (1e20::DOUBLE),"
        " (1e-7::DOUBLE), ('nan'::DOUBLE), ('inf'::DOUBLE), ('-inf'::DOUBLE), (1.0/3), (NULL::DOUBLE)) t(x)",
    ),
    ("V04", "FLOAT single precision", "SELECT 1.1::FLOAT AS f, -2.5::REAL AS r"),
    (
        "V05",
        "DECIMAL money: cast rounding, negative zero, NULL, DECIMAL(38,4)",
        "SELECT CAST(x AS DECIMAL(18,2)) AS money, CAST(y AS DECIMAL(38,4)) AS big FROM (VALUES"
        " (12.345::DOUBLE, 1234567890123456789012345678901234.5678::DECIMAL(38,4)),"
        " (-0.001, 0), (NULL, -1.5)) t(x, y)",
    ),
    (
        "V06",
        "DATE and naive timestamps of every precision",
        "SELECT DATE '2026-01-02' AS d, TIMESTAMP '2026-01-02 03:04:05.123456' AS ts,"
        " TIMESTAMP_S '2026-01-01 00:00:00' AS ts_s, TIMESTAMP_MS '2026-01-01 00:00:00.123' AS ts_ms,"
        " TIMESTAMP_NS '2026-01-01 00:00:00.123456789' AS ts_ns",
    ),
    (
        "V07",
        "TIMESTAMP WITH TIME ZONE from several offsets",
        "SELECT * FROM (VALUES (TIMESTAMPTZ '2026-01-02 03:04:05+02'),"
        " (TIMESTAMPTZ '2026-06-30 23:59:59.999999-05'), (TIMESTAMPTZ '2026-03-29 01:30:00+00')) t(happened_at)",
    ),
    (
        "V08",
        "TIME and INTERVAL including months and negatives",
        "SELECT TIME '01:02:03.5' AS t, INTERVAL '1 month 2 days 3.5 seconds' AS i1,"
        " -INTERVAL 90 MINUTE AS i2",
    ),
    (
        "V09",
        "text types: unicode, quotes, newline, empty, UUID, JSON, ENUM",
        "SELECT * FROM (VALUES ('héllo \"q\"' || chr(10) || 'x', '00000000-0000-0000-0000-000000000001'::UUID,"
        " '{\"a\": 1}'::JSON, 'b'::ENUM('a', 'b')), ('', '00000000-0000-0000-0000-00000000000f'::UUID,"
        " '[]'::JSON, 'a'::ENUM('a', 'b'))) t(s, u, j, e)",
    ),
    (
        "V10",
        "BOOLEAN and BLOB",
        "SELECT * FROM (VALUES (true, '\\x00\\xFF'::BLOB), (false, ''::BLOB), (NULL, NULL::BLOB)) t(b, bl)",
    ),
    (
        "V11",
        "lists and fixed arrays with NULL elements",
        "SELECT ['a', NULL] AS vs, [1, 2]::INTEGER[2] AS arr, [1.5, NULL, -0.0]::DOUBLE[] AS ds,"
        " [1.25::DECIMAL(10,1)] AS decs, [[1], []]::BIGINT[][] AS nested",
    ),
    (
        "V12",
        "STRUCT, MAP and UNION",
        "SELECT {'b': 1, 'a': 'x', 'u': '00000000-0000-0000-0000-000000000002'::UUID,"
        " 'm': 2.50::DECIMAL(9,2), 'arr': [1, 2]::INTEGER[2]} AS st, MAP {'k': 1, 'j': 2} AS mp,"
        " union_value(num := 2) AS un, [MAP {'a': 1}] AS lm",
    ),
    ("V13", "duplicate column names keep order", "SELECT 1 AS a, 2 AS a"),
    ("V14", "ST04-12 string one", "SELECT '1'::VARCHAR AS c"),
    ("V15", "ST04-12 integer one", "SELECT 1::INTEGER AS c"),
    (
        "V16",
        "duplicate rows form a multiset",
        "SELECT 'x' AS k, 1::BIGINT AS n FROM range(3)",
    ),
    (
        "V17",
        "aggregate metric shape: grouped counts, ratios and money",
        "SELECT org, count(*)::BIGINT AS n, avg(v) AS mean_v,"
        " CAST(sum(v) * 100 AS DECIMAL(18,2)) AS cost FROM (VALUES ('o1', 1.25), ('o1', 2.5),"
        " ('o2', 0.125)) t(org, v) GROUP BY org ORDER BY org",
    ),
    (
        "V18",
        "HUGEINT and MAP nested in STRUCT (fetchall int/dict vs Arrow Decimal/pairs)",
        "SELECT {'h': 5::HUGEINT, 'u': 7::UHUGEINT, 'm': MAP {'k': 1}} AS st,"
        " MAP {'k': 5::HUGEINT} AS mh",
    ),
    (
        "V19",
        "UNION with a MAP member and a two-member UNION list",
        "SELECT union_value(m := MAP {'k': 1}) AS um,"
        " [union_value(n := 1)::UNION(n INTEGER, s VARCHAR),"
        " union_value(s := 'x')::UNION(n INTEGER, s VARCHAR)] AS ul",
    ),
    (
        "V20",
        "MAP with list and STRUCT keys, quoted STRUCT field names",
        "SELECT MAP {[1]: 'a'} AS lk, MAP {{'a': 1}: 'b'} AS sk,"
        " {'my field': 1, 'x': [MAP {1: 2::HUGEINT}]} AS q",
    ),
]


def main(out: str = "tests/fixtures/result_hash_vectors.json") -> None:
    con = duckdb.connect()
    vectors = []
    for vid, name, sql in VECTORS:
        rel = con.sql(sql)
        columns = [(n, str(t)) for n, t in zip(rel.columns, rel.types, strict=True)]
        rows = rel.fetchall()
        names, types = [c[0] for c in columns], [c[1] for c in columns]
        pairs = sorted((row_digest(names, types, r) for r in rows), key=lambda p: p[0])
        vectors.append(
            {
                "id": vid,
                "name": name,
                "sql": sql,
                "columns": [list(c) for c in columns],
                "row_json": [t for _, t in pairs],
                "result_hash": result_hash(columns, rows),
            }
        )
    doc = {
        "description": (
            "Golden vectors for herness.metrics.evidence.result_hash (impl 04 U04-05, R-15, DD04-07)."
            " columns freeze str() of DuckDB relation column types on the pinned DuckDB (VI04-02);"
            " row_json lists the canonical row texts in ascending SHA-256 digest order;"
            " result_hash = sha256(header + newline + newline-joined sorted row digest hex)."
        ),
        "duckdb_version": duckdb.__version__,
        "vectors": vectors,
    }
    Path(out).write_text(
        json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
    )


if __name__ == "__main__":
    main(*sys.argv[1:2])
