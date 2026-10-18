"""Measurement helpers of the swarm benchmarks (impl 06 §10, T06-28; BT06-01 … BT06-10).

`Overhead` (BT06-01) wraps, test-side and with `time.perf_counter`, the landed scheduler
bookkeeping (`PhaseRunner._pass`, the `count_open` re-check of `run_phase`, and
`execute_task` minus the agent call of the task) and every Blackboard operation (the public
`Blackboard` methods, which the swarm tools `post_finding` / `list_findings` call, and the
ops functions `insert_finding`, `query_findings`, `transition_finding` wherever a
`herness` module bound them). Each call is one wall-clock interval; `union_s` is the length
of their union (time during which any scheduler or Blackboard work was in progress, nested
and concurrent calls counted once) and `sum_s` the plain sum (an upper bound); both are info
lines only (T06-30): an awaited span stays open while other tasks' agent loops run.

The GATE statistic of BT06-01 is the EXCLUSIVE busy time (`busy_s`, T06-30): per frame (an
event-loop step of a wrapped coroutine, or one on-thread call of the sync work of the
Blackboard and the scheduler, the writer-side functions included) the wall time not spent in
nested frames, summed over the tags `TAGS`. The frames tagged `agent` (the `run_agent` calls)
and `queue` (the blocking wait for the writer in `Blackboard._sync`) are not counted, and
neither is time suspended awaiting a thread, the writer or the agent. `cpu_s` is the same
sum on `time.thread_time` (no GIL or I/O wait), a cross-check.

`SlowTaskClient` is the T06-26 `TaskClient` (a `FakeLLMClient` on per-task scripts) that
awaits `LATENCY_S` before every answer: the 12 s per call of the BT06-01 row scaled x0.01.

`full_gate` is the skip mark of the rows owed on the target PC: they run only with
`HERNESS_BENCH_FULL=1` and every service variable the row names set.
"""

from __future__ import annotations

import asyncio
import contextvars
import ctypes
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
_PARTS: Final = (  # Blackboard.post / _commit steps timed by name (a finer split of "blackboard")
    "evidence_build_id",
    "get_task",
    "list_task_findings",
    "save_checkpoint",
    "get_run",
    "validate_markers",
    "find_uncited",
)
_OPS_FUNCS: Final = ("insert_finding", "query_findings", "transition_finding")
_BB_SKIP: Final = frozenset({"close", "run_on_writer"})  # run_on_writer: wrapped by hand
_WRITER_TAGS: Final = {  # writer-side functions by tag; any other writer function: "blackboard"
    "claim_task": "pass",
    "complete_task": "execute_task",
    "fail_task": "execute_task",
    "release_task": "execute_task",
}
TAGS: Final = ("pass", "execute_task", "blackboard")  # scheduler pass, task bookkeeping, board


def full_gate(*envs: str) -> pytest.MarkDecorator:
    """Skip unless `HERNESS_BENCH_FULL=1` and each of `envs` is set (flags must be "1")."""
    names = (FULL_ENV, *envs)
    ok = all(
        os.environ.get(n) == "1" if n in _FLAG_ENVS else bool(os.environ.get(n)) for n in names
    )
    need = ", ".join(f"{n}=1" if n in _FLAG_ENVS else n for n in names)
    reason = (
        f"owed on the target PC: needs {need}; the process herness config must name the 5M"
        " build (paths.data) and the profile (the variables are the documented switches)"
    )
    return pytest.mark.skipif(not ok, reason=reason)


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


def _thread_cpu() -> float:
    """CPU seconds of the calling thread; Windows `thread_time` ticks at 15.6 ms, so there the
    cycle counter is read and scaled by the cycles-per-second measured once on a busy spin."""
    return _cycles() / _CYCLES_PER_S if _CYCLES_PER_S else time.thread_time()


def _cycle_counter() -> Callable[[], int]:
    if sys.platform != "win32":
        return lambda: 0
    kernel32: Any = ctypes.windll.kernel32  # type: ignore[attr-defined,unused-ignore]
    thread = kernel32.GetCurrentThread
    thread.restype = ctypes.c_void_p
    query = kernel32.QueryThreadCycleTime
    query.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulonglong)]
    handle = thread()  # the pseudo handle of the calling thread, valid on every thread

    def cycles() -> int:
        count = ctypes.c_ulonglong()
        query(handle, ctypes.byref(count))
        return int(count.value)

    return cycles


_cycles = _cycle_counter()


def _calibrate() -> float:
    if sys.platform != "win32":
        return 0.0
    c0, t0 = _cycles(), time.perf_counter()
    while time.perf_counter() - t0 < 0.2:
        pass
    return (_cycles() - c0) / (time.perf_counter() - t0)


_CYCLES_PER_S: float = _calibrate()

_AGENT: contextvars.ContextVar[list[tuple[float, float]] | None] = contextvars.ContextVar(
    "bt06_01_agent", default=None
)


class _Steps:
    """Awaitable over `coro` that times each event-loop step as one frame of `rec` (T06-30)."""

    def __init__(self, coro: Any, rec: Overhead, tag: str, name: str) -> None:
        self._coro, self._rec, self._tag, self._name = coro, rec, tag, name
        rec.count(name)

    def __await__(self) -> _Steps:
        return self

    def __iter__(self) -> _Steps:
        return self

    def __next__(self) -> Any:
        return self.send(None)

    def send(self, value: Any) -> Any:
        return self._run(self._coro.send, value)

    def throw(self, *exc: Any) -> Any:
        return self._run(self._coro.throw, *exc)

    def close(self) -> None:
        self._coro.close()

    def _run(self, call: Callable[..., Any], *args: Any) -> Any:
        frame = self._rec.enter(self._tag, self._name)
        try:
            return call(*args)
        finally:
            self._rec.leave(frame, calls=0)


class Overhead:
    """Recorder for the scheduler and Blackboard time of one run (see module doc)."""

    def __init__(self) -> None:
        self.intervals: list[tuple[float, float, str]] = []
        self.wall: dict[str, float] = {}  # exclusive wall seconds by tag
        self.cpu: dict[str, float] = {}  # exclusive thread-CPU seconds by tag
        self.names: dict[str, list[float]] = {}  # name -> [calls, exclusive wall, exclusive cpu]
        self._lock = threading.Lock()
        self._tls = threading.local()

    def reset(self) -> None:
        with self._lock:
            self.intervals.clear()
            self.wall.clear()
            self.cpu.clear()
            self.names.clear()

    # --- exclusive frames ----------------------------------------------------------------------

    def count(self, name: str) -> None:
        with self._lock:
            self.names.setdefault(name, [0.0, 0.0, 0.0])[0] += 1

    def enter(self, tag: str, name: str) -> list[Any]:
        stack: list[list[Any]] = self._tls.__dict__.setdefault("stack", [])
        frame = [tag, name, time.perf_counter(), _thread_cpu(), 0.0, 0.0]
        stack.append(frame)
        return frame

    def leave(self, frame: list[Any], calls: int = 1) -> None:
        wall, cpu = time.perf_counter() - frame[2], _thread_cpu() - frame[3]
        stack: list[list[Any]] = self._tls.stack
        stack.pop()
        if stack:  # the parent's exclusive time excludes this frame, counted or not
            stack[-1][4] += wall
            stack[-1][5] += cpu
        tag, name = frame[0], frame[1]
        with self._lock:
            self.wall[tag] = self.wall.get(tag, 0.0) + wall - frame[4]
            self.cpu[tag] = self.cpu.get(tag, 0.0) + cpu - frame[5]
            row = self.names.setdefault(name, [0.0, 0.0, 0.0])
            row[0] += calls
            row[1] += wall - frame[4]
            row[2] += cpu - frame[5]

    def busy_s(self, tag: str | None = None) -> float:
        """Exclusive busy wall seconds of `tag`, or of the gated `TAGS`."""
        return sum(self.wall.get(t, 0.0) for t in ((tag,) if tag else TAGS))

    def cpu_s(self, tag: str | None = None) -> float:
        """Exclusive thread-CPU seconds of `tag`, or of the gated `TAGS`."""
        return sum(self.cpu.get(t, 0.0) for t in ((tag,) if tag else TAGS))

    def frame_cost_s(self) -> float:
        """Thread-CPU seconds one empty frame costs the recorder itself (calibration)."""
        probe = self._frame(lambda: None, "probe", "probe")
        c0 = _thread_cpu()
        for _ in range(2000):
            probe()
        cost = (_thread_cpu() - c0) / 2000
        self.wall.pop("probe", None), self.cpu.pop("probe", None), self.names.pop("probe", None)
        return cost

    def frames(self) -> int:
        """Frames recorded since `reset` (calls of the named wrappers plus coroutine steps)."""
        return int(sum(row[0] for row in self.names.values()))

    def _frame(
        self, fn: Callable[..., Any], tag: str, name: str | None = None
    ) -> Callable[..., Any]:
        label: str = name or str(getattr(fn, "__qualname__", "fn"))

        @functools.wraps(fn)
        def run(*args: Any, **kwargs: Any) -> Any:
            frame = self.enter(tag, label)
            try:
                return fn(*args, **kwargs)
            finally:
                self.leave(frame)

        return run

    def _steps(self, fn: Callable[..., Any], tag: str) -> Callable[..., Any]:
        @functools.wraps(fn)
        async def run(*args: Any, **kwargs: Any) -> Any:
            return await _Steps(fn(*args, **kwargs), self, tag, fn.__qualname__)

        return run

    def _on_writer(self, fn: Callable[[], Any]) -> Callable[[], Any]:
        """`fn` as a frame on the writer thread, tagged by the function it runs."""
        name = getattr(getattr(fn, "func", fn), "__name__", "fn")
        return self._frame(fn, _WRITER_TAGS.get(name, "blackboard"), f"writer:{name}")

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
        pass_ = self._steps(scheduler.PhaseRunner._pass, "pass")
        mp.setattr(scheduler.PhaseRunner, "_pass", self._timed(pass_, "pass"))
        for fname in ("count_open", "ready_tasks"):
            fn = self._frame(getattr(scheduler, fname), "pass")
            mp.setattr(scheduler, fname, self._timed(fn, "pass"))
        mp.setattr(scheduler, "_child_ids", self._frame(scheduler._child_ids, "execute_task"))
        task = self._steps(scheduler.execute_task, "execute_task")
        mp.setattr(scheduler, "execute_task", self._execute(task))
        for module in (planner, scheduler, challenge, writer):
            mp.setattr(module, "run_agent", self._agent(self._steps(module.run_agent, "agent")))
        for name, member in vars(Blackboard).items():
            if name.startswith("_") or name in _BB_SKIP or isinstance(member, staticmethod):
                continue
            if callable(member):
                wrap = self._steps if inspect.iscoroutinefunction(member) else self._frame
                mp.setattr(Blackboard, name, self._timed(wrap(member, "blackboard"), "blackboard"))
        self._install_writer(mp)
        self._install_ops(mp)

    def _install_ops(self, mp: pytest.MonkeyPatch) -> None:
        """Named frames of the post steps (a finer split of `blackboard`) and the ops writes."""
        bb_mod = sys.modules[Blackboard.__module__]
        for fname in _PARTS:
            mp.setattr(bb_mod, fname, self._frame(getattr(bb_mod, fname), "blackboard"))
        for mname in ("_build", "_validate", "_unknown_queries", "_commit"):
            mp.setattr(Blackboard, mname, self._frame(getattr(Blackboard, mname), "blackboard"))
        for fname in _OPS_FUNCS:
            original = getattr(ops_findings, fname)
            timed = self._timed(self._frame(original, "blackboard"), "blackboard")
            for module in list(sys.modules.values()):
                name = getattr(module, "__name__", "")
                if name.startswith("herness") and getattr(module, fname, None) is original:
                    mp.setattr(module, fname, timed)

    def _install_writer(self, mp: pytest.MonkeyPatch) -> None:
        """Frames for the work submitted to the writer; the blocking wait is a `queue` frame."""
        sync, on_writer = Blackboard._sync, Blackboard.run_on_writer
        waiting = self._frame(sync, "queue", "queue_wait")

        def sync_frames(bb: Blackboard, fn: Callable[[], Any], timeout_s: float, busy: str) -> Any:
            return waiting(bb, self._on_writer(fn), timeout_s, busy)

        async def run_on_writer(bb: Blackboard, fn: Callable[[], Any]) -> Any:
            inner = on_writer(bb, self._on_writer(fn))
            return await _Steps(inner, self, "blackboard", "run_on_writer")

        mp.setattr(Blackboard, "_sync", sync_frames)
        mp.setattr(Blackboard, "run_on_writer", self._timed(run_on_writer, "blackboard"))
