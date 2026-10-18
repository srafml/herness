"""Clock and coroutine-step helpers of the BT06-01 exclusive recorder (T06-30)."""

from __future__ import annotations

import ctypes
import sys
import time
from collections.abc import Callable
from typing import Any

__all__ = ["Steps", "thread_cpu"]


def thread_cpu() -> float:
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


class Steps:
    """Awaitable over `coro` that times each event-loop step as one frame of `rec` (T06-30)."""

    def __init__(self, coro: Any, rec: Any, tag: str, name: str) -> None:
        self._coro, self._rec, self._tag, self._name = coro, rec, tag, name
        rec.count(name)

    def __await__(self) -> Steps:
        return self

    def __iter__(self) -> Steps:
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
