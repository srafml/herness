"""Worker bootstrap of the impl 08 fault suite's GPU rows (T08-24: FT08-10, FT08-12).

`bootstrap` ("tests.support._f08_gpu_handlers:bootstrap") is `tests.support.worker_bootstrap`'s
bootstrap with one dispatching handler registered for every kind: a `build_pipeline` job whose
payload `mode` is `f10` or `f12` runs the handler below, every other job the fake handler of
`worker_bootstrap`. Supervisor and job children call it alike, so the same handler object is
registered in each process. The handlers run in the job child and reach the GPU only through
`JobContext` requests to the supervisor's GPU slot.
"""

from __future__ import annotations

import os
import time
from typing import Any, Final

from tests.support import worker_bootstrap as wb

BOOTSTRAP: Final = "tests.support._f08_gpu_handlers:bootstrap"
F10_BATCHES: Final = 4
F10_FIRST_RUN: Final = 2  # batches finished before the first run waits for its stop
WAITING_NOTE: Final = "waiting for stop"
_WAIT_LIMIT_S: Final = 300.0
_POLL_S: Final = 0.05


def _f12(ctx: Any) -> Any:
    """FT08-12: the build's decider scope, OpenJev started in it, then the reasoning class."""
    from herness.core.types import JobOutcome  # noqa: PLC0415 - light import for children

    with ctx.gpu_scope("decider"):
        ctx.services.start("openjev")
        ctx.require_gpu_class("reasoning")
    return JobOutcome(status="done", result={"pid": os.getpid()})


def _f10(ctx: Any) -> Any:
    """FT08-10: decider batches with a checkpoint after each; the first run then waits until
    it is stopped (heartbeating `WAITING_NOTE`) and yields; a later run skips finished ones."""
    from herness.core.types import JobOutcome  # noqa: PLC0415 - light import for children

    state = ctx.load_state()
    done = [int(b) for b in state.get("done", [])]
    runs = [list(r) for r in state.get("runs", [])]
    this: list[int] = []
    with ctx.gpu_scope("decider"):
        for batch in range(F10_BATCHES):
            if batch in done:
                continue
            if not runs and len(this) == F10_FIRST_RUN:
                break
            done.append(batch)
            this.append(batch)
            ctx.save_state({"done": done, "runs": [*runs, this]})
        deadline = time.monotonic() + _WAIT_LIMIT_S
        while not runs and not ctx.should_yield() and time.monotonic() < deadline:
            ctx.heartbeat(WAITING_NOTE)
            time.sleep(_POLL_S)
    if len(done) < F10_BATCHES:
        return JobOutcome(status="yield")
    return JobOutcome(status="done", result={"done": done, "runs": [*runs, this]})


def dispatch(ctx: Any) -> Any:
    """The handler of every kind: `f10` / `f12` build jobs here, the rest `wb.fake_handler`."""
    mode = ctx.job.payload.get("mode")
    if ctx.kind == "build_pipeline" and mode == "f10":
        return _f10(ctx)
    if ctx.kind == "build_pipeline" and mode == "f12":
        return _f12(ctx)
    return _FAKE(ctx)


_FAKE: Final = wb.fake_handler


def bootstrap() -> None:
    """`worker_bootstrap.bootstrap` with `dispatch` registered for every kind."""
    wb.fake_handler = dispatch  # type: ignore[assignment]  # read by wb.bootstrap at call time
    try:
        wb.bootstrap()
    finally:
        wb.fake_handler = _FAKE  # type: ignore[assignment]
