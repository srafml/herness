"""Text stage: compose, redact and hash classifier text (U03-24 ... U03-28; design 03 §4.2).

``build_text_redacted`` fills ``enrich.text_redacted`` in the new warehouse. Rows of records
whose ``source_updated_at`` is unchanged since the previous warehouse are copied from it; the
rest are composed, redacted through spec 10's ``redact_table`` and hashed. Raw ``core.*`` text
is read only here and handed only to redaction; it is never logged, written to ``enrich`` or
put in an error message (TH03-02).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

import duckdb
import pyarrow as pa

from herness.core import redact
from herness.core import time as clock
from herness.core.errors import SchemaViolation
from herness.core.logging import get_logger

__all__ = ["build_text_redacted", "compose_text", "content_hash", "normalize_text", "pair_text"]

MAX_FIELD_CHARS: Final = 4_000
CHUNK_ROWS: Final = 20_000
_WS: Final = re.compile(r"\s+")
_PREV: Final = "prev"
_VIEW: Final = "_enrich_text_chunk"
_FIRST_PRINTABLE: Final = 0x20
_SCHEMA_ERRORS: Final = (duckdb.CatalogException, duckdb.BinderException)

_log = get_logger("enrich.text")


class _Report(Protocol):
    """Counters the stage mutates.

    T03-xx pipeline: retype to StageReport (U03-142, herness.enrich.pipeline).
    """

    rows: int
    cache_hits: int
    failed: int


@dataclass(frozen=True)
class _Entity:
    name: str
    table: str
    short_col: str | None  # None: the entity has no short description (problem)
    body_col: str


_ENTITIES: Final = (
    _Entity("incident", "core.incident", "short_description", "description"),
    _Entity("change", "core.change", "short_description", "description"),
    _Entity("problem", "core.problem", None, "root_cause_text"),
)


@dataclass
class _Counts:
    copied: int = 0
    redacted: int = 0
    failed: int = 0
    empty: int = 0


# --- U03-24 ... U03-27 pure helpers ------------------------------------------------------------


def normalize_text(s: str | None) -> str:
    """NFKC, whitespace runs to one space, stripped, at most 4,000 characters (U03-24)."""
    if s is None:
        return ""
    text = _WS.sub(" ", unicodedata.normalize("NFKC", s)).strip()
    return text[:MAX_FIELD_CHARS].strip()


def compose_text(short_description: str | None, body: str | None) -> str:
    """Normalized short description and body joined by a blank line; ``""`` if both empty."""
    a = normalize_text(short_description)
    b = normalize_text(body)
    return (a + "\n\n" + b).strip()


def content_hash(redacted_text: str) -> str:
    """First 32 hex characters of sha256 of the redacted text (U03-26, spec 00 §5)."""
    return hashlib.sha256(redacted_text.encode("utf-8")).hexdigest()[:32]


def pair_text(incident_text: str, change_text: str) -> str:
    """State text for ``change_caused_pair`` built from two redacted texts (U03-27)."""
    return "INCIDENT:\n" + incident_text + "\n\nCHANGE:\n" + change_text


# --- U03-28 stage ------------------------------------------------------------------------------


def build_text_redacted(
    wh: duckdb.DuckDBPyConnection, *, prev_warehouse: Path | None, report: _Report
) -> None:
    """Fill ``enrich.text_redacted``, copying unchanged rows from ``prev_warehouse`` (U03-28)."""
    started = clock.monotonic()
    attached = prev_warehouse is not None and _attach_prev(wh, prev_warehouse)
    totals = _Counts()
    try:
        for entity in _ENTITIES:
            _run_entity(wh, entity, totals, attached=attached)
    finally:
        if attached:
            _detach_prev(wh)
    report.rows += totals.copied + totals.redacted
    report.cache_hits += totals.copied
    report.failed += totals.failed
    _log.info(
        "enrich.text.redacted",
        rows=totals.copied + totals.redacted,
        copied=totals.copied,
        redacted=totals.redacted,
        failed=totals.failed,
        empty=totals.empty,
        prev_attached=attached,
        duration_ms=round((clock.monotonic() - started) * 1000),
    )


def _attach_prev(wh: duckdb.DuckDBPyConnection, path: Path) -> bool:
    """``ATTACH <path> AS prev (READ_ONLY)``; False and a WARNING when that fails."""
    # DuckDB's ATTACH takes no prepared parameter: the path is quoted as a string literal.
    value = str(path)
    if any(ord(ch) < _FIRST_PRINTABLE for ch in value):
        _log.warning("enrich.text.prev_unavailable", error_type="InvalidPath")
        return False
    literal = "'" + value.replace("'", "''") + "'"
    try:
        wh.execute(f"ATTACH {literal} AS {_PREV} (READ_ONLY)")
    except duckdb.Error as exc:
        _log.warning("enrich.text.prev_unavailable", error_type=type(exc).__name__)
        return False
    return True


def _detach_prev(wh: duckdb.DuckDBPyConnection) -> None:
    try:
        wh.execute(f"DETACH {_PREV}")
    except duckdb.Error as exc:  # never masks the stage's own error; logged, not raised
        _log.warning("enrich.text.detach_failed", error_type=type(exc).__name__)


def _run_entity(
    wh: duckdb.DuckDBPyConnection, entity: _Entity, counts: _Counts, *, attached: bool
) -> None:
    """Copy then redact one entity, adding to ``counts``; DuckDB errors are SchemaViolation."""
    try:
        if attached:
            counts.copied += _copy_unchanged(wh, entity)
        for batch in _pending_batches(wh, entity):
            _redact_batch(wh, entity, batch, counts)
    except duckdb.Error as exc:
        msg = f"text stage {entity.name}: {_duck_reason(exc)}"
        raise SchemaViolation(msg) from (exc if isinstance(exc, _SCHEMA_ERRORS) else None)


def _duck_reason(exc: duckdb.Error) -> str:
    """The DuckDB message for catalog and binder errors (names only), else the error class.

    Other DuckDB messages (conversion, constraint) can quote row values, which must never
    reach an error message (ENG §3.4).
    """
    if isinstance(exc, _SCHEMA_ERRORS):
        return str(exc).splitlines()[0] if str(exc) else type(exc).__name__
    return type(exc).__name__


def _copy_unchanged(wh: duckdb.DuckDBPyConnection, entity: _Entity) -> int:
    """Insert prev rows of records present in both warehouses with equal ``source_updated_at``."""
    sql = f"""
        INSERT INTO enrich.text_redacted (record_id, entity, text, content_hash)
        SELECT p.record_id, p.entity, p.text, p.content_hash
        FROM {_PREV}.enrich.text_redacted AS p
        JOIN {entity.table} AS cur ON cur.record_id = p.record_id
        JOIN {_PREV}.{entity.table} AS old ON old.record_id = p.record_id
        WHERE p.entity = ? AND cur.source_updated_at = old.source_updated_at
    """  # noqa: S608 - table names come from the fixed _ENTITIES tuple
    row = wh.execute(sql, [entity.name]).fetchone()
    return int(row[0]) if row else 0


def _pending_batches(wh: duckdb.DuckDBPyConnection, entity: _Entity) -> Iterator[pa.RecordBatch]:
    """Records of ``entity`` without a row yet, with raw fields, in 20,000-row Arrow chunks.

    A separate cursor streams the selection so that inserts on ``wh`` do not end it.
    """
    short = entity.short_col or "CAST(NULL AS VARCHAR)"
    sql = f"""
        SELECT c.record_id, {short} AS short_description, c.{entity.body_col} AS body
        FROM {entity.table} AS c
        WHERE c.record_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM enrich.text_redacted AS t
            WHERE t.entity = ? AND t.record_id = c.record_id)
        ORDER BY c.record_id
    """  # noqa: S608 - table and column names come from the fixed _ENTITIES tuple
    cursor = wh.cursor()
    try:
        reader = cursor.execute(sql, [entity.name]).to_arrow_reader(CHUNK_ROWS)
        yield from reader
    finally:
        cursor.close()


def _redact_batch(
    wh: duckdb.DuckDBPyConnection, entity: _Entity, batch: pa.RecordBatch, counts: _Counts
) -> None:
    """Compose, drop empty texts, redact, hash and insert one chunk."""
    ids: list[str] = []
    texts: list[str] = []
    for record_id, short, body in zip(
        batch.column(0).to_pylist(),
        batch.column(1).to_pylist(),
        batch.column(2).to_pylist(),
        strict=True,
    ):
        text = compose_text(short, body)
        if text:
            ids.append(record_id)
            texts.append(text)
    counts.empty += batch.num_rows - len(ids)
    if not ids:
        return
    tbl = pa.table({"record_id": pa.array(ids, pa.string()), "text": pa.array(texts, pa.string())})
    out = redact.redact_table(tbl, ["text"], "record_id")
    out = out.filter(out.column("text").is_valid())
    counts.failed += tbl.num_rows - out.num_rows
    if out.num_rows == 0:
        return
    hashes = [content_hash(text) for text in out.column("text").to_pylist()]
    rows = pa.table(
        {
            "record_id": out.column("record_id"),
            "entity": pa.array([entity.name] * out.num_rows, pa.string()),
            "text": out.column("text"),
            "content_hash": pa.array(hashes, pa.string()),
        }
    )
    sql = f"INSERT INTO enrich.text_redacted BY NAME SELECT * FROM {_VIEW}"  # noqa: S608 - fixed name
    wh.register(_VIEW, rows)
    try:
        wh.execute(sql)
    finally:
        wh.unregister(_VIEW)
    counts.redacted += out.num_rows
