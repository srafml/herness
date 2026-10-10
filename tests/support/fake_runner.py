"""``fake_runner`` fixture (impl 10 §11): ``run_cmd`` answered from an argv-prefix table.

It replaces U10-76 ``herness.admin.wsl.run_cmd`` (the only permitted OS-level mock, ENG §6) and
every ``herness.*`` module-level name bound to it, so ``run_wsl`` and ``run_powershell`` keep
their own argument checks and no test needs a real ``wsl.exe``, ``docker``, ``gh`` or
``powershell.exe``. ``add(prefix, answer)`` answers every argv starting with ``prefix``; the
answer is a ``CmdResult``, a callable ``argv -> CmdResult``, or a stdout string (with
``returncode``/``stderr`` keywords). The longest matching prefix wins (the newest on a tie).
The runner's checks are mirrored (``wsl.admits``, which admits the staged blue/green venv
interpreters of T10-26c, plus the running interpreter ``sys.executable``, read live; NUL-free
string elements, timeout range) before a call is recorded; ``check=True`` turns a non-zero
exit into ``FatalError``. An argv no row matches fails the test (``pytest.fail``, a
``BaseException``) unless ``passthrough`` is set (integration only: the real runner runs it).
The private ``_execute`` (the one place ``subprocess.run`` is called) is replaced too, so code
holding the real ``run_cmd`` by name fails the test instead of starting a process.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, NamedTuple

import pytest

from herness.admin import wsl
from herness.admin.wsl import CmdResult
from herness.core.errors import ConfigError, FatalError

__all__ = ["Call", "FakeCall", "FakeRunner", "fake_runner"]

Responder = CmdResult | Callable[[list[str]], CmdResult]
_REAL_RUN_CMD = wsl.run_cmd


class Call(NamedTuple):
    """One recorded ``run_cmd`` call; unpacks as ``(argv, kwargs)``."""

    argv: list[str]
    kwargs: dict[str, Any]

    @property
    def timeout_s(self) -> float:
        return float(self.kwargs["timeout_s"])

    @property
    def stdin(self) -> bytes | None:
        stdin: bytes | None = self.kwargs["stdin"]
        return stdin

    @property
    def encoding(self) -> str:
        return str(self.kwargs["encoding"])

    @property
    def check(self) -> bool:
        return bool(self.kwargs["check"])


FakeCall = Call


class _Argvs(list[list[str]]):
    """The recorded argvs; usable as a list and, for older callers, as ``argvs()``."""

    def __call__(self) -> list[list[str]]:
        return list(self)


@dataclass
class FakeRunner:
    """Scripted ``run_cmd``: ``add`` rows, then read ``calls`` after the code under test ran."""

    table: list[tuple[tuple[str, ...], Responder]] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)
    passthrough: bool = False
    _passing: bool = field(default=False, repr=False)

    def add(
        self,
        prefix: Sequence[str],
        result: Responder | str = "",
        *,
        stdout: str | None = None,
        returncode: int = 0,
        stderr: str = "",
    ) -> None:
        """Answer every argv starting with ``prefix`` (a result, a callable or a stdout string)."""
        answer: Responder
        if isinstance(result, str):
            answer = CmdResult(returncode, result if stdout is None else stdout, stderr, 0.0)
        else:
            answer = result
        self.table.append((tuple(prefix), answer))

    @property
    def argvs(self) -> _Argvs:
        """The argv of every recorded call, in order."""
        return _Argvs(call.argv for call in self.calls)

    def __call__(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float,
        stdin: bytes | None = None,
        encoding: str = "utf-8",
        check: bool = False,
    ) -> CmdResult:
        args = list(argv)
        argv0 = str(args[0]) if args else ""
        if not args or not (
            wsl.admits(argv0) or os.path.normcase(argv0) == os.path.normcase(sys.executable)
        ):
            msg = f"command not allowed: {argv0}"
            raise ConfigError(msg)
        elements: Sequence[object] = args
        if not all(isinstance(a, str) and "\0" not in a for a in elements):
            msg = "command element is not a string without NUL"
            raise ConfigError(msg)
        if not 0 < timeout_s <= wsl.MAX_TIMEOUT_S:
            msg = f"timeout_s must be in (0, {wsl.MAX_TIMEOUT_S}]"
            raise ConfigError(msg)
        kwargs = {"timeout_s": timeout_s, "stdin": stdin, "encoding": encoding, "check": check}
        self.calls.append(Call(args, kwargs))
        best: tuple[tuple[str, ...], Responder] | None = None
        for prefix, answer in self.table:
            if tuple(args[: len(prefix)]) == prefix and (
                best is None or len(prefix) >= len(best[0])
            ):
                best = (prefix, answer)
        if best is None:
            if self.passthrough:
                self._passing = True
                try:
                    return _REAL_RUN_CMD(
                        args, timeout_s=timeout_s, stdin=stdin, encoding=encoding, check=check
                    )
                finally:
                    self._passing = False
            pytest.fail(f"fake_runner: no row matches {args!r}")
        responder = best[1]
        result = responder if isinstance(responder, CmdResult) else responder(args)
        if check and result.returncode != 0:
            msg = f"{args[0]} failed: exit {result.returncode}"
            raise FatalError(msg)
        return result


@pytest.fixture
def fake_runner(monkeypatch: pytest.MonkeyPatch) -> FakeRunner:
    """Install a ``FakeRunner`` as ``run_cmd`` in ``herness.admin.wsl`` and its importers."""
    runner = FakeRunner()
    for name, module in list(sys.modules.items()):
        if (
            name.partition(".")[0] == "herness"
            and getattr(module, "run_cmd", None) is _REAL_RUN_CMD
        ):
            monkeypatch.setattr(module, "run_cmd", runner)
    real_execute = wsl._execute

    def _no_process(argv: list[str], *args: Any) -> CmdResult:
        """Stands in for ``wsl._execute``: a bypass of the fake must never start a process."""
        if runner._passing:
            return real_execute(argv, *args)
        pytest.fail(f"fake_runner bypassed: real run_cmd reached for {argv[0]!r}")

    monkeypatch.setattr(wsl, "_execute", _no_process)
    return runner
