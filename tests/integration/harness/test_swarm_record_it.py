"""IT06-22, IT06-31: the record step over the real ops store (impl 06 T06-20, U06-122).

The scheduler IT setup (`sched`: migrated ops store, real Blackboard writer) with a stub memory
store (idempotent `write_recommendations`), a stub pipeline and an injected `render` callable.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import structlog
from tests.integration.harness import test_swarm_scheduler_it as base_it
from tests.unit.harness.swarm import _swarm_drafts as d
from tests.unit.harness.swarm._swarm_findings import fid, finding, number, run_row

from herness.core import time as clock
from herness.core.errors import FatalError, ReportContractError
from herness.core.types import ReportDraft
from herness.harness.swarm import draft_rules as dr
from herness.harness.swarm import record, writer
from herness.store.ops import get_run, run_write, set_run_status

pytestmark = pytest.mark.integration

sched = base_it.sched
bb_env = base_it.bb_env
test_redactor = base_it.test_redactor
_REC = "rec_{:026d}"
_LIVE = {"pending", "planning", "running", "verifying", "writing", "recording"}


class _Memory:
    """Idempotent per run: the first call mints ids, later calls return them."""

    def __init__(self, *, short: bool = False) -> None:
        self.ids: list[str] | None = None
        self.promoted = 0
        self.fail_promote = False
        self.short = short

    def write_recommendations(self, run_id: str, drafts: Sequence[Any]) -> list[str]:
        del run_id
        if self.ids is None:
            self.ids = [_REC.format(i + 1) for i in range(len(drafts))]
        if len(drafts) != len(self.ids):
            msg = "recommendations changed on resume"
            raise ReportContractError(msg)
        return self.ids[:-1] if self.short else list(self.ids)

    def promote_procedural(self, run_id: str) -> None:
        del run_id
        if self.fail_promote:
            msg = "crash"
            raise RuntimeError(msg)
        self.promoted += 1


class _Pipeline(d.StubPipeline):
    def recommendation_drafts(self, draft: ReportDraft, findings: dict[str, Any]) -> list[Any]:
        del findings
        return [f"draft-{r.target_id}" for r in draft.recommendations]


class _Render:
    def __init__(self, exc: BaseException | None = None) -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self.exc = exc

    def __call__(self, run_id: str, formats: Sequence[str]) -> None:
        self.calls.append((run_id, list(formats)))
        if self.exc is not None:
            raise self.exc


def _setup(s: base_it.Sched, tmp_path: Path, memory: _Memory) -> Path:
    env = s.env
    env.memory = memory  # type: ignore[assignment]
    env.pipeline = _Pipeline()  # type: ignore[assignment]
    paths = env.hcfg.paths.model_copy(update={"data": tmp_path / "data"})
    env.hcfg = env.hcfg.model_copy(update={"paths": paths})
    out = d.writer_out(recommendations=[d.fund_rec("c1"), d.fund_rec("c2")])
    draft = dr.fill_draft(
        out, run=run_row(), pipeline=d.StubPipeline(), plan_ctx=d.plan_ctx(),
        findings={fid(1): finding(1, numbers=[number(1, q=5)])},
        extras=dr.DraftExtras(), verification=d.vresult(),
    )  # fmt: skip
    draft = draft.model_copy(update={"run_id": s.bb.run_id})
    path = writer.draft_path(s.bb.run_id, tmp_path / "data" / "reports")
    writer.write_draft_atomic(path, draft)
    run_write(lambda c: set_run_status(c, s.bb.run_id, "recording", _LIVE, now=clock.now()), op="t")
    return path


def _run(s: base_it.Sched, render: _Render) -> None:
    asyncio.run(record.run_record_step(s.env, render))


def _status(s: base_it.Sched) -> str:
    run = get_run(s.bb.run_id)
    assert run is not None
    return run.status


def test_it06_31_record_twice_same_rec_ids(sched: base_it.Sched, tmp_path: Path) -> None:
    """IT06-31 record twice: same `rec_id`s in draft.json, no duplicate writes, run `done`."""
    mem, render = _Memory(), _Render()
    path = _setup(sched, tmp_path, mem)
    _run(sched, render)
    first = ReportDraft.model_validate_json(path.read_bytes())
    assert [r.rec_id for r in first.recommendations] == [_REC.format(1), _REC.format(2)]
    assert _status(sched) == "done"
    _run(sched, render)
    assert ReportDraft.model_validate_json(path.read_bytes()) == first
    assert mem.ids == [_REC.format(1), _REC.format(2)]
    assert len(render.calls) == 2
    assert render.calls[0][0] == sched.bb.run_id


def test_it06_31_resume_after_crash_before_status(sched: base_it.Sched, tmp_path: Path) -> None:
    """IT06-31 a crash after the rec_id fill and before `done` is completed by a re-run."""
    mem, render = _Memory(), _Render()
    path = _setup(sched, tmp_path, mem)
    mem.fail_promote = True
    with pytest.raises(RuntimeError):
        _run(sched, render)
    assert _status(sched) == "recording"
    assert not render.calls
    mem.fail_promote = False
    _run(sched, render)
    done = ReportDraft.model_validate_json(path.read_bytes())
    assert [r.rec_id for r in done.recommendations] == [_REC.format(1), _REC.format(2)]
    assert _status(sched) == "done"
    assert len(render.calls) == 1


def test_it06_22_render_failure_keeps_done(sched: base_it.Sched, tmp_path: Path) -> None:
    """IT06-22 render raises: run `done`, `meta.render_error` set (scrubbed), recs promoted."""
    mem = _Memory()
    _setup(sched, tmp_path, mem)
    with structlog.testing.capture_logs() as logs:
        _run(sched, _Render(FatalError("template broke token=sk-abcdefghijklmnop1234")))
    run = get_run(sched.bb.run_id)
    assert run is not None
    assert run.status == "done"
    err = str(run.meta["render_error"])
    assert err.startswith("FatalError: template broke")
    assert "sk-abcdefghijklmnop1234" not in err
    assert mem.promoted == 1
    failed = [e for e in logs if e["event"] == "harness.render.failed"]
    assert failed
    assert failed[0]["log_level"] == "error"
    assert "sk-abcdefghijklmnop1234" not in str(failed)
    done = [e for e in logs if e["event"] == "harness.record.completed"]
    assert done[0]["n_recs"] == 2


def test_it06_22_rec_id_count_mismatch_fails_closed(sched: base_it.Sched, tmp_path: Path) -> None:
    """IT06-22 fewer ids than recommendations → FatalError; draft untouched, run not `done`."""
    path = _setup(sched, tmp_path, _Memory(short=True))
    before = path.read_bytes()
    with pytest.raises(FatalError):
        _run(sched, _Render())
    assert path.read_bytes() == before
    assert _status(sched) == "recording"


def test_it06_22_changed_recommendations_propagates(sched: base_it.Sched, tmp_path: Path) -> None:
    """IT06-22 `ReportContractError` from memory propagates (the driver fails the run)."""
    mem = _Memory()
    _setup(sched, tmp_path, mem)
    mem.ids = [_REC.format(1)]
    with pytest.raises(ReportContractError):
        _run(sched, _Render())
