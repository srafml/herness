"""The `fake_runner` fixture (impl 10 §11): `run_cmd` answered from an argv-prefix table.

Replaces U10-76 `herness.admin.wsl.run_cmd`, and every `herness.*` module-level reference to
it, with a `FakeRunner`. `add(prefix, stdout, returncode=..., stderr=...)` sets the answer for
every argv that starts with `prefix` (the longest matching prefix wins); every call is
recorded in `calls`. The allowlist check of the real runner is kept, and an argv with no
answer fails the test loudly (`pytest.fail`, a `BaseException`, so no `except Exception`
in the code under test can swallow it). This is the only permitted OS-level mock (ENG §6).
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest

from herness.admin import wsl
from herness.admin.wsl import ALLOWED_COMMANDS, CmdResult
from herness.core.errors import ConfigError, FatalError


@dataclass
class FakeRunner:
    """A table of argv prefix → `CmdResult` standing in for `run_cmd`."""

    table: dict[tuple[str, ...], CmdResult] = field(default_factory=dict)
    calls: list[tuple[list[str], dict[str, Any]]] = field(default_factory=list)

    def add(
        self, prefix: Sequence[str], stdout: str = "", *, returncode: int = 0, stderr: str = ""
    ) -> None:
        """Answer every argv starting with `prefix`."""
        self.table[tuple(prefix)] = CmdResult(returncode, stdout, stderr, 0.0)

    @property
    def argvs(self) -> list[list[str]]:
        """The argv of every recorded call, in order."""
        return [argv for argv, _ in self.calls]

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
        if not args or args[0] not in ALLOWED_COMMANDS:
            msg = f"command not allowed: {args[0] if args else ''}"
            raise ConfigError(msg)
        self.calls.append(
            (args, {"timeout_s": timeout_s, "stdin": stdin, "encoding": encoding, "check": check})
        )
        matches = [p for p in self.table if tuple(args[: len(p)]) == p]
        if not matches:
            pytest.fail(f"fake_runner: no recorded answer for argv {args!r}")
        result = self.table[max(matches, key=len)]
        if check and result.returncode != 0:
            msg = f"{args[0]} failed: exit {result.returncode}"
            raise FatalError(msg)
        return result


@pytest.fixture
def fake_runner(monkeypatch: pytest.MonkeyPatch) -> FakeRunner:
    """Install a `FakeRunner` as `run_cmd` in `herness.admin.wsl` and its importers."""
    fake = FakeRunner()
    original = wsl.run_cmd
    for name, module in list(sys.modules.items()):
        if name.partition(".")[0] == "herness" and getattr(module, "run_cmd", None) is original:
            monkeypatch.setattr(module, "run_cmd", fake)
    return fake
