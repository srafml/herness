"""Review and chat pipelines (impl 06): lazy re-exports only (R-03, D06-34).

Importing this package imports nothing eagerly, so `herness.core.config` importing
`herness.harness.pipelines.settings` loads no other harness module. Each public name maps to its
owner module and is imported on first attribute access (module `__getattr__`, PEP 562). Later
cards add `Pipeline`, `PlanContext`, `get_pipeline` and `ChatService` to `_EXPORTS`.
"""

from importlib import import_module
from typing import Final

# public name -> owner module; empty until the owner modules exist (T06-03).
_EXPORTS: Final[dict[str, str]] = {}

__all__: tuple[str, ...] = tuple(_EXPORTS)


def __getattr__(name: str) -> object:
    """Import `name` from its owner module on first access (D06-34)."""
    owner = _EXPORTS.get(name)
    if owner is None:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    value = getattr(import_module(owner), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))
