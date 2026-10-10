"""Golden-file snapshots (impl 09 T09-11): option `--snapshot-update`, fixture `snapshot`.

`snapshot.check(path, data)` compares ``data`` byte for byte with the committed golden file
``path``. Without `--snapshot-update` a missing or different golden fails the test (the
message names the first differing line); with it the golden is (re)written atomically and the
test passes. No snapshot plugin is pinned in `uv.lock`, so this tiny one stands in.
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import Final

import pytest

__all__ = ["Snapshot", "first_difference", "snapshot"]

_OPTION: Final = "--snapshot-update"


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("herness-snapshot", "Golden files (impl 09 T09-11)")
    group.addoption(
        _OPTION,
        action="store_true",
        dest="snapshot_update",
        default=False,
        help="rewrite golden files instead of comparing against them",
    )


def first_difference(expected: bytes, actual: bytes) -> str:
    """``line N`` of the first differing line, or the length difference."""
    old, new = expected.splitlines(keepends=True), actual.splitlines(keepends=True)
    for number, (a, b) in enumerate(zip(old, new, strict=False), start=1):
        if a != b:
            return f"line {number}: expected {a[:120]!r}, got {b[:120]!r}"
    return f"{len(old)} expected lines, {len(new)} actual lines"


@dataclasses.dataclass(frozen=True, slots=True)
class Snapshot:
    """Compares bytes with golden files, or rewrites them under `--snapshot-update`."""

    update: bool

    def check(self, path: Path, data: bytes) -> None:
        if self.update:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)
            return
        if not path.is_file():
            pytest.fail(f"golden file {path.name} missing; run pytest {_OPTION}")
        expected = path.read_bytes()
        if expected != data:
            where = first_difference(expected, data)
            pytest.fail(f"{path.name} differs from its golden ({where}); see {_OPTION}")


@pytest.fixture
def snapshot(pytestconfig: pytest.Config) -> Snapshot:
    """The golden comparer; `--snapshot-update` turns it into a writer."""
    return Snapshot(update=bool(pytestconfig.getoption("snapshot_update")))
