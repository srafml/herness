"""Hand-built warehouses for the enrich text stage (impl 03 T03-05).

The core DDL (spec 02 SQL 100-299) is not in the tree yet, so these helpers create minimal
``core.incident``, ``core.change`` and ``core.problem`` tables with only the columns the text
stage reads, plus the real ``herness/model/sql/000_settings.sql`` (``enrich.text_redacted``).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb

SETTINGS_SQL = Path(__file__).resolve().parents[2] / "herness/model/sql/000_settings.sql"
T0 = datetime(2026, 9, 1, tzinfo=UTC)

_CORE_DDL = (
    "CREATE TABLE core.incident (record_id VARCHAR, source_updated_at TIMESTAMPTZ, "
    "short_description VARCHAR, description VARCHAR)",
    "CREATE TABLE core.change (record_id VARCHAR, source_updated_at TIMESTAMPTZ, "
    "short_description VARCHAR, description VARCHAR)",
    "CREATE TABLE core.problem (record_id VARCHAR, source_updated_at TIMESTAMPTZ, "
    "root_cause_text VARCHAR)",
)


@dataclasses.dataclass(frozen=True)
class Rec:
    """One core record; ``short`` is ignored for problems (``body`` is ``root_cause_text``)."""

    entity: str
    record_id: str
    short: str | None
    body: str | None
    updated_at: datetime = T0

    def touched(self, *, short: str | None = None, body: str | None = None) -> Rec:
        """The record edited one hour later."""
        return dataclasses.replace(
            self,
            short=self.short if short is None else short,
            body=self.body if body is None else body,
            updated_at=self.updated_at + timedelta(hours=1),
        )


def create_warehouse(path: Path, records: Iterable[Rec]) -> duckdb.DuckDBPyConnection:
    """A new warehouse file at ``path`` holding ``records``; the connection stays open."""
    wh = duckdb.connect(str(path))
    wh.execute(SETTINGS_SQL.read_text("utf-8"))
    for ddl in _CORE_DDL:
        wh.execute(ddl)
    for rec in records:
        if rec.entity == "problem":
            wh.execute(
                "INSERT INTO core.problem VALUES (?, ?, ?)",
                [rec.record_id, rec.updated_at, rec.body],
            )
        else:
            wh.execute(
                f"INSERT INTO core.{rec.entity} VALUES (?, ?, ?, ?)",  # noqa: S608 - test names
                [rec.record_id, rec.updated_at, rec.short, rec.body],
            )
    return wh


def stored(wh: duckdb.DuckDBPyConnection) -> dict[str, tuple[str, str, str]]:
    """``record_id -> (entity, text, content_hash)`` of ``enrich.text_redacted``."""
    rows = wh.execute(
        "SELECT record_id, entity, text, content_hash FROM enrich.text_redacted"
    ).fetchall()
    return {rid: (entity, text, digest) for rid, entity, text, digest in rows}
