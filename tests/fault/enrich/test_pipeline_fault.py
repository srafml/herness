"""FT03-04, FT03-06: Laya `CURRENT` corrupt, and a preempt during `embed` (T03-28, F03-01).

Both run impl 02's real `build_pipeline` handler over the card-local small build
(`tests/integration/enrich/_pipeline_env.py`). FT03-06 shows the whole mapping: the stage
flushes, `YieldRequested` leaves `run_enrichment`, `_stage_enrich` returns `yield`, the
handler returns `JobOutcome(status="yield")`; the rerun embeds only the remainder. The
pipeline's own checkpoint (`enrich.stages_done`) is replayed at the `run_enrichment`
boundary, because impl 02's `_yield` rewrites the job state with its own keys only.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any, cast

import duckdb
import pytest
from pydantic import JsonValue
from tests.integration.enrich._pipeline_env import (
    EXTRA,
    MergingContext,
    PipelineEnv,
    pipeline_env,
)
from tests.unit.enrich._cluster_support import (
    cluster_warehouse,
    enrich_paths,
    finalize_auto,
    fixed_ids,
    freeze_now,
    planted,
    planted_incidents,
    run_stage,
    settings,
    store_vectors,
    vector_store,
)

from herness.core.types import JobOutcome
from herness.enrich import cluster_stage, embed_stage
from herness.enrich.cluster_stage import ClusterSnapshot
from herness.enrich.pipeline import EnrichReport, YieldRequested, run_enrichment
from herness.store import warehouse
from herness.store.vectors import VectorStore

pytestmark = pytest.mark.fault

__all__ = ["pipeline_env"]  # the fixture is used by name

_PAYLOAD: dict[str, Any] = {"stages": ["build", "enrich"], "depth": "standard"}


@pytest.mark.parametrize("damage", ["garbage", "missing", "tampered"])
def test_ft03_04_corrupt_laya_current_teacher_primary_and_promotable(
    pipeline_env: PipelineEnv, damage: str
) -> None:
    """FT03-04 corrupt (or missing) `laya/CURRENT`, or tampered weights: teacher-primary
    degraded mode, warning `laya_degraded` in the report, the build completes (promotable)."""
    env = pipeline_env
    laya_root = env.data_root / "models" / "laya"
    if damage == "garbage":
        (laya_root / "CURRENT").write_bytes(b"\x00not a version\xff")
    elif damage == "missing":
        (laya_root / "CURRENT").unlink()
    else:
        version = (laya_root / "CURRENT").read_text("utf-8").strip()
        (laya_root / version / "model.safetensors").write_bytes(b"tampered")
    outcome, ctx = env.job(_PAYLOAD)
    assert isinstance(outcome, JobOutcome), outcome
    assert outcome.status == "done"
    report = EnrichReport.model_validate(outcome.result["enrich"])
    assert "laya_degraded" in report.warnings
    assert set(report.question_primary.values()) == {"openjev"}
    assert "laya" not in report.decider_versions
    primary = report.stages["decide-primary"]
    assert (primary.status, primary.note) == ("skipped", "laya_degraded")
    assert not [n for n, s in report.stages.items() if s.status == "failed"]
    assert env.calls()["laya"] == 0
    assert env.calls()["openjev"] > 0  # the teacher answers every question
    build_id = str(outcome.result["build_id"])
    status = env.query(build_id, "SELECT status, finished_at IS NOT NULL FROM meta.build")
    assert status == [("building", True)]  # a completed, unpromoted (promotable) build
    assert ctx.current_class == "none"


def test_ft03_06_preempt_during_embed_flushes_yields_then_resumes(
    pipeline_env: PipelineEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FT03-06 `stop("preempt")` during `embed`: flush, `JobOutcome(yield)`; the rerun
    continues and embeds only the remainder."""
    env = pipeline_env
    monkeypatch.setattr(embed_stage, "FLUSH_BATCHES", 1)  # an 8-row window: 4 windows here

    def preempt() -> None:  # the worker's stop("preempt") arrives during the first window
        assert env.gpu.ctx is not None
        env.gpu.ctx.request_yield("preempt")

    env.encoder.on_encode.append(preempt)
    outcome, ctx = env.job(_PAYLOAD)
    assert isinstance(outcome, JobOutcome), outcome
    assert outcome.status == "yield"
    build_id = str(outcome.result["build_id"])
    assert ctx.current_class == "none"
    assert ctx.load_state() == {"build_id": build_id, "stages_done": ["build"]}
    # the pipeline checkpointed `text` before the yield (merged into the build's state)
    enrich_states = [s["enrich"] for s in ctx.saved_states if "enrich" in s]
    assert cast("dict[str, JsonValue]", enrich_states[-1])["stages_done"] == ["text"]
    stored = env.data_root / "vectors"
    flushed = VectorStore(stored).table("ticket_embedding").count_rows()
    assert flushed == 8  # the first window was flushed before the yield
    texts = {r[0] for r in env.query(build_id, "SELECT text FROM enrich.text_redacted")}
    first_inputs = [t for t in env.encoder.inputs if t in texts]
    assert len(first_inputs) == 8

    env.encoder.on_encode.clear()
    again, ctx2 = env.job(_PAYLOAD, state=ctx.load_state())
    assert isinstance(again, JobOutcome), again
    assert (again.status, again.result["build_id"]) == ("done", build_id)
    report = EnrichReport.model_validate(again.result["enrich"])
    total = len(texts)
    assert total == 4 + 2 + 1 + EXTRA
    assert report.stages["embed"].embedded == total - 8
    rerun_inputs = [t for t in env.encoder.inputs[len(first_inputs) :] if t in texts]
    assert sorted(rerun_inputs) == sorted(texts - set(first_inputs))
    assert VectorStore(stored).table("ticket_embedding").count_rows() == total
    assert ctx2.current_class == "none"


def test_ft03_06_checkpoint_resume_skips_done_stages(
    pipeline_env: PipelineEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FT03-06 the pipeline checkpoint (`enrich.stages_done`, `started_at`) replayed on the same
    build: `text` is not redone, `embed` continues with the remainder only."""
    env = pipeline_env
    monkeypatch.setattr(embed_stage, "FLUSH_BATCHES", 1)
    outcome, ctx = env.job({"stages": ["build"]})
    assert isinstance(outcome, JobOutcome), outcome
    build_id = str(outcome.result["build_id"])
    path = warehouse.build_path(build_id, layout=env.layout)
    first = MergingContext(_PAYLOAD, state=ctx.load_state())  # T02-19b view semantics
    env.gpu.ctx = first
    env.encoder.on_encode.append(lambda: first.request_yield("preempt"))
    con = duckdb.connect(str(path))
    try:
        with pytest.raises(YieldRequested, match="yield requested at embed"):
            run_enrichment(con, build_id, depth="standard", ctx=first, prev_warehouse=None,
                           llm_factory=env.factory)  # fmt: skip
        env.encoder.on_encode.clear()
        saved = first.load_state()
        assert saved["build_id"] == build_id  # the build's own keys are kept
        assert saved["stages_done"] == ["build"]
        checkpoint = cast("dict[str, JsonValue]", saved["enrich"])
        assert checkpoint["stages_done"] == ["text"]
        texts_before = con.execute("SELECT count(*) FROM enrich.text_redacted").fetchone()
        encoded = len(env.encoder.inputs)
        resumed = MergingContext(_PAYLOAD, state=saved)
        env.gpu.ctx = resumed
        report = run_enrichment(con, build_id, depth="standard", ctx=resumed,
                                prev_warehouse=None, llm_factory=env.factory)  # fmt: skip
    finally:
        con.close()
    assert (report.stages["text"].status, report.stages["text"].note) == ("skipped", "resumed")
    assert report.stages["embed"].embedded == cast("tuple[int]", texts_before)[0] - 8
    done = cast("dict[str, JsonValue]", resumed.load_state()["enrich"])
    assert done["started_at"] == checkpoint["started_at"]  # spot-check window kept
    assert len(env.encoder.inputs) > encoded


def test_ft03_06_crash_between_mark_final_writes_rerun_is_not_stuck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FT03-06 a crash after `snapshot.json` says final but before `CURRENT` moved: the rerun
    of the build recomputes (the snapshot is not `assigned`) and publishes; no forced run."""
    freeze_now(monkeypatch)
    fixed_ids(monkeypatch)
    monkeypatch.setattr(cluster_stage, "CHUNK", 128)
    vectors, truth = planted(12, 50, noise=60, seed=2026)
    incs = planted_incidents(vectors, truth)
    store_vectors(vector_store(tmp_path), incs)
    paths = enrich_paths(tmp_path)
    cfg = settings(proto_per=5, min_incidents=15)
    wh = cluster_warehouse(tmp_path / "b.duckdb", incs)
    result, _ = run_stage(wh, paths, "b-0001", cfg=cfg)
    real = ClusterSnapshot.mark_final

    def crash(self: ClusterSnapshot, paths_: Any) -> None:
        self._write_meta(self.folder(paths_), dataclasses.replace(self.meta, status="final"))
        msg = "power cut"
        raise OSError(msg)

    monkeypatch.setattr(ClusterSnapshot, "mark_final", crash)
    with pytest.raises(OSError, match="power cut"):
        finalize_auto(wh, result, paths)
    assert ClusterSnapshot.load_current(paths) is None
    monkeypatch.setattr(ClusterSnapshot, "mark_final", real)
    again, _ = run_stage(wh, paths, "b-0001", cfg=cfg)  # the same build, rerun
    assert again.kind == "full"
    finalize_auto(wh, again, paths)
    current = ClusterSnapshot.load_current(paths)
    assert current is not None
    assert (current.snapshot_id, current.meta.status) == ("b-0001", "final")
    wh.close()
