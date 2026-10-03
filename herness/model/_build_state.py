"""Job state of the ``build_pipeline`` job (T02-19b).

Private sibling of ``herness.model.build`` (impl 02 §2 module-map note), split off for the
400-line budget of ``build.py``. The build keeps the latest job state it knows in
``_BuildRun.state`` and saves its own keys (``build_id``, ``stages_done``) merged over it, so a
checkpoint a stage hook saved earlier in the job (the ``scoring`` key of impl 04
``run_scoring``) survives the build's own saves (U02-98 step 5, a stage yield). A real job
context's ``load_state`` returns the state at the attempt's start, so the hook gets a view of
the job context whose state reads and writes go through ``_BuildRun.state``; every other member
is the job context's own. Imported only by ``build`` and ``_build_stages``.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from pydantic import JsonValue

    from herness.core.jobs.ports import JobContext
    from herness.model.build import _BuildRun


def save_state(run: _BuildRun) -> None:
    """Save ``build_id`` and ``stages_done`` merged over the latest job state."""
    run.state = {**run.state, "build_id": run.build_id, "stages_done": list(run.stages_done)}
    run.ctx.save_state(run.state)


class _HookContext:
    """The job context a stage hook sees: its state goes through ``run.state``."""

    def __init__(self, run: _BuildRun) -> None:
        self._run = run

    def __getattr__(self, name: str) -> object:
        return getattr(self._run.ctx, name)

    def load_state(self) -> dict[str, JsonValue]:
        return copy.deepcopy(self._run.state)

    def save_state(self, state: dict[str, JsonValue]) -> None:
        # the hook's keys are merged in; the build's own keys stay those of this run
        self._run.state = {**self._run.state, **state}
        save_state(self._run)


def hook_context(run: _BuildRun) -> JobContext:
    """``run.ctx`` as passed to a stage hook that checkpoints through the job state."""
    return cast("JobContext", _HookContext(run))
