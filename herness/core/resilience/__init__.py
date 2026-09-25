"""Resilience API of impl 08 (delta D08-01), re-exported lazily (PEP 562) so importing
`herness.core.resilience.settings` does not import the rest of the package. Later 08 cards
add their names to `_EXPORTS` and the `TYPE_CHECKING` block.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Final

_EXPORTS: Final[dict[str, str]] = {
    "AsyncCompleter": "ports",
    "ChainRegistry": "ports",
    "ClientInfo": "ports",
    "DeciderLike": "ports",
    "EventRow": "ports",
    "GpuStateReader": "ports",
    "HealthRow": "ports",
    "ProcessState": "_state",
    "ResilienceBackend": "ports",
    "TracerLike": "ports",
    "bind_chain_registry": "_state",
    "bind_ops_backend": "_state",
    "process_state": "_state",
    "reset_process_state": "_state",
}

__all__: tuple[str, ...] = tuple(sorted(_EXPORTS))

# Explicit `X as X` re-exports, one statement per submodule.
# isort: off
if TYPE_CHECKING:
    from herness.core.resilience._state import (
        ProcessState as ProcessState,
        bind_chain_registry as bind_chain_registry,
        bind_ops_backend as bind_ops_backend,
        process_state as process_state,
        reset_process_state as reset_process_state,
    )
    from herness.core.resilience.ports import (
        AsyncCompleter as AsyncCompleter,
        ChainRegistry as ChainRegistry,
        ClientInfo as ClientInfo,
        DeciderLike as DeciderLike,
        EventRow as EventRow,
        GpuStateReader as GpuStateReader,
        HealthRow as HealthRow,
        ResilienceBackend as ResilienceBackend,
        TracerLike as TracerLike,
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
