"""``fake_runner``: the U10-76 ``run_cmd`` stand-in (impl 10 §11; the only permitted OS mock).

A table of argv prefix → ``CmdResult`` (or a callable ``argv -> CmdResult``). The fixture
replaces ``herness.admin.wsl.run_cmd`` and every ``run_cmd`` name a loaded ``herness.admin``
module imported from it, records each call, applies ``check=True`` the way the real runner
does, and fails the test on an unmatched argv unless ``passthrough`` is set (then the real
runner runs it; integration tests only).
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import pytest

from herness.admin import wsl
from herness.admin.wsl import CmdResult
from herness.core.errors import FatalError

__all__ = ["Call", "FakeRunner", "fake_runner"]

Responder = CmdResult | Callable[[list[str]], CmdResult]


@dataclass(frozen=True)
class Call:
    """One recorded ``run_cmd`` call."""

    argv: list[str]
    timeout_s: float
    stdin: bytes | None
    encoding: str
    check: bool


@dataclass
class FakeRunner:
    """Longest matching argv prefix wins; ``calls`` records every call in order."""

    table: list[tuple[tuple[str, ...], Responder]] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)
    passthrough: bool = False

    def add(self, prefix: Sequence[str], result: Responder) -> None:
        """Answer every argv starting with ``prefix`` with ``result``."""
        self.table.append((tuple(prefix), result))

    def argvs(self) -> list[list[str]]:
        """The argv of each recorded call."""
        return [call.argv for call in self.calls]

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
        self.calls.append(Call(args, timeout_s, stdin, encoding, check))
        matches = [(p, r) for p, r in self.table if tuple(args[: len(p)]) == p]
        if not matches:
            if self.passthrough:
                return _REAL_RUN_CMD(
                    args, timeout_s=timeout_s, stdin=stdin, encoding=encoding, check=check
                )
            pytest.fail(f"fake_runner: no entry for argv {args!r}")
        _, responder = max(matches, key=lambda item: len(item[0]))
        result = responder(args) if callable(responder) else responder
        if check and result.returncode != 0:
            msg = f"{args[0]} failed: exit {result.returncode}"
            raise FatalError(msg)
        return result


_REAL_RUN_CMD = wsl.run_cmd


@pytest.fixture
def fake_runner(monkeypatch: pytest.MonkeyPatch) -> FakeRunner:
    """Replace ``run_cmd`` in ``herness.admin.wsl`` and in each admin module that imported it."""
    runner = FakeRunner()
    for name, module in list(sys.modules.items()):
        if (name == "herness.admin" or name.startswith("herness.admin.")) and getattr(
            module, "run_cmd", None
        ) is _REAL_RUN_CMD:
            monkeypatch.setattr(module, "run_cmd", runner)
    return runner
