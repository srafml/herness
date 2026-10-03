"""Tests for herness.harness.memory.maintenance (impl 07 U07-96, T07-22; UT07-84, TH07-13)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs
from tests.support.ops_store import OpsStoreHandle
from tests.unit.harness.memory._maintenance_env import (
    MODEL,
    STAMP,
    FakeCtx,
    MaintEnv,
    make_env,
    put_vector,
    row_of,
    seed_done_run,
    seed_item,
    vector_metas,
    yearly_reviews,
)
from tests.unit.harness.memory._procedural_env import incidents_sql, run_with
from tests.unit.harness.memory._write_env import NOW, memory_rows, proposal

from herness.core.errors import ConfigError, ModelUnavailable, StoreBusy
from herness.core.ids import new_ulid
from herness.core.jobs.handlers import register_handler, resolve_handler, run_handler
from herness.core.resilience import ProcessState
from herness.core.types import JobOutcome
from herness.harness.memory import _maintenance_vectors as mv
from herness.harness.memory import maintenance
from herness.store.errors import NotFoundError
from herness.store.ops import core
from herness.store.ops import memory as ops

pytestmark = pytest.mark.unit

STEPS = ["expire", "promote", "templates", "fts", "vectors", "backfill", "review"]


@pytest.fixture
def env(ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MaintEnv:
    """Configured maintenance deps on a migrated ops store; time is NOW."""
    del ops_store
    return make_env(tmp_path, monkeypatch)


def _metrics() -> dict[tuple[str, str], float]:
    rows = core.read_all("SELECT name, labels, value FROM metric_sample ORDER BY rowid")
    return {(r["name"], r["labels"]): r["value"] for r in rows}


def _spy(monkeypatch: pytest.MonkeyPatch, name: str, owner: Any = maintenance) -> list[Any]:
    """Record the calls of `owner.<name>` and pass them through."""
    real, calls = getattr(owner, name), []

    def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(owner, name, spy)
    return calls


# --- UT07-84 the full run ---------------------------------------------------------------------


def test_ut07_84_repairs_backfills_and_requests_review(env: MaintEnv) -> None:
    """UT07-84 orphan vectors, a stale status, a pending embedding and an old business rule:
    one run repairs, backfills and creates the yearly review item."""
    ok = seed_item(content="healthy item")
    put_vector(env, ok)
    stale = seed_item(content="stale status")
    put_vector(env, stale, status="rejected")
    orphan = "mem_" + new_ulid()
    put_vector(env, orphan)
    pending = seed_item(content="waiting for embedding", pending=True)
    rule = seed_item(kind="business_rule", content="rank team Y by MTTR",
                     created_at=NOW - timedelta(days=400))  # fmt: skip
    put_vector(env, rule, kind="business_rule")
    with capture_logs() as logs:
        outcome, ctx = env.run()
    assert outcome.status == "done"
    metas = vector_metas(env)
    assert orphan not in metas
    assert metas[stale].status == "active"
    assert metas[pending].model == MODEL
    assert row_of(pending)["data"]["embedding_pending"] is False
    assert "embedding_pending" not in row_of(pending)["data"]["flags"]
    (review,) = yearly_reviews()
    payload = review["payload"]
    assert payload["memory_id"] == rule
    assert payload["flags"] == ["yearly_review"]
    assert {"layer", "kind", "content", "numbers", "entities", "provenance",
            "conflicts_with"} <= set(payload)  # fmt: skip
    assert row_of(rule)["data"]["last_review_requested_at"] == STAMP
    assert row_of(rule)["status"] == "active"
    counts = outcome.result
    assert list(counts) == STEPS
    assert counts["vectors"] == {"deleted": 1, "restatused": 1, "flagged": 0,
                                 "statuses": {"active": 4}}  # fmt: skip
    assert counts["backfill"] == {"embedded": 1, "stopped": False}
    assert counts["review"] == 1
    assert [s["step"] for s in ctx.saves] == [1, 2, 3, 4, 5, 6, 7]
    done = [e for e in logs if e["event"] == "memory.maintenance.completed"]
    assert done == [{"event": "memory.maintenance.completed", "log_level": "info",
                     "component": "memory", "job_id": "job_maint", "counts": counts}]  # fmt: skip


def test_ut07_84_model_change_reembeds_every_item(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-84 a vector whose model differs from `embedder.model_name` (LLM03): the item is
    flagged in step 5 and re-embedded with the new model in step 6."""
    del ops_store
    env = make_env(tmp_path, monkeypatch, model="bge-m4")
    ids = [seed_item(content=f"item {i}") for i in range(3)]
    for memory_id in ids:
        put_vector(env, memory_id, model=MODEL)
    outcome, _ = env.run()
    assert outcome.result["vectors"]["flagged"] == 3  # type: ignore[index]
    assert outcome.result["backfill"] == {"embedded": 3, "stopped": False}
    assert {m.model for m in vector_metas(env).values()} == {"bge-m4"}
    assert all(row_of(i)["data"]["embedding_pending"] is False for i in ids)


def test_ut07_84_content_hash_mismatch_and_missing_vector_flagged(env: MaintEnv) -> None:
    """UT07-84 a vector with another content hash and an item without vector are marked
    `embedding_pending` in step 5 and backfilled in step 6 with the row's hash."""
    changed = seed_item(content="changed text")
    put_vector(env, changed, content_hash="old-hash")
    missing = seed_item(content="never embedded")
    outcome, _ = env.run()
    assert outcome.result["vectors"]["flagged"] == 2  # type: ignore[index]
    metas = vector_metas(env)
    assert metas[changed].content_hash == row_of(changed)["data"]["content_hash"]
    assert metas[missing].content_hash == row_of(missing)["data"]["content_hash"]


def test_ut07_84_step5_pages_both_streams(env: MaintEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT07-84 step 5 merges SQLite and LanceDB pages in memory_id order across page edges."""
    monkeypatch.setattr(mv, "PAGE", 2)
    ids = [seed_item(content=f"paged {i}") for i in range(5)]
    for memory_id in ids[:4]:
        put_vector(env, memory_id, status="expired")
    orphans = ["mem_" + new_ulid() for _ in range(3)]
    for memory_id in orphans:
        put_vector(env, memory_id)
    outcome, _ = env.run()
    assert outcome.result["vectors"] == {"deleted": 3, "restatused": 4, "flagged": 1,
                                         "statuses": {"active": 5}}  # fmt: skip
    assert sorted(vector_metas(env)) == sorted(ids)
    assert {m.status for m in vector_metas(env).values()} == {"active"}


def test_ut07_84_steps_1_to_4_compose_the_units(
    env: MaintEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-84 step 1 expires due items, step 2 promotes each run done in the last 7 days,
    step 3 validates on a fresh read-only connection that is closed, step 4 checks FTS."""
    due = seed_item(content="expires")
    core.run_write(lambda c: c.execute("UPDATE memory_item SET expires_at = ? WHERE memory_id = ?",
                                       ("2026-08-01T00:00:00.000000Z", due)), op="t")  # fmt: skip
    recent = run_with([(incidents_sql(), True)])
    core.run_write(lambda c: c.execute("UPDATE run SET finished_at = ? WHERE run_id = ?",
                                       (STAMP, recent)), op="t")  # fmt: skip
    seed_done_run(NOW - timedelta(days=8))
    promoted, validated = _spy(monkeypatch, "promote_procedural"), _spy(
        monkeypatch, "validate_templates")  # fmt: skip
    outcome, _ = env.run()
    assert row_of(due)["status"] == "expired"
    assert outcome.result["expire"] == 1
    assert [a[0] for a, _ in promoted] == [recent]
    assert outcome.result["promote"] == {"runs": 1, "templates_created": 1}
    ((_, kwargs),) = validated
    assert kwargs["con"] is env.opened[0]
    assert kwargs["deps"] is env.deps.procedural
    with pytest.raises(Exception, match="closed"):
        env.opened[0].execute("SELECT 1")
    assert outcome.result["templates"] == {"validated": 0, "expired": 0}
    assert outcome.result["fts"] == {"rebuilt": False}


def test_ut07_84_no_current_build_skips_step3(
    env: MaintEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-84 without a promoted CURRENT build step 3 is skipped; the job still finishes."""

    def missing() -> Any:
        msg = "no promoted warehouse build"
        raise NotFoundError(msg, kind="current", key="CURRENT")

    deps = maintenance.MaintenanceDeps(
        lifecycle=env.deps.lifecycle, procedural=env.deps.procedural, vectors=env.vectors,
        embedder=env.deps.embedder, open_current=missing,
    )  # fmt: skip
    maintenance.configure_maintenance(deps)
    calls = _spy(monkeypatch, "validate_templates")
    outcome, _ = env.run()
    assert outcome.status == "done"
    assert outcome.result["templates"] == {"skipped": "no_current_build"}
    assert calls == []


# --- step 6 backfill bounds -------------------------------------------------------------------


def test_ut07_84_backfill_bound_and_heartbeat_every_256(
    env: MaintEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-84 step 6 selects at most 5,000 pending items and heartbeats every 256 items."""
    calls = _spy(monkeypatch, "maintenance_rows", ops)
    ids = [seed_item(content=f"bulk item {i}", pending=True) for i in range(257)]
    outcome, ctx = env.run()
    pending_calls = [k for _, k in calls if k["selector"] == "embedding_pending"]
    assert [k["limit"] for k in pending_calls] == [5_000]
    assert (mv.BACKFILL_MAX, mv.BEAT_EVERY) == (5_000, 256)
    assert outcome.result["backfill"] == {"embedded": 257, "stopped": False}
    beats = [n for n in ctx.notes if n and n.startswith("memory_maintenance backfill")]
    assert beats == ["memory_maintenance backfill 256", "memory_maintenance backfill 257"]
    assert len(vector_metas(env)) == len(ids)


def test_ut07_84_backfill_never_exceeds_5000(
    env: MaintEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-84 TH07-13 a selection larger than the bound is cut to 5,000 embeddings."""
    monkeypatch.setattr(mv, "BACKFILL_MAX", 3)
    for i in range(5):
        seed_item(content=f"bounded {i}", pending=True)
    outcome, _ = env.run()
    assert outcome.result["backfill"] == {"embedded": 3, "stopped": False}
    assert len(vector_metas(env)) == 3
    assert sum(r["data"]["embedding_pending"] is True for r in memory_rows()) == 2


def test_ut07_84_model_unavailable_stops_backfill_only(env: MaintEnv) -> None:
    """UT07-84 a ModelUnavailable stops step 6 (item stays pending); step 7 still runs."""
    pending = seed_item(content="cannot embed now", pending=True)
    seed_item(kind="business_rule", content="old rule", created_at=NOW - timedelta(days=400))
    env.embed.fail = True
    with capture_logs() as logs:
        outcome, _ = env.run()
    assert outcome.status == "done"
    assert outcome.result["backfill"] == {"embedded": 0, "stopped": True}
    assert row_of(pending)["data"]["embedding_pending"] is True
    assert len(yearly_reviews()) == 1
    assert any(e["event"] == "memory.maintenance.backfill_stopped" for e in logs)


def test_ut07_84_vector_upsert_failure_stops_backfill(
    env: MaintEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-84 a LanceDB failure on upsert stops step 6 and keeps the flag set."""
    pending = seed_item(content="lance down", pending=True)

    def down(rows: Any) -> None:
        msg = "memory vector store unavailable: upsert"
        raise ModelUnavailable(msg)

    monkeypatch.setattr(env.vectors, "upsert", down)
    outcome, _ = env.run()
    assert outcome.result["backfill"] == {"embedded": 0, "stopped": True}
    assert row_of(pending)["data"]["embedding_pending"] is True


def test_ut07_84_backfill_keeps_flag_when_content_changed(
    env: MaintEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-84 an item whose content hash changed while it was embedded keeps
    `embedding_pending` (the vector is stale; the next run re-embeds it)."""
    moving = seed_item(content="moving target", pending=True)
    real = env.deps.embedder.embed_item

    def embed_item(kind: Any, content: str) -> Any:
        data = {**row_of(moving)["data"], "content_hash": "new-hash"}
        ops.update_memory_item(moving, data=data)
        return real(kind, content)

    monkeypatch.setattr(env.deps.embedder, "embed_item", embed_item)
    outcome, _ = env.run()
    assert outcome.result["backfill"] == {"embedded": 1, "stopped": False}
    assert row_of(moving)["data"]["embedding_pending"] is True


# --- step 7 yearly review ---------------------------------------------------------------------


def test_ut07_84_yearly_review_once_per_item(env: MaintEnv) -> None:
    """UT07-84 a due business rule gets one review item; a second run creates none; recent,
    inactive and recently reviewed rules are not due."""
    due = seed_item(kind="business_rule", content="due rule", created_at=NOW - timedelta(days=366))
    seed_item(kind="business_rule", content="young rule", created_at=NOW - timedelta(days=100))
    seed_item(kind="business_rule", status="expired", content="gone",
              created_at=NOW - timedelta(days=500))  # fmt: skip
    seed_item(kind="business_rule", content="reviewed", created_at=NOW - timedelta(days=500),
              data={"last_review_requested_at": "2026-06-01T00:00:00.000000Z"})  # fmt: skip
    first, _ = env.run()
    second, _ = env.run()
    assert [r["payload"]["memory_id"] for r in yearly_reviews()] == [due]
    assert (first.result["review"], second.result["review"]) == (1, 0)


def test_ut07_84_yearly_review_rechecks_inside_the_transaction(
    env: MaintEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-84 a rule changed after selection (no longer active) gets no review item."""
    due = seed_item(kind="business_rule", content="race", created_at=NOW - timedelta(days=400))
    real = ops.maintenance_rows

    def select(**kwargs: Any) -> Any:
        rows = real(**kwargs)
        if kwargs["selector"] == "business_rule_review_due":
            core.run_write(
                lambda c: c.execute(
                    "UPDATE memory_item SET status = 'expired' WHERE memory_id = ?", (due,)
                ),
                op="t",
            )
        return rows

    monkeypatch.setattr(ops, "maintenance_rows", select)
    outcome, _ = env.run()
    assert outcome.result["review"] == 0
    assert yearly_reviews() == []


# --- gauges, errors, seam, registration ---------------------------------------------------------


def test_ut07_84_gauges_written_with_record_metric_samples(env: MaintEnv) -> None:
    """UT07-84 gauges herness_memory_items_total{status} and
    herness_memory_embedding_pending_total go through record_metric_samples (R-12)."""
    put_vector(env, seed_item(content="a"))
    seed_item(status="rejected", content="b")
    seed_item(status="rejected", content="c", pending=True)  # rejected: not counted pending
    env.embed.fail = True
    seed_item(content="d", pending=True)
    env.run()
    found = _metrics()
    assert found[("herness_memory_items_total", '{"status":"active"}')] == 2.0
    assert found[("herness_memory_items_total", '{"status":"rejected"}')] == 2.0
    assert found[("herness_memory_items_total", '{"status":"candidate"}')] == 0.0
    assert found[("herness_memory_embedding_pending_total", "{}")] == 1.0
    kinds = core.read_all(
        "SELECT DISTINCT kind, component FROM metric_sample WHERE name IN"
        " ('herness_memory_items_total', 'herness_memory_embedding_pending_total')"
    )
    assert [tuple(r) for r in kinds] == [("gauge", "memory")]


RETRYABLE = [StoreBusy("ops store busy in fts_check", op="fts_check"),
             ModelUnavailable("memory vector store unavailable")]  # fmt: skip


@pytest.mark.parametrize("error", RETRYABLE)
def test_ut07_84_retryable_errors_propagate(
    env: MaintEnv, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """UT07-84 StoreBusy and RetryableError propagate (spec 08 retries); state keeps the step."""

    def boom(**_: Any) -> bool:
        raise error

    monkeypatch.setattr(maintenance.ops, "fts_check_and_rebuild", boom)
    ctx = FakeCtx()
    with pytest.raises(type(error)):
        env.run(ctx)
    assert ctx.state["step"] == 3


def test_ut07_84_unconfigured_seam_is_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT07-84 the handler without configured deps raises ConfigError naming the seam."""
    monkeypatch.setattr(maintenance, "_SEAM", {})
    with pytest.raises(ConfigError, match="configure_maintenance"):
        maintenance.memory_maintenance_handler(FakeCtx())  # type: ignore[arg-type]
    maintenance.configure_maintenance(None)
    with pytest.raises(ConfigError):
        maintenance.maintenance_deps()


def test_ut07_84_registered_handler_runs(env: MaintEnv, reset_process_state: ProcessState) -> None:
    """UT07-84 register_handler("memory_maintenance", …) then resolve_handler/run_handler
    runs it to a done outcome (composition-root registration is T07-23's)."""
    del reset_process_state
    register_handler("memory_maintenance", maintenance.memory_maintenance_handler)
    handler = resolve_handler("memory_maintenance")
    assert handler is maintenance.memory_maintenance_handler
    outcome = run_handler(FakeCtx(), handler)  # type: ignore[arg-type]
    assert isinstance(outcome, JobOutcome)
    assert outcome.status == "done"


# --- resume and idempotency -------------------------------------------------------------------


def test_ut07_84_yield_saves_step_and_resume_skips_done_steps(
    env: MaintEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-84 the job yields after step 3 with its step saved; a fresh attempt resumes at
    `load_state()["step"]` without redoing steps 1-3 and finishes."""
    seed_item(kind="business_rule", content="resume rule", created_at=NOW - timedelta(days=400))
    expires = _spy(monkeypatch, "expire", env.deps.lifecycle)
    ctx = FakeCtx(yield_after={3})
    outcome, _ = env.run(ctx)
    assert outcome.status == "yield"
    assert list(outcome.result) == STEPS[:3]
    assert ctx.state["step"] == 3
    assert len(expires) == 1
    resumed = FakeCtx(state=ctx.state)
    final, _ = env.run(resumed)
    assert final.status == "done"
    assert len(expires) == 1
    assert [s["step"] for s in resumed.saves] == [4, 5, 6, 7]
    assert list(final.result) == STEPS
    assert len(yearly_reviews()) == 1


def test_ut07_84_rerun_of_finished_job_is_idempotent(
    env: MaintEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-84 re-running a finished job (saved step 7, or a fresh run) duplicates no review
    item and deletes no vector twice."""
    seed_item(kind="business_rule", content="idem rule", created_at=NOW - timedelta(days=400))
    put_vector(env, "mem_" + new_ulid())
    first, ctx = env.run()
    deletes = _spy(monkeypatch, "expire", env.deps.lifecycle)
    again, _ = env.run(FakeCtx(state=ctx.state))
    assert again.status == "done"
    assert deletes == []
    assert again.result == first.result
    fresh, _ = env.run()
    assert fresh.result["vectors"]["deleted"] == 0  # type: ignore[index]
    assert fresh.result["review"] == 0
    assert len(yearly_reviews()) == 1


def test_ut07_84_bad_saved_state_starts_over(env: MaintEnv) -> None:
    """UT07-84 a saved state that is not a step number in 0..7 restarts at step 1."""
    outcome, ctx = env.run(FakeCtx(state={"step": "x", "counts": []}))
    assert outcome.status == "done"
    assert ctx.saves[0]["step"] == 1


# --- TH07-13 SQLite is the source of truth ------------------------------------------------------


def test_ut07_84_th07_13_step5_never_changes_sqlite(
    env: MaintEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-84 TH07-13 vector statuses that disagree are repaired FROM SQLite; no item status
    changes and no SQLite row is deleted; fine items are untouched."""
    only5 = tuple((n, maintenance._vectors if n == "vectors" else (lambda r: 0)) for n in STEPS)
    monkeypatch.setattr(maintenance, "_STEPS", only5)
    rejected = seed_item(status="rejected", content="rejected item")
    put_vector(env, rejected, status="active")
    expired = seed_item(status="expired", content="expired item")
    put_vector(env, expired, status="active")
    active = seed_item(content="active item")
    put_vector(env, active, status="expired")
    fine = seed_item(content="fine item")
    put_vector(env, fine)
    unembedded_rejected = seed_item(status="rejected", content="never embedded")
    before = memory_rows()
    sets = _spy_vectors(env, monkeypatch)
    outcome, _ = env.run()
    after = memory_rows()
    assert [(r["memory_id"], r["status"], r["data"]) for r in after] == [
        (r["memory_id"], r["status"], r["data"]) for r in before]  # fmt: skip
    metas = vector_metas(env)
    assert (metas[rejected].status, metas[expired].status, metas[active].status) == (
        "rejected", "expired", "active")  # fmt: skip
    assert sorted(sets) == sorted([([active], "active"), ([expired], "expired"),
                                   ([rejected], "rejected")])  # fmt: skip
    assert unembedded_rejected not in metas
    assert outcome.result["vectors"]["flagged"] == 0  # type: ignore[index]


def _spy_vectors(env: MaintEnv, monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], str]]:
    real, calls = env.vectors.set_status, []

    def spy(ids: Any, status: Any) -> None:
        calls.append((list(ids), status))
        real(ids, status)

    monkeypatch.setattr(env.vectors, "set_status", spy)
    return calls


def test_ut07_84_th07_13_orphan_rechecked_before_delete(
    env: MaintEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-84 TH07-13 a vector whose row appears after the SQLite page was read is not an
    orphan: the batch re-reads SQLite before deleting, so only vectors are ever deleted."""
    late = "mem_" + new_ulid()
    put_vector(env, late, content_hash="late-hash")
    real = mv._vector_ids

    def vector_ids(vectors: Any) -> Any:
        for meta in real(vectors):
            if meta.memory_id == late and not any(r["memory_id"] == late for r in memory_rows()):
                _insert_late(late)
            yield meta

    monkeypatch.setattr(mv, "_vector_ids", vector_ids)
    outcome, _ = env.run()
    assert late in vector_metas(env)
    assert outcome.result["vectors"]["deleted"] == 0  # type: ignore[index]
    assert row_of(late)["status"] == "active"


def _insert_late(memory_id: str) -> None:
    ops.insert_memory_item({
        "memory_id": memory_id, "layer": "semantic", "kind": "glossary", "content": "late",
        "data": {"content_hash": "late-hash", "flags": [], "embedding_pending": False},
        "provenance": {}, "confidence": 0.8, "status": "active", "created_at": STAMP,
        "expires_at": None, "last_used_at": None, "use_count": 0,
    })  # fmt: skip


def test_ut07_84_written_items_stay_consistent(env: MaintEnv) -> None:
    """UT07-84 items stored by the real write path are consistent: nothing is repaired."""
    writer = env.deps.procedural.writer
    for text in ("Churn means customers who left.", "MTTR means mean time to restore."):
        writer.propose(proposal(text), now=NOW)
    before = memory_rows()
    outcome, _ = env.run()
    assert outcome.result["vectors"] == {"deleted": 0, "restatused": 0, "flagged": 0,
                                         "statuses": {"active": 2}}  # fmt: skip
    assert outcome.result["backfill"] == {"embedded": 0, "stopped": False}
    assert memory_rows() == before
