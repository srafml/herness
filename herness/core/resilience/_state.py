"""Process state of impl 08 (U08-09; the only mutable module state, ENG §2.3 D08-19) and
the port bind functions (U08-10). Tests reset it; spawned children build their own.
"""

from __future__ import annotations

import asyncio
import atexit
import enum
import importlib
import random
import secrets
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Literal

from herness.core.errors import ConfigError
from herness.core.logging import get_logger

if TYPE_CHECKING:
    from herness.core.jobs.ports import JobContext, JobsBackend
    from herness.core.resilience.faults import FaultPlan
    from herness.core.resilience.policies import RetryPolicy
    from herness.core.resilience.ports import ChainRegistry, ResilienceBackend
    from herness.core.types import JobKind, JobOutcome

# Placeholder for an 08 class of a later card; it becomes a TYPE_CHECKING import then.
type CircuitBreaker = Any  # U08-24, T08-06 (herness.core.resilience.breaker.CircuitBreaker)

OPS_UNBOUND: Final = (
    "resilience backend not bound; call herness.store.ops.resilience.bind_core_backends()"
)
_log = get_logger("resilience")


class _NotLoaded(enum.Enum):
    TOKEN = 0  # the fault plan has not been read yet in this process


NOT_LOADED: Final = _NotLoaded.TOKEN


@dataclass
class ProcessState:
    """Per-process state of 08; every mutable attribute is guarded by `lock`."""

    ops: ResilienceBackend | None = None
    jobs: JobsBackend | None = None
    chains: ChainRegistry | None = None
    breakers: dict[str, CircuitBreaker] = field(default_factory=dict)
    probes: dict[str, Callable[[str], None]] = field(default_factory=dict)
    handlers: dict[JobKind, Callable[[JobContext], JobOutcome]] = field(default_factory=dict)
    fault_plan: FaultPlan | _NotLoaded | None = NOT_LOADED
    faults_enabled: bool = False
    # Placeholder: T08-05 (U08-19) replaces this with its `MetricBuffer()`.
    metric_buffer: object = field(default_factory=object)
    auth_dropped: set[tuple[str, str]] = field(default_factory=set)
    rng: random.Random = field(default_factory=secrets.SystemRandom)
    sleep: Callable[[float], None] = time.sleep
    asleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    kill_service_hook: Callable[[str], None] | None = None
    policies_cache: dict[str, RetryPolicy] = field(default_factory=dict)
    policies_hash: str | None = None  # the config_hash policies_cache was built for (U08-12)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


class _Holder:  # owns the singleton so that replacing it needs no `global` statement
    def __init__(self) -> None:
        self.state = ProcessState()
        self.swap = threading.Lock()
        self.atexit_registered = False


_holder: Final = _Holder()


def process_state() -> ProcessState:
    """Return this process's `ProcessState` singleton."""
    return _holder.state


def reset_process_state() -> ProcessState:
    """Replace the singleton with a fresh instance and return it (test fixture)."""
    with _holder.swap:
        _holder.state = ProcessState()
        return _holder.state


def _flush_metrics_at_exit() -> None:
    """Flush buffered metrics once at interpreter exit (U08-22)."""
    try:
        metrics = importlib.import_module("herness.core.resilience.metrics")
    except ModuleNotFoundError:  # the metrics module arrives with T08-05
        return
    metrics.flush_metrics()


def bind_port(port: Literal["ops", "jobs", "chains"], value: object) -> None:
    """Set one bound port under the lock; rebinding replaces it and logs at DEBUG."""
    if value is None:
        msg = f"{port} port must not be bound to None"
        raise ConfigError(msg)
    state = process_state()
    with state.lock:
        rebound = getattr(state, port) is not None
        setattr(state, port, value)
    _log.debug("resilience.backend.bound", port=port, rebound=rebound)


def bind_ops_backend(backend: ResilienceBackend) -> None:
    """Bind the resilience SQL port (U08-10); also registers the atexit metric flush once."""
    bind_port("ops", backend)
    with _holder.swap:
        if not _holder.atexit_registered:
            atexit.register(_flush_metrics_at_exit)
            _holder.atexit_registered = True


def bind_chain_registry(registry: ChainRegistry) -> None:
    """Bind the model chain registry (U08-10); rebinding replaces the previous registry."""
    bind_port("chains", registry)


def require_ops_backend() -> ResilienceBackend:
    """Return the bound resilience backend; unbound → `ConfigError` (U08-10 Errors)."""
    ops = process_state().ops
    if ops is None:
        raise ConfigError(OPS_UNBOUND)
    return ops


def require_chain_registry() -> ChainRegistry:
    """Return the bound chain registry; unbound → `ConfigError`."""
    chains = process_state().chains
    if chains is None:
        msg = "chain registry not bound; call herness.core.resilience.bind_chain_registry()"
        raise ConfigError(msg)
    return chains
