"""Loop-signal policy: nudge on the first signal of a task, stop with `guard_stop` on the
second (impl 08 U08-40; design 08 §3.2, §5.6; TH08-14).
"""

from __future__ import annotations

from typing import Final, Literal

from herness.core.config import get_config
from herness.core.resilience.events import record_event
from herness.core.resilience.metrics import record_counter
from herness.core.resilience.ports import TracerLike
from herness.core.types import LoopSignal, LoopState

__all__ = ("loop_signal_policy",)

GUARD_STOPS_METRIC: Final = "herness_resilience_guard_stops_total"


def loop_signal_policy(
    state: LoopState, signal: LoopSignal, *, tracer: TracerLike | None = None
) -> Literal["nudge", "stop"]:
    """Nudge on the first loop signal of a task, stop with `guard_stop` on the second (U08-40).

    `state.loop_signals` is spec 05's dedicated loop-signal counter, separate from `nudges`
    (which spec 05 increments before calling this policy): a `max_tokens` continuation nudge
    never counts as a loop signal here (R-66, D08-04). On stop, writes one `resilience_event`
    row and increments `herness_resilience_guard_stops_total{cause}` (TH08-14).
    """
    limit = get_config().resilience.resilience.loop.stop_on_signal_no
    if state.loop_signals < limit:
        return "nudge"
    record_event(
        "guard_stop",
        component="resilience",
        run_id=tracer.run_id if tracer else None,
        task_id=tracer.task_id if tracer else None,
        detail={"cause": signal.cause, "step": state.step},
        tracer=tracer,
    )
    record_counter(GUARD_STOPS_METRIC, component="resilience", labels={"cause": signal.cause})
    return "stop"
