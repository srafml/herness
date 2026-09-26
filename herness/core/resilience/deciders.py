"""Decider batch fallback chain (impl 08 U08-39; design 08 §5.5; T08-10).

`DeciderChain.decide` walks a fixed decider order, skipping entries whose GPU class or
egress prerequisite is not met, retrying each available entry under its own policy and
breaker (`decider_local` or `decider_cloud`), and falling back to the next entry on
`ModelUnavailable` or `CircuitOpen`. A crash inside a decider's `decide()` is converted to
`ModelUnavailable` here, so a caller never sees a foreign exception from this chain (impl 03
T03-14 parked finding M5). The constructor does no I/O; `order` and `gpu` never change.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Final

from herness.core import registry
from herness.core.config import get_config
from herness.core.errors import CircuitOpen, ConfigError, HernessError, ModelUnavailable
from herness.core.logging import get_logger
from herness.core.resilience.faults import fault_point
from herness.core.resilience.ports import DeciderLike, GpuStateReader
from herness.core.resilience.retry import retry_call
from herness.core.types import DecisionInput, DecisionOutput, PolicyName, QuestionSet

__all__ = ("DeciderChain",)

_log: Final = get_logger("resilience")


def _default_resolve(name: str) -> DeciderLike:
    factory: Callable[[], DeciderLike] = registry.get("decider", name)
    return factory()


def _call_entry(
    name: str,
    items: Sequence[DecisionInput],
    questions: QuestionSet,
    resolve: Callable[[str], DeciderLike],
) -> list[DecisionOutput]:
    """One decider's attempt (U08-39 step 3): the fault hook, then `decide`.

    Any exception that is not already a `HernessError` is a decider crash, converted to
    `ModelUnavailable` so retry and fallback see only resilience errors here.
    """
    fault_point("decider.batch", model=name)
    try:
        return resolve(name).decide(items, questions)
    except HernessError:
        raise
    except Exception as exc:
        msg = f"decider crash: {type(exc).__name__}"
        raise ModelUnavailable(msg) from exc


class DeciderChain:
    """Move a decision batch down `order` on failure (U08-39, design 08 §5.5)."""

    __slots__ = ("_gpu", "_order", "_resolve")

    def __init__(
        self,
        order: Sequence[str],
        *,
        gpu: GpuStateReader,
        resolve: Callable[[str], DeciderLike] | None = None,
    ) -> None:
        if not order:
            msg = "DeciderChain order must not be empty"
            raise ConfigError(msg)
        self._order = tuple(order)
        self._gpu = gpu
        self._resolve = resolve if resolve is not None else _default_resolve

    @property
    def order(self) -> tuple[str, ...]:
        """The decider order this chain walks, in priority order."""
        return self._order

    @property
    def gpu(self) -> GpuStateReader:
        """The GPU state reader gating `openjev` and `llm`."""
        return self._gpu

    def _available(self, name: str, *, egress: bool, loaded: str) -> bool:
        """U08-39 step 2: availability of one decider entry."""
        if name == "openjev":
            return loaded == "decider" and self._gpu.service_healthy("openjev")
        if name == "jev":
            return egress
        if name == "llm":
            return loaded == "reasoning" and self._gpu.service_healthy("vllm-reasoning")
        return True  # laya and any other name: always available

    def decide(
        self, items: Sequence[DecisionInput], questions: QuestionSet
    ) -> tuple[list[DecisionOutput], list[DecisionInput]]:
        """Try each decider of `order` in turn; return `(decided, deferred)` (U08-39)."""
        remaining = list(items)
        egress = get_config().security.egress.enabled
        loaded = self._gpu.loaded_class()
        for name in self._order:
            if not self._available(name, egress=egress, loaded=loaded):
                _log.debug("resilience.decider.skipped", decider=name)
                continue
            policy_name: PolicyName = "decider_cloud" if name == "jev" else "decider_local"
            try:
                out = retry_call(
                    policy_name,
                    _call_entry,
                    name,
                    remaining,
                    questions,
                    self._resolve,
                    breaker_key="decider:" + name,
                )
            except (ModelUnavailable, CircuitOpen) as exc:
                reason = "unavailable" if isinstance(exc, ModelUnavailable) else "circuit_open"
                _log.warning("resilience.decider.fallback", **{"from": name, "reason": reason})
                continue
            return out, []
        return [], remaining
