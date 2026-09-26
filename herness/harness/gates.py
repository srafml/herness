"""Per-client LLM call gates and agent task slots (U06-29..U06-31; TH06-03, TH06-11).

Imports nothing from `herness.harness` except the settings leaf for the `ClientConfig`
type (D06-05). Gates and slots are async-safe within one event loop; their semaphores are
created lazily inside the running loop, so each `asyncio.run` gets fresh ones.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

from herness.core.errors import ConfigError

if TYPE_CHECKING:
    from herness.harness.llm.settings import ClientConfig

__all__ = ["CallGate", "TaskSlots", "build_gates"]

OnWait = Callable[[str, float], None]


class _LoopSemaphore:
    """An `asyncio.Semaphore(size)` rebuilt lazily for the running event loop."""

    def __init__(self, size: int) -> None:
        self._size = size
        self._sem: asyncio.Semaphore | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def get(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        if self._sem is None or self._loop is not loop:
            self._sem = asyncio.Semaphore(self._size)
            self._loop = loop
        return self._sem

    def release(self) -> None:
        if self._sem is not None:
            self._sem.release()


class CallGate:
    """Bounds concurrent LLM calls to one client; held by spec 05 `GatedClient` per call."""

    def __init__(self, client: str, size: int, *, on_wait: OnWait | None = None) -> None:
        if size < 1:
            msg = f"gate size must be >= 1: client={client}"
            raise ConfigError(msg)
        self.client = client
        self._size = size
        self._on_wait = on_wait
        self._sem = _LoopSemaphore(size)
        self._in_flight = 0
        self._max_in_flight = 0

    @property
    def size(self) -> int:
        """Maximum concurrent calls."""
        return self._size

    @property
    def in_flight(self) -> int:
        """Calls currently holding the gate."""
        return self._in_flight

    @property
    def max_in_flight(self) -> int:
        """High-water mark of `in_flight`."""
        return self._max_in_flight

    async def __aenter__(self) -> None:
        t0 = time.monotonic()
        await self._sem.get().acquire()
        self._in_flight += 1
        self._max_in_flight = max(self._max_in_flight, self._in_flight)
        if self._on_wait is not None:
            self._on_wait(self.client, time.monotonic() - t0)

    async def __aexit__(self, *exc: object) -> None:
        self._in_flight -= 1
        self._sem.release()


def build_gates(
    client_configs: Mapping[str, ClientConfig],
    *,
    mode: Literal["review", "chat"],
    on_wait: OnWait | None = None,
) -> dict[str, CallGate]:
    """One gate per client key, in sorted key order (U06-30, D06-09)."""
    gates: dict[str, CallGate] = {}
    for key in sorted(client_configs):
        cfg = client_configs[key]
        # Typed ClientConfig always has both; the defaults serve partial stand-ins (U06-30).
        max_conc = int(getattr(cfg, "max_concurrency", None) or 1)
        reserved = int(getattr(cfg, "chat_reserved_slots", None) or 0)
        if mode == "review":
            size = max(1, max_conc - reserved)
        else:
            size = reserved if reserved >= 1 else max_conc
        gates[key] = CallGate(key, size, on_wait=on_wait)
    return gates


class TaskSlots:
    """Bounds concurrently running agent tasks (U06-31)."""

    def __init__(self, size: int) -> None:
        if size < 1:
            msg = "task slots must be >= 1"
            raise ConfigError(msg)
        self._size = size
        self._sem = _LoopSemaphore(size)
        self._held = 0

    @classmethod
    def for_run(cls, analyst_max_concurrency: int, oversubscribe: float) -> TaskSlots:
        """Slots for one run: `max(1, ceil(analyst_max_concurrency * oversubscribe))`."""
        product = Decimal(analyst_max_concurrency) * Decimal(str(oversubscribe))
        return cls(max(1, math.ceil(product)))

    async def acquire(self) -> None:
        """Wait for a free slot and hold it."""
        await self._sem.get().acquire()
        self._held += 1

    def release(self) -> None:
        """Give back one held slot; releasing more than acquired is a `ConfigError`."""
        if self._held <= 0:
            msg = "slot released twice"
            raise ConfigError(msg)
        self._held -= 1
        self._sem.release()

    def free(self) -> int:
        """`size - held`."""
        return self._size - self._held
