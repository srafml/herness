"""Measurement helpers of the swarm benchmarks (impl 06 §10, T06-28; BT06-01 … BT06-10).

`Overhead` (BT06-01) wraps, test-side and with `time.perf_counter`, the landed scheduler
bookkeeping (`PhaseRunner._pass`, the `count_open` re-check of `run_phase`, and
`execute_task` minus the agent call of the task) and every Blackboard operation (the public
`Blackboard` methods, which the swarm tools `post_finding` / `list_findings` call, and the
ops functions `insert_finding`, `query_findings`, `transition_finding` wherever a
`herness` module bound them). Each call is one wall-clock interval; `union_s` is the length
of their union (time during which any scheduler or Blackboard work was in progress, nested
and concurrent calls counted once) and `sum_s` the plain sum (an upper bound).

`SlowTaskClient` is the T06-26 `TaskClient` (a `FakeLLMClient` on per-task scripts) that
awaits `LATENCY_S` before every answer: the 12 s per call of the BT06-01 row scaled x0.01.

`full_gate` is the skip mark of the rows owed on the target PC: they run only with
`HERNESS_BENCH_FULL=1` and every service variable the row names set.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import inspect
import os
import sys
import threading
import time
from collections.abc import Callable, Coroutine
from typing import Any, Final

import pytest
from tests.support import _f06_swarm as f6

from herness.core.types import LLMRequest, LLMResponse
from herness.harness.blackboard import Blackboard
from herness.harness.swarm import challenge, planner, scheduler, writer
from herness.store.ops import findings as ops_findings

__all__ = [
    "FULL_ENV",
    "GPU_ENV",
    "LATENCY_S",
    "PREMIUM_ENV",
    "PROFILE_ENV",
    "SYNTH_ENV",
    "TAGS",
    "Overhead",
    "SlowTaskClient",
    "full_gate",
    "use_slow_clients",
]

LATENCY_S: Final = 12.0 * 0.01  # 12 s per call scaled x0.01
FULL_ENV: Final = "HERNESS_BENCH_FULL"
PROFILE_ENV: Final = "HERNESS_BENCH_LLM_PROFILE"  # a profile with real local-30b serving
GPU_ENV: Final = "HERNESS_BENCH_GPU"  # "1": one 24 GB GPU serving the local models
PREMIUM_ENV: Final = "HERNESS_BENCH_PREMIUM"  # "1": premium approved, API key in the keyring
SYNTH_ENV: Final = "HERNESS_SYNTH_FULL_ROOT"  # seed 42 full (5M) synthetic build root
_FLAG_ENVS: Final = frozenset({FULL_ENV, GPU_ENV, PREMIUM_ENV})
_OPS_FUNCS: Final = ("insert_finding", "query_findings", "transition_finding")
_BB_SKIP: Final = frozenset({"close"})
TAGS: Final = ("pass", "execute_task", "blackboard")  # scheduler pass, task bookkeeping, board


def full_gate(*envs: str) -> pytest.MarkDecorator:
    """Skip unless `HERNESS_BENCH_FULL=1` and each of `envs` is set (flags must be "1")."""
    names = (FULL_ENV, *envs)
    ok = all(
        os.environ.get(n) == "1" if n in _FLAG_ENVS else bool(os.environ.get(n)) for n in names
    )
    need = ", ".join(f"{n}=1" if n in _FLAG_ENVS else n for n in names)
    return pytest.mark.skipif(not ok, reason=f"owed on the target PC: needs {need}")


class SlowTaskClient(f6.TaskClient):
    """`TaskClient` that waits `LATENCY_S` before each answer (the scaled fake latency)."""

    async def acomplete(self, req: LLMRequest) -> LLMResponse:
        await asyncio.sleep(LATENCY_S)
        return await super().acomplete(req)


def use_slow_clients(registry: Any, scripts: f6.TaskScripts, mp: pytest.MonkeyPatch) -> None:
    """Route every client key of `registry` to one `SlowTaskClient` per key."""
    clients: dict[str, SlowTaskClient] = {}

    def client(name: str) -> SlowTaskClient:
        if name not in clients:
            clients[name] = SlowTaskClient(scripts, name)
        return clients[name]

    mp.setattr(registry, "client", client)


_AGENT: contextvars.ContextVar[list[tuple[float, float]] | None] = contextvars.ContextVar(
    "bt06_01_agent", default=None
)


class Overhead:
    """Interval recorder for the scheduler and Blackboard time of one run (see module doc)."""

    def __init__(self) -> None:
        self.intervals: list[tuple[float, float, str]] = []
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self.intervals.clear()

    def add(self, start: float, end: float, tag: str) -> None:
        if end > start:
            with self._lock:
                self.intervals.append((start, end, tag))

    def sum_s(self) -> float:
        return sum(end - start for start, end, _ in self.intervals)

    def union_s(self, tag: str | None = None) -> float:
        """Length of the union of the intervals (of `tag` only when given)."""
        spans = sorted((a, b) for a, b, t in self.intervals if tag is None or t == tag)
        total, cur_start, cur_end = 0.0, None, 0.0
        for start, end in spans:
            if cur_start is None or start > cur_end:
                if cur_start is not None:
                    total += cur_end - cur_start
                cur_start, cur_end = start, end
            else:
                cur_end = max(cur_end, end)
        return total + (cur_end - cur_start if cur_start is not None else 0.0)

    # --- wrapping ------------------------------------------------------------------------------

    def _timed(self, fn: Callable[..., Any], tag: str) -> Callable[..., Any]:
        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def run_async(*args: Any, **kwargs: Any) -> Any:
                start = time.perf_counter()
                try:
                    return await fn(*args, **kwargs)
                finally:
                    self.add(start, time.perf_counter(), tag)

            return run_async

        @functools.wraps(fn)
        def run_sync(*args: Any, **kwargs: Any) -> Any:
            start = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                self.add(start, time.perf_counter(), tag)

        return run_sync

    def _execute(self, fn: Callable[..., Coroutine[Any, Any, None]]) -> Callable[..., Any]:
        """`execute_task` timed minus the agent intervals recorded inside it."""

        @functools.wraps(fn)
        async def run(*args: Any, **kwargs: Any) -> None:
            agent: list[tuple[float, float]] = []
            token = _AGENT.set(agent)
            start = time.perf_counter()
            try:
                await fn(*args, **kwargs)
            finally:
                end = time.perf_counter()
                _AGENT.reset(token)
                cursor = start
                for a_start, a_end in sorted(agent):
                    self.add(cursor, a_start, "execute_task")
                    cursor = max(cursor, a_end)
                self.add(cursor, end, "execute_task")

        return run

    @staticmethod
    def _agent(fn: Callable[..., Coroutine[Any, Any, Any]]) -> Callable[..., Any]:
        @functools.wraps(fn)
        async def run(*args: Any, **kwargs: Any) -> Any:
            start = time.perf_counter()
            try:
                return await fn(*args, **kwargs)
            finally:
                spans = _AGENT.get()
                if spans is not None:
                    spans.append((start, time.perf_counter()))

        return run

    def install(self, mp: pytest.MonkeyPatch) -> None:
        """Wrap the scheduler, the agent calls and the Blackboard (after `use_real_agents`)."""
        mp.setattr(scheduler.PhaseRunner, "_pass", self._timed(scheduler.PhaseRunner._pass, "pass"))
        mp.setattr(scheduler, "count_open", self._timed(scheduler.count_open, "pass"))
        mp.setattr(scheduler, "execute_task", self._execute(scheduler.execute_task))
        for module in (planner, scheduler, challenge, writer):
            mp.setattr(module, "run_agent", self._agent(module.run_agent))
        for name, member in vars(Blackboard).items():
            if name.startswith("_") or name in _BB_SKIP or isinstance(member, staticmethod):
                continue
            if callable(member):
                mp.setattr(Blackboard, name, self._timed(member, "blackboard"))
        for fname in _OPS_FUNCS:
            original = getattr(ops_findings, fname)
            timed = self._timed(original, "blackboard")
            for module in list(sys.modules.values()):
                name = getattr(module, "__name__", "")
                if name.startswith("herness") and getattr(module, fname, None) is original:
                    mp.setattr(module, fname, timed)
