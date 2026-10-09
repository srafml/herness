"""Benchmark recorder and regression rule (U11-51; design 11 §8).

Bench tests measure with `tests.support.bench_stats` and record through the session fixture
`bench_recorder` (`record_median`, `record_value`). At session end, when any record exists,
the fixture writes `<bench dir>/<YYYYMMDD-HHMMSS>-<git sha>.json`; the bench dir is
`<rootdir>/data/bench/` (gitignored) unless env `HERNESS_BENCH_DIR` names another. With
`HERNESS_BENCH_GATE=1` the file is a gate file, compared with the newest earlier one; a
regression beyond `thresholds.bench_regression_max` fails the session (exit code 1).
"""

from __future__ import annotations

import dataclasses
import json
import os
import platform
import subprocess
import sys
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Final, Literal

import psutil
import pytest
import yaml
from tests.support.bench_stats import report

from herness.core import time as clock
from herness.eval.settings import load_eval_config

_REPO: Final = Path(__file__).resolve().parents[2]
_GIT = "git"
SCHEMA: Final = 1
BENCH_DIR_ENV: Final = "HERNESS_BENCH_DIR"
GATE_ENV: Final = "HERNESS_BENCH_GATE"

type Better = Literal["lower", "higher"]


@dataclasses.dataclass(frozen=True, slots=True)
class BenchRecord:
    """One measured benchmark result (U11-51)."""

    target_id: str
    owner_spec: str
    metric: str
    value: float
    unit: str
    better: Better
    threshold: float
    passed: bool
    scale: str

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True, slots=True)
class BenchTarget:
    """What a bench test measures and its pass threshold (everything but the value)."""

    target_id: str
    owner_spec: str
    metric: str
    unit: str
    threshold: float
    scale: str
    better: Better = "lower"

    def judge(self, value: float) -> BenchRecord:
        """The record for `value`: below the threshold passes for `lower`, at or above
        it for `higher`."""
        passed = value < self.threshold if self.better == "lower" else value >= self.threshold
        fields = dataclasses.asdict(self)
        return BenchRecord(value=value, passed=passed, **fields)


@dataclasses.dataclass(frozen=True, slots=True)
class Regression:
    """A `target_id + metric` worse than the previous gate bench by more than allowed."""

    target_id: str
    metric: str
    previous: float
    current: float
    change: float


class BenchRecorder:
    """Collects the session's records and writes the bench file."""

    def __init__(self) -> None:
        self._records: list[BenchRecord] = []

    @property
    def records(self) -> tuple[BenchRecord, ...]:
        return tuple(self._records)

    def record(self, rec: BenchRecord) -> None:
        self._records.append(rec)

    def write(self, out_dir: Path, *, gate: bool, benchmark_json: Path | None) -> Path:
        """Write `<out_dir>/<YYYYMMDD-HHMMSS>-<git sha>.json` atomically; return its path."""
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = clock.now().strftime("%Y%m%d-%H%M%S")
        path = out_dir / f"{stamp}-{_git_sha()}.json"
        embedded = None
        if benchmark_json is not None and benchmark_json.is_file():
            embedded = json.loads(benchmark_json.read_text(encoding="utf-8"))
        doc = {
            "schema": SCHEMA,
            "gate": gate,
            "hardware": _hardware(),
            "records": [rec.as_dict() for rec in self._records],
            "pytest_benchmark": embedded,
        }
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        return path


def _git_sha() -> str:
    """`git rev-parse --short=12 HEAD` (timeout 10 s), or `nogit` on any failure."""
    try:
        done = subprocess.run(  # noqa: S603 - fixed argument list, no shell
            [_GIT, "rev-parse", "--short=12", "HEAD"],
            cwd=_REPO,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "nogit"
    sha = done.stdout.strip()
    return sha if done.returncode == 0 and sha else "nogit"


def _hardware() -> dict[str, Any]:
    ram_gb = round(psutil.virtual_memory().total / 2**30, 1)
    return {"cpu_count": os.cpu_count(), "ram_gb": ram_gb, "platform": platform.platform()}


def _index(doc: Mapping[str, Any]) -> dict[tuple[str, str], Mapping[str, Any]]:
    return {(rec["target_id"], rec["metric"]): rec for rec in doc.get("records", ())}


def compare_bench(
    current: Mapping[str, Any], previous: Mapping[str, Any], *, max_regression: float
) -> list[Regression]:
    """Regressions of `current` against `previous` (two bench documents) for each
    `target_id + metric` present in both, in `current` record order."""
    before = _index(previous)
    found: list[Regression] = []
    for key, rec in _index(current).items():
        if key not in before:
            continue
        cur, prev = float(rec["value"]), float(before[key]["value"])
        if rec["better"] == "lower":
            worse = cur > prev * (1 + max_regression)
        else:
            worse = cur < prev * (1 - max_regression)
        if worse:
            change = (cur - prev) / prev if prev else float("inf")
            found.append(Regression(key[0], key[1], prev, cur, change))
    return found


def record_value(recorder: BenchRecorder, target: BenchTarget, value: float) -> BenchRecord:
    """Record one measurement of `target`."""
    rec = target.judge(value)
    recorder.record(rec)
    return rec


def record_median(
    recorder: BenchRecorder, target: BenchTarget, values: Sequence[float]
) -> BenchRecord:
    """Report the repeats with `bench_stats.report` (median of at least `REPEATS`) and
    record the median."""
    sign = "<" if target.better == "lower" else ">="
    median = report(
        target.target_id, target.metric, values, target.unit, f"{sign} {target.threshold:g}"
    )
    return record_value(recorder, target, median)


def max_regression(repo_root: Path = _REPO) -> float:
    """`config/eval.yaml: thresholds.bench_regression_max`, validated by the U11-52 model."""
    raw = yaml.safe_load((repo_root / "config" / "eval.yaml").read_text(encoding="utf-8"))
    raw.pop("version", None)  # checked by the config loader (U10-16), not the owner model
    return load_eval_config(raw).thresholds.bench_regression_max


def bench_dir(rootdir: Path) -> Path:
    """`HERNESS_BENCH_DIR` when set, else `<rootdir>/data/bench`."""
    override = os.environ.get(BENCH_DIR_ENV)
    return Path(override) if override else rootdir / "data" / "bench"


def _newest_earlier_gate(out_dir: Path, written: Path) -> dict[str, Any] | None:
    for path in sorted(out_dir.glob("*.json"), reverse=True):
        if path.name >= written.name:
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict) and doc.get("gate") is True:
            return doc
    return None


def finish_session(recorder: BenchRecorder, *, out_dir: Path, gate: bool) -> list[Regression]:
    """Write the bench file when any record exists; on a gate bench, return the
    regressions against the newest earlier gate file."""
    if not recorder.records:
        return []
    path = recorder.write(out_dir, gate=gate, benchmark_json=None)
    sys.stderr.write(f"bench: wrote {path.as_posix()}\n")
    if not gate:
        return []
    previous = _newest_earlier_gate(out_dir, path)
    if previous is None:
        return []
    current = json.loads(path.read_text(encoding="utf-8"))
    return compare_bench(current, previous, max_regression=max_regression())


@pytest.fixture(scope="session")
def bench_recorder(request: pytest.FixtureRequest) -> Iterator[BenchRecorder]:
    """Session recorder; writes and (with `HERNESS_BENCH_GATE=1`) gates at session end."""
    recorder = BenchRecorder()
    yield recorder
    gate = os.environ.get(GATE_ENV) == "1"
    out_dir = bench_dir(Path(request.config.rootpath))
    regressions = finish_session(recorder, out_dir=out_dir, gate=gate)
    for reg in regressions:
        sys.stderr.write(
            f"bench regression: {reg.target_id} {reg.metric} {reg.previous:g} -> "
            f"{reg.current:g} ({reg.change:+.1%})\n"
        )
    if regressions:
        ids = ", ".join(f"{reg.target_id} {reg.metric}" for reg in regressions)
        request.session.shouldfail = f"bench regression beyond bench_regression_max: {ids}"
