"""Helpers for the embed stage tests (impl 03 T03-07; unit, integration, security, fault).

The core DDL (spec 02 SQL 100-299) needs the whole build, so `embed_warehouse` creates
minimal `core.incident`, `core.change` and `core.problem` tables with the columns the stage
reads, plus the real `herness/model/sql/000_settings.sql` (`enrich.text_redacted`), whose
rows are written directly (as the text stage T03-05 would, with `content_hash`).
"""

from __future__ import annotations

import dataclasses
import hashlib
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import duckdb
import numpy as np
import pyarrow as pa
import pytest

from herness.enrich import embed_stage
from herness.enrich.text import content_hash
from herness.store.vectors import EMBEDDING_DIM, TICKET_EMBEDDING_SCHEMA, VectorStore

SETTINGS_SQL = Path(__file__).resolve().parents[3] / "herness/model/sql/000_settings.sql"
T0 = datetime(2026, 9, 1, tzinfo=UTC)
MODEL_ID = "test/fake@tiny"

_CORE_DDL = (
    "CREATE TABLE core.incident (record_id VARCHAR, service_id VARCHAR, opened_at TIMESTAMPTZ)",
    "CREATE TABLE core.change (record_id VARCHAR, service_id VARCHAR, opened_at TIMESTAMPTZ, "
    "planned_start TIMESTAMPTZ, actual_start TIMESTAMPTZ)",
    "CREATE TABLE core.problem (record_id VARCHAR, service_id VARCHAR, opened_at TIMESTAMPTZ)",
)


@dataclasses.dataclass(frozen=True)
class Rec:
    """One core record and its redacted text (None: no `enrich.text_redacted` row)."""

    entity: str
    number: str
    text: str | None
    service_id: str | None = "servicenow:cmdb_ci:SVC1"
    opened_at: datetime | None = T0
    planned_start: datetime | None = None

    @property
    def record_id(self) -> str:
        return f"servicenow:{self.entity}:{self.number}"


@dataclasses.dataclass
class Report:
    """Local stand-in for StageReport (U03-142, T03-28 not built yet)."""

    embedded: int = 0
    cache_hits: int = 0
    rows: int = 0


def embed_warehouse(path: Path, records: Iterable[Rec]) -> duckdb.DuckDBPyConnection:
    """A new warehouse file at `path` holding `records`; the connection stays open."""
    wh = duckdb.connect(str(path))
    wh.execute(SETTINGS_SQL.read_text("utf-8"))
    for ddl in _CORE_DDL:
        wh.execute(ddl)
    for rec in records:
        add_record(wh, rec)
    return wh


def add_record(wh: duckdb.DuckDBPyConnection, rec: Rec) -> None:
    """Insert `rec` into its `core.*` table and, with text, into `enrich.text_redacted`."""
    values: list[object] = [rec.record_id, rec.service_id, rec.opened_at]
    if rec.entity == "change":
        values += [rec.planned_start, None]
    marks = ", ".join("?" * len(values))
    wh.execute(f"INSERT INTO core.{rec.entity} VALUES ({marks})", values)  # noqa: S608 - test
    if rec.text is not None:
        wh.execute(
            "INSERT INTO enrich.text_redacted VALUES (?, ?, ?, ?)",
            [rec.record_id, rec.entity, rec.text, content_hash(rec.text)],
        )


def unit_vector(text: str) -> np.ndarray:
    """A deterministic unit float32 vector for `text`."""
    seed = int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)
    vec = np.random.default_rng(seed).standard_normal(EMBEDDING_DIM).astype(np.float32)
    return vec / np.linalg.norm(vec)


class FakeEncoder:
    """Stands in for `Encoder`: records every input text; one vector per text."""

    def __init__(self, model_id: str = MODEL_ID, *, width: int = EMBEDDING_DIM) -> None:
        self.model_id = model_id
        self.width = width
        self.calls: list[list[str]] = []

    @property
    def inputs(self) -> list[str]:
        return [text for call in self.calls for text in call]

    def encode(self, texts: Sequence[str], *, batch_size: int) -> np.ndarray:
        assert batch_size == len(texts)
        self.calls.append(list(texts))
        rows = np.stack([unit_vector(t) for t in texts]) if texts else np.zeros((0, 1024))
        return rows[:, : self.width].astype(np.float32)


def use_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> VectorStore:
    """A LanceDB store under `tmp_path` with both tables; the stage opens this one."""
    path = tmp_path / "vectors"
    store = VectorStore(path)
    store.ensure_tables()
    monkeypatch.setattr(embed_stage, "VectorStore", lambda: VectorStore(path))
    return store


def use_batch_size(monkeypatch: pytest.MonkeyPatch, size: int) -> None:
    """Make the stage read `embedding.batch_size = size` without loading a config."""
    cfg = SimpleNamespace(decisions=SimpleNamespace(embedding=SimpleNamespace(batch_size=size)))
    monkeypatch.setattr(embed_stage, "get_config", lambda: cfg)


def add_rows(store: VectorStore, rows: Sequence[tuple[str, str, str]]) -> None:
    """Add `(record_id, text, model)` rows to `ticket_embedding` (hash and vector of text)."""
    table = pa.Table.from_pylist(
        [
            {
                "record_id": record_id,
                "entity": record_id.split(":")[1] if record_id.count(":") == 2 else "incident",
                "service_id": None,
                "opened_at": None,
                "content_hash": content_hash(text),
                "model": model,
                "vector": unit_vector(text).tolist(),
            }
            for record_id, text, model in rows
        ],
        TICKET_EMBEDDING_SCHEMA,
    )
    store.table("ticket_embedding").add(table)


def stored(store: VectorStore) -> dict[str, dict[str, object]]:
    """`record_id -> row` of `ticket_embedding` (vector as a numpy array)."""
    rows = store.table("ticket_embedding").to_arrow().to_pylist()
    out: dict[str, dict[str, object]] = {}
    for row in rows:
        row["vector"] = np.asarray(row["vector"], dtype=np.float32)
        out[str(row["record_id"])] = row
    return out
