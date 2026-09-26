"""Jobs API of impl 08 (design 08 §3, §5; delta D08-01).

Names are re-exported lazily (PEP 562): the owning submodule is imported on first access.
`JobContext` and `ServiceControl` live here, not in `herness.core.types` (R-02).
Later 08 cards add their public names to `_EXPORTS` and the `TYPE_CHECKING` block.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Final

_EXPORTS: Final[dict[str, str]] = {
    "ActiveWindow": "windows",
    "ArbiterDecision": "arbiter",
    "CronExpr": "cron",
    "JobContext": "ports",
    "JobRow": "ports",
    "JobsBackend": "ports",
    "NewJob": "ports",
    "SchedCheck": "ports",
    "arbiter_decide": "arbiter",
    "ServiceControl": "ports",
    "WorkerRow": "ports",
    "bind_jobs_backend": "ports",
    "next_window_allowing": "windows",
    "preempt_deadline": "windows",
    "resolve_local": "cron",
    "validate_resilience_config": "validate",
    "validate_windows": "validate",
    "window_at": "windows",
}

__all__: tuple[str, ...] = tuple(sorted(_EXPORTS))

# Explicit `X as X` re-exports, one statement per submodule.
# isort: off
if TYPE_CHECKING:
    from herness.core.jobs.arbiter import (
        ArbiterDecision as ArbiterDecision,
        arbiter_decide as arbiter_decide,
    )
    from herness.core.jobs.cron import CronExpr as CronExpr, resolve_local as resolve_local
    from herness.core.jobs.ports import (
        JobContext as JobContext,
        JobRow as JobRow,
        JobsBackend as JobsBackend,
        NewJob as NewJob,
        SchedCheck as SchedCheck,
        ServiceControl as ServiceControl,
        WorkerRow as WorkerRow,
        bind_jobs_backend as bind_jobs_backend,
    )
    from herness.core.jobs.validate import (
        validate_resilience_config as validate_resilience_config,
        validate_windows as validate_windows,
    )
    from herness.core.jobs.windows import (
        ActiveWindow as ActiveWindow,
        next_window_allowing as next_window_allowing,
        preempt_deadline as preempt_deadline,
        window_at as window_at,
    )
# isort: on


def __getattr__(name: str) -> object:
    """Import the owning submodule of `name` on first access (PEP 562)."""
    submodule = _EXPORTS.get(name)
    if submodule is None:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    value: object = getattr(importlib.import_module(f"{__name__}.{submodule}"), name)
    globals()[name] = value
    return value
