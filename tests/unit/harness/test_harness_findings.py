"""Tests for herness.harness.findings (T06-07): U06-47, U06-49, U06-50, U06-143.

UT06-34 builds a local tiny DuckDB warehouse in `tmp_path` (the spec 11 `tiny_build`
fixture, T11-17, does not exist yet) and wraps the real read-only `DuckWarehouse` in a
handle that counts cursor executes, so "one query per call" is asserted directly.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest
from hypothesis import event, find, given
from hypothesis import strategies as st

from herness.core.types import EntityScope, Finding, NumberRef, ScopeEntityType
from herness.harness import findings
from herness.harness import warehouse as wh
from herness.harness.findings import (
    EntityCatalog,
    compute_dedup_key,
    extract_markers,
    impact_usd,
    normalize_objective,
    validate_markers,
)
from herness.harness.llm.settings import SqlSettings
from herness.store.ops import core, runs
from herness.store.ops.runs import RunRow

pytestmark = pytest.mark.unit

_QID = "q_" + "a" * 16
_ULID = "0" * 25
BUILD_ID = "20260926-101500-ABCDEF"
_T0 = datetime(2026, 9, 26, 10, 0, tzinfo=UTC)


def _num(nid: str, value: object = 1, unit: str = "count") -> NumberRef:
    return NumberRef.model_validate(
        {"id": nid, "value": value, "unit": unit, "query_id": _QID, "column": "c", "row_key": None}
    )


def _finding(numbers: list[NumberRef]) -> Finding:
    return Finding.model_validate(
        {
            "finding_id": f"fnd_{_ULID}1",
            "run_id": f"run_{_ULID}1",
            "task_id": f"task_{_ULID}1",
            "author_role": "analyst",
            "claim": "x",
            "entity_type": "team",
            "entity_id": "t1",
            "numbers": numbers,
            "query_ids": [_QID],
            "confidence": 0.5,
            "created_at": _T0,
        }
    )


# UT06-05 ----------------------------------------------------------------------------------


def test_ut06_05_impact_usd_max_usd_value() -> None:
    """UT06-05 impact_usd returns the largest usd value as a Decimal."""
    f = _finding(
        [_num("n1", "3.10", "usd"), _num("n2", "1250000.00", "usd"), _num("n3", 9e9, "count")]
    )
    assert impact_usd(f) == Decimal("1250000.00")
    assert isinstance(impact_usd(f), Decimal)


def test_ut06_05_impact_usd_zero_without_usd() -> None:
    """UT06-05 impact_usd is Decimal("0") when the finding cites no usd number."""
    assert impact_usd(_finding([_num("n1", 42, "count")])) == Decimal("0")


# UT06-31 ----------------------------------------------------------------------------------


def test_ut06_31_extract_markers_returns_parser_pair() -> None:
    """UT06-31 extract_markers returns valid ids in order and malformed inner strings."""
    ids, bad = extract_markers("a [[n2]] b [[n1]] [[x]] [[n2]] [[N3]]")
    assert ids == ["n2", "n1", "n2"]
    assert bad == ["x", "N3"]


@pytest.mark.parametrize(
    ("text", "numbers", "expected"),
    [
        ("cost [[n1]] and [[n2]]", ["n1", "n2"], []),
        ("repeat [[n1]] [[n1]]", ["n1"], []),
        ("bad [[x]] [[n1]]", ["n1"], ["malformed marker [[x]]"]),
        ("dup [[n1]]", ["n1", "n1"], ["duplicate number id n1"]),
        ("unknown [[n1]] [[n7]] [[n7]]", ["n1"], ["marker [[n7]] has no number"]),
        ("unused [[n1]]", ["n1", "n2"], ["number n2 is not referenced in the text"]),
        (
            "[[n9]] [[?]] all",
            ["n1", "n1"],
            [
                "malformed marker [[?]]",
                "duplicate number id n1",
                "marker [[n9]] has no number",
                "number n1 is not referenced in the text",
            ],
        ),
    ],
)
def test_ut06_31_validate_markers_table(text: str, numbers: list[str], expected: list[str]) -> None:
    """UT06-31 malformed, duplicate, unknown and unused are each reported, in that order."""
    assert validate_markers(text, [_num(n) for n in numbers]) == expected


def test_ut06_31_require_all_used_false_skips_unused() -> None:
    """UT06-31 require_all_used=False does not report unreferenced numbers."""
    assert validate_markers("none", [_num("n1")], require_all_used=False) == []


# UT06-33 ----------------------------------------------------------------------------------


def _scope(ids: list[str], entity_type: str = "team", **kw: object) -> EntityScope:
    return EntityScope.model_validate({"entity_type": entity_type, "entity_ids": ids, **kw})


def test_ut06_33_normalize_objective() -> None:
    """UT06-33 NFKC, lower case, collapsed whitespace, stripped, trailing punctuation removed."""
    fullwidth_pain = "".join(chr(0xFEE0 + ord(c)) for c in "pain")  # NFKC folds to "pain"
    assert normalize_objective(f"  Check\t\tTHE  {fullwidth_pain}!?. ") == "check the pain"
    assert normalize_objective("a . ;") == "a"
    assert normalize_objective("") == ""


def test_ut06_33_dedup_key_objective_variants_equal() -> None:
    """UT06-33 objective case and whitespace variants give equal 16-hex keys."""
    s = _scope(["t2", "t1"])
    a = compute_dedup_key("analyst", "ops", s, "Assess the team.")
    b = compute_dedup_key("analyst", "ops", _scope(["t1", "t2"]), "  assess   THE\nteam ")
    assert a == b
    assert len(a) == 16
    assert a == a.lower()
    int(a, 16)


def test_ut06_33_dedup_key_differs_by_scope() -> None:
    """UT06-33 a different scope (ids, type or period) gives a different key."""
    base = compute_dedup_key("analyst", "ops", _scope(["t1"]), "x")
    others = {
        compute_dedup_key("analyst", "ops", _scope(["t2"]), "x"),
        compute_dedup_key("analyst", "ops", _scope(["t1"], "service"), "x"),
        compute_dedup_key("analyst", "ops", _scope(["t1"], period_start=date(2026, 1, 1)), "x"),
        compute_dedup_key("analyst", "ops", _scope(["t1"], period_end=date(2026, 1, 1)), "x"),
        compute_dedup_key("skeptic", "ops", _scope(["t1"]), "x"),
        compute_dedup_key("analyst", "change", _scope(["t1"]), "x"),
    }
    assert base not in others
    assert len(others) == 6


# UT06-34 ----------------------------------------------------------------------------------

_TABLES = (
    ("core", "service", "service_id", "name", [("s1", "Payments"), ("s2", None)]),
    ("core", "team", "team_id", "name", [("t1", "Blue")]),
    ("core", "org", "org_id", "name", [("o1", "Eng")]),
    ("core", "work_item", "record_id", "key", [("w1", "EPIC-1")]),
    ("enrich", "cluster", "cluster_id", "label", [("c1", "DNS flaps")]),
    ("score", "funding", "candidate_id", "title", [("f1", "Fix DNS")]),
)


def _make_build(warehouse_dir: Path) -> None:
    warehouse_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(warehouse_dir / f"wh-{BUILD_ID}.duckdb"))
    for schema in ("core", "enrich", "metrics", "score", "meta"):
        con.execute(f"CREATE SCHEMA {schema}")
    for schema, table, id_col, name_col, rows in _TABLES:
        con.execute(f"CREATE TABLE {schema}.{table} ({id_col} VARCHAR, {name_col} VARCHAR)")
        con.executemany(f"INSERT INTO {schema}.{table} VALUES (?, ?)", rows)  # noqa: S608 - fixture
    con.close()


class _CountingCursor:
    def __init__(self, inner: duckdb.DuckDBPyConnection, log: list[str]) -> None:
        self._inner = inner
        self._log = log

    def execute(self, sql: str, params: object = None) -> duckdb.DuckDBPyConnection:
        self._log.append(sql)
        return self._inner.execute(sql, params)


class _CountingHandle:
    """Minimal WarehouseHandle over the real read-only DuckWarehouse, counting executes."""

    def __init__(self, inner: wh.DuckWarehouse) -> None:
        self._inner = inner
        self.queries: list[str] = []

    @property
    def build_id(self) -> str:
        return self._inner.build_id

    @property
    def path(self) -> Path:
        return self._inner.path

    def cursor(self) -> object:
        return _CountingCursor(self._inner.cursor(), self.queries)

    def schema(self) -> Mapping[str, Mapping[str, Mapping[str, str]]]:
        return self._inner.schema()

    def table_comment(self, qualified: str) -> str:
        return self._inner.table_comment(qualified)


@pytest.fixture
def handle(tmp_path: Path) -> _CountingHandle:
    _make_build(tmp_path / "wh")
    return _CountingHandle(
        wh.open_warehouse(BUILD_ID, warehouse_dir=tmp_path / "wh", sql=SqlSettings())
    )


@pytest.mark.parametrize(
    ("entity_type", "known", "name"),
    [
        ("service", "s1", "Payments"),
        ("team", "t1", "Blue"),
        ("org", "o1", "Eng"),
        ("work_item", "w1", "EPIC-1"),
        ("cluster", "c1", "DNS flaps"),
        ("candidate", "f1", "Fix DNS"),
    ],
)
def test_ut06_34_missing_and_names_one_query_per_call(
    handle: _CountingHandle, entity_type: ScopeEntityType, known: str, name: str
) -> None:
    """UT06-34 unknown ids are reported, names map found ids, one query per call."""
    cat = EntityCatalog(handle)
    assert cat.missing(entity_type, [known, "zz1", "zz2"]) == {"zz1", "zz2"}
    assert len(handle.queries) == 1
    assert cat.names(entity_type, [known, "zz1", "new"]) == {known: name}
    assert len(handle.queries) == 2  # only "new" was uncached: still one query
    assert cat.names(entity_type, [known, "zz1", "new"]) == {known: name}
    assert cat.missing(entity_type, []) == set()
    assert len(handle.queries) == 2  # fully cached: no query


def test_ut06_34_null_name_falls_back_to_id(handle: _CountingHandle) -> None:
    """UT06-34 a found id whose name column is NULL maps to its own id."""
    assert EntityCatalog(handle).names("service", ["s2"]) == {"s2": "s2"}


def test_ut06_34_sql_uses_allowlist_and_bound_ids(handle: _CountingHandle) -> None:
    """UT06-34 identifiers come from the allowlist map; ids are bound, never inlined."""
    EntityCatalog(handle).missing("work_item", ["w1'; DROP TABLE x; --"])
    (sql,) = handle.queries
    assert "core.work_item" in sql
    assert "record_id" in sql
    assert "unnest(?)" in sql
    assert "DROP" not in sql


def test_ut06_34_unknown_entity_type_raises(handle: _CountingHandle) -> None:
    """UT06-34 an entity type outside the allowlist map is refused before any query."""
    with pytest.raises(ValueError, match="entity type"):
        EntityCatalog(handle).missing("bogus", ["x"])  # type: ignore[arg-type]
    assert handle.queries == []


def _write_run(run_id: str) -> None:
    row = RunRow(
        run_id=run_id,
        kind="funding_review",
        depth="standard",
        profile="default",
        build_id="b_1",
        status="created",
        started_at=_T0,
        finished_at=None,
        token_usage={},
        cost_usd=Decimal("0"),
        config_hash="c" * 16,
        meta={},
    )
    fn: Callable[[sqlite3.Connection], Any] = lambda conn: runs.insert_run(conn, row)  # noqa: E731
    core.run_write(fn, op="test_findings")


def test_ut06_34_run_entities_use_ops_store(ops_store: object, handle: _CountingHandle) -> None:
    """UT06-34 `run` ids are checked in the ops store; the name is the run id; cached."""
    run_id = f"run_{_ULID}7"
    _write_run(run_id)
    cat = EntityCatalog(handle)
    assert cat.missing("run", [run_id, f"run_{_ULID}8"]) == {f"run_{_ULID}8"}
    assert cat.names("run", [run_id]) == {run_id: run_id}
    assert handle.queries == []


def test_ut06_34_run_lookup_cached(
    monkeypatch: pytest.MonkeyPatch, handle: _CountingHandle
) -> None:
    """UT06-34 get_run is called once per uncached run id."""
    calls: list[str] = []

    def fake_get_run(run_id: str) -> object:
        calls.append(run_id)
        return None

    monkeypatch.setattr(findings, "get_run", fake_get_run)
    cat = EntityCatalog(handle)
    assert cat.missing("run", ["r1", "r1"]) == {"r1"}
    assert cat.missing("run", ["r1"]) == {"r1"}
    assert calls == ["r1"]


# PT06-01 ----------------------------------------------------------------------------------

_WORDS = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_", min_size=1, max_size=8)
_WS = st.sampled_from([" ", "  ", "\t", "\n", " \t ", "　", "\r\n"])


@given(
    words=st.lists(_WORDS, min_size=1, max_size=8),
    ids=st.lists(
        st.text(alphabet="abcdef0123", min_size=1, max_size=6), min_size=1, max_size=6, unique=True
    ),
    data=st.data(),
)
def test_pt06_01_key_invariant(words: list[str], ids: list[str], data: st.DataObject) -> None:
    """PT06-01 key invariant under entity order, case and whitespace of the objective."""
    canonical = " ".join(words) + "."
    variant_words = [
        "".join(c.upper() if data.draw(st.booleans()) else c for c in w) for w in words
    ]
    seps = [data.draw(_WS) for _ in range(len(words) + 1)]
    variant = seps[0] + "".join(w + s for w, s in zip(variant_words, seps[1:], strict=True))
    variant = variant.rstrip() + data.draw(st.sampled_from(["", ".", "!?", " ;", ": "]))
    shuffled = data.draw(st.permutations(ids))
    a = compute_dedup_key("analyst", "ops", _scope(ids), canonical)
    b = compute_dedup_key("analyst", "ops", _scope(list(shuffled)), variant)
    assert a == b


# PT06-02 ----------------------------------------------------------------------------------

_POOL = [f"n{i}" for i in range(6)]
_SEGMENT = st.one_of(
    st.sampled_from(["text ", "cost ", "42 ", " "]),
    st.sampled_from([f"[[{n}]]" for n in _POOL]),
    st.sampled_from(["[[x]]", "[[n]]", "[[ n1]]"]),
)


_PLAIN = st.sampled_from(["text ", "cost ", "42 ", " "])
_MUTATIONS = st.sampled_from(["none", "none", "malformed", "dup_number", "drop_number", "unknown"])
type _MarkerCase = tuple[str, list[str]]


@st.composite
def _bijection_case(draw: st.DrawFn) -> _MarkerCase:
    """Unique ids, each referenced one or more times, then optionally one mutation."""
    number_ids = draw(st.lists(st.sampled_from(_POOL), min_size=1, max_size=6, unique=True))
    parts = [f"[[{n}]]" for n in number_ids for _ in range(draw(st.integers(1, 3)))]
    parts += draw(st.lists(_PLAIN, max_size=4))
    mutation = draw(_MUTATIONS)
    if mutation == "malformed":
        parts.append(draw(st.sampled_from(["[[x]]", "[[n]]", "[[ n1]]"])))
    elif mutation == "dup_number":
        number_ids = [*number_ids, draw(st.sampled_from(number_ids))]
    elif mutation == "drop_number":
        number_ids = number_ids[1:]
    elif mutation == "unknown":
        parts.append("[[n99]]")
    return "".join(draw(st.permutations(parts))), number_ids


@st.composite
def _random_case(draw: st.DrawFn) -> _MarkerCase:
    segments = draw(st.lists(_SEGMENT, max_size=12))
    return "".join(segments), draw(st.lists(st.sampled_from(_POOL), max_size=6))


_MARKER_CASE = st.one_of(_bijection_case(), _random_case())


def _is_bijection(text: str, number_ids: list[str]) -> bool:
    ids, malformed = extract_markers(text)
    unique = len(set(number_ids)) == len(number_ids)
    return not malformed and unique and set(ids) == set(number_ids)


@given(case=_MARKER_CASE)
def test_pt06_02_errors_empty_iff_bijection(case: _MarkerCase) -> None:
    """PT06-02 errors empty iff markers and number ids are a bijection (and none malformed)."""
    text, number_ids = case
    bijection = _is_bijection(text, number_ids)
    event("non-empty bijection" if bijection and number_ids else "other")
    errors = validate_markers(text, [_num(n) for n in number_ids])
    assert (errors == []) == bijection


def test_pt06_02_strategy_generates_non_empty_bijections() -> None:
    """PT06-02 the case strategy really yields non-empty bijections and mutated near-misses."""
    found = find(_MARKER_CASE, lambda c: len(c[1]) >= 2 and _is_bijection(*c))
    assert validate_markers(found[0], [_num(n) for n in found[1]]) == []
    near = find(
        _MARKER_CASE,
        lambda c: bool(c[1]) and not _is_bijection(*c) and "[[" in c[0] and len(set(c[1])) > 1,
    )
    assert validate_markers(near[0], [_num(n) for n in near[1]]) != []
