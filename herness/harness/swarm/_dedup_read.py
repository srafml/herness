"""Paged read of a run's `proposed` findings for dedup (impl 06 U06-103 step 1).

Private sibling of `herness.harness.swarm.dedup`. `Blackboard.list_findings` returns at most
500 findings per call and has no offset, so findings are read per chunk of at most 200 author
task ids (`FindingFilter.task_ids`); a chunk that fills a page is split and read again until
each read is below the page limit. A single task that alone fills a page is logged
(`harness.dedup.truncated`, WARNING), since nothing finer can be selected through the API.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import TYPE_CHECKING, Final

from herness.core.logging import get_logger
from herness.harness.blackboard import FindingFilter
from herness.store.ops.runs import select_tasks

if TYPE_CHECKING:
    from herness.core.types import Finding, FindingStatus
    from herness.harness.blackboard import Blackboard

__all__ = ["PAGE_LIMIT", "TASK_CHUNK", "read_proposed"]

PAGE_LIMIT: Final = 500
TASK_CHUNK: Final = 200
_log = get_logger("harness.swarm")


def _author_task_ids(run_id: str) -> list[str]:
    """Sorted ids of the run's tasks except crosscheck tasks (analyst, specialty `crosscheck`)."""
    rows = select_tasks(run_id)
    return sorted(row.task_id for row in rows if row.spec.specialty != "crosscheck")


async def _read_chunk(
    bb: Blackboard, run_id: str, task_ids: Sequence[str], *, status: FindingStatus = "proposed"
) -> list[Finding]:
    """One chunk's `status` findings (the Writer step of T06-19 reads `verified`, `rejected`)."""
    flt = FindingFilter(run_id=run_id, status={status}, task_ids=set(task_ids), limit=PAGE_LIMIT)
    page = await bb.list_findings(flt)
    if len(page) < PAGE_LIMIT:
        return page
    if len(task_ids) == 1:
        _log.warning(
            "harness.dedup.truncated", run_id=run_id, task_id=task_ids[0], limit=PAGE_LIMIT
        )
        return page
    half = len(task_ids) // 2
    return [
        *await _read_chunk(bb, run_id, task_ids[:half], status=status),
        *await _read_chunk(bb, run_id, task_ids[half:], status=status),
    ]


async def read_proposed(bb: Blackboard, run_id: str) -> list[Finding]:
    """Every `proposed` finding of `run_id` not authored by a crosscheck task, by id."""
    task_ids = await asyncio.to_thread(_author_task_ids, run_id)
    out: list[Finding] = []
    for start in range(0, len(task_ids), TASK_CHUNK):
        out += await _read_chunk(bb, run_id, task_ids[start : start + TASK_CHUNK])
    return sorted(out, key=lambda f: f.finding_id)
