"""Swarm run lifecycle (impl 06 §3): lazy §2 re-exports, so importing the package stays cheap."""

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from herness.harness.swarm.handler import review_job_handler as review_job_handler
    from herness.harness.swarm.lifecycle import RunRequest as RunRequest
    from herness.harness.swarm.lifecycle import RunResult as RunResult
    from herness.harness.swarm.run import Swarm as Swarm

__all__ = ["RunRequest", "RunResult", "Swarm", "review_job_handler"]


def __getattr__(name: str) -> object:
    if name in __all__:
        owner = {"RunRequest": "lifecycle", "RunResult": "lifecycle", "Swarm": "run"}
        return getattr(import_module(f"{__name__}.{owner.get(name, 'handler')}"), name)
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
