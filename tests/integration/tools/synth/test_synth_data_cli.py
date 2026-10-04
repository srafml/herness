"""CLI runs of tools/synth_data.py through the real spawn pool: IT11-01, IT11-02 (F11-01,
F11-03). The `small` variants are marked `slow` (nightly)."""

import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from herness.core.logging import reset_logging
from tools.synth.verify import content_hashes, verify_root
from tools.synth_data import main

pytestmark = pytest.mark.integration

_REPO = Path(__file__).resolve().parents[4]
_SCRIPT = _REPO / "tools" / "synth_data.py"


@pytest.fixture(autouse=True)
def _reset_logging() -> Iterator[None]:
    yield
    reset_logging()  # `main` configures process logging


def _generate(root: Path, seed: int, scale: str, workers: int) -> dict[str, str]:
    argv = ["--seed", str(seed), "--scale", scale, "--root", str(root), "--workers", str(workers)]
    assert main(argv) == 0
    return content_hashes(root)


def _determinism(tmp_path: Path, seed: int, scale: str) -> None:
    one = _generate(tmp_path / "w1", seed, scale, 1)
    eight = _generate(tmp_path / "w8", seed, scale, 8)
    assert one
    assert one == eight


def test_it11_01_tiny_content_hashes_equal_across_worker_counts(tmp_path: Path) -> None:
    """IT11-01 seed 7 tiny with `--workers 1` and `--workers 8`: equal `content_hashes`
    for every `source/entity`."""
    _determinism(tmp_path, 7, "tiny")


@pytest.mark.slow
def test_it11_01_small_content_hashes_equal_across_worker_counts(tmp_path: Path) -> None:
    """IT11-01 seed 42 small with `--workers 1` and `--workers 8` (slow)."""
    _determinism(tmp_path, 42, "small")


def _cli_verify(root: Path, scale: str) -> None:
    cmd = [sys.executable, str(_SCRIPT), "--seed", "7", "--scale", scale, "--verify"]
    done = subprocess.run(  # noqa: S603 - fixed interpreter and script, tmp root
        [*cmd, "--root", str(root)],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=False,
        timeout=3600,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    lines = done.stdout.splitlines()
    assert len(lines) == 1, done.stdout[:2000]
    assert set(json.loads(lines[0])) == {"root", "rows", "seconds", "params_hash"}
    assert verify_root(root).ok


def test_it11_02_tiny_cli_verify_exits_0(tmp_path: Path) -> None:
    """IT11-02 `python tools/synth_data.py --seed 7 --scale tiny --verify` exits 0 and
    prints exactly one JSON line on stdout (worker logs stay off stdout)."""
    _cli_verify(tmp_path / "root", "tiny")


@pytest.mark.slow
def test_it11_02_small_cli_verify_exits_0(tmp_path: Path) -> None:
    """IT11-02 the same at `small` (slow)."""
    _cli_verify(tmp_path / "root", "small")
