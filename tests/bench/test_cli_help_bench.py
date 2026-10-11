"""CLI start-up benchmark BT09-07 (impl 09 §10; T09-26).

Run: `pytest -m "integration and slow" tests/bench/test_cli_help_bench.py -s`. Each of 10 runs
starts the installed `herness` console script (`[project.scripts]`, next to the interpreter)
as a new process with `--help`, in an empty working directory with no config, and times it
until it exits; the target is the median below 1 s (design §8: the lazy imports of
`herness.cli` and `herness._cli`, U09-84). Writes one stderr line.
"""

from __future__ import annotations

import os
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Final

import pytest
from tests.support.bench import BenchRecorder, BenchTarget, record_value

pytestmark = [pytest.mark.integration, pytest.mark.slow]

RUNS: Final = 10
LIMIT_S: Final = 1.0
TARGET: Final = BenchTarget("BT09-07", "09", "help_median_s", "s", LIMIT_S, "dev")


def _script() -> str:
    """The console script of this environment (``herness.exe`` on Windows)."""
    folder = Path(sys.executable).parent
    found = shutil.which("herness", path=str(folder))
    assert found is not None, f"no herness console script in {folder}"
    return found


def test_bt09_07_herness_help_cold_median_under_1_s(
    tmp_path: Path, bench_recorder: BenchRecorder
) -> None:
    """BT09-07 `herness --help`, 10 cold subprocess runs: exit 0 with the usage text and the
    exit-code epilog; median wall time < 1 s."""
    script = _script()
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERNESS_")}
    times: list[float] = []
    for _ in range(RUNS):
        started = time.perf_counter()
        done = subprocess.run(  # noqa: S603 - the environment's own console script
            [script, "--help"], cwd=tmp_path, env=env, capture_output=True, timeout=60,
            check=False,
        )  # fmt: skip
        times.append(time.perf_counter() - started)
        assert done.returncode == 0, done.stderr.decode("utf-8", "replace")[-2000:]
        text = done.stdout.decode("utf-8", "replace")
        assert "Usage:" in text
        assert "Exit codes" in text
    median = statistics.median(times)
    each = ", ".join(f"{t:.4f}" for t in times)
    sys.stderr.write(f"BT09-07 help_median_s {median:.4f} s (target < 1) runs=[{each}]\n")
    rec = record_value(bench_recorder, TARGET, median)
    assert rec.passed, f"BT09-07 median {median:.3f} s >= {LIMIT_S:g} s"
