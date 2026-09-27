"""Shared helpers for the MemoryWriter tests (T07-08): writer factory, seeds, proposals.

Not a test module. The writer is built on the real collaborators: `Redactor` with a name
directory holding PLANTED_NAME, `InjectionScanner` over the shipped injection patterns,
`Embedder` around a recording fake embed function and a `VectorIndex` on LanceDB in tmp.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from pydantic import JsonValue

from herness.core import time as clock
from herness.core.ids import new_ulid
from herness.core.redact import Redactor
from herness.core.redact_directory import NameDirectory
from herness.core.settings import RedactionConfig
from herness.core.types import MemoryItem, MemoryProposal, NumberRef, Provenance, RecallHit
from herness.harness.memory.policy import InjectionScanner
from herness.harness.memory.settings import MemoryConfig, parse_injection_patterns
from herness.harness.memory.store import Embedder, VectorIndex
from herness.harness.memory.write import MemoryWriter
from herness.store.ops import chat, core
from herness.store.ops import memory as ops
from herness.store.vectors import EMBEDDING_DIM, VectorStore

ROOT = Path(__file__).resolve().parents[4]
NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)
RUN_ID = "run_" + new_ulid()
TASK_ID = "task_" + new_ulid()
AUTHOR = "a" * 32
PLANTED_NAME = "Jane Doakes"
PLANTED_EMAIL = "jane.doakes@example.com"
PATTERNS = parse_injection_patterns(
    (ROOT / "config" / "injection_patterns.txt").read_text(encoding="utf-8")
)


def unit(i: int) -> np.ndarray:
    """Basis vector e_i."""
    vec = np.zeros(EMBEDDING_DIM, dtype=np.float64)
    vec[i] = 1.0
    return vec


def at_cosine(cos: float, base: int = 0, other: int = 1) -> np.ndarray:
    """A unit vector with cosine `cos` to e_base."""
    return cos * unit(base) + (1 - cos**2) ** 0.5 * unit(other)


@dataclass
class FakeEmbed:
    """Deterministic hash-seeded embed_fn that records every text; `fail` raises."""

    overrides: dict[str, np.ndarray] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    fail: bool = False

    def __call__(self, text: str) -> np.ndarray:
        self.calls.append(text)
        if self.fail:
            msg = "embedding server down"
            raise RuntimeError(msg)
        if text in self.overrides:
            return self.overrides[text]
        seed = int.from_bytes(text.encode("utf-8")[-8:].ljust(8, b"\0"), "little") + len(text)
        return np.random.default_rng(seed).normal(size=EMBEDDING_DIM)


@dataclass
class Env:
    """A writer with handles on its fakes."""

    writer: MemoryWriter
    embed: FakeEmbed
    vectors: VectorIndex
    redactor: Redactor


def make_writer(
    tmp_path: Path,
    *,
    cfg: MemoryConfig | None = None,
    vectors: VectorIndex | None = None,
    embed: FakeEmbed | None = None,
    conn_factory: Callable[[], Any] = core.connection,
) -> Env:
    """MemoryWriter on the migrated `ops_store` of the test and LanceDB in `tmp_path`."""
    cfg = cfg or MemoryConfig(injection_patterns=PATTERNS)
    directory = NameDirectory.from_files(None, (PLANTED_NAME,), None)
    redactor = Redactor(RedactionConfig(directory_file=None), bytes(range(32)), directory)
    fake = embed or FakeEmbed()
    index = vectors or VectorIndex(lambda: VectorStore(tmp_path / "vectors"))
    writer = MemoryWriter(
        cfg,
        redactor=redactor,
        scanner=InjectionScanner(cfg.injection_patterns),
        allowed_patterns=(),
        conn_factory=conn_factory,
        vectors=index,
        embedder=Embedder(fake, model_name="bge-m3"),
    )
    return Env(writer, fake, index, redactor)


def execute(sql: str, params: tuple[object, ...] = ()) -> None:
    """One statement in its own write transaction."""
    core.run_write(lambda c: c.execute(sql, params), op="test_seed")


def seed_evidence(query_id: str = "q_" + "a" * 16) -> str:
    """An `evidence` row, so the query id resolves."""
    execute(
        "INSERT INTO evidence (query_id, build_id, sql, result_hash, params, row_count,"
        " duration_ms, executed_at) VALUES (?, 'b1', 'SELECT 1', 'h', '{}', 1, 1, ?)",
        (query_id, "2026-08-01T00:00:00.000000Z"),
    )
    return query_id


def seed_finding(status: str = "verified", confidence: float = 0.7) -> str:
    """A `finding` row of RUN_ID with the given status and confidence."""
    finding_id = "fnd_" + new_ulid()
    execute(
        "INSERT INTO finding (finding_id, run_id, task_id, author_role, claim, query_ids,"
        " confidence, status, created_at) VALUES (?, ?, ?, 'analyst', 'c', '[]', ?, ?, ?)",
        (finding_id, RUN_ID, TASK_ID, confidence, status, "2026-08-01T00:00:00.000000Z"),
    )
    return finding_id


def seed_session(user_ref: str = AUTHOR) -> tuple[str, str]:
    """A chat session of `user_ref` with one user message: (session_id, message_id)."""
    session_id = chat.create_chat_session(user_ref, now=NOW)
    message_id = chat.append_chat_message(session_id, "user", "the metric is wrong", now=NOW)
    assert message_id is not None
    return session_id, message_id


def provenance(author_type: str = "human", **fields: Any) -> Provenance:
    """Provenance with defaults per author type (human: cli + AUTHOR; agent: tool + run)."""
    base: dict[str, Any] = {"author_role": None, "author_ref": None, "run_id": None,
                            "task_id": None, "via": "cli"}  # fmt: skip
    if author_type == "human":
        base["author_ref"] = AUTHOR
    elif author_type == "agent":
        base |= {"author_role": "analyst", "run_id": RUN_ID, "task_id": TASK_ID, "via": "tool"}
    else:
        base |= {"run_id": RUN_ID, "via": "pipeline"}
    return Provenance(author_type=author_type, **(base | fields))  # type: ignore[arg-type]


KIND_DATA: dict[str, dict[str, JsonValue]] = {
    "glossary": {"term": "churn", "definition": "customers who left"},
    "business_rule": {"rule_id": "br_x", "applies_to": ["service"]},
    "insight": {"finding_ids": [], "valid_from": None, "valid_to": None},
    "user_correction": {"statement": "use the other metric", "effective_date": None,
                        "suggested_action": "none"},
    "analysis_recipe": {"steps": ["look"], "template_ids": []},
    "run_summary": {"run_kind": "org_review", "question": None, "top_finding_ids": [],
                    "rec_ids": [], "dead_task_count": 0},
    "outcome_summary": {"rec_id": "rec_x", "outcome_id": "out_x", "measurement": 1,
                        "verdict": "paid_off", "metric": "mttr", "baseline": None,
                        "actual": None, "delta": None, "rel": None, "query_id": "q_x"},
    "decision_note": {"rec_id": "rec_x", "decision": "accepted"},
    "mapping": {"review_item_id": "rev_x", "service_id": "svc_x", "team_id": "team_x"},
    "sql_template": {"fingerprint": "f" * 16, "sql_template": "SELECT 1", "params": [],
                     "question_examples": [], "passes": 1, "fails": 0, "run_ids": [],
                     "build_id_last_ok": "b", "metrics_used": []},
    "qa_pair": {"question": "q", "sql": "SELECT 1", "query_id": "q_x", "template_id": "t"},
}  # fmt: skip
LAYER = {
    "glossary": "semantic", "business_rule": "semantic", "insight": "semantic",
    "user_correction": "semantic", "mapping": "semantic", "analysis_recipe": "procedural",
    "sql_template": "procedural", "qa_pair": "procedural", "run_summary": "episodic",
    "outcome_summary": "episodic", "decision_note": "episodic",
}  # fmt: skip


def proposal(  # noqa: PLR0913 - one keyword per MemoryProposal field under test
    content: str,
    prov: Provenance | None = None,
    *,
    kind: str = "glossary",
    data: dict[str, JsonValue] | None = None,
    numbers: list[NumberRef] | None = None,
    confidence: float = 0.8,
    layer: str | None = None,
) -> MemoryProposal:
    """A MemoryProposal (default: human glossary via cli, which the matrix makes active)."""
    return MemoryProposal(
        layer=layer or LAYER[kind],  # type: ignore[arg-type]
        kind=kind,  # type: ignore[arg-type]
        content=content,
        data=dict(KIND_DATA[kind]) if data is None else data,
        numbers=numbers or [],
        confidence=confidence,
        provenance=prov or provenance(),
    )


def memory_rows() -> list[dict[str, Any]]:
    """Every memory_item row as a dict, data and provenance parsed."""
    rows = core.read_all("SELECT * FROM memory_item ORDER BY created_at, memory_id")
    out = []
    for row in rows:
        item = dict(row)
        for name in ("data", "provenance"):
            item[name] = core.load_json(item[name], field=name)
        out.append(item)
    return out


def to_item(row: dict[str, Any]) -> MemoryItem:
    """Hydrate one `memory_rows()` row as a MemoryItem."""

    def ts(value: str | None) -> datetime | None:
        return None if value is None else clock.parse_utc(value)

    return MemoryItem(
        **{k: row[k] for k in ("memory_id", "layer", "kind", "content", "data", "confidence",
                                "status", "use_count")},
        provenance=Provenance.model_validate(row["provenance"]),
        created_at=clock.parse_utc(row["created_at"]),
        expires_at=ts(row["expires_at"]),
        last_used_at=ts(row["last_used_at"]),
    )  # fmt: skip


def hit(row: dict[str, Any]) -> RecallHit:
    """A RecallHit around the hydrated row (score 0.5)."""
    item = to_item(row)
    comps: dict[Any, float] = {"sim": 0, "kw": 0, "ent": 0, "rec": 0, "conf": 0, "final": 0.5}
    return RecallHit(
        item=item, score=0.5, components=comps, unconfirmed=item.status == "pending_approval"
    )


def insert_rows(
    n: int, prov: dict[str, Any], *, kind: str = "glossary", created_at: datetime = NOW
) -> None:
    """`n` stored proposals with the given provenance JSON (for the rate-limit counts)."""
    for i in range(n):
        ops.insert_memory_item({
            "memory_id": "mem_" + new_ulid(), "layer": LAYER[kind], "kind": kind,
            "content": f"seed {i}", "data": {}, "provenance": prov, "confidence": 0.5,
            "status": "pending_approval", "created_at": clock.format_utc(created_at),
            "expires_at": None, "last_used_at": None, "use_count": 0,
        })  # fmt: skip


def review_rows() -> list[dict[str, Any]]:
    """Every review_item row with its payload parsed."""
    rows = core.read_all("SELECT * FROM review_item ORDER BY created_at, item_id")
    return [{**dict(r), "payload": core.load_json(r["payload"], field="payload")} for r in rows]
