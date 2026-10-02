"""Helpers for the purge tests (impl 03 T03-34; unit, security, integration).

`PurgeEnv` loads a full config whose `paths.data` is `<tmp>/data`, so `purge_record` finds
the vectors (T02-08 default layout), the `CURRENT` warehouse (T02-09), the decision cache,
the label parts and the pair index under one data root, exactly as in production.
"""

from __future__ import annotations

import dataclasses
import datetime
from collections.abc import Iterable
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
from tests.support.config_tree import write_full_config
from tests.unit.enrich._embed_support import SETTINGS_SQL, unit_vector

from herness.core import config
from herness.enrich.cache import CACHE_SCHEMA, DecisionCache
from herness.enrich.labels import GOLD_SCHEMA, HUMAN_SCHEMA, TEACHER_SCHEMA, LabelStore
from herness.enrich.layout import EnrichPaths
from herness.store import _warehouse_rw
from herness.store.vectors import TICKET_EMBEDDING_SCHEMA, VectorStore
from herness.store.warehouse import build_path

QSV = "qs-2026-10-01.1"
BUILD = "20261001-120000-ABCDEF"
T0 = datetime.datetime(2026, 10, 1, tzinfo=datetime.UTC)
FP = "aaaaaaaaaaaaaaaa"
OJ_V = "openjev-0.4.0"
INC1, INC2, INC3 = (f"servicenow:incident:INC{n}" for n in (1, 2, 3))
CHG1 = "servicenow:change:CHG1"
H1, H2, H3, HP, HX = ("1" * 32, "2" * 32, "3" * 32, "a" * 32, "f" * 32)
PAIR_INDEX = pa.schema(
    [("incident_id", pa.string()), ("change_id", pa.string()), ("content_hash", pa.string())]
)


@dataclasses.dataclass
class PurgeEnv:
    """Every store `purge_record` touches, under `<tmp>/data`."""

    root: Path

    @property
    def paths(self) -> EnrichPaths:
        return EnrichPaths.from_config(config.get_config())

    def store(self) -> VectorStore:
        """A freshly opened handle on the vector store (re-reads see committed deletes)."""
        store = VectorStore(self.paths.vectors_dir())
        store.ensure_tables()
        return store

    def add_vectors(self, rows: Iterable[tuple[str, str]]) -> None:
        """`(record_id, content_hash)` rows in `ticket_embedding`."""
        data = [
            {"record_id": rid, "entity": rid.split(":")[1], "service_id": None,
             "opened_at": None, "content_hash": h, "model": "test/fake@tiny",
             "vector": unit_vector(rid + h).tolist()}
            for rid, h in rows
        ]  # fmt: skip
        self.store().table("ticket_embedding").add(
            pa.Table.from_pylist(data, TICKET_EMBEDDING_SCHEMA)
        )

    def vector_ids(self) -> list[tuple[str, str]]:
        rows = self.store().table("ticket_embedding").to_arrow().to_pylist()
        return sorted((str(r["record_id"]), str(r["content_hash"])) for r in rows)

    def warehouse(self, rows: Iterable[tuple[str, str]]) -> None:
        """A promoted `CURRENT` build whose `enrich.text_redacted` has `(record_id, hash)`."""
        target = build_path(BUILD)
        target.parent.mkdir(parents=True, exist_ok=True)
        with duckdb.connect(str(target)) as wh:
            wh.execute(SETTINGS_SQL.read_text("utf-8"))
            for rid, h in rows:
                wh.execute(
                    "INSERT INTO enrich.text_redacted VALUES (?, ?, ?, ?)",
                    [rid, rid.split(":")[1], "redacted text", h],
                )
        _warehouse_rw.write_current(BUILD)

    def cache(self, hashes: Iterable[str]) -> None:
        """One openjev cache part with one row per hash."""
        rows = [
            {"content_hash": h, "question": "q_a", "question_fingerprint": FP,
             "answer": "true", "probability": 0.8, "distribution": [("true", 0.8)],
             "backend_confidence": None, "samples": 1, "decided_at": T0}
            for h in hashes
        ]  # fmt: skip
        partition = self.paths.cache_partition(QSV, "openjev", OJ_V)
        partition.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.Table.from_pylist(rows, CACHE_SCHEMA), partition / "part-01.parquet")

    def cache_hashes(self) -> list[str]:
        dataset = DecisionCache(self.paths, QSV).dataset()
        if dataset is None:
            return []
        return sorted(dataset.to_table(columns=["content_hash"]).column(0).to_pylist())

    def labels(self, kind: str, rows: Iterable[tuple[str, str, str]]) -> None:
        """Label rows `(record_id, content_hash, question)` of `kind` (incl. gold_reviews)."""
        schema = {"teacher": TEACHER_SCHEMA, "gold": GOLD_SCHEMA}.get(kind, HUMAN_SCHEMA)
        base = {"question_fingerprint": FP, "answer": "true", "labeled_by": "u1",
                "labeled_at": T0, "fold": 1, "adjudicated": False,
                "distribution": [("true", 0.9)], "decider": "openjev",
                "decider_version": OJ_V, "round": 1, "stratum": "uniform",
                "purpose": "train"}  # fmt: skip
        data = [
            {**base, "record_id": rid, "content_hash": h, "question": q, "item_id": f"it_{i}"}
            for i, (rid, h, q) in enumerate(rows)
        ]
        table = pa.Table.from_pylist([{n: r[n] for n in schema.names} for r in data], schema)
        LabelStore(self.paths, QSV).append(kind, table)  # type: ignore[arg-type]

    def label_rows(self, kind: str) -> list[tuple[str, str]]:
        table = LabelStore(self.paths, QSV).read(kind)  # type: ignore[arg-type]
        return sorted(
            zip(*(table.column(n).to_pylist() for n in ("record_id", "content_hash")), strict=True)
        )

    def pairs(self, rows: Iterable[tuple[str, str, str]], build: str = BUILD) -> Path:
        """A pair index part `(incident_id, change_id, content_hash)` as U03-109 writes it."""
        target = self.paths.pairs_dir() / f"part-{build}.parquet"
        target.parent.mkdir(parents=True, exist_ok=True)
        table = pa.Table.from_pylist(
            [dict(zip(PAIR_INDEX.names, row, strict=True)) for row in rows], PAIR_INDEX
        )
        pq.write_table(table, target)
        return target

    def pair_rows(self) -> list[tuple[str, str, str]]:
        out: list[tuple[str, str, str]] = []
        for part in sorted(self.paths.pairs_dir().glob("part-*.parquet")):
            table = pq.read_table(part)
            out += zip(*(table.column(n).to_pylist() for n in PAIR_INDEX.names), strict=True)
        return sorted(out)


def purge_env(tmp_path: Path) -> PurgeEnv:
    """Load a full config with `paths.data = <tmp_path>/data` and return the environment."""
    config.reset_config()
    config.init_config("local", config_dir=write_full_config(tmp_path), env={})
    return PurgeEnv(tmp_path)
