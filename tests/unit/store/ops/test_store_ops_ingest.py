"""Unit tests for the ingest area herness.store.ops.ingest (impl 01 U01-27 … U01-32, U01-92;
UT01-20, UT01-22 … UT01-24, UT01-95).

Impl 01 §11 names `tests/unit/connectors/`; these tests sit next to the other ops-area tests
because the module under test is `herness/store/ops/ingest.py` (L1).
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from herness.core.errors import SchemaViolation
from herness.store import ops
from herness.store.ops import core, ingest
from herness.store.ops.ingest import FileIngestRow, SliceRow, Watermark
from herness.store.ops.migrate import migrate

pytestmark = pytest.mark.unit

UTC = datetime.UTC
T0 = datetime.datetime(2026, 9, 1, tzinfo=UTC)
NOW = datetime.datetime(2026, 9, 26, 10, 0, 0, 123456, tzinfo=UTC)
FP = "a" * 64


def _day(n: int) -> datetime.datetime:
    return T0 + datetime.timedelta(days=n)


@pytest.fixture
def store(ops_store: Path) -> Path:
    """A migrated temp ops store."""
    migrate()
    return ops_store


def _raw(sql: str, params: tuple[object, ...] = ()) -> None:
    core.run_write(lambda conn: conn.execute(sql, params), op="test_raw")


# --- UT01-20 watermarks -----------------------------------------------------------------


def test_ut01_20_set_lower_ignored_higher_applied(store: Path) -> None:
    """UT01-20 set, set lower (False), set higher, get: monotonic, fixed-width round trip."""
    assert ingest.get_watermark("servicenow", "incident") is None
    v1 = datetime.datetime(2026, 9, 2, 3, 4, 5, 60, tzinfo=UTC)
    assert ingest.set_watermark("servicenow", "incident", "sys_updated_on", v1, now=NOW)
    lower = v1 - datetime.timedelta(microseconds=1)
    assert ingest.set_watermark("servicenow", "incident", "other", lower, now=NOW) is False
    assert ingest.set_watermark("servicenow", "incident", "sys_updated_on", v1, now=NOW) is False
    tz = datetime.timezone(datetime.timedelta(hours=2))
    v2 = datetime.datetime(2026, 9, 3, 12, 0, tzinfo=tz)
    later = NOW + datetime.timedelta(hours=1)
    assert ingest.set_watermark("servicenow", "incident", "sys_updated_on", v2, now=later)
    got = ingest.get_watermark("servicenow", "incident")
    assert got == Watermark("servicenow", "incident", "sys_updated_on", v2, later)
    assert got is not None
    assert got.value.tzinfo is UTC
    row = core.read_one("SELECT value FROM watermark")
    assert row is not None
    assert row["value"] == "2026-09-03T10:00:00.000000Z"


def test_ut01_20_naive_rejected(store: Path) -> None:
    """UT01-20 a naive value raises SchemaViolation and writes nothing."""
    with pytest.raises(SchemaViolation):
        ingest.set_watermark("s", "e", "f", datetime.datetime(2026, 1, 1), now=NOW)  # noqa: DTZ001 - the case under test
    assert ingest.get_watermark("s", "e") is None


def test_ut01_20_corrupt_value_raises(store: Path) -> None:
    """UT01-20 a stored value that passes the CHECK but is no real time is corrupt."""
    _raw(
        "INSERT INTO watermark VALUES ('s', 'e', 'f', ?, ?)",
        ("2026-13-45T10:00:00.000000Z", "2026-09-26T10:00:00.000000Z"),
    )
    with pytest.raises(SchemaViolation, match="corrupt watermark") as info:
        ingest.get_watermark("s", "e")
    assert dict(info.value.context) == {"source": "s", "entity": "e"}


# --- UT01-22 ensure_slices --------------------------------------------------------------


def _plan(*ends: int) -> list[tuple[datetime.datetime, datetime.datetime]]:
    starts = (0, *ends[:-1])
    return [(_day(s), _day(e)) for s, e in zip(starts, ends, strict=True)]


def _key(rows: list[SliceRow]) -> list[tuple[datetime.datetime, datetime.datetime, str]]:
    return [(r.slice_start, r.slice_end, r.status) for r in rows]


def test_ut01_22_plan_and_rerun_idempotent(store: Path) -> None:
    """UT01-22 plan A creates pending rows; rerunning A changes nothing."""
    plan = _plan(1, 2, 3)
    rows = ingest.ensure_slices("servicenow", "incident", plan, now=NOW)
    assert _key(rows) == [(s, e, "pending") for s, e in plan]
    first = rows[0]
    assert first == SliceRow(
        "servicenow", "incident", _day(0), _day(1), "pending", 0, (), 0, None, NOW
    )
    ingest.mark_slice_running("servicenow", "incident", _day(0), now=NOW)
    ingest.mark_slice_done(
        "servicenow", "incident", _day(0), rows=5, files=["a/b.parquet"], now=NOW
    )
    again = ingest.ensure_slices("servicenow", "incident", plan, now=NOW + datetime.timedelta(1))
    assert again[0] == SliceRow(
        "servicenow", "incident", _day(0), _day(1), "done", 5, ("a/b.parquet",), 1, None, NOW
    )
    assert again[1:] == rows[1:]


def test_ut01_22_longer_last_slice_resets(store: Path) -> None:
    """UT01-22 a plan with a longer last slice resets that row (running/failed/done) only."""
    ingest.ensure_slices("s", "e", _plan(1, 2), now=NOW)
    ingest.mark_slice_running("s", "e", _day(1), now=NOW)
    ingest.mark_slice_done("s", "e", _day(1), rows=3, files=["x.parquet"], now=NOW)
    later = NOW + datetime.timedelta(hours=1)
    rows = ingest.ensure_slices("s", "e", _plan(1, 4), now=later)
    assert rows[1] == SliceRow("s", "e", _day(1), _day(4), "pending", 0, (), 1, None, later)
    assert rows[0].updated_at == NOW


def test_ut01_22_done_with_larger_end_stays_done(store: Path) -> None:
    """UT01-22 a done row whose stored end is past the planned end stays done; others reset."""
    ingest.ensure_slices("s", "e", _plan(1, 5), now=NOW)
    ingest.mark_slice_done("s", "e", _day(1), rows=7, files=[], now=NOW)
    ingest.ensure_slices("s", "e", [(_day(10), _day(15))], now=NOW)
    ingest.mark_slice_failed("s", "e", _day(10), error="boom", now=NOW)
    later = NOW + datetime.timedelta(hours=1)
    shorter = ingest.ensure_slices("s", "e", [(_day(1), _day(3))], now=later)
    assert _key(shorter) == [(_day(1), _day(5), "done")]
    assert shorter[0].rows == 7
    failed = ingest.ensure_slices("s", "e", [(_day(10), _day(12))], now=later)
    assert failed[0] == SliceRow("s", "e", _day(10), _day(12), "pending", 0, (), 0, "boom", later)


def test_ut01_22_reads_in_chunks(store: Path) -> None:
    """UT01-22 more than 500 slice starts are read back in chunks, ordered by start."""
    plan = [
        (_day(0) + datetime.timedelta(hours=h), _day(0) + datetime.timedelta(hours=h + 1))
        for h in range(1201)
    ]
    rows = ingest.ensure_slices("s", "e", list(reversed(plan)), now=NOW)
    assert [(r.slice_start, r.slice_end) for r in rows] == plan
    other = ingest.ensure_slices("s", "e2", plan[:1], now=NOW)
    assert len(other) == 1


def test_ut01_22_corrupt_files_json(store: Path) -> None:
    """UT01-22 a `files` value that is valid JSON but not a list of paths is rejected."""
    ingest.ensure_slices("s", "e", _plan(1), now=NOW)
    _raw("UPDATE sync_slice SET files = '{\"a\": 1}'")
    with pytest.raises(SchemaViolation, match=r"sync_slice\.files"):
        ingest.ensure_slices("s", "e", _plan(1), now=NOW)


# --- UT01-23 slice transitions -----------------------------------------------------------


def test_ut01_23_transitions(store: Path) -> None:
    """UT01-23 running, failed (600 chars), running, done: attempts 2, error cut, then cleared."""
    ingest.ensure_slices("s", "e", _plan(1), now=NOW)
    ingest.mark_slice_running("s", "e", _day(0), now=NOW)
    ingest.mark_slice_failed("s", "e", _day(0), error="E" * 600, now=NOW)
    row = core.read_one("SELECT status, attempts, last_error FROM sync_slice")
    assert row is not None
    assert (row["status"], row["attempts"], row["last_error"]) == ("failed", 1, "E" * 500)
    ingest.mark_slice_running("s", "e", _day(0), now=NOW)
    later = NOW + datetime.timedelta(minutes=5)
    ingest.mark_slice_done(
        "s", "e", _day(0), rows=9, files=("p/1.parquet", "p/2.parquet"), now=later
    )
    (got,) = ingest.ensure_slices("s", "e", _plan(1), now=NOW)
    assert got == SliceRow(
        "s", "e", _day(0), _day(1), "done", 9, ("p/1.parquet", "p/2.parquet"), 2, None, later
    )


@pytest.mark.parametrize("fn", ["mark_slice_running", "mark_slice_done", "mark_slice_failed"])
def test_ut01_23_missing_row(store: Path, fn: str) -> None:
    """UT01-23 a transition on a missing row raises SchemaViolation("slice not found")."""
    extra: dict[str, object] = {
        "mark_slice_running": {},
        "mark_slice_done": {"rows": 1, "files": []},
        "mark_slice_failed": {"error": "x"},
    }[fn]  # type: ignore[assignment]
    with pytest.raises(SchemaViolation, match="slice not found") as info:
        getattr(ingest, fn)("s", "e", _day(0), now=NOW, **extra)
    assert dict(info.value.context) == {"source": "s", "entity": "e"}


def test_ut01_23_negative_rows_rejected(store: Path) -> None:
    """UT01-23 `rows < 0` fails the CHECK; the row keeps its state."""
    ingest.ensure_slices("s", "e", _plan(1), now=NOW)
    with pytest.raises(SchemaViolation):
        ingest.mark_slice_done("s", "e", _day(0), rows=-1, files=[], now=NOW)
    assert ingest.ensure_slices("s", "e", _plan(1), now=NOW)[0].status == "pending"


# --- UT01-24 file_ingest ----------------------------------------------------------------


def _file_row(fp: str = FP) -> FileIngestRow:
    mtime = datetime.datetime(2026, 9, 25, 8, 30, 1, 5, tzinfo=UTC)
    return FileIngestRow(
        fp, "files", "incident", "inbox/a b.csv", 1234, mtime, 10, ("l/x.parquet",), NOW
    )


def test_ut01_24_record_twice_get(store: Path) -> None:
    """UT01-24 record twice: second insert returns False; get returns an equal row."""
    assert ingest.get_file_ingest(FP) is None
    row = _file_row()
    assert ingest.record_file_ingest(row) is True
    assert ingest.record_file_ingest(row) is False
    assert ingest.get_file_ingest(FP) == row


@pytest.mark.parametrize("fp", ["A" * 64, "a" * 63, "g" * 64, "a" * 64 + "\n"])
def test_ut01_24_bad_fingerprint(store: Path, fp: str) -> None:
    """UT01-24 a fingerprint that is not 64 lower-case hex raises SchemaViolation."""
    with pytest.raises(SchemaViolation, match="fingerprint"):
        ingest.get_file_ingest(fp)
    with pytest.raises(SchemaViolation, match="fingerprint"):
        ingest.record_file_ingest(_file_row(fp))


# --- UT01-95 list_watermarks ------------------------------------------------------------


def test_ut01_95_list_watermarks(store: Path) -> None:
    """UT01-95 three rows ordered by (source, entity), aware UTC, per-tool monitoring keys."""
    assert ingest.list_watermarks() == []
    streams = [
        ("servicenow", "incident", "sys_updated_on"),
        ("monitoring:prometheus", "metric_daily", "day"),
        ("monitoring:datadog", "event", "date_happened"),
    ]
    for i, (source, entity, field) in enumerate(streams):
        ingest.set_watermark(source, entity, field, _day(i), now=NOW)
    got = ingest.list_watermarks()
    assert [(w.source, w.entity) for w in got] == [
        ("monitoring:datadog", "event"),
        ("monitoring:prometheus", "metric_daily"),
        ("servicenow", "incident"),
    ]
    assert got[0] == Watermark("monitoring:datadog", "event", "date_happened", _day(2), NOW)
    assert all(w.value.tzinfo is UTC and w.updated_at.tzinfo is UTC for w in got)


def test_ut01_95_corrupt_value_in_second_store(store: Path, tmp_path: Path) -> None:
    """UT01-95 a corrupt stored value in a second store raises SchemaViolation."""
    ingest.set_watermark("servicenow", "incident", "f", _day(0), now=NOW)
    core.reset_connections(path=tmp_path / "second" / "ops.sqlite")
    migrate()
    _raw(
        "INSERT INTO watermark VALUES ('servicenow', 'incident', 'f', ?, ?)",
        ("2026-09-26T10:00:00.000000Z", "2026-02-30T10:00:00.000000Z"),
    )
    with pytest.raises(SchemaViolation, match="corrupt watermark"):
        ingest.list_watermarks()


# --- package re-exports -----------------------------------------------------------------


def test_ut01_20_reexported_through_package() -> None:
    """UT01-20 the ingest names are the same objects through `herness.store.ops`."""
    for name in ("Watermark", "get_watermark", "set_watermark", "list_watermarks", "ensure_slices"):
        assert getattr(ops, name) is getattr(ingest, name)
