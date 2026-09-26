"""Resilience API of impl 08 (delta D08-01), re-exported lazily (PEP 562) so importing
`herness.core.resilience.settings` does not import the rest of the package. Later 08 cards
add their names to `_EXPORTS` and the `TYPE_CHECKING` block.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Final

_EXPORTS: Final[dict[str, str]] = {
    "AsyncCompleter": "ports",
    "CircuitBreaker": "breaker",
    "ChainRegistry": "ports",
    "ClientInfo": "ports",
    "DeciderLike": "ports",
    "EventRow": "ports",
    "FaultRule": "faults",
    "GpuStateReader": "ports",
    "HealthRow": "ports",
    "NAMED_POINTS": "faults",
    "ProcessState": "_state",
    "ResilienceBackend": "ports",
    "RetryPolicy": "policies",
    "TracerLike": "ports",
    "bind_chain_registry": "_state",
    "breaker": "breaker",
    "bind_ops_backend": "_state",
    "classify": "classify",
    "fault_point": "faults",
    "guard": "breaker",
    "load_fault_plan": "faults",
    "policy": "policies",
    "process_state": "_state",
    "register_probe": "breaker",
    "reset_process_state": "_state",
    "run_due_probes": "breaker",
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
    from herness.core.resilience.breaker import (
        CircuitBreaker as CircuitBreaker,
        breaker as breaker,
        guard as guard,
        register_probe as register_probe,
        run_due_probes as run_due_probes,
    )
    from herness.core.resilience.classify import classify as classify
    from herness.core.resilience.faults import (
        NAMED_POINTS as NAMED_POINTS,
        FaultRule as FaultRule,
        fault_point as fault_point,
        load_fault_plan as load_fault_plan,
    )
    from herness.core.resilience.policies import RetryPolicy as RetryPolicy, policy as policy
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
    module = importlib.import_module(f"{__name__}.{submodule}")
    # `classify` and `breaker` name their (callable) submodules too: always hand out the
    # module, never the function, so `import herness.core.resilience.<name> as m` works.
    value: object = module if name == submodule else getattr(module, name)
    globals()[name] = value
    return value
