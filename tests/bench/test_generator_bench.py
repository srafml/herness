"""Generator benchmarks (impl 11 §10: BT11-01, -02, -03, -04, -07, -08; T11-18).

Each target times the real CLI (`python tools/synth_data.py`) into `tmp_path` and records its
result through the session `bench_recorder` (U11-51). BT11-02 (`small`) and BT11-03 (`tiny`)
run in every slow-suite session. The `full`-scale targets (BT11-01, BT11-07, BT11-08: up to
30 min and tens of GB) run only with `HERNESS_BENCH_FULL=1`, and BT11-04 (the whole unit
suite inside a test) only with `HERNESS_BENCH_UNIT_SUITE=1`; the phase gate sets the former.

Run: pytest -m slow tests/bench/test_generator_bench.py
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import psutil
import pytest
from tests.support.bench import (
    BenchRecorder,
    BenchTarget,
    bench_flag,
    record_median,
    record_value,
)
from tests.support.bench_stats import REPEATS

from tools.synth.verify import verify_root

pytestmark = [pytest.mark.integration, pytest.mark.slow]

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "tools" / "synth_data.py"
FULL_ENV = "HERNESS_BENCH_FULL"
UNIT_SUITE_ENV = "HERNESS_BENCH_UNIT_SUITE"
_GB = 2**30


def _require(config: pytest.Config, name: str, what: str) -> None:
    """Skip unless the opt-in `name` was `1` at session start (read via `bench_flag`)."""
    if bench_flag(config, name) != "1":
        pytest.skip(f"{what}; set {name}=1")


def _generate(root: Path, seed: int, scale: str, *extra: str) -> subprocess.Popen[bytes]:
    cmd = [sys.executable, str(_SCRIPT), "--seed", str(seed), "--scale", scale]
    return subprocess.Popen(  # noqa: S603 - fixed interpreter and script, tmp root
        [*cmd, "--root", str(root), *extra],
        cwd=_REPO,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def _timed(root: Path, seed: int, scale: str, *extra: str) -> float:
    """Seconds for one CLI run; the run must exit 0."""
    started = time.perf_counter()
    proc = _generate(root, seed, scale, *extra)
    _, err = proc.communicate(timeout=3600)
    seconds = time.perf_counter() - started
    assert proc.returncode == 0, err.decode("utf-8", "replace")[-2000:]
    return seconds


def test_bt11_03_generator_tiny_under_5_s(tmp_path: Path, bench_recorder: BenchRecorder) -> None:
    """BT11-03 seed 7 `tiny`: the median of the timed CLI runs is below 5 s."""
    target = BenchTarget("BT11-03", "11", "seconds", "s", 5.0, "tiny")
    values = [_timed(tmp_path / f"run{n}", 7, "tiny") for n in range(REPEATS)]
    rec = record_median(bench_recorder, target, values)
    assert rec.passed, f"BT11-03 median {rec.value:.2f} s >= {target.threshold:g} s"


def test_bt11_02_generator_small_under_90_s(tmp_path: Path, bench_recorder: BenchRecorder) -> None:
    """BT11-02 seed 42 `small`: one timed CLI run is below 90 s."""
    target = BenchTarget("BT11-02", "11", "seconds", "s", 90.0, "small")
    seconds = _timed(tmp_path / "root", 42, "small")
    sys.stderr.write(f"BT11-02 seconds {seconds:.2f} s (target < 90)\n")
    rec = record_value(bench_recorder, target, seconds)
    assert rec.passed, f"BT11-02 {seconds:.2f} s >= {target.threshold:g} s"


@dataclass(frozen=True, slots=True)
class FullRun:
    """One `full` generation: its root, wall time and the peak RSS of any worker."""

    root: Path
    seconds: float
    peak_worker_rss: int


def _sample_workers(proc: psutil.Process, peak: dict[int, int], stop: threading.Event) -> None:
    """Every 1 s, record each child process's RSS high-water mark."""
    while not stop.wait(1.0):
        try:
            children = proc.children(recursive=True)
        except psutil.Error:
            return
        for child in children:
            try:
                rss = child.memory_info().rss
            except psutil.Error:
                continue
            peak[child.pid] = max(peak.get(child.pid, 0), rss)


@pytest.fixture(scope="module")
def full_run(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> FullRun:
    """Seed 42 `full` with `--workers 16`, timed, with workers sampled by psutil."""
    _require(request.config, FULL_ENV, "full-scale generator bench (~30 min, large disk)")
    root = tmp_path_factory.mktemp("bt11_full") / "root"
    peak: dict[int, int] = {}
    stop = threading.Event()
    started = time.perf_counter()
    proc = _generate(root, 42, "full", "--workers", "16")
    sampler = threading.Thread(target=_sample_workers, args=(psutil.Process(proc.pid), peak, stop))
    sampler.start()
    try:
        _, err = proc.communicate(timeout=7200)
    finally:
        stop.set()
        sampler.join()
    seconds = time.perf_counter() - started
    assert proc.returncode == 0, err.decode("utf-8", "replace")[-2000:]
    return FullRun(root, seconds, max(peak.values(), default=0))


def test_bt11_01_generator_full_under_1800_s(
    full_run: FullRun, bench_recorder: BenchRecorder
) -> None:
    """BT11-01 seed 42 `full`, `--workers 16`: the timed CLI run is below 1,800 s."""
    target = BenchTarget("BT11-01", "11", "seconds", "s", 1800.0, "full")
    rec = record_value(bench_recorder, target, full_run.seconds)
    assert rec.passed, f"BT11-01 {full_run.seconds:.0f} s >= 1800 s"


def test_bt11_07_generator_worker_peak_rss_under_2_gb(
    full_run: FullRun, bench_recorder: BenchRecorder
) -> None:
    """BT11-07 `full`: the peak RSS of every worker (psutil, every 1 s) is below 2 GB."""
    target = BenchTarget("BT11-07", "11", "peak_worker_rss", "GB", 2.0, "full")
    gb = full_run.peak_worker_rss / _GB
    assert full_run.peak_worker_rss > 0, "no worker process was sampled"
    rec = record_value(bench_recorder, target, gb)
    assert rec.passed, f"BT11-07 peak worker RSS {gb:.2f} GB >= 2 GB"


def test_bt11_08_verify_full_under_300_s(full_run: FullRun, bench_recorder: BenchRecorder) -> None:
    """BT11-08 seed 42 `full`: `--verify` of the generated root takes below 300 s."""
    target = BenchTarget("BT11-08", "11", "seconds", "s", 300.0, "full")
    started = time.perf_counter()
    report = verify_root(full_run.root)
    seconds = time.perf_counter() - started
    assert report.ok
    rec = record_value(bench_recorder, target, seconds)
    assert rec.passed, f"BT11-08 verify {seconds:.0f} s >= 300 s"


def test_bt11_04_unit_suite_under_60_s(
    request: pytest.FixtureRequest, bench_recorder: BenchRecorder
) -> None:
    """BT11-04 `pytest -m unit` over the whole suite: session duration below 60 s."""
    _require(request.config, UNIT_SUITE_ENV, "runs the whole unit suite in a subprocess")
    target = BenchTarget("BT11-04", "11", "seconds", "s", 60.0, "unit")
    cmd = [sys.executable, "-m", "pytest", "-m", "unit", "-q", "-p", "no:cacheprovider"]
    started = time.perf_counter()
    done = subprocess.run(  # noqa: S603 - fixed interpreter and arguments
        cmd, cwd=_REPO, capture_output=True, text=True, check=False, timeout=1800
    )
    seconds = time.perf_counter() - started
    assert done.returncode == 0, done.stdout[-2000:]
    rec = record_value(bench_recorder, target, seconds)
    assert rec.passed, f"BT11-04 unit suite {seconds:.1f} s >= 60 s"
