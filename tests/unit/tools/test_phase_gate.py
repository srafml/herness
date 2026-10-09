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
    or bench check selects at least one test ID; every check selecting a BT ID is a bench."""
    assert sorted(GATES) == list(range(1, 8))
    phase1 = [(c.check_id, c.kind, c.timeout_s) for c in GATES[1]]
    assert phase1 == [
        ("G1.1", "bench", 3600),
        ("G1.2", "pytest", 600),
        ("G1.3", "bench", 3600),
        ("G1.4", "cli", 1200),
        ("G1.4", "cli", 120),
    ]
    assert set(GATES[1][0].selector) == {"BT11-01", "BT11-02", "BT11-03", "BT11-07"} | {
        "IT11-01",
        "IT11-02",
    }
    assert {"BT02-01", "BT02-02", "BT02-03"} <= set(GATES[1][2].selector)
    assert {"BT11-05", "IT03-12", "IT03-13"} <= set(GATES[4][0].selector)
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
            if any(entry.startswith("BT") for entry in check.selector):
                assert check.kind == "bench", check.check_id


def test_ut11_112_coverage_gates_use_a_fresh_coverage_json(tmp_path: Path) -> None:
    """UT11-112 G1.4 and G2.1 write a fresh coverage JSON under the out dir, then apply the
    U11-32 targets to core and store (G1.4) and metrics (G2.1)."""
    for phase, prefixes in ((1, ["herness/core/", "herness/store/"]), (2, ["herness/metrics/"])):
        run, check = [c for c in GATES[phase] if c.kind == "cli"]
        run_argv = phase_gate.command_for(run, tmp_path)[0]
        cov = f"{tmp_path.as_posix()}/data/reports/gates/coverage.json"
        assert f"--cov-report=json:{cov}" in run_argv
        assert "--cov" in run_argv
        check_argv = phase_gate.command_for(check, tmp_path)[0]
        assert check_argv[-1 - len(prefixes) :] == [cov, *prefixes]
        assert "--check-coverage" in check_argv


def test_ut11_112_g3_2_uses_the_local_profile() -> None:
    """UT11-112 G3.2 runs ET11-02's command: golden suite, profile local, depth standard."""
    (g32,) = [c for c in GATES[3] if c.check_id == "G3.2"]
    at = g32.selector.index("--profile")
    assert g32.selector[at : at + 2] == ("--profile", "local")


def test_ut11_112_cli_eval_checks_carry_inline() -> None:
    """UT11-112 every `herness eval` gate command carries `--inline` (R-45)."""
    evals = [c for checks in GATES.values() for c in checks if "eval" in c.selector]
    assert evals
    for check in evals:
        assert check.kind == "cli"
        assert "--inline" in check.selector


def test_ut11_112_command_for_each_kind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT11-112 pytest checks select the IDs and pass other entries through; bench checks
    add the gate env and the bench dir; cli checks expand `$VAR` and `{out}`."""
    check = GateCheck("G9.1", 9, "x", "pytest", ("UT11-38", "-m", "not slow", "IT11-30"), 5)
    argv, env = phase_gate.command_for(check, tmp_path)
    expected = [*phase_gate.PYTEST, "--select-test-ids=UT11-38,IT11-30", "-q", "-m", "not slow"]
    assert argv == expected
    assert env == {}
    bench = GateCheck("G9.2", 9, "x", "bench", ("BT11-03",), 5)
    assert phase_gate.command_for(bench, tmp_path)[1] == {
        "HERNESS_BENCH_GATE": "1",
        "HERNESS_BENCH_FULL": "1",
        "HERNESS_BENCH_DIR": (tmp_path / "data" / "bench").as_posix(),
    }
    monkeypatch.setenv("PHASE_GATE_COMPARE_RUNS", "a,b")
    cli = GateCheck("G9.3", 9, "x", "cli", ("h", "$PHASE_GATE_COMPARE_RUNS", "{out}/c"), 5)
    assert phase_gate.command_for(cli, tmp_path)[0] == ["h", "a,b", f"{tmp_path.as_posix()}/c"]


def test_ut11_112_unknown_test_ids_fail_the_check_without_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT11-112 a check selecting an ID that no collected test carries fails with exit code
    3 and its unknown IDs in the report, without running; resolved checks still run."""
    monkeypatch.setattr(phase_gate, "collect_test_ids", lambda *_: frozenset({"UT11-38"}))
    missing = GateCheck("G99.1", 99, "x", "bench", ("UT11-38", "BT02-01"), 60)
    monkeypatch.setitem(GATES, 99, (missing, _py("G99.2", "pass")))
    assert phase_gate.main(["--phase", "99", "--out-dir", str(tmp_path)]) == 1
    _, doc = _report(tmp_path)
    first, second = doc["checks"]
    assert (first["exit_code"], first["problems"]) == (3, ["unknown test ID BT02-01"])
    assert "unknown test ID BT02-01" in Path(first["log"]).read_text(encoding="utf-8")
    assert (second["exit_code"], second["problems"]) == (0, [])
    assert not (tmp_path / "data" / "bench").exists()


def test_ut11_112_failed_collection_and_unset_variable_fail_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT11-112 a failed test-ID collection fails every ID check; a cli check naming an
    unset `$VAR` fails fast with a clear problem (exit code 3)."""
    monkeypatch.setattr(phase_gate, "collect_test_ids", lambda *_: None)
    monkeypatch.delenv("PHASE_GATE_COMPARE_RUNS", raising=False)
    ids = GateCheck("G99.1", 99, "x", "pytest", ("UT11-38",), 60)
    var = GateCheck("G99.2", 99, "x", "cli", ("x", "$PHASE_GATE_COMPARE_RUNS"), 60)
    monkeypatch.setitem(GATES, 99, (ids, var))
    report = phase_gate.run_gate(99, repo_root=tmp_path, out_dir=tmp_path)
    assert [(r.exit_code, r.problems) for r in report.results] == [
        (3, ("test-ID collection failed",)),
        (3, ("unset PHASE_GATE_COMPARE_RUNS",)),
    ]


def _coverage_json(path: Path, core: int, store: int) -> Path:
    keys = ("covered_lines", "num_statements", "covered_branches", "num_branches")

    def summary(pct: int) -> dict[str, Any]:
        return {"summary": dict(zip(keys, (pct, 100, pct, 100), strict=True))}

    files = {"herness/core/a.py": summary(core), "herness/store/b.py": summary(store)}
    path.write_text(json.dumps({"files": files}), encoding="utf-8")
    return path


def test_ut11_112_check_coverage_limits_u11_32_to_the_named_groups(tmp_path: Path) -> None:
    """UT11-112 `--check-coverage` applies the U11-32 targets to the groups of the given
    prefixes only: exit 0 when they hold, 1 when one is below, a prefix names no group or
    the JSON cannot be read."""
    cov = str(_coverage_json(tmp_path / "coverage.json", core=95, store=50))
    assert phase_gate.main(["--check-coverage", cov, "herness/core/"]) == 0
    assert phase_gate.main(["--check-coverage", cov, "herness/core/", "herness/store/"]) == 1
    assert phase_gate.main(["--check-coverage", cov, "herness/nothing/"]) == 1
    missing = str(tmp_path / "missing.json")  # the coverage run failed: no stale pass
    assert phase_gate.main(["--check-coverage", missing, "herness/core/"]) == 1


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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT11-112 the runner exits 1 and logs `phase_gate.report.failed` at ERROR when the
    report folder cannot be written (R-73)."""
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setitem(GATES, 99, (_py("G99.1", "pass"),))
    assert phase_gate.main(["--phase", "99", "--out-dir", str(blocker)]) == 1
    assert "phase_gate.report.failed" in capsys.readouterr().err


def test_ut11_112_dry_run_lists_phase_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT11-112 `--phase 1 --dry-run` lists G1.1-G1.4 with their commands and the IDs no
    collected test carries, and runs nothing."""
    known = {c for check in GATES[1] for c in check.selector} - {"BT02-01"}
    monkeypatch.setattr(phase_gate, "collect_test_ids", lambda *_: frozenset(known))
    assert phase_gate.main(["--phase", "1", "--dry-run"]) == 0
    out = capsys.readouterr().out
    listed = [line.split()[0] for line in out.splitlines() if line.startswith("G")]
    assert listed == ["G1.1", "G1.2", "G1.3", "G1.4", "G1.4"]
    assert out.count("UNRESOLVED:") == 1
    assert "UNRESOLVED: unknown test ID BT02-01" in out
    assert "--select-test-ids=BT11-01,BT11-02,BT11-03,BT11-07,IT11-01,IT11-02" in out
    assert "HERNESS_BENCH_GATE=1" in out
