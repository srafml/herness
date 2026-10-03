"""Shared helpers for the LoRA export tests (T07-20): deps, seeds, fakes, output readers.

Not a test module. `make_deps` builds `LoraDeps` on the migrated `ops_store` of the test, the
real `Redactor` of `_write_env` (name directory holding PLANTED_NAME), the shipped injection
patterns, an in-memory DuckDB with the four digest schemas and a fake embedder whose
`overrides` place chosen texts at a chosen cosine to a golden question. Seeds insert
`sql_template` and `qa_pair` rows directly through the ops area.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
from tests.unit.harness.memory._write_env import NOW, PATTERNS, PLANTED_NAME, at_cosine, unit

from herness.core import time as clock
from herness.core.redact import Redactor
from herness.core.redact_directory import NameDirectory
from herness.core.settings import RedactionConfig
from herness.harness.memory.lora import LoraDeps
from herness.harness.memory.policy import InjectionScanner
from herness.harness.memory.settings import LoraConfig
from herness.store.ops import core
from herness.store.ops import memory as ops
from herness.store.vectors import EMBEDDING_DIM

__all__ = ["NOW", "PLANTED_NAME", "at_cosine", "unit"]

BUILD = "20260901-120000-ABCDEF"
CONFIG_HASH = "cfg_0123456789abcdef"
METRICS = ("mttr", "change_failure_rate")
GOLDEN = "How many incidents did the checkout service have last month?"
PARAPHRASE = "Count the incidents of checkout over the previous month"
SQL = "SELECT service_id, COUNT(*) AS n FROM core.incident GROUP BY service_id"
SCHEMA_DDL = (
    "CREATE SCHEMA core", "CREATE SCHEMA enrich", "CREATE SCHEMA metrics", "CREATE SCHEMA score",
    "CREATE TABLE core.incident (incident_id VARCHAR, service_id VARCHAR, opened_at TIMESTAMP)",
    "CREATE TABLE enrich.incident_topic (incident_id VARCHAR, topic VARCHAR)",
    "CREATE TABLE metrics.daily_incidents (service_id VARCHAR, day DATE, incidents BIGINT)",
    "CREATE TABLE score.service_score (service_id VARCHAR, score DOUBLE)",
    "CREATE TABLE main.scratch (x INTEGER)",
)  # fmt: skip


@dataclass
class FakeEmbedder:
    """Hash-seeded unit vectors; `overrides` pins texts; `fail` raises ModelUnavailable."""

    overrides: dict[str, np.ndarray] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    fail: bool = False

    def embed(self, text: str) -> np.ndarray:
        from herness.core.errors import ModelUnavailable  # noqa: PLC0415 - local to the fake

        self.calls.append(text)
        if self.fail:
            msg = "embedding model down"
            raise ModelUnavailable(msg)
        if text in self.overrides:
            return self.overrides[text]
        seed = int.from_bytes(text.encode("utf-8")[-8:].ljust(8, b"\0"), "little") + len(text)
        raw = np.random.default_rng(seed).normal(size=EMBEDDING_DIM)
        return raw / np.linalg.norm(raw)


@dataclass
class Warehouse:
    """In-memory DuckDB with the digest schemas; `ddl` adds statements; `opened` counts."""

    ddl: list[str] = field(default_factory=lambda: list(SCHEMA_DDL))
    opened: int = 0

    def __call__(self) -> duckdb.DuckDBPyConnection:
        self.opened += 1
        con = duckdb.connect(":memory:")
        for stmt in self.ddl:
            con.execute(stmt)
        return con


@dataclass
class LoraEnv:
    deps: LoraDeps
    root: Path
    embedder: FakeEmbedder
    warehouse: Warehouse


def redactor() -> Redactor:
    directory = NameDirectory.from_files(None, (PLANTED_NAME,), None)
    return Redactor(RedactionConfig(directory_file=None), bytes(range(32)), directory)


def make_deps(tmp_path: Path, cfg: LoraConfig | None = None) -> LoraEnv:
    """LoraDeps with the export root `<tmp>/data/models/lora_data` (created)."""
    root = tmp_path / "data" / "models" / "lora_data"
    root.mkdir(parents=True, exist_ok=True)
    emb = FakeEmbedder(overrides={GOLDEN: unit(0), PARAPHRASE: at_cosine(0.95)})
    wh = Warehouse()
    deps = LoraDeps(
        conn_factory=core.connection,
        embedder=emb,
        redactor=redactor(),
        scanner=InjectionScanner(PATTERNS),
        config=cfg or LoraConfig(),
        export_root=root,
        config_hash=CONFIG_HASH,
        open_warehouse=wh,
        metric_names=lambda: METRICS,
    )
    return LoraEnv(deps, root, emb, wh)


def mid(n: int) -> str:
    return f"mem_{n:026d}"


def _insert(memory_id: str, kind: str, content: str, data: dict[str, Any], **over: Any) -> str:
    row: dict[str, Any] = {
        "memory_id": memory_id, "layer": "procedural", "kind": kind, "content": content,
        "data": data, "provenance": {"author_type": "system", "via": "promotion"},
        "confidence": 0.9, "status": "active", "created_at": clock.format_utc(NOW),
        "expires_at": None, "last_used_at": None, "use_count": 0,
    }  # fmt: skip
    row |= over
    ops.insert_memory_item(row)  # type: ignore[arg-type]
    return memory_id


def seed_template(n: int, fp: str, *, passes: int = 30, fails: int = 0, **over: Any) -> str:
    """An active `sql_template` with fingerprint `fp` (Wilson lb of 30/0 is about 0.89)."""
    data = {"fingerprint": fp, "sql_template": SQL, "params": [], "question_examples": [],
            "passes": passes, "fails": fails, "run_ids": [], "build_id_last_ok": BUILD,
            "metrics_used": [], "qa_ids": []}  # fmt: skip
    return _insert(mid(n), "sql_template", f"template {fp}", data, **over)


def seed_qa(n: int, template_id: str, question: str, sql: str = SQL, **over: Any) -> str:
    """An active `qa_pair` of `template_id`."""
    data = {"question": question, "sql": sql, "query_id": f"q_{n:016d}",
            "template_id": template_id}  # fmt: skip
    return _insert(mid(n), "qa_pair", question, data, **over)


def fingerprints(count: int) -> list[str]:
    """`count` distinct 16-hex fingerprints."""
    return [f"{i:016x}" for i in range(1, count + 1)]


def export_dirs(root: Path) -> list[Path]:
    return sorted(p for p in root.iterdir())


def read_lines(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line]


def tree(root: Path) -> list[str]:
    """Every path under `root`, relative and sorted."""
    return sorted(str(Path(d, f).relative_to(root)) for d, _, fs in os.walk(root) for f in fs)


def golden(*extra: str) -> Sequence[str]:
    return (GOLDEN, *extra)
