"""Unit tests for the batched post-commit vector upsert of `promote_procedural` (T07-28)."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest
from structlog.testing import capture_logs
from tests.support.ops_store import OpsStoreHandle
from tests.unit.harness.memory._procedural_env import LOW, incidents_sql, make_deps, rows, run_with
from tests.unit.harness.memory._write_env import NOW, memory_rows

from herness.core.errors import ModelUnavailable
from herness.harness.memory.procedural import promote_procedural
from herness.harness.memory.store import VectorRow

pytestmark = pytest.mark.unit

SHAPES = 3  # three distinct fingerprints: one template and one qa_pair each


def _shapes_run() -> str:
    sqls = [incidents_sql().replace("AS n", f"AS n{i}") for i in range(SHAPES)]
    return run_with([(sql, True) for sql in sqls])


def test_ut07_78_one_vector_upsert_per_promotion_run(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-78 (T07-28) n new items over several fingerprints: exactly one upsert with all n."""
    pe = make_deps(tmp_path, LOW)
    calls: list[list[VectorRow]] = []
    real = pe.env.vectors.upsert

    def recording(batch: Sequence[VectorRow]) -> None:
        calls.append(list(batch))
        real(batch)

    monkeypatch.setattr(pe.env.vectors, "upsert", recording)
    report = promote_procedural(_shapes_run(), deps=pe.deps, now=NOW)
    assert report.templates_created == SHAPES
    new_ids = {r["memory_id"] for r in memory_rows()}
    assert len(new_ids) == 2 * SHAPES
    assert len(calls) == 1
    assert {v.memory_id for v in calls[0]} == new_ids
    assert all(not r["data"]["embedding_pending"] for r in memory_rows())
    stored = {m.memory_id: m.status for m in pe.env.vectors.list_ids("", 100)}
    assert stored == {r["memory_id"]: r["status"] for r in memory_rows()}


def test_ut07_78_failed_batch_upsert_is_logged_not_raised(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-78 (T07-28) a raising upsert: `memory.vector.sync_failed` op=promote, no raise,
    SQLite rows stand with `embedding_pending` still set (maintenance re-embeds, U07-57)."""
    pe = make_deps(tmp_path, LOW)

    def down(_rows: Sequence[VectorRow]) -> None:
        msg = "down"
        raise ModelUnavailable(msg)

    monkeypatch.setattr(pe.env.vectors, "upsert", down)
    with capture_logs() as logs:
        report = promote_procedural(_shapes_run(), deps=pe.deps, now=NOW)
    assert report.templates_created == SHAPES
    failed = [e for e in logs if e["event"] == "memory.vector.sync_failed"]
    assert [(e["op"], e["count"]) for e in failed] == [("promote", 2 * SHAPES)]
    assert len(rows("sql_template")) == SHAPES
    assert all(r["data"]["embedding_pending"] for r in memory_rows())


def test_ut07_78_embed_failure_of_one_item_skips_only_it(
    ops_store: OpsStoreHandle, tmp_path: Path
) -> None:
    """UT07-78 (T07-28) an embedding failure for one item leaves only that item pending."""
    pe = make_deps(tmp_path, LOW)
    run_id = _shapes_run()
    embedder = pe.deps.writer._embedder
    original, seen = embedder.embed_item, []

    def flaky(kind: str, text: str) -> object:
        seen.append(text)
        if len(seen) == 1:
            msg = "embed down"
            raise ModelUnavailable(msg)
        return original(kind, text)  # type: ignore[arg-type]

    embedder.embed_item = flaky  # type: ignore[method-assign,assignment]
    promote_procedural(run_id, deps=pe.deps, now=NOW)
    pending = [r for r in memory_rows() if r["data"]["embedding_pending"]]
    assert len(pending) == 1
    assert len(pe.env.vectors.list_ids("", 100)) == 2 * SHAPES - 1
