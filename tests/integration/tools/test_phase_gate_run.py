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
        "G1.9", 1, "failing command", "cli",
        (sys.executable, "-c", "raise SystemExit(1)"), 60,
    ),
)
# fmt: on


def test_it11_30_tiny_phase_1_gate_report_and_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT11-30 `run_gate(1)` on the tiny phase 1 gate: the report lists every check with its
    exit code, duration and log; the real checks pass, the failing one makes the exit 1; the
    bench check wrote a gate bench file under the out dir."""
    monkeypatch.setitem(GATES, 1, _TINY_PHASE_1)
    monkeypatch.delenv("HERNESS_BENCH_DIR", raising=False)
    assert phase_gate.main(["--phase", "1", "--out-dir", str(tmp_path)]) == 1
    (path,) = (tmp_path / "data" / "reports" / "gates").glob("phase1-*.json")
    doc = json.loads(path.read_text(encoding="utf-8"))
    codes = {c["check_id"]: c["exit_code"] for c in doc["checks"]}
    logs = {c["check_id"]: Path(c["log"]).read_text(encoding="utf-8") for c in doc["checks"]}
    assert codes == {"G1.1": 0, "G1.2": 0, "G1.4": 0, "G1.9": 1}, logs
    assert doc["passed"] is False
    assert all(c["duration_s"] > 0 for c in doc["checks"])
    (bench_file,) = (tmp_path / "data" / "bench").glob("*.json")
    bench_doc = json.loads(bench_file.read_text(encoding="utf-8"))
    assert bench_doc["gate"] is True
    assert [r["target_id"] for r in bench_doc["records"]] == ["BT11-03"]
