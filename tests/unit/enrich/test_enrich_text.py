"""Tests for the text stage helpers and ``build_text_redacted`` (impl 03 T03-05, U03-24 ... U03-28).

``redact_table`` is replaced by a stub redactor so that these tests need no key or config.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.support.text_warehouse import Rec, create_warehouse, stored

from herness.core import redact
from herness.core.errors import SchemaViolation
from herness.core.logging import configure_logging, reset_logging
from herness.enrich import text as tx

pytestmark = pytest.mark.unit

FAIL_MARK = "POISONED"
FULLWIDTH = "".join(chr(0xFEE0 + ord(ch)) for ch in "Full")  # U+FF26 U+FF55 U+FF4C U+FF4C


@dataclasses.dataclass
class Report:
    """Local stand-in for StageReport (U03-142, not built yet)."""

    rows: int = 0
    cache_hits: int = 0
    failed: int = 0


class StubRedactor:
    """``redact_table`` stand-in: ``<r>`` + upper-cased text; NULL when the text is poisoned."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def __call__(self, tbl: pa.Table, text_cols: list[str], id_col: str = "record_id") -> pa.Table:
        assert (text_cols, id_col) == (["text"], "record_id")
        ids = tbl.column(id_col).to_pylist()
        self.seen.extend(ids)
        texts = [
            None if FAIL_MARK in text else "<r>" + text.upper()
            for text in tbl.column("text").to_pylist()
        ]
        return pa.table({id_col: pa.array(ids, pa.string()), "text": pa.array(texts, pa.string())})


def expected(short: str | None, body: str | None) -> str:
    return "<r>" + tx.compose_text(short, body).upper()


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> StubRedactor:
    fake = StubRedactor()
    monkeypatch.setattr(redact, "redact_table", fake)
    return fake


@pytest.fixture
def logs(capsys: pytest.CaptureFixture[str]) -> Iterator[pytest.CaptureFixture[str]]:
    configure_logging("INFO")
    capsys.readouterr()
    yield capsys
    reset_logging()


def _events(capsys: pytest.CaptureFixture[str]) -> tuple[str, list[dict[str, Any]]]:
    err = capsys.readouterr().err
    return err, [json.loads(line) for line in err.splitlines() if line.strip()]


# --- UT03-22 normalize and compose --------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        (None, ""),
        ("", ""),
        ("  \t\n ", ""),
        ("\N{LATIN SMALL LIGATURE FI}le \N{LATIN SMALL LIGATURE FL}ow", "file flow"),
        (FULLWIDTH + "\N{NO-BREAK SPACE}width", "Full width"),  # NFKC width, NBSP
        ("a\t\tb\r\n\nc  d", "a b c d"),
        ("\N{EM SPACE}lead and trail\N{LINE SEPARATOR}", "lead and trail"),
    ],
)
def test_ut03_22_normalize_text_cases(raw: str | None, want: str) -> None:
    """UT03-22 NFKC, whitespace runs collapsed, stripped; None gives ""."""
    assert tx.normalize_text(raw) == want


def test_ut03_22_normalize_text_truncates_to_4000() -> None:
    """UT03-22 5,000 chars are cut to 4,000; a trailing space left by the cut is stripped."""
    assert tx.normalize_text("x" * 5_000) == "x" * 4_000
    assert tx.normalize_text("y" * 3_999 + " " + "z" * 1_000) == "y" * 3_999


@pytest.mark.parametrize(
    ("short", "body", "want"),
    [
        (None, None, ""),
        ("  ", "\t", ""),
        ("Disk full", None, "Disk full"),
        (None, "root cause: bad\tpatch", "root cause: bad patch"),
        ("\N{LATIN SMALL LIGATURE FI}x  it", "now\n\nplease", "fix it\n\nnow please"),
    ],
)
def test_ut03_22_compose_text_cases(short: str | None, body: str | None, want: str) -> None:
    """UT03-22 compose joins with a blank line, strips, and returns "" for two empties."""
    assert tx.compose_text(short, body) == want


def test_ut03_22_compose_text_bounds() -> None:
    """UT03-22 each part is truncated to 4,000, so the composed text is at most 8,002 chars."""
    out = tx.compose_text("a" * 5_000, "b" * 5_000)
    assert out == "a" * 4_000 + "\n\n" + "b" * 4_000
    assert len(out) == 8_002


# --- UT03-23 content hash -----------------------------------------------------------------------


def test_ut03_23_content_hash_abc() -> None:
    """UT03-23 content_hash("abc") is the first 32 hex chars of sha256("abc")."""
    sha_abc = "ba7816bf8f01cfea414140de5dae2223"  # pragma: allowlist secret - sha256 test vector
    assert tx.content_hash("abc") == sha_abc
    assert tx.content_hash("abc") == hashlib.sha256(b"abc").hexdigest()[:32]


# --- UT03-24 pair text --------------------------------------------------------------------------


def test_ut03_24_pair_text_format() -> None:
    """UT03-24 pair_text has the exact INCIDENT/CHANGE layout."""
    assert tx.pair_text("inc text", "chg text") == "INCIDENT:\ninc text\n\nCHANGE:\nchg text"


# --- PT03-03 properties -------------------------------------------------------------------------


@given(st.one_of(st.none(), st.text(max_size=6_000)))
def test_pt03_03_normalize_idempotent_and_bounded(s: str | None) -> None:
    """PT03-03 normalize is idempotent and at most 4,000 characters."""
    once = tx.normalize_text(s)
    assert len(once) <= tx.MAX_FIELD_CHARS
    assert tx.normalize_text(once) == once


@given(st.text())
def test_pt03_03_hash_is_32_hex_and_deterministic(s: str) -> None:
    """PT03-03 content_hash is 32 lowercase hex chars and deterministic."""
    digest = tx.content_hash(s)
    assert len(digest) == 32
    assert set(digest) <= set("0123456789abcdef")
    assert digest == tx.content_hash(s)


# --- UT03-25 stage ------------------------------------------------------------------------------

UNCHANGED = (
    Rec("incident", "INC-A", "Printer jam", "floor 2"),
    Rec("change", "CHG-A", "Patch db", "window sunday"),
    Rec("problem", "PRB-A", None, "memory leak"),
)
CHANGED = (
    Rec("incident", "INC-B", "VPN down", "all users"),
    Rec("change", "CHG-B", "Rotate certs", "yearly"),
)


def _prev(tmp_path: Path, stub: StubRedactor) -> Path:
    """A previous warehouse whose text stage already ran (with the stub)."""
    path = tmp_path / "prev.duckdb"
    wh = create_warehouse(path, [*UNCHANGED, *CHANGED])
    tx.build_text_redacted(wh, prev_warehouse=None, report=Report())
    wh.close()
    stub.seen.clear()
    return path


def test_ut03_25_copies_unchanged_redacts_changed(
    tmp_path: Path, stub: StubRedactor, logs: pytest.CaptureFixture[str]
) -> None:
    """UT03-25 3 copied, 2 redacted, 1 failure counted, 1 empty skipped; prev detached."""
    prev = _prev(tmp_path, stub)
    changed = [rec.touched(body="edited " + rec.record_id) for rec in CHANGED]
    empty = Rec("problem", "PRB-E", None, " \t ")
    poisoned = Rec("incident", "INC-F", "mail " + FAIL_MARK, None)
    wh = create_warehouse(tmp_path / "new.duckdb", [*UNCHANGED, *changed, empty, poisoned])
    report = Report()
    _events(logs)

    tx.build_text_redacted(wh, prev_warehouse=prev, report=report)

    assert (report.rows, report.cache_hits, report.failed) == (5, 3, 1)
    rows = stored(wh)
    assert set(rows) == {"INC-A", "CHG-A", "PRB-A", "INC-B", "CHG-B"}
    assert sorted(stub.seen) == ["CHG-B", "INC-B", "INC-F"]  # copied rows are not re-redacted
    for rec in (*UNCHANGED, *changed):
        entity, text, digest = rows[rec.record_id]
        assert (entity, text) == (rec.entity, expected(rec.short, rec.body))
        assert digest == tx.content_hash(text)
    assert rows["INC-B"][1] == "<r>VPN DOWN\n\nEDITED INC-B"
    names = [
        row[0] for row in wh.execute("SELECT database_name FROM duckdb_databases()").fetchall()
    ]
    assert "prev" not in names
    err, events = _events(logs)
    done = [e for e in events if e["event"] == "enrich.text.redacted"]
    assert len(done) == 1
    assert (done[0]["copied"], done[0]["redacted"], done[0]["failed"], done[0]["empty"]) == (
        3,
        2,
        1,
        1,
    )
    assert "Printer jam" not in err
    assert FAIL_MARK not in err


def test_ut03_25_without_prev_redacts_everything(tmp_path: Path, stub: StubRedactor) -> None:
    """UT03-25 no previous warehouse: every non-empty record is redacted, no cache hits."""
    wh = create_warehouse(tmp_path / "w.duckdb", [*UNCHANGED, *CHANGED])
    report = Report(rows=1, cache_hits=2, failed=3)
    tx.build_text_redacted(wh, prev_warehouse=None, report=report)
    assert (report.rows, report.cache_hits, report.failed) == (6, 2, 3)  # counters are added to
    assert len(stub.seen) == 5
    assert stored(wh)["PRB-A"] == ("problem", "<r>MEMORY LEAK", tx.content_hash("<r>MEMORY LEAK"))


def test_ut03_25_redacts_in_chunks(
    tmp_path: Path, stub: StubRedactor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-25 records are read and redacted per chunk of CHUNK_ROWS rows."""
    calls: list[int] = []
    inner = redact.redact_table

    def counting(tbl: pa.Table, text_cols: list[str], id_col: str = "record_id") -> pa.Table:
        calls.append(tbl.num_rows)
        return inner(tbl, text_cols, id_col)

    monkeypatch.setattr(redact, "redact_table", counting)
    monkeypatch.setattr(tx, "CHUNK_ROWS", 2)
    recs = [Rec("incident", f"INC-{i}", f"text {i}", None) for i in range(5)]
    wh = create_warehouse(tmp_path / "w.duckdb", recs)
    report = Report()
    tx.build_text_redacted(wh, prev_warehouse=None, report=report)
    assert calls == [2, 2, 1]
    assert report.rows == 5


@pytest.mark.parametrize("name", ["missing.duckdb", "bad\x01name.duckdb"])
def test_ut03_25_prev_unavailable_logs_and_redacts_all(
    tmp_path: Path, stub: StubRedactor, logs: pytest.CaptureFixture[str], name: str
) -> None:
    """UT03-25 an unattachable prev warehouse logs a WARNING and every record is redacted."""
    wh = create_warehouse(tmp_path / "w.duckdb", UNCHANGED)
    report = Report()
    _events(logs)
    tx.build_text_redacted(wh, prev_warehouse=tmp_path / name, report=report)
    assert (report.rows, report.cache_hits) == (3, 0)
    _, events = _events(logs)
    warn = [e for e in events if e["event"] == "enrich.text.prev_unavailable"]
    assert len(warn) == 1
    assert warn[0]["level"] == "warning"


def test_ut03_25_duckdb_error_is_schema_violation_and_detaches(
    tmp_path: Path, stub: StubRedactor
) -> None:
    """UT03-25 a catalog error becomes SchemaViolation naming the entity; prev is detached."""
    prev = _prev(tmp_path, stub)
    wh = create_warehouse(tmp_path / "new.duckdb", UNCHANGED)
    wh.execute("DROP TABLE core.change")
    pattern = r"^text stage change: Catalog Error: Table with name change does not exist"
    with pytest.raises(SchemaViolation, match=pattern):
        tx.build_text_redacted(wh, prev_warehouse=prev, report=Report())
    names = [
        row[0] for row in wh.execute("SELECT database_name FROM duckdb_databases()").fetchall()
    ]
    assert "prev" not in names


def test_ut03_25_value_errors_never_quote_row_values(tmp_path: Path, stub: StubRedactor) -> None:
    """UT03-25 a DuckDB error that could quote row values carries only its class name."""
    prev_path = tmp_path / "prev.duckdb"
    prev = create_warehouse(prev_path, [])
    prev.execute("ALTER TABLE core.incident ALTER source_updated_at TYPE VARCHAR")
    prev.execute("INSERT INTO core.incident VALUES ('INC-A', 'secret words', NULL, NULL)")
    prev.execute("INSERT INTO enrich.text_redacted VALUES ('INC-A', 'incident', 'x', 'h')")
    prev.close()
    wh = create_warehouse(tmp_path / "new.duckdb", UNCHANGED)
    with pytest.raises(SchemaViolation) as info:
        tx.build_text_redacted(wh, prev_warehouse=prev_path, report=Report())
    assert info.value.message == "text stage incident: ConversionException"


def test_ut03_25_detach_failure_is_logged(logs: pytest.CaptureFixture[str]) -> None:
    """UT03-25 a failed DETACH is logged as a WARNING and never raised."""
    tx._detach_prev(duckdb.connect())
    _, events = _events(logs)
    assert [e["event"] for e in events] == ["enrich.text.detach_failed"]


def test_ut03_25_chunk_where_every_record_fails(tmp_path: Path, stub: StubRedactor) -> None:
    """UT03-25 a chunk whose every redaction fails inserts nothing and counts each failure."""
    recs = [Rec("change", f"CHG-{i}", FAIL_MARK, None) for i in range(2)]
    wh = create_warehouse(tmp_path / "w.duckdb", recs)
    report = Report()
    tx.build_text_redacted(wh, prev_warehouse=None, report=report)
    assert (report.rows, report.failed) == (0, 2)
    assert stored(wh) == {}
