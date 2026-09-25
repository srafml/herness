"""Tests for herness.metrics.evidence pure functions (U04-05 … U04-09, U04-13)."""

import datetime
import decimal
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.support.result_hash_vectors import VECTORS as VECTOR_SOURCES

from herness.core import ids
from herness.core.errors import ConfigError, SchemaViolation
from herness.metrics import evidence
from herness.metrics._encode import row_digest
from herness.metrics.evidence import (
    canonical_params,
    iter_batch_rows,
    result_hash,
    result_sample,
    rows_equivalent,
)

pytestmark = pytest.mark.unit

D = decimal.Decimal
UTC = datetime.UTC
VECTORS = Path(__file__).resolve().parents[2] / "fixtures" / "result_hash_vectors.json"
BUILD = "20260924-211403-ABCDEF"


def _load_vectors() -> list[dict[str, Any]]:
    data = json.loads(VECTORS.read_text(encoding="utf-8"))
    vectors: list[dict[str, Any]] = data["vectors"]
    return vectors


def _arrow_rows(con: duckdb.DuckDBPyConnection, sql: str) -> list[tuple[object, ...]]:
    result = con.sql(sql).arrow()
    batches = result.to_batches() if isinstance(result, pa.Table) else list(result)
    return list(iter_batch_rows(batches))


def test_ut04_02_vector_file_has_enough_vectors() -> None:
    """UT04-02 the golden vector file holds at least 12 vectors with unique IDs."""
    vectors = _load_vectors()
    assert len(vectors) >= 12
    assert len({v["id"] for v in vectors}) == len(vectors)
    generated = [(vid, name, sql) for vid, name, sql in VECTOR_SOURCES]
    assert [(v["id"], v["name"], v["sql"]) for v in vectors] == generated


@pytest.mark.parametrize("vector", _load_vectors(), ids=lambda v: v["id"])
def test_ut04_02_golden_vectors(vector: dict[str, Any]) -> None:
    """UT04-02 result_hash equals the stored hash via fetchall and Arrow (VI04-02 frozen)."""
    con = duckdb.connect()
    rel = con.sql(vector["sql"])
    columns = [(n, str(t)) for n, t in zip(rel.columns, rel.types, strict=True)]
    assert [list(c) for c in columns] == vector["columns"]
    rows = rel.fetchall()
    assert result_hash(columns, rows) == vector["result_hash"]
    assert result_hash(columns, _arrow_rows(con, vector["sql"])) == vector["result_hash"]
    names, types = [c[0] for c in columns], [c[1] for c in columns]
    texts = sorted((row_digest(names, types, r) for r in rows), key=lambda p: p[0])
    assert [t for _, t in texts] == vector["row_json"]
    header = json.dumps(vector["columns"], separators=(",", ":"), ensure_ascii=False)
    digests = sorted(hashlib.sha256(t.encode("utf-8")).hexdigest() for t in vector["row_json"])
    payload = header + "\n" + "\n".join(digests)
    assert hashlib.sha256(payload.encode("utf-8")).hexdigest() == vector["result_hash"]


def test_ut04_04_empty_result_hash() -> None:
    """UT04-04 no rows hash to sha256(header + newline)."""
    cols = [("a", "BIGINT"), ("b", "VARCHAR")]
    header = '[["a","BIGINT"],["b","VARCHAR"]]'
    assert result_hash(cols, []) == hashlib.sha256((header + "\n").encode()).hexdigest()


def test_ut04_05_sample_of_120_rows() -> None:
    """UT04-05 120 rows sample to 50 rows in ascending digest order."""
    cols = [("n", "BIGINT")]
    rows = [(i,) for i in range(120)]
    sample = result_sample(cols, rows, limit=50)
    assert len(sample) == 50
    digests = [row_digest(["n"], ["BIGINT"], (s["n"],))[0] for s in sample]
    assert digests == sorted(row_digest(["n"], ["BIGINT"], r)[0] for r in rows)[:50]
    assert result_sample(cols, rows[:3]) == sorted(
        [{"n": 0}, {"n": 1}, {"n": 2}], key=lambda s: row_digest(["n"], ["BIGINT"], (s["n"],))[0]
    )
    assert result_sample(cols, rows, limit=0) == []


def test_ut04_05_sample_limit_bounds() -> None:
    """UT04-05 limits outside 0..1000 raise SchemaViolation."""
    with pytest.raises(SchemaViolation):
        result_sample([("n", "BIGINT")], [], limit=1001)


def test_ut04_06_sample_value_encoding() -> None:
    """UT04-06 samples carry .9g floats, decimal strings and ISO Z timestamps."""
    cols = [
        ("f", "DOUBLE"),
        ("m", "DECIMAL(18,2)"),
        ("t", "TIMESTAMP WITH TIME ZONE"),
        ("x", "DOUBLE"),
        ("l", "VARCHAR[]"),
    ]
    ts = datetime.datetime(2026, 3, 1, 12, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=1)))
    rows = [(1 / 3, D("10.005"), ts, float("inf"), ["a"])]
    (sample,) = result_sample(cols, rows)
    assert sample == {
        "f": 0.333333333,
        "m": "10.00",
        "t": "2026-03-01T11:00:00.000000Z",
        "x": "inf",
        "l": ["a"],
    }


def test_ut04_11_tolerance_compare() -> None:
    """UT04-11 cells differing by 1e-12 are equivalent, by 1e-3 are not."""
    cols = [("k", "VARCHAR"), ("v", "DOUBLE"), ("m", "DECIMAL(18,2)")]
    a = [("x", 1.0, D("2.00")), ("y", 5.0, None)]
    assert rows_equivalent(cols, a, [("y", 5.0 + 1e-12, None), ("x", 1.0 + 1e-12, D("2.00"))])
    assert not rows_equivalent(cols, a, [("x", 1.0 + 1e-3, D("2.00")), ("y", 5.0, None)])
    assert not rows_equivalent(cols, a, a[:1])
    assert not rows_equivalent(cols, a, [("x", 1.0, D("2.00")), ("y", 5.0, D("1.00"))])
    assert not rows_equivalent(cols, a, [("x", 1.0, D("2.00")), ("z", 5.0, None)])
    special = [("n", math.nan, None), ("i", math.inf, None)]
    assert rows_equivalent(cols, special, list(reversed(special)))
    one = [("x", 1.0, None)]
    for left, right in ((math.inf, 1.0), (math.inf, -math.inf), (math.nan, 1.0), (1e308, math.inf)):
        a_row, b_row = [("x", left, None)], [("x", right, None)]
        assert not rows_equivalent(cols, a_row, b_row)
        assert not rows_equivalent(cols, b_row, a_row)
    assert rows_equivalent(cols, [("x", -math.inf, None)], [("x", -math.inf, None)])
    assert rows_equivalent(cols, one, one)
    with pytest.raises(SchemaViolation, match="row width 2 != column count 3"):
        rows_equivalent(cols, [("x", 1.0)], [("x", 1.0)])


def test_ut04_12_iter_batch_rows() -> None:
    """UT04-12 iter_batch_rows yields rows in batch order as to_pylist values."""
    b1 = pa.record_batch({"a": [1, 2], "b": ["x", None]})
    b2 = pa.record_batch({"a": [3], "b": ["z"]})
    assert list(iter_batch_rows([b1, b2])) == [(1, "x"), (2, None), (3, "z")]


def test_ut04_09_constants() -> None:
    """UT04-09 (U04-13 part) limits and allowlists are fixed at import; core.* not writable."""
    assert evidence.RESULT_SAMPLE_LIMIT == 50
    assert evidence.MAX_RESULT_ROWS == 1_000_000
    assert evidence.HASH_PARALLEL_MIN_ROWS == 200_000
    assert 1 <= evidence.HASH_WORKERS <= 8
    assert isinstance(evidence.WRITABLE_TABLES, frozenset)
    assert len(evidence.WRITABLE_TABLES) == 11
    assert "score.portfolio" in evidence.WRITABLE_TABLES
    assert "core.incident" not in evidence.WRITABLE_TABLES


def test_ut04_10_canonical_params_json_types() -> None:
    """UT04-10 params become JSON types, keys sorted, text equal to canonical_json (R-14)."""
    bind = {
        "z_amount": D("12.50"),
        "a_day": datetime.date(2026, 1, 31),
        "m_at": datetime.datetime(
            2026, 1, 1, 2, tzinfo=datetime.timezone(datetime.timedelta(hours=2))
        ),
        "ids": ("i1", "i2"),
        "flag": True,
        "n": None,
        "ratio": 0.5,
        "nested": {"b": [D("1.0")], "a": 1},
    }
    params = canonical_params(bind, {"grain": "month", "where": ["a", "b"]})
    assert params == {
        "bind": {
            "a_day": "2026-01-31",
            "flag": True,
            "ids": ["i1", "i2"],
            "m_at": "2026-01-01T00:00:00.000000Z",
            "n": None,
            "nested": {"a": 1, "b": ["1.0"]},
            "ratio": 0.5,
            "z_amount": "12.50",
        },
        "template": {"grain": "month", "where": ["a", "b"]},
    }
    assert list(params) == ["bind", "template"]
    assert list(params["bind"]) == sorted(params["bind"])  # type: ignore[call-overload]
    text = ids.canonical_json(params)
    assert json.dumps(params, sort_keys=True, separators=(",", ":"), ensure_ascii=False) == text
    assert ids.query_id("SELECT 1", params, BUILD) == ids.query_id("SELECT  1", params, BUILD)


@pytest.mark.parametrize("name", ["Bad", "1x", "a-b", "", "a" * 64, "a\n"])
def test_ut04_10_canonical_params_bad_bind_name(name: str) -> None:
    """UT04-10 bind names outside ^[a-z_][a-z0-9_]{0,62}$ raise ConfigError."""
    with pytest.raises(ConfigError, match="bad bind name"):
        canonical_params({name: 1}, {})


@pytest.mark.parametrize(
    "value",
    [
        datetime.datetime(2026, 1, 1),  # noqa: DTZ001 - naive on purpose
        math.nan,
        math.inf,
        D("NaN"),
        object(),
        {1, 2},
        [datetime.datetime(2026, 1, 1)],  # noqa: DTZ001 - naive on purpose
    ],
)
def test_ut04_10_canonical_params_unsupported_value(value: object) -> None:
    """UT04-10 naive datetimes, non-finite floats and unsupported types raise ConfigError."""
    with pytest.raises(ConfigError, match="unsupported bind value for k"):
        canonical_params({"k": value}, {})


def test_ut04_10_canonical_params_not_canonicalisable() -> None:
    """UT04-10 too-deep params or non-string keys re-raise as ConfigError from SchemaViolation."""
    deep: object = 1
    for _ in range(70):
        deep = [deep]
    with pytest.raises(ConfigError, match="params not canonicalisable") as info:
        canonical_params({"k": deep}, {})
    assert info.value.__cause__ is not None
    with pytest.raises(ConfigError, match="params not canonicalisable"):
        canonical_params({}, {"t": {1: "x"}})
    too_deep: object = 1
    for _ in range(2000):
        too_deep = [too_deep]
    with pytest.raises(ConfigError, match="params not canonicalisable"):
        canonical_params({"k": too_deep}, {})


ROWS = st.lists(
    st.tuples(
        st.one_of(st.none(), st.integers(-1000, 1000)),
        st.one_of(st.none(), st.floats(allow_nan=True, allow_infinity=True)),
        st.one_of(st.none(), st.text(max_size=5)),
    ),
    max_size=15,
)
PCOLS = [("i", "BIGINT"), ("f", "DOUBLE"), ("s", "VARCHAR")]


@given(ROWS, st.randoms(use_true_random=False))
def test_pt04_01_hash_invariant_under_permutation(rows: list[tuple[Any, ...]], rnd: Any) -> None:
    """PT04-01 result_hash is invariant under row permutation."""
    shuffled = list(rows)
    rnd.shuffle(shuffled)
    assert result_hash(PCOLS, shuffled) == result_hash(PCOLS, rows)


PERTURB = st.sampled_from([0.0, 1e-12, 1e-7, 1e-3])


def _perturb(f: float | None, delta: float) -> float | None:
    return f if f is None or not math.isfinite(f) else f * (1 + delta) + delta


@given(ROWS, ROWS, st.data())
def test_pt04_11_rows_equivalent_symmetric_reflexive(
    a: list[tuple[Any, ...]], other: list[tuple[Any, ...]], data: Any
) -> None:
    """PT04-11 rows_equivalent is symmetric and reflexive (b near a, and b independent)."""
    deltas = data.draw(st.lists(PERTURB, min_size=len(a), max_size=len(a)))
    near = [(i, _perturb(f, d), s) for (i, f, s), d in zip(a, deltas, strict=True)]
    b = data.draw(st.permutations(near))
    assert rows_equivalent(PCOLS, a, a)
    assert rows_equivalent(PCOLS, a, data.draw(st.permutations(a)))
    assert rows_equivalent(PCOLS, a, b) == rows_equivalent(PCOLS, b, a)
    assert rows_equivalent(PCOLS, a, other) == rows_equivalent(PCOLS, other, a)
