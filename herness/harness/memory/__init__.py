"""Memory (impl 07): the `MemoryStore` facade, its process accessor and composition root.

Importing the package is cheap and cycle-free: `herness.core.config` imports
`herness.harness.memory.settings` (ENG §2.1 settings exception), which runs this module first,
so nothing from `herness` is imported here at module level. `MemoryStore` and `HealthResult`
live in the private sibling `_facade` and are re-exported lazily (PEP 562, as in
`herness.core.jobs`); the composition imports its collaborators inside the functions
(T07-23 ruling, impl 07 §2). The cached store is the only module-level state (U07-98);
`_reset_memory_store` is the hook of the `reset_harness_state` test fixture.
"""

from __future__ import annotations

import importlib
import sys
import threading
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from herness.core.jobs.handlers import Handler
    from herness.core.types import JobKind
    from herness.harness.memory._facade import HealthResult as HealthResult
    from herness.harness.memory._facade import MemoryStore as MemoryStore
    from herness.harness.tools import ToolRegistry

__all__ = [
    "HealthResult", "MemoryStore", "get_memory_store", "register_memory_components",
]  # fmt: skip

_EXPORTS: Final[dict[str, str]] = {"HealthResult": "_facade", "MemoryStore": "_facade"}
# The handler modules whose one-argument seams `register_memory_components` configures.
_SEAMS: Final = {
    "herness.harness.memory.outcome": "configure_outcome",
    "herness.harness.memory.maintenance": "configure_maintenance",
}
_LOCK: Final = threading.Lock()


class _State:
    store: MemoryStore | None = None


def __getattr__(name: str) -> object:
    """Import the owning submodule of `name` on first access (PEP 562)."""
    submodule = _EXPORTS.get(name)
    if submodule is None:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    value: object = getattr(importlib.import_module(f"{__name__}.{submodule}"), name)
    globals()[name] = value
    return value


def get_memory_store() -> MemoryStore:
    """The process `MemoryStore`, built once from `get_config()` under a lock (U07-98).

    Called by `herness.cli` and `app/common/` only (ENG §2.2). Raises `ConfigError`."""
    with _LOCK:
        if _State.store is None:
            from herness.core.config import get_config  # noqa: PLC0415 - cheap package import
            from herness.harness.memory._facade import MemoryStore  # noqa: PLC0415 - idem

            _State.store = MemoryStore.from_config(get_config())
        return _State.store


def register_memory_components(tool_registry: ToolRegistry, store: MemoryStore) -> None:
    """Register both memory tools (U07-65) and the `outcome_measure` (U07-86) and
    `memory_maintenance` (U07-96) job handlers (T08-12), and point the handlers' one-argument
    seams at `store`'s collaborators (T07-18, T07-22 notes). Calling it again is a no-op
    except that the seams follow the latest `store`. A different handler already registered
    for either kind is a `ConfigError` raised before anything is registered or configured."""
    from herness.core.errors import ConfigError  # noqa: PLC0415 - cheap module
    from herness.core.jobs.handlers import register_handler, resolve_handler  # noqa: PLC0415
    from herness.harness.memory import maintenance, outcome  # noqa: PLC0415 - idem
    from herness.harness.memory.tools import register_memory_tools  # noqa: PLC0415 - idem

    handlers: tuple[tuple[JobKind, Handler], ...] = (
        ("outcome_measure", outcome.outcome_measure_handler),
        ("memory_maintenance", maintenance.memory_maintenance_handler),
    )
    for kind, handler in handlers:  # check both first: a conflict changes nothing
        try:
            current = resolve_handler(kind)
        except ConfigError:  # none registered yet
            continue
        if current is not handler:
            msg = f"handler already registered for {kind}"
            raise ConfigError(msg)
    for kind, handler in handlers:
        register_handler(kind, handler)
    register_memory_tools(tool_registry, store)
    outcome.configure_outcome(store.outcome_deps)
    maintenance.configure_maintenance(store.maintenance_deps)


def _reset_memory_store() -> None:
    """Test hook: drop the cached store and clear the handler seams of loaded modules."""
    with _LOCK:
        _State.store = None
    for name, configure in _SEAMS.items():
        if (module := sys.modules.get(name)) is not None:  # never imported: nothing to clear
            getattr(module, configure)(None)
