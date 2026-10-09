"""Phase gate runner end to end (U11-73): IT11-30.

The phase 1 gate with its `slow` and full-scale checks replaced by tiny equivalents: the
`tiny` generator bench (BT11-03) as the bench check, the files-connector inbox test, the
coverage-target test and a failing command, each run as a real subprocess.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from herness.core.logging import reset_logging
from tools import phase_gate
from tools.phase_gate import GATES, GateCheck

pytestmark = pytest.mark.integration

_REPO = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _reset_logging() -> Iterator[None]:
    yield
    reset_logging()  # `main` configures process logging


# fmt: off
_TINY_PHASE_1 = (
    GateCheck(
        "G1.1", 1, "tiny generator bench", "bench",
        ("BT11-03", "tests/bench/test_generator_bench.py"), 600,
    ),
    GateCheck(
        "G1.2", 1, "files connector inbox", "pytest",
        ("IT01-01", "tests/integration/connectors/test_sync_jobs_flow.py"), 600,
    ),
    GateCheck(
        "G1.4", 1, "coverage targets", "pytest",
        ("UT11-38", "tests/unit/test_coverage_targets.py"), 600,
    ),
    GateCheck(
        "G1.8", 1, "test ID no test carries", "pytest",
        ("UT11-38", "UT99-99", "tests/unit/test_coverage_targets.py"), 600,
    ),
    GateCheck(
        "G1.9", 1, "failing command", "cli",
        (sys.executable, "-c", "raise SystemExit(1)"), 60,
    ),
)
# fmt: on


def test_it11_30_tiny_phase_1_gate_report_and_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT11-30 `run_gate(1)` on the tiny phase 1 gate: the report lists every check with its
    exit code, duration and log; the real checks pass, the failing one and the one selecting
    an ID no collected test carries (exit 3, not run) make the gate fail; the bench check wrote
    a gate bench file under the out dir and its exit code follows the recorded BT11-03 verdict
    (the plumbing is under test, not the host's speed). ID collection is limited to the test
    files the gate selects."""
    monkeypatch.setitem(GATES, 1, _TINY_PHASE_1)
    monkeypatch.delenv("HERNESS_BENCH_DIR", raising=False)
    paths = [entry for check in _TINY_PHASE_1 for entry in check.selector if "/" in entry]
    report = phase_gate.run_gate(1, repo_root=_REPO, out_dir=tmp_path, collect_paths=paths)
    assert not report.passed
    doc = json.loads(report.path.read_text(encoding="utf-8"))
    codes = {c["check_id"]: c["exit_code"] for c in doc["checks"]}
    logs = {c["check_id"]: Path(c["log"]).read_text(encoding="utf-8") for c in doc["checks"]}
    (bench_file,) = (tmp_path / "data" / "bench").glob("*.json")
    bench_doc = json.loads(bench_file.read_text(encoding="utf-8"))
    (record,) = bench_doc["records"]
    assert (record["target_id"], bench_doc["gate"]) == ("BT11-03", True)
    bench_code = 0 if record["passed"] else 1
    assert codes == {"G1.1": bench_code, "G1.2": 0, "G1.4": 0, "G1.8": 3, "G1.9": 1}, logs
    assert doc["passed"] is False
    ran = [c for c in doc["checks"] if c["check_id"] != "G1.8"]
    assert all(c["duration_s"] > 0 and c["problems"] == [] for c in ran)
    assert doc["checks"][3]["problems"] == ["unknown test ID UT99-99"]
