"""Record step and render hand-off (impl 06 U06-122; design 06 §5.10).

`run_record_step` hands the stored draft's recommendations to memory, fills `rec_id` in the
draft, promotes procedural memory, sets the run `done` and calls the injected `render`
(`herness.reports.render.render_run`, L5, never imported here, §13 O06-12). Every step is
idempotent, so a re-run after a crash converges on the same `rec_id`s and the same state.
"""

from __future__ import annotations

import asyncio
import functools
from pathlib import Path
from typing import TYPE_CHECKING, Final

from herness.core import time as clock
from herness.core.errors import FatalError, HernessError
from herness.core.logging import get_logger
from herness.core.secrets import scrub_secrets
from herness.core.types import ReportDraft
from herness.harness.swarm.writer import draft_path, write_draft_atomic
from herness.store.ops import get_findings, get_run, run_write, set_run_status, update_run_fields

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Sequence

    from herness.harness.swarm.routing import RunEnv

__all__ = ["run_record_step"]

_log = get_logger("harness.swarm")
_MAX_ERROR_CHARS: Final = 500
_WITHHELD: Final = "message withheld"


def _fill_rec_ids(draft: ReportDraft, rec_ids: Sequence[str]) -> ReportDraft:
    """`draft` with `rec_ids[i]` on recommendation `i`; a count mismatch, a repeated id or a
    different id already on the draft fails closed (`FatalError`)."""
    recs = draft.recommendations
    if len(rec_ids) != len(recs) or len(set(rec_ids)) != len(rec_ids):
        msg = f"rec_id mismatch: run_id={draft.run_id} drafts={len(recs)} ids={len(rec_ids)}"
        raise FatalError(msg)
    if any(r.rec_id not in (None, rid) for r, rid in zip(recs, rec_ids, strict=True)):
        msg = f"rec_id differs from the stored draft: run_id={draft.run_id}"
        raise FatalError(msg)
    filled = [r.model_copy(update={"rec_id": rid}) for r, rid in zip(recs, rec_ids, strict=True)]
    return draft.model_copy(update={"recommendations": filled})


async def _set_done(env: RunEnv) -> None:
    """`recording` → `done`; a run that is already `done` (resume) is fine, anything else not."""
    run_id = env.run.run_id
    now = clock.now()

    def to_done(conn: sqlite3.Connection) -> bool:
        return set_run_status(conn, run_id, "done", {"recording"}, now=now)

    if await env.bb.run_on_writer(lambda: run_write(to_done, op="swarm_record_done")):
        return
    row = await asyncio.to_thread(get_run, run_id)
    if row is None or row.status != "done":
        msg = f"run not in recording: run_id={run_id} status={row.status if row else None}"
        raise FatalError(msg)


async def _render(env: RunEnv, render: Callable[[str, Sequence[str]], object]) -> None:
    """Step 6: a `HernessError` is recorded in `meta.render_error`; the run stays `done`."""
    run_id = env.run.run_id
    formats = list(env.hcfg.app.reports.formats)
    try:
        await asyncio.to_thread(render, run_id, formats)
    except HernessError as err:
        scrubbed = scrub_secrets(None, "", {"m": str(err)}).get("m", _WITHHELD)
        text = f"{type(err).__name__}: {scrubbed}"[:_MAX_ERROR_CHARS]
        patch = functools.partial(
            update_run_fields, run_id=run_id, meta_patch={"render_error": text}
        )
        await env.bb.run_on_writer(lambda: run_write(patch, op="swarm_render_error"))
        _log.error("harness.render.failed", run_id=run_id, error=text)


async def run_record_step(env: RunEnv, render: Callable[[str, Sequence[str]], object]) -> None:
    """Hand-off, `rec_id` fill, promotion, `done` and render (U06-122).

    `ReportContractError` from memory (changed recommendations on resume) propagates; the
    driver fails the run (U06-82 step 8). A render `HernessError` does not fail the run.
    """
    run_id = env.run.run_id
    path = draft_path(run_id, Path(env.hcfg.paths.data) / "reports")
    draft = ReportDraft.model_validate_json(await asyncio.to_thread(path.read_bytes))
    cited = sorted({fid for rec in draft.recommendations for fid in rec.finding_ids})
    findings = await asyncio.to_thread(get_findings, cited)
    drafts = env.pipeline.recommendation_drafts(draft, findings)
    rec_ids = await asyncio.to_thread(env.memory.write_recommendations, run_id, drafts)
    filled = _fill_rec_ids(draft, rec_ids)
    if filled != draft:
        await asyncio.to_thread(write_draft_atomic, path, filled)
    await asyncio.to_thread(env.memory.promote_procedural, run_id)
    await _set_done(env)
    await _render(env, render)
    _log.info("harness.record.completed", run_id=run_id, n_recs=len(rec_ids))
