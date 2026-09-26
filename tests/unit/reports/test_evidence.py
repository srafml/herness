"""Tests for herness.reports._evidence: collector and evidence loading (T09-07)."""

from __future__ import annotations

import json
from collections.abc import Collection
from datetime import UTC, datetime
from typing import Any, cast

import duckdb
import pytest
from structlog.testing import capture_logs
from tests.support.ops_store import OpsStoreHandle

from herness.core import errors as e
from herness.core.ids import query_id as compute_query_id
from herness.core.types import Evidence
from herness.reports import _evidence as ev
from herness.store import ops
from herness.store.ops import core

pytestmark = pytest.mark.unit

BUILD = "20260925-120000-ABCDEF"
OTHER_BUILD = "20260101-000000-ZZZZZZ"
_HASH = "0" * 64


def _qid(sql: str, params: dict[str, Any] | None = None, build: str = BUILD) -> str:
    return compute_query_id(sql, params or {}, build)


def _record(sql: str, params: dict[str, Any] | None = None, **kw: Any) -> str:
    params = params or {}
    build = kw.pop("build_id", BUILD)
    qid = _qid(sql, params, build)
    evidence = Evidence(
        query_id=qid, run_id=None, build_id=build, sql=sql, params=params, result_hash=_HASH,
        row_count=kw.get("row_count", 1), result_sample=kw.get("sample", [{"n": 1}]),
        executed_at=datetime(2026, 9, 25, 12, tzinfo=UTC), duration_ms=5,
    )  # fmt: skip
    assert ops.record_evidence(evidence)
    return qid


def _warehouse(*, with_sample: bool = True) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    sample = ", result_sample JSON" if with_sample else ""
    con.execute(
        "CREATE SCHEMA meta; CREATE TABLE meta.evidence (query_id VARCHAR PRIMARY KEY,"
        f" sql VARCHAR, params JSON, row_count BIGINT, executed_at VARCHAR{sample})"
    )
    return con


def _meta(con: duckdb.DuckDBPyConnection, qid: str, sql: str, **kw: Any) -> None:
    cols = ["query_id", "sql", "params", "row_count", "executed_at"]
    vals: list[object] = [qid, sql, kw.get("params", "{}"), kw.get("row_count", 7), kw.get("at")]
    if "sample" in kw:
        cols.append("result_sample")
        vals.append(kw["sample"])
    marks = ", ".join("?" * len(vals))
    con.execute(f"INSERT INTO meta.evidence ({', '.join(cols)}) VALUES ({marks})", vals)  # noqa: S608


def _collector(*ids: str) -> ev.EvidenceCollector:
    collector = ev.EvidenceCollector()
    for qid in ids:
        collector.use(qid, "title")
    return collector


# --- UT09-22 EvidenceCollector (U09-13) ---------------------------------------------------------


def test_ut09_22_first_use_order_and_deduplicated_used_by() -> None:
    """UT09-22 uses in mixed, repeated order keep first-use order and dedupe used_by."""
    a, b, c = "q_000000000000000a", "q_000000000000000b", "q_000000000000000c"
    collector = ev.EvidenceCollector()
    for qid, where in [(b, "x"), (a, "y"), (b, "z"), (c, "x"), (a, "y"), (b, "x")]:
        collector.use(qid, where)
    assert collector.ordered_ids() == [b, a, c]
    assert collector.used_by(b) == ["x", "z"]
    assert collector.used_by(a) == ["y"]
    assert collector.used_by("q_ffffffffffffffff") == []


def test_ut09_22_used_by_capped_at_fifty() -> None:
    """UT09-22 used_by keeps 50 locations, then one "… and N more" entry."""
    collector = ev.EvidenceCollector()
    for i in range(53):
        collector.use("q_000000000000000a", f"w{i}")
    listed = collector.used_by("q_000000000000000a")
    assert listed[:50] == [f"w{i}" for i in range(50)]
    assert listed[50:] == ["… and 3 more"]


@pytest.mark.parametrize("bad", ["q_123", "Q_000000000000000a", "q_000000000000000a\n", ""])
def test_ut09_22_invalid_query_id_rejected(bad: str) -> None:
    """UT09-22 an id not matching QUERY_ID_RE raises ReportContractError."""
    with pytest.raises(e.ReportContractError, match="invalid query_id"):
        ev.EvidenceCollector().use(bad, "title")


# --- UT09-23 load_evidence_entries (U09-14) -----------------------------------------------------


def test_ut09_23_ops_meta_both_none(ops_store: OpsStoreHandle) -> None:
    """UT09-23 ops only, meta only, both (ops wins) and none (found=False)."""
    ops_only = _record("SELECT 1", {"a": 1}, row_count=3, sample=[{"n": 1, "m": None}])
    both = _record("SELECT 2", build_id=OTHER_BUILD)
    meta_only, none = "q_1111111111111111", "q_2222222222222222"
    con = _warehouse()
    _meta(con, both, "SELECT meta", sample='[{"z": 1}]')
    _meta(con, meta_only, "SELECT 3", params='{"k": "v"}', sample='[{"b": 2, "a": "x"}]', at="t0")
    collector = _collector(none, meta_only, both, ops_only)
    collector.use(ops_only, "sections[0].paragraphs[0]")
    entries = ev.load_evidence_entries(collector, con, build_id=BUILD, sample_rows=10)
    assert [x.query_id for x in entries] == [none, meta_only, both, ops_only]
    missing, meta, dual, own = entries
    assert missing == ev.EvidenceEntry(
        none, "", {}, None, None, BUILD, None, [], ["title"], found=False
    )
    assert (meta.sql, meta.params, meta.row_count, meta.executed_at) == (
        "SELECT 3",
        {"k": "v"},
        7,
        "t0",
    )
    assert (meta.build_id, meta.result_sample, meta.sample_columns) == (
        BUILD,
        [{"b": "2", "a": "x"}],
        ["b", "a"],
    )
    assert meta.found
    assert (dual.sql, dual.build_id, dual.result_sample) == ("SELECT 2", OTHER_BUILD, [{"n": "1"}])
    assert (own.sql, own.params, own.row_count) == ("SELECT 1", {"a": 1}, 3)
    assert own.executed_at == "2026-09-25T12:00:00.000000Z"
    assert own.result_sample == [{"n": "1", "m": ""}]
    assert own.used_by == ["title", "sections[0].paragraphs[0]"]  # fmt: skip


def test_ut09_23_v1_build_without_result_sample(ops_store: OpsStoreHandle) -> None:
    """UT09-23 a v1 warehouse without result_sample gives None ("Sample not stored")."""
    con = _warehouse(with_sample=False)
    qid = "q_1111111111111111"
    _meta(con, qid, "SELECT 3")
    (entry,) = ev.load_evidence_entries(_collector(qid), con, build_id=BUILD, sample_rows=5)
    assert entry.found
    assert entry.result_sample is None
    assert entry.sample_columns == []


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (None, None),
        ('{"a": 1}', None),
        ("[]", []),
        ('[1, {"a": null}]', [{"a": ""}]),
    ],
)
def test_ut09_23_sample_null_invalid_or_empty(
    ops_store: OpsStoreHandle, stored: str | None, expected: list[dict[str, str]] | None
) -> None:
    """UT09-23 NULL, unparsable or non-list samples are None; non-dict rows are skipped."""
    con = _warehouse()
    qid = "q_1111111111111111"
    con.execute("INSERT INTO meta.evidence VALUES (?, 's', NULL, NULL, NULL, ?)", [qid, stored])
    (entry,) = ev.load_evidence_entries(_collector(qid), con, build_id=BUILD, sample_rows=5)
    assert entry.result_sample == expected
    assert entry.params == {}
    assert entry.row_count is None


def test_ut09_23_sample_rows_and_cell_cut(ops_store: OpsStoreHandle) -> None:
    """UT09-23 only the first sample_rows rows are kept and each cell is cut to 200 chars."""
    con = _warehouse()
    qid = "q_1111111111111111"
    rows = [{"c": "x" * 300, "d": [1, 2]}, {"c": "y", "d": 2.5}, {"c": "z", "d": True}]
    _meta(con, qid, "s", sample=json.dumps(rows))
    (entry,) = ev.load_evidence_entries(_collector(qid), con, build_id=BUILD, sample_rows=2)
    assert entry.result_sample == [{"c": "x" * 200, "d": "[1, 2]"}, {"c": "y", "d": "2.5"}]
    assert entry.sample_columns == ["c", "d"]
    (zero,) = ev.load_evidence_entries(_collector(qid), con, build_id=BUILD, sample_rows=0)
    assert zero.result_sample == []


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ('{"a": [1]}', {"a": [1]}),
        ("[1, 2]", {"_raw": "[1, 2]"}),
        ('"' + "p" * 600 + '"', {"_raw": '"' + "p" * 499}),
    ],
)
def test_ut09_23_params_parsed_or_raw(
    ops_store: OpsStoreHandle, stored: str, expected: dict[str, object]
) -> None:
    """UT09-23 params parse to a dict; anything else is kept as _raw cut to 500 chars."""
    con = _warehouse()
    qid = "q_1111111111111111"
    _meta(con, qid, "s", params=stored)
    (entry,) = ev.load_evidence_entries(_collector(qid), con, build_id=BUILD, sample_rows=1)
    assert entry.params == expected


def test_ut09_23_params_invalid_json_is_raw() -> None:
    """UT09-23 invalid params JSON text becomes {"_raw": text[:500]}."""
    assert ev._params("{bad" + "x" * 600) == {"_raw": ("{bad" + "x" * 600)[:500]}
    assert ev._params({"a": 1}) == {"a": 1}
    assert ev._params(5) == {"_raw": "5"}
    assert ev._sample("not json", 5) == (None, [])  # a VARCHAR sample column on old builds


def test_ut09_23_executed_at_timestamps() -> None:
    """UT09-23 DuckDB timestamps become fixed-width UTC text; None stays None."""
    naive = datetime(2026, 9, 25, 12, 30)  # noqa: DTZ001 - DuckDB TIMESTAMP is naive
    assert ev._executed_at(naive) == "2026-09-25T12:30:00.000000Z"
    assert ev._executed_at(naive.replace(tzinfo=UTC)) == "2026-09-25T12:30:00.000000Z"
    assert ev._executed_at(None) is None
    assert ev._executed_at("t") == "t"


def test_ut09_23_tampered_ops_row_falls_through(ops_store: OpsStoreHandle) -> None:
    """UT09-23 an ops row whose query_id does not recompute is dropped and logged (TH05-12)."""
    good = _qid("SELECT 9")
    bad_build = "q_3333333333333333"
    core.run_write(
        lambda conn: conn.executemany(
            "INSERT INTO evidence (query_id, run_id, build_id, sql, params, result_hash,"
            " row_count, result_sample, executed_at, duration_ms)"
            " VALUES (?, NULL, ?, ?, ?, ?, 1, '[]', '2026-09-25T12:00:00.000000Z', 1)",
            [
                (good, BUILD, "SELECT tampered", "{}", _HASH),
                (bad_build, "not-a-build", "SELECT 1", "{}", _HASH),
            ],
        ),
        op="test_tamper",
    )
    con = _warehouse()
    _meta(con, good, "SELECT 9", sample="[]")
    with capture_logs() as logs:
        entries = ev.load_evidence_entries(
            _collector(good, bad_build), con, build_id=BUILD, sample_rows=5
        )
    assert [(x.sql, x.found) for x in entries] == [("SELECT 9", True), ("", False)]
    tampered = [log for log in logs if log["event"] == "reports.evidence.tampered"]
    assert sorted(log["query_id"] for log in tampered) == sorted([good, bad_build])
    assert all(log["log_level"] == "warning" for log in tampered)
    assert all("sql" not in log for log in tampered)


def test_ut09_23_tamper_check_rejects_non_dict_params() -> None:
    """UT09-23 an ops row whose params are not an object is treated as tampered."""
    row = {"query_id": "q_1111111111111111", "sql": "s", "params": [1], "build_id": BUILD}
    assert not ev._trusted(cast(ops.UiEvidenceRow, row))


class _Spy:
    """Wraps a DuckDB connection and records every statement's bound ids."""

    def __init__(self, con: duckdb.DuckDBPyConnection) -> None:
        self.con, self.batches = con, cast(list[list[str]], [])

    def execute(self, sql: str, params: Any = None) -> duckdb.DuckDBPyConnection:
        if isinstance(params, dict) and "ids" in params:
            self.batches.append(list(params["ids"]))
        return self.con.execute(sql, params)


def test_ut09_23_warehouse_chunks_of_500(
    ops_store: OpsStoreHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT09-23 ids not in ops are looked up in meta.evidence at most 500 per statement."""
    ids = [f"q_{n:016x}" for n in range(1, 1202)]
    asked: list[Collection[str]] = []

    def none_in_ops(query_ids: Collection[str]) -> dict[str, ops.UiEvidenceRow]:
        asked.append(list(query_ids))
        return {}

    monkeypatch.setattr(ops, "ui_get_evidence_rows", none_in_ops)
    con = _warehouse(with_sample=False)
    con.executemany(
        "INSERT INTO meta.evidence (query_id, sql) VALUES (?, 's')", [[q] for q in ids[:3]]
    )
    spy = _Spy(con)
    entries = ev.load_evidence_entries(
        _collector(*ids), cast(duckdb.DuckDBPyConnection, spy), build_id=BUILD, sample_rows=1
    )
    assert [len(b) for b in spy.batches] == [500, 500, 201]
    assert asked == [ids]
    assert sum(x.found for x in entries) == 3


def test_ut09_23_all_in_ops_skips_warehouse(ops_store: OpsStoreHandle) -> None:
    """UT09-23 when ops holds every id the warehouse is not queried."""
    qid = _record("SELECT 1")
    con = duckdb.connect(":memory:")  # no meta schema: a query would fail
    (entry,) = ev.load_evidence_entries(_collector(qid), con, build_id=BUILD, sample_rows=5)
    assert entry.found


def test_ut09_23_duckdb_error_is_query_error(ops_store: OpsStoreHandle) -> None:
    """UT09-23 a DuckDB error becomes QueryError naming build_id."""
    con = duckdb.connect(":memory:")
    with pytest.raises(e.QueryError) as exc:
        ev.load_evidence_entries(
            _collector("q_1111111111111111"), con, build_id=BUILD, sample_rows=5
        )
    assert exc.value.details == {"build_id": BUILD}
    assert BUILD in exc.value.message


def test_ut09_23_store_busy_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT09-23 StoreBusy from the ops read propagates unchanged."""

    def busy(ids: Collection[str]) -> dict[str, ops.UiEvidenceRow]:
        msg = "busy"
        raise e.StoreBusy(msg)

    monkeypatch.setattr(ops, "ui_get_evidence_rows", busy)
    with pytest.raises(e.StoreBusy):
        ev.load_evidence_entries(
            _collector("q_1111111111111111"), _warehouse(), build_id=BUILD, sample_rows=5
        )


def test_ut09_23_empty_collector() -> None:
    """UT09-23 an empty collector gives no entries and touches no store."""
    con = duckdb.connect(":memory:")
    assert (
        ev.load_evidence_entries(ev.EvidenceCollector(), con, build_id=BUILD, sample_rows=5) == []
    )
