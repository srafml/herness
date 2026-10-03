"""Export map of `herness.core.resilience` (private; T08-23, impl 08 §2 module-map note).

`EXPORTS` maps every public name of the §2 rows of the API submodules (§3.3 to §3.9: everything
but `settings`, R-03) to its owning submodule; the package `__getattr__` imports that
submodule on first access (PEP 562). Kept apart from `__init__.py` for its line budget. This
module imports nothing at run time; the `TYPE_CHECKING` block gives type checkers the
re-exported names (the package star-imports it under `TYPE_CHECKING` only).
"""

# ruff: noqa: PLC0414 - explicit `X as X` re-exports are this module's purpose (PEP 484)

from __future__ import annotations

from typing import TYPE_CHECKING, Final

EXPORTS: Final[dict[str, str]] = {
    "AsyncCompleter": "ports",
    "ChainRegistry": "ports",
    "CircuitBreaker": "breaker",
    "ClientInfo": "ports",
    "DeciderChain": "deciders",
    "DeciderLike": "ports",
    "EVENT_KINDS": "events",
    "EventRow": "ports",
    "FaultRule": "faults",
    "FullJitterRetryAfter": "policies",
    "GPU_HEALTH_POLICY": "policies",
    "GpuStateReader": "ports",
    "HealthRow": "ports",
    "ModelChain": "chain",
    "NAMED_POINTS": "faults",
    "POLICY_FAMILY": "policies",
    "ProcessState": "_state",
    "ResilienceBackend": "ports",
    "RetryPolicy": "policies",
    "TracerLike": "ports",
    "aretry_call": "retry",
    "bind_chain_registry": "_state",
    "bind_ops_backend": "_state",
    "breaker": "breaker",
    "breaker_transition": "breaker",
    "build_repair_request": "chain",
    "call_with_timeout": "retry",
    "classify": "classify",
    "complete_validated": "chain",
    "fault_point": "faults",
    "flush_metrics": "metrics",
    "full_jitter_delay": "policies",
    "guard": "breaker",
    "job_backoff_delay": "policies",
    "load_fault_plan": "faults",
    "loop_signal_policy": "loop_policy",
    "parse_retry_after": "classify",
    "policy": "policies",
    "policy_for_client": "policies",
    "probe_due": "breaker",
    "process_state": "_state",
    "record_counter": "metrics",
    "record_event": "events",
    "record_gauge": "metrics",
    "record_histogram": "metrics",
    "register_probe": "breaker",
    "reset_process_state": "_state",
    "retry_call": "retry",
    "retry_page": "retry",
    "retrying": "retry",
    "run_due_probes": "breaker",
    "timed": "metrics",
}

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
        breaker_transition as breaker_transition,
        guard as guard,
        probe_due as probe_due,
        register_probe as register_probe,
        run_due_probes as run_due_probes,
    )
    from herness.core.resilience.chain import (
        ModelChain as ModelChain,
        build_repair_request as build_repair_request,
        complete_validated as complete_validated,
    )
    from herness.core.resilience.classify import (
        classify as classify,
        parse_retry_after as parse_retry_after,
    )
    from herness.core.resilience.deciders import DeciderChain as DeciderChain
    from herness.core.resilience.events import (
        EVENT_KINDS as EVENT_KINDS,
        record_event as record_event,
    )
    from herness.core.resilience.faults import (
        NAMED_POINTS as NAMED_POINTS,
        FaultRule as FaultRule,
        fault_point as fault_point,
        load_fault_plan as load_fault_plan,
    )
    from herness.core.resilience.loop_policy import loop_signal_policy as loop_signal_policy
    from herness.core.resilience.metrics import (
        flush_metrics as flush_metrics,
        record_counter as record_counter,
        record_gauge as record_gauge,
        record_histogram as record_histogram,
        timed as timed,
    )
    from herness.core.resilience.policies import (
        GPU_HEALTH_POLICY as GPU_HEALTH_POLICY,
        POLICY_FAMILY as POLICY_FAMILY,
        FullJitterRetryAfter as FullJitterRetryAfter,
        RetryPolicy as RetryPolicy,
        full_jitter_delay as full_jitter_delay,
        job_backoff_delay as job_backoff_delay,
        policy as policy,
        policy_for_client as policy_for_client,
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
    from herness.core.resilience.retry import (
        aretry_call as aretry_call,
        call_with_timeout as call_with_timeout,
        retry_call as retry_call,
        retry_page as retry_page,
        retrying as retrying,
    )
# isort: on
