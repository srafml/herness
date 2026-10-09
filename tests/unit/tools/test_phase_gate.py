"""Phase gate runner (U11-73): UT11-112."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from herness.core.logging import reset_logging
from tools import phase_gate
from tools.phase_gate import GATES, GateCheck

pytestmark = pytest.mark.unit

_REPORT = re.compile(r"^phase99-\d{8}-\d{6}\.json$")
_TEST_ID = re.compile(r"^[A-Z]{2,3}\d{2}-\d{2,3}$")


@pytest.fixture(autouse=True)
def _reset_logging() -> Iterator[None]:
    yield
    reset_logging()  # `main` configures process logging


def _py(check_id: str, code: str, timeout_s: int = 60) -> GateCheck:
    argv = (sys.executable, "-c", code)
    return GateCheck(check_id, 99, f"fake {check_id}", "cli", argv, timeout_s)


def _report(out_dir: Path) -> tuple[Path, dict[str, Any]]:
    (path,) = (out_dir / "data" / "reports" / "gates").glob("*.json")
    return path, json.loads(path.read_text(encoding="utf-8"))


def test_ut11_112_gates_hold_table_g() -> None:
    """UT11-112 GATES covers phases 1-7; phase 1 is G1.1-G1.4 with the Table G kinds and
    timeouts; every check has a selector, a positive timeout and its own phase; every pytest
    or bench check selects at least one test ID."""
    assert sorted(GATES) == list(range(1, 8))
    phase1 = [(c.check_id, c.kind, c.timeout_s) for c in GATES[1]]
    assert phase1 == [
        ("G1.1", "bench", 3600),
        ("G1.2", "pytest", 600),
        ("G1.3", "bench", 3600),
        ("G1.4", "pytest", 1200),
    ]
    assert set(GATES[1][0].selector) == {"BT11-01", "BT11-02", "BT11-03", "BT11-07"} | {
        "IT11-01",
        "IT11-02",
    }
    for phase, checks in GATES.items():
        assert checks
        for check in checks:
            assert check.phase == phase
            assert check.check_id.startswith(f"G{phase}.")
            assert check.selector
            assert check.timeout_s > 0
            assert check.kind in {"pytest", "cli", "bench"}
            if check.kind != "cli":
                assert any(_TEST_ID.fullmatch(entry) for entry in check.selector)


def test_ut11_112_cli_eval_checks_carry_inline() -> None:
    """UT11-112 every `herness eval` gate command carries `--inline` (R-45)."""
    evals = [c for checks in GATES.values() for c in checks if "eval" in c.selector]
    assert evals
    for check in evals:
        assert check.kind == "cli"
        assert "--inline" in check.selector


def test_ut11_112_command_for_each_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT11-112 pytest checks select the IDs and pass other entries through; bench checks
    add the gate env; cli checks expand `$VAR`."""
    check = GateCheck("G9.1", 9, "x", "pytest", ("UT11-38", "-m", "not slow", "IT11-30"), 5)
    argv, env = phase_gate.command_for(check)
    expected = [*phase_gate.PYTEST, "--select-test-ids=UT11-38,IT11-30", "-q", "-m", "not slow"]
    assert argv == expected
    assert env == {}
    bench = GateCheck("G9.2", 9, "x", "bench", ("BT11-03",), 5)
    assert phase_gate.command_for(bench)[1] == {
        "HERNESS_BENCH_GATE": "1",
        "HERNESS_BENCH_FULL": "1",
    }
    monkeypatch.setenv("PHASE_GATE_COMPARE_RUNS", "a,b")
    cli = GateCheck(
        "G9.3", 9, "x", "cli", ("herness", "--compare-runs", "$PHASE_GATE_COMPARE_RUNS"), 5
    )
    assert phase_gate.command_for(cli)[0] == ["herness", "--compare-runs", "a,b"]


def test_ut11_112_all_pass_writes_report_and_exits_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT11-112 a fake gate of passing `python -c` commands: report written, exit 0."""
    monkeypatch.setitem(GATES, 99, (_py("G99.1", "pass"), _py("G99.2", "print('ok')")))
    assert phase_gate.main(["--phase", "99", "--out-dir", str(tmp_path)]) == 0
    path, doc = _report(tmp_path)
    assert _REPORT.match(path.name), path.name
    assert doc["phase"] == 99
    assert doc["passed"] is True
    assert [c["check_id"] for c in doc["checks"]] == ["G99.1", "G99.2"]
    for entry in doc["checks"]:
        assert entry["exit_code"] == 0
        assert entry["duration_s"] >= 0
        assert Path(entry["log"]).is_file()
    assert "ok" in Path(doc["checks"][1]["log"]).read_text(encoding="utf-8")


def test_ut11_112_one_failing_command_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT11-112 one failing command: every check still runs, exit 1, its code recorded."""
    checks = (_py("G99.1", "raise SystemExit(3)"), _py("G99.2", "pass"))
    monkeypatch.setitem(GATES, 99, checks)
    assert phase_gate.main(["--phase", "99", "--out-dir", str(tmp_path)]) == 1
    _, doc = _report(tmp_path)
    assert [c["exit_code"] for c in doc["checks"]] == [3, 0]
    assert doc["passed"] is False


def test_ut11_112_timeout_and_missing_program_fail_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT11-112 a check over its timeout records 124; one whose program is missing 127."""
    slow = _py("G99.1", "import time; time.sleep(30)", timeout_s=1)
    missing = GateCheck("G99.2", 99, "x", "cli", ("no-such-program-for-gate",), 5)
    monkeypatch.setitem(GATES, 99, (slow, missing))
    report = phase_gate.run_gate(99, repo_root=tmp_path, out_dir=tmp_path)
    assert [r.exit_code for r in report.results] == [124, 127]
    assert not report.passed
    assert report.path.is_file()


def test_ut11_112_unknown_phase_and_bad_usage_exit_2(capsys: pytest.CaptureFixture[str]) -> None:
    """UT11-112 a `--phase` naming no gate and a usage error exit 2 (R-73)."""
    assert phase_gate.main(["--phase", "42", "--dry-run"]) == 2
    assert "no gate for phase 42" in capsys.readouterr().err
    assert phase_gate.main(["--phase", "one"]) == 2
    assert phase_gate.main([]) == 2


def test_ut11_112_unwritable_report_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT11-112 the runner exits 1 when the report folder cannot be written."""
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setitem(GATES, 99, (_py("G99.1", "pass"),))
    assert phase_gate.main(["--phase", "99", "--out-dir", str(blocker)]) == 1


def test_ut11_112_dry_run_lists_phase_1(capsys: pytest.CaptureFixture[str]) -> None:
    """UT11-112 `--phase 1 --dry-run` lists G1.1-G1.4 with their commands, runs nothing."""
    assert phase_gate.main(["--phase", "1", "--dry-run"]) == 0
    out = capsys.readouterr().out
    listed = [line.split()[0] for line in out.splitlines() if line.startswith("G")]
    assert listed == ["G1.1", "G1.2", "G1.3", "G1.4"]
    assert "--select-test-ids=BT11-01,BT11-02,BT11-03,BT11-07,IT11-01,IT11-02" in out
    assert "HERNESS_BENCH_GATE=1" in out
