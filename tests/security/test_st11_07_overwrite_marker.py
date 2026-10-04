"""Security test for the overwrite marker (ST11-07, TH11-07, U11-23, U11-24)."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from herness.core.logging import reset_logging
from tools.synth_data import main

pytestmark = pytest.mark.unit

_SENTINEL = "operator data that the generator does not own\n"


@pytest.fixture(autouse=True)
def _reset_logging() -> Iterator[None]:
    yield
    reset_logging()  # `main` configures process logging


def _argv(root: Path) -> list[str]:
    return ["--seed", "7", "--scale", "tiny", "--root", str(root), "--overwrite"]


def test_st11_07_overwrite_without_marker_exits_3_and_keeps_sentinel(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """ST11-07 `--overwrite` on a directory with a sentinel file and no `.synth_root`:
    exit 3, the sentinel and its sibling directory intact, nothing added."""
    root = tmp_path / "not_a_synth_root"
    (root / "nested").mkdir(parents=True)
    sentinel = root / "sentinel.txt"
    sentinel.write_text(_SENTINEL, encoding="utf-8")
    (root / "nested" / "keep.bin").write_bytes(b"\x01")

    assert main(_argv(root)) == 3

    assert sentinel.read_text(encoding="utf-8") == _SENTINEL
    assert sorted(p.relative_to(root).as_posix() for p in root.rglob("*")) == [
        "nested",
        "nested/keep.bin",
        "sentinel.txt",
    ]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert _SENTINEL.strip() not in captured.err  # error output names paths and keys only


def test_st11_07_marker_that_is_a_directory_or_link_is_not_a_marker(tmp_path: Path) -> None:
    """ST11-07 a `.synth_root` that is a directory or a symlink does not authorise deletion."""
    target = tmp_path / "elsewhere.json"
    target.write_text("{}", encoding="utf-8")
    for kind in ("dir", "link"):
        root = tmp_path / kind
        root.mkdir()
        (root / "sentinel.txt").write_text(_SENTINEL, encoding="utf-8")
        marker = root / ".synth_root"
        if kind == "dir":
            marker.mkdir()
        else:
            marker.symlink_to(target)

        assert main(_argv(root)) == 3
        assert (root / "sentinel.txt").read_text(encoding="utf-8") == _SENTINEL
    assert target.read_text(encoding="utf-8") == "{}"
