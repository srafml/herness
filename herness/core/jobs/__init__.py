"""Jobs API of impl 08 (design 08 §3, §5; delta D08-01).

Names are re-exported lazily (PEP 562): the owning submodule is imported on first access.
`JobContext` and `ServiceControl` live here, not in `herness.core.types` (R-02).
Later 08 cards add their public names to `_EXPORTS` and the `TYPE_CHECKING` block.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Final

_EXPORTS: Final[dict[str, str]] = {
    "CronExpr": "cron",
    "JobContext": "ports",
    "JobRow": "ports",
    "JobsBackend": "ports",
    "NewJob": "ports",
    "SchedCheck": "ports",
    "ServiceControl": "ports",
    "WorkerRow": "ports",
    "bind_jobs_backend": "ports",
    "resolve_local": "cron",
    "validate_resilience_config": "validate",
    "validate_windows": "validate",
}

__all__: tuple[str, ...] = tuple(sorted(_EXPORTS))

# Explicit `X as X` re-exports, one statement per submodule.
# isort: off
if TYPE_CHECKING:
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
