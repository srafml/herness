"""Helpers for the cluster stage tests (impl 03 T03-25; unit, security and integration).

`small_build` and the spec 11 T2/T2c planted-cluster truths are not in the tree yet: a
hand-built DuckDB warehouse (`core.incident` + the real `enrich.*` DDL), a LanceDB store
under `tmp_path` holding synthetic unit vectors, and a seeded planted-cluster generator
stand in for them. Test code only (R-64): nothing here is imported by `herness/`.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import duckdb
import numpy as np
import pyarrow as pa
import pytest
from tests.support.build_harness import FakeJobContext

from herness.core import time as clock
from herness.core.types import Question, QuestionSet
from herness.enrich import _cluster_full, cluster_stage
from herness.enrich.cluster import PcaModel
from herness.enrich.cluster_describe import name_clusters
from herness.enrich.cluster_stage import (
    ClusterSnapshot,
    ClusterStageResult,
    SnapshotMeta,
    finalize_clusters,
    run_cluster_stage,
)
from herness.enrich.layout import EnrichPaths
from herness.enrich.settings import ClusteringSettings
from herness.enrich.text import content_hash
from herness.store.vectors import EMBEDDING_DIM, TICKET_EMBEDDING_SCHEMA, VectorStore

SETTINGS_SQL = Path(__file__).resolve().parents[3] / "herness/model/sql/000_settings.sql"
T_NOW = datetime(2026, 9, 20, 3, 0, tzinfo=UTC)
MODEL_ID = "test/fake@tiny"
_WORDS = (
    "disk", "network", "login", "printer", "vpn", "backup", "queue", "email", "database",
    "certificate", "firewall", "storage", "payroll", "badge", "wifi", "license",
)  # fmt: skip


@dataclasses.dataclass(frozen=True)
class Inc:
    """One incident: its redacted text and its stored embedding."""

    number: str
    text: str
    vector: np.ndarray
    service_id: str | None = "servicenow:cmdb_ci:SVC1"
    opened_at: datetime = T_NOW - timedelta(days=3)

    @property
    def record_id(self) -> str:
        return f"servicenow:incident:{self.number}"


@dataclasses.dataclass
class Report:
    """Local stand-in for StageReport (U03-142, T03-28 not built yet)."""

    rows: int = 0


def unit(rows: np.ndarray) -> np.ndarray:
    out: np.ndarray = (rows / np.linalg.norm(rows, axis=-1, keepdims=True)).astype(np.float32)
    return out


def planted(
    n_clusters: int, per: int, *, noise: int, seed: int, spread: float = 0.6
) -> tuple[np.ndarray, np.ndarray]:
    """Seeded planted clusters in 1024-d: `per` points around each of `n_clusters` random
    unit centres (per-dimension Gaussian noise of `spread / sqrt(1024)`) plus `noise`
    uniform points labelled -1. Returns (unit float32 vectors, truth labels)."""
    rng = np.random.default_rng(seed)
    centres = unit(rng.standard_normal((n_clusters, EMBEDDING_DIM)))
    scale = spread / np.sqrt(EMBEDDING_DIM)
    parts = [c + scale * rng.standard_normal((per, EMBEDDING_DIM)) for c in centres]
    parts.append(rng.standard_normal((noise, EMBEDDING_DIM)))
    truth = np.concatenate([np.repeat(np.arange(n_clusters), per), np.full(noise, -1)])
    return unit(np.concatenate(parts)), truth


def planted_incidents(
    vectors: np.ndarray, truth: np.ndarray, *, prefix: str = "INC", start: int = 0
) -> list[Inc]:
    """Incidents for planted vectors; texts share their cluster's vocabulary (c-TF-IDF)."""
    out = []
    for i, (vec, label) in enumerate(zip(vectors, truth, strict=True)):
        n = start + i
        if label >= 0:
            w = _WORDS[int(label) % len(_WORDS)]
            text = f"{w} {w}s outage {w} failure number {n}"
            service = f"servicenow:cmdb_ci:SVC{int(label) % 3}"
        else:
            text, service = f"misc question {n}", "servicenow:cmdb_ci:SVC9"
        out.append(Inc(f"{prefix}{n:05d}", text, vec, service_id=service))
    return out


def near(inc: Inc, number: str, text: str, seed: int, scale: float = 0.01) -> Inc:
    """A new incident (or an edit of `inc` when `number` is its own) close to `inc`."""
    noise = np.random.default_rng(seed).standard_normal(inc.vector.shape) * scale
    return Inc(number, text, unit(inc.vector + noise), service_id=inc.service_id)


def cluster_warehouse(path: Path, incidents: Iterable[Inc]) -> duckdb.DuckDBPyConnection:
    """A new warehouse file at `path` holding `incidents` (core + enrich.text_redacted)."""
    wh = duckdb.connect(str(path))
    wh.execute(SETTINGS_SQL.read_text("utf-8"))
    wh.execute(
        "CREATE TABLE core.incident (record_id VARCHAR, service_id VARCHAR, opened_at TIMESTAMPTZ)"
    )
    for inc in incidents:
        wh.execute(
            "INSERT INTO core.incident VALUES (?, ?, ?)",
            [inc.record_id, inc.service_id, inc.opened_at],
        )
        wh.execute(
            "INSERT INTO enrich.text_redacted VALUES (?, 'incident', ?, ?)",
            [inc.record_id, inc.text, content_hash(inc.text)],
        )
    return wh


def enrich_paths(tmp_path: Path) -> EnrichPaths:
    return EnrichPaths(
        data_root=tmp_path / "data",
        embedding_path="data/models/embedding",
        laya_current_file="data/models/laya/CURRENT",
    )


def vector_store(tmp_path: Path) -> VectorStore:
    """The LanceDB store the stage opens (`EnrichPaths.vectors_dir()`), tables created."""
    store = VectorStore(enrich_paths(tmp_path).vectors_dir())
    store.ensure_tables()
    return store


def store_vectors(store: VectorStore, incidents: Sequence[Inc]) -> None:
    """Upsert the incidents' vectors by `record_id`, as the embed stage does."""
    rows = pa.Table.from_pylist(
        [
            {
                "record_id": inc.record_id,
                "entity": "incident",
                "service_id": inc.service_id,
                "opened_at": inc.opened_at,
                "content_hash": content_hash(inc.text),
                "model": MODEL_ID,
                "vector": inc.vector.astype(np.float32).tolist(),
            }
            for inc in incidents
        ],
        TICKET_EMBEDDING_SCHEMA,
    )
    builder = store.table("ticket_embedding").merge_insert("record_id")
    builder.when_matched_update_all().when_not_matched_insert_all().execute(rows)


def settings(**overrides: Any) -> SimpleNamespace:
    """A `DecisionsConfig` stand-in holding only `clustering` (small-data values)."""
    values: dict[str, Any] = {
        "pca_dims": 16,
        "pca_sample": 1000,
        "proto_per": 4,
        "k_min": 10,
        "k_max": 400,
        "iters": 10,
        "min_cluster_size": 3,
        "min_samples": 2,
        "assign_min_sim": 0.5,
        "full_sim": 0.8,
        "min_incidents": 10,
    }
    values.update(overrides)
    return SimpleNamespace(clustering=ClusteringSettings(**values))


def freeze_now(monkeypatch: pytest.MonkeyPatch, now: datetime = T_NOW) -> None:
    monkeypatch.setattr(clock, "now", lambda: now)


def fixed_ids(monkeypatch: pytest.MonkeyPatch, prefix: str = "cl_T") -> list[str]:
    """Deterministic new cluster ids `<prefix>0000`, ...; returns the ids handed out."""
    issued: list[str] = []

    def new_id() -> str:
        issued.append(f"{prefix}{len(issued):04d}")
        return issued[-1]

    monkeypatch.setattr(_cluster_full, "_new_cluster_id", new_id)
    return issued


def run_stage(
    wh: duckdb.DuckDBPyConnection, paths: EnrichPaths, build_id: str, **kw: Any
) -> tuple[ClusterStageResult, Report]:
    """`run_cluster_stage` with test defaults (cpu, no previous warehouse, not forced)."""
    report = Report()
    args: dict[str, Any] = {
        "prev_warehouse": None, "cfg": settings(), "force_full": False, "device": "cpu",
        "ctx": FakeJobContext(), **kw,
    }  # fmt: skip
    result = run_cluster_stage(wh, paths=paths, build_id=build_id, report=report, **args)
    return result, report


def question_set(*, root_cause: bool = True) -> QuestionSet:
    base = {"instructions": "Classify this ticket with care, please.", "threshold": 0.7}
    questions = [Question.model_validate({**base, "id": "q_bool", "type": "bool"})]
    if root_cause:
        opts = {"hardware": "Hardware.", "network": "Network.", "software": "Software."}
        questions.append(
            Question.model_validate({**base, "id": "root_cause", "type": "choice", "options": opts})
        )
    return QuestionSet(version="qs-2026-09-01", questions=tuple(questions))


def finalize_auto(
    wh: duckdb.DuckDBPyConnection, result: ClusterStageResult, paths: EnrichPaths
) -> None:
    """`finalize_clusters` with auto names for every naming candidate (no LLM client)."""
    names = name_clusters(
        result.naming, client=None, root_cause_labels=None, max_calls=0, prompt_path=Path("x")
    )
    finalize_clusters(wh, result=result, names=names, qs=question_set(), paths=paths)


def members_of(wh: duckdb.DuckDBPyConnection) -> dict[str, str]:
    return dict(wh.execute("SELECT record_id, cluster_id FROM enrich.cluster_member").fetchall())


def clusters_of(wh: duckdb.DuckDBPyConnection) -> dict[str, tuple[Any, ...]]:
    rows = wh.execute(
        "SELECT cluster_id, label, root_cause_category, size FROM enrich.cluster"
    ).fetchall()
    return {r[0]: r[1:] for r in rows}


def tiny_snapshot(
    *, snapshot_id: str = "b-0001", dim: int = 32, k: int = 4, status: str = "final"
) -> ClusterSnapshot:
    """A small valid snapshot: PCA dim -> 8, k prototypes, clusters cl_A, cl_B, retired cl_R."""
    rng = np.random.default_rng(5)
    pca = PcaModel(
        components=rng.standard_normal((8, dim)).astype(np.float32),
        mean=rng.standard_normal(dim).astype(np.float32),
        fit_id="abcdef012345",
    )
    centroid = unit(rng.standard_normal((3, dim)))
    fsl = pa.list_(pa.float32(), dim)
    centroids = pa.table(
        {
            "cluster_id": ["cl_A", "cl_B", "cl_R"],
            "centroid": pa.array(centroid.tolist(), fsl),
            "size": pa.array([30, 40, 25], pa.int64()),
            "named_centroid": pa.array([centroid[0].tolist(), None, None], fsl),
            "named_size": pa.array([30, None, None], pa.int64()),
            "label": ["disk failures", None, "old"],
            "root_cause_category": ["hardware", None, None],
            "retired_at": pa.array(
                [None, None, T_NOW - timedelta(days=10)], pa.timestamp("us", tz="UTC")
            ),
        }
    )
    proto = pa.table(
        {
            "proto_idx": pa.array(range(k), pa.int32()),
            "cluster_id": pa.array(["cl_A", "cl_B", None, "cl_A"][:k], pa.string()),
            "hdbscan_prob": pa.array([1.0, 0.9, 0.0, 0.8][:k], pa.float64()),
            "weight": pa.array([10, 12, 3, 9][:k], pa.int64()),
        }
    )
    meta = SnapshotMeta(
        kind="full", status=status,  # type: ignore[arg-type]
        full_at=T_NOW - timedelta(days=1), created_at=T_NOW - timedelta(days=1), n=95, k=k,
    )  # fmt: skip
    return ClusterSnapshot(
        algorithm_version=f"{cluster_stage.ALGORITHM_BASE}-{pca.fit_id}",
        snapshot_id=snapshot_id,
        meta=meta,
        pca=pca,
        prototypes=unit(rng.standard_normal((k, 8))),
        proto_cluster=proto,
        centroids=centroids,
    )
