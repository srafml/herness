"""Swarm run lifecycle (impl 06 §3): lazy §2 re-exports, so importing the package stays cheap.

`Swarm` and `review_job_handler` join with their cards (`run.py`, `handler.py`).
"""

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from herness.harness.swarm.lifecycle import RunRequest as RunRequest
    from herness.harness.swarm.lifecycle import RunResult as RunResult

__all__ = ["RunRequest", "RunResult"]


def __getattr__(name: str) -> object:
    if name in __all__:
        return getattr(import_module("herness.harness.swarm.lifecycle"), name)
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
