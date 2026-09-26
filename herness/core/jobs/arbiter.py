"""GPU arbiter decision (U08-70; design 08 §5.10, R-43, TH08-09).

Run by the supervisor only while the GPU slot is free. Pure: the caller supplies the loaded
class, the active window, the claimable counts and any external class request, and acts on
the decision (swap, claim, or wait). A missing `claimable` key counts as 0.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from herness.core.jobs.windows import ActiveWindow
    from herness.core.types import GpuClass

__all__ = ["ArbiterDecision", "arbiter_decide"]

type ArbiterAction = Literal["keep", "swap", "idle"]


@dataclass(frozen=True, slots=True)
class ArbiterDecision:
    """What the GPU slot does next: keep `target` loaded, swap to it, or idle on it.

    `reason` is one of `requested`, `request_not_allowed`, `loaded_claimable`,
    `slot_kind_claimable`, `claimable`, `preload`, `no_work`.
    """

    action: ArbiterAction
    target: GpuClass
    reason: str


def _for_claimable(
    loaded: GpuClass, classes: Sequence[GpuClass], claimable: Mapping[GpuClass, int]
) -> ArbiterDecision | None:
    """U08-70 steps 2, 2a and 3; None when no allowed class has claimable jobs."""
    if loaded in classes and claimable.get(loaded, 0) > 0:
        return ArbiterDecision("keep", loaded, "loaded_claimable")
    if claimable.get("none", 0) > 0:
        return ArbiterDecision("keep", loaded, "slot_kind_claimable")
    for cls in classes:
        if claimable.get(cls, 0) > 0:
            return ArbiterDecision("keep" if cls == loaded else "swap", cls, "claimable")
    return None


def arbiter_decide(
    loaded: GpuClass,
    window: ActiveWindow,
    claimable: Mapping[GpuClass, int],
    *,
    requested: GpuClass | None = None,
) -> ArbiterDecision:
    """Decide the GPU class for a free GPU slot (U08-70 steps 1-5).

    1. An external request (`worker.requested_class`) different from `loaded` is swapped to
       when it is `none` or allowed by the window, else rejected (`idle`,
       `request_not_allowed`; the caller logs `jobs.gpu.request_rejected` and clears it,
       TH08-09). 2. Keep an allowed loaded class with claimable jobs. 2a. Keep when
       `GPU_SLOT_KINDS` jobs (key `none`) are claimable: they start on the loaded class and
       switch in-job (R-43). 3. The first allowed class with claimable jobs. 4. The
       window's `preload`. 5. Idle on the loaded class.
    """
    classes = window.spec.classes
    if requested is not None and requested != loaded:
        if requested == "none" or requested in classes:
            return ArbiterDecision("swap", requested, "requested")
        return ArbiterDecision("idle", loaded, "request_not_allowed")
    decision = _for_claimable(loaded, classes, claimable)
    if decision is not None:
        return decision
    preload = window.spec.preload
    if preload is not None and preload != loaded:
        return ArbiterDecision("swap", preload, "preload")
    return ArbiterDecision("idle", loaded, "no_work")
