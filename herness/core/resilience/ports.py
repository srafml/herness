"""Protocols and row types the resilience code depends on (U08-08).

L0 code reaches L1 SQL and L4 registries only through these ports (ENG §2.1, §2.2; R-04).
Imports only `herness.core.types`, `herness.core.errors` and the standard library.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from herness.core.types import BreakerState, GpuClass, MetricSample, ServiceName

type JsonScalar = str | int | float | bool | None

# Placeholders for cross-spec types that do not exist in the tree yet. Each is replaced by
# an import from `herness.core.types` when its owner card lands (R-01); `Any` keeps the
# structural checks against the real implementations open until then.
type _LLMRequest = Any  # T05-01 (herness.core.types.LLMRequest)
type _LLMResponse = Any  # T05-01 (herness.core.types.LLMResponse)
type _DecisionInput = Any  # T03-01 (herness.core.types.DecisionInput)
type _QuestionSet = Any  # T03-01 (herness.core.types.QuestionSet)
type _DecisionOutput = Any  # T03-01 (herness.core.types.DecisionOutput)


@dataclass(frozen=True)
class HealthRow:
    """One `source_health` row (§4.1.3)."""

    source: str
    state: BreakerState
    failures: int
    trips: int
    opened_at: datetime | None
    last_error: str | None
    updated_at: datetime


@dataclass(frozen=True)
class EventRow:
    """One `resilience_event` row (§4.1.4)."""

    event_id: str
    ts: datetime
    kind: str
    component: Literal["resilience", "jobs"]
    target: str | None
    run_id: str | None
    job_id: str | None
    task_id: str | None
    detail: dict[str, JsonScalar | list[JsonScalar]]


class TracerLike(Protocol):
    """Structural subset of impl 05 `Tracer` (R-66); L0 code never imports the tracer."""

    @property
    def run_id(self) -> str | None: ...
    @property
    def task_id(self) -> str | None: ...
    def emit(self, type: str, **fields: object) -> None: ...  # noqa: A002 - spec 05 name


class ResilienceBackend(Protocol):
    """SQL port for `source_health`, `resilience_event` and metric rows (U08-94)."""

    def health_get(self, key: str) -> HealthRow | None: ...
    def health_list(self, states: Sequence[BreakerState]) -> list[HealthRow]: ...
    def health_apply(
        self,
        key: str,
        fn: Callable[[HealthRow | None], HealthRow | None],
        now: datetime,
    ) -> tuple[HealthRow | None, HealthRow | None]:
        """Read-modify-write in one `BEGIN IMMEDIATE`; `fn` returning None writes nothing."""
        ...

    def health_claim_probe(
        self, key: str, now: datetime, probe_due: datetime, stale_before: datetime
    ) -> bool: ...
    def health_reset(self, keys: Sequence[str], now: datetime) -> list[str]: ...
    def insert_event(self, row: EventRow) -> None: ...
    def count_events(self, kind: str, *, target: str | None = None, since: datetime) -> int: ...
    def event_counts(self, since: datetime, kinds: Sequence[str]) -> dict[str, int]: ...
    def latest_event(self, kind: str) -> EventRow | None: ...
    def insert_metric_samples(self, rows: Sequence[MetricSample]) -> int: ...
    def purge_events(self, before: datetime) -> int: ...
    def purge_metric_samples(self, before: datetime) -> int: ...


class ClientInfo(Protocol):
    """The registry facts a model chain needs about one client."""

    @property
    def off_network(self) -> bool: ...
    @property
    def gpu_class(self) -> str | None: ...
    @property
    def timeout_s(self) -> float: ...
    @property
    def model(self) -> str: ...
    @property
    def base_url(self) -> str | None: ...
    @property
    def api_key(self) -> str | None: ...


class ChainRegistry(Protocol):
    """Model chain lookup; spec 05 `LLMRegistry` satisfies it structurally."""

    def chain_for(self, model_role: str, depth: str) -> list[str]: ...
    def config(self, name: str) -> ClientInfo: ...


class GpuStateReader(Protocol):
    """Read-only view of the worker's GPU state (design 08 §3.5)."""

    def loaded_class(self) -> GpuClass | Literal["swapping"]: ...
    def service_healthy(self, name: ServiceName) -> bool: ...


class AsyncCompleter(Protocol):
    """Structural subset of spec 05 `LLMClient`."""

    @property
    def name(self) -> str: ...
    async def acomplete(self, req: _LLMRequest) -> _LLMResponse: ...


class DeciderLike(Protocol):
    """Structural subset of spec 03 `Decider`."""

    @property
    def name(self) -> str: ...
    def decide(
        self, items: Sequence[_DecisionInput], questions: _QuestionSet
    ) -> list[_DecisionOutput]: ...
    def health(self) -> None: ...
