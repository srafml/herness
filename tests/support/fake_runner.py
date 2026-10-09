"""``fake_runner`` fixture (impl 10 §11): a table of argv prefix -> ``CmdResult``.

It replaces U10-76 ``herness.admin.wsl.run_cmd``, the only permitted OS-level mock (ENG §6), so
``run_wsl`` and ``run_powershell`` keep their own argument checks and no test needs a real
``wsl.exe``, ``docker`` or ``powershell.exe``. The longest matching prefix wins (the newest on a
tie); an argv no row matches fails the test. Every call is recorded with its stdin. The private
``_execute`` (the one place ``subprocess.run`` is called) is replaced too, so code holding the
real ``run_cmd`` by name fails the test instead of starting a process.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import pytest

from herness.admin import wsl
from herness.admin.wsl import CmdResult
from herness.core.errors import ConfigError, FatalError

__all__ = ["FakeCall", "FakeRunner", "fake_runner"]


@dataclass(frozen=True)
class FakeCall:
    """One recorded ``run_cmd`` call."""

    argv: tuple[str, ...]
    stdin: bytes | None
    timeout_s: float


@dataclass
class FakeRunner:
    """Scripted ``run_cmd``: ``add`` rows, then read ``calls`` after the code under test ran."""

    rows: list[tuple[tuple[str, ...], CmdResult]] = field(default_factory=list)
    calls: list[FakeCall] = field(default_factory=list)

    def add(
        self, prefix: Sequence[str], returncode: int = 0, stdout: str = "", stderr: str = ""
    ) -> None:
        """Answer every argv that starts with ``prefix`` with the given result."""
        self.rows.append((tuple(prefix), CmdResult(returncode, stdout, stderr, 0.0)))

    def __call__(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float,
        stdin: bytes | None = None,
        encoding: str = "utf-8",
        check: bool = False,
    ) -> CmdResult:
        del encoding
        if not argv or argv[0] not in wsl.ALLOWED_COMMANDS:  # keep the U10-76 allowlist
            msg = f"command not allowed: {argv[0] if argv else ''}"
            raise ConfigError(msg)
        elements: Sequence[object] = argv
        if not all(isinstance(a, str) and "\0" not in a for a in elements):
            msg = "command element is not a string without NUL"
            raise ConfigError(msg)
        if not 0 < timeout_s <= wsl.MAX_TIMEOUT_S:
            msg = f"timeout_s must be in (0, {wsl.MAX_TIMEOUT_S}]"
            raise ConfigError(msg)
        args = tuple(argv)
        self.calls.append(FakeCall(args, stdin, timeout_s))
        best: tuple[tuple[str, ...], CmdResult] | None = None
        for prefix, result in self.rows:
            if args[: len(prefix)] == prefix and (best is None or len(prefix) >= len(best[0])):
                best = (prefix, result)
        if best is None:
            pytest.fail(f"fake_runner: no row matches {list(args)}")
        if check and best[1].returncode != 0:
            msg = f"{args[0]} failed: exit {best[1].returncode}"
            raise FatalError(msg)
        return best[1]


@pytest.fixture
def fake_runner(monkeypatch: pytest.MonkeyPatch) -> FakeRunner:
    """Install a ``FakeRunner`` as ``herness.admin.wsl.run_cmd`` for one test."""
    runner = FakeRunner()
    monkeypatch.setattr(wsl, "run_cmd", runner)
    monkeypatch.setattr(wsl, "_execute", _no_process)
    return runner


def _no_process(argv: list[str], *_args: object) -> CmdResult:
    """Stands in for ``wsl._execute``: a bypass of the fake must never start a real process."""
    pytest.fail(f"fake_runner bypassed: real run_cmd reached for {argv[0]!r}")
