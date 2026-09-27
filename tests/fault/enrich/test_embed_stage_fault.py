"""FT03-06: a stop during the embed stage flushes, yields, and a rerun continues (T03-07).

The `JobOutcome(yield)` mapping belongs to the pipeline card (T03-28, U02-100): here the
stage's half is checked, namely that the buffer is flushed before `YieldRequested("embed")`
and that a rerun encodes only the remainder. The second test drives an interruption through
the impl 08 fault plan at the `embed.batch` point (R-40, `HERNESS_ENV=test`).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.support.build_harness import FakeJobContext
from tests.support.fault_env import FaultEnv
from tests.unit.enrich._embed_support import (
    FakeEncoder,
    Rec,
    Report,
    embed_warehouse,
    stored,
    use_batch_size,
    use_store,
)

from herness.core.errors import ModelUnavailable
from herness.enrich.embed_stage import run_embed_stage
from herness.enrich.gpu import YieldRequested

pytestmark = pytest.mark.fault

_RECS = [Rec("incident", f"INC{i:04d}", f"ticket text {i:04d}") for i in range(170)]


def test_ft03_06_preempt_flushes_then_yields_and_rerun_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FT03-06 stop("preempt") during embed: flush, YieldRequested; rerun encodes the rest."""
    store = use_store(tmp_path, monkeypatch)
    use_batch_size(monkeypatch, 8)
    wh = embed_warehouse(tmp_path / "wh.duckdb", _RECS)
    ctx = FakeJobContext()
    ctx.request_yield("preempt")
    first = FakeEncoder()
    with pytest.raises(YieldRequested) as info:
        run_embed_stage(wh, encoder=first, ctx=ctx, report=Report())  # type: ignore[arg-type]
    assert info.value.stage == "embed"
    assert ctx.stop_reason == "preempt"
    assert store.count("ticket_embedding") == 160  # flushed before the yield
    assert len(first.inputs) == 160

    second = FakeEncoder()
    report = Report()
    run_embed_stage(wh, encoder=second, ctx=FakeJobContext(), report=report)  # type: ignore[arg-type]
    assert len(second.inputs) == 10
    assert not set(second.inputs) & set(first.inputs)
    assert (report.embedded, report.rows) == (10, 10)
    assert set(stored(store)) == {rec.record_id for rec in _RECS}


def test_ft03_06_fault_plan_at_embed_batch_keeps_flushed_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault_env: FaultEnv
) -> None:
    """FT03-06 fault plan error at the 22nd embed.batch: 160 rows kept; rerun encodes 10."""
    store = use_store(tmp_path, monkeypatch)
    use_batch_size(monkeypatch, 8)
    wh = embed_warehouse(tmp_path / "wh.duckdb", _RECS)
    fault_env([{"point": "embed.batch", "action": "error:ModelUnavailable", "nth": 22}])
    first = FakeEncoder()
    with pytest.raises(ModelUnavailable):
        run_embed_stage(wh, encoder=first, ctx=FakeJobContext(), report=Report())  # type: ignore[arg-type]
    assert store.count("ticket_embedding") == 160
    second = FakeEncoder()
    run_embed_stage(wh, encoder=second, ctx=FakeJobContext(), report=Report())  # type: ignore[arg-type]
    assert len(second.inputs) == 10
    assert store.count("ticket_embedding") == 170
