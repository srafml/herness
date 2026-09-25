"""Fixture helpers for registry tests (impl 10 UT10-25 … UT10-27, ST10-46).

Not collected by pytest: the module does not start with ``test_``.
"""

from __future__ import annotations

from collections.abc import Callable


class FixtureBuiltin:
    """Stand-in class resolved through `_BUILTINS` (UT10-25)."""


class FakeDistribution:
    """Duck-typed stand-in for `importlib.metadata.Distribution`."""

    def __init__(self, name: str, version: str) -> None:
        self.name = name
        self.version = version


class FakeEntryPoint:
    """Duck-typed stand-in for `importlib.metadata.EntryPoint`."""

    def __init__(
        self, name: str, loader: Callable[[], object], dist: FakeDistribution | None
    ) -> None:
        self.name = name
        self.dist = dist
        self._loader = loader

    def load(self) -> object:
        return self._loader()


def entry_points_stub(
    loaded: list[FakeEntryPoint],
) -> Callable[..., list[FakeEntryPoint]]:
    """Build a stand-in for `importlib.metadata.entry_points(group=...)`."""

    def _stub(*, group: str) -> list[FakeEntryPoint]:
        return list(loaded) if group == "herness.plugins" else []

    return _stub
