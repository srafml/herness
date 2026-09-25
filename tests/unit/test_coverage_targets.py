"""Coverage gate tests (U11-32): tests/unit/test_coverage_targets.py."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from tests.support.coverage_gate import COVERAGE_TARGETS, CoverageTarget, check_coverage

pytestmark = pytest.mark.unit


def _summary(
    covered_lines: int, num_statements: int, covered_branches: int, num_branches: int
) -> dict[str, int]:
    return {
        "covered_lines": covered_lines,
        "num_statements": num_statements,
        "covered_branches": covered_branches,
        "num_branches": num_branches,
    }


def _report(files: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {"files": files}


def _file(path: str, summary: dict[str, int]) -> dict[str, dict[str, Any]]:
    return {path: {"summary": summary}}


def _report_at_targets() -> dict[str, Any]:
    """One file per group, exactly meeting its line and branch targets."""
    files: dict[str, dict[str, Any]] = {}
    for target in COVERAGE_TARGETS:
        path = f"{target.prefixes[0]}mod.py"
        num_statements = 100
        covered_lines = int(target.line)
        num_branches = 100 if target.branch is not None else 0
        covered_branches = int(target.branch) if target.branch is not None else 0
        files.update(
            _file(path, _summary(covered_lines, num_statements, covered_branches, num_branches))
        )
    return _report(files)


def test_ut11_38_check_coverage_no_violations_at_target() -> None:
    """UT11-38 a coverage JSON meeting every group's target yields no violations."""
    assert check_coverage(_report_at_targets()) == []


def test_ut11_38_check_coverage_violation_below_target() -> None:
    """UT11-38 a group below target reports a violation listing its group and measure."""
    report = _report_at_targets()
    # Push herness/core below its 90% line target and 85% branch target.
    report["files"]["herness/core/mod.py"] = {"summary": _summary(50, 100, 50, 100)}
    violations = check_coverage(report)
    by_measure = {v.measure: v for v in violations}
    assert by_measure["line"].group == "herness/core"
    assert by_measure["line"].actual == pytest.approx(50.0)
    assert by_measure["line"].target == pytest.approx(90.0)
    assert by_measure["branch"].group == "herness/core"
    assert by_measure["branch"].actual == pytest.approx(50.0)
    assert by_measure["branch"].target == pytest.approx(85.0)


def test_ut11_38_check_coverage_group_with_no_branch_target() -> None:
    """UT11-38 a group with no branch target (`herness/eval/`) never reports a branch violation."""
    eval_target = next(t for t in COVERAGE_TARGETS if t.branch is None)
    assert eval_target.prefixes == ("herness/eval/", "herness/reports/", "herness/cli.py")
    report = _report_at_targets()
    violations = check_coverage(report)
    assert not any(v.group == "herness/eval, herness/reports, herness/cli.py" for v in violations)


def test_ut11_39_check_coverage_no_files_violation_for_eval_group() -> None:
    """UT11-39 a coverage JSON without herness/eval/ files reports a `no files` violation."""
    files = {
        path: data
        for path, data in _report_at_targets()["files"].items()
        if not path.startswith(("herness/eval/", "herness/reports/", "herness/cli.py"))
    }
    violations = check_coverage(_report(files))
    eval_violations = [
        v for v in violations if v.group == "herness/eval, herness/reports, herness/cli.py"
    ]
    assert len(eval_violations) == 1
    assert eval_violations[0].measure == "no files"


def test_ut11_38_targets_match_design_4_3() -> None:
    """UT11-38 COVERAGE_TARGETS matches design §4.3 verbatim."""
    assert (
        CoverageTarget(("herness/metrics/",), 95.0, 90.0),
        CoverageTarget(("herness/core/",), 90.0, 85.0),
        CoverageTarget(("herness/store/", "herness/model/"), 90.0, 80.0),
        CoverageTarget(("herness/harness/",), 85.0, 75.0),
        CoverageTarget(("herness/connectors/",), 85.0, 75.0),
        CoverageTarget(("herness/enrich/",), 80.0, 70.0),
        CoverageTarget(("herness/eval/", "herness/reports/", "herness/cli.py"), 80.0, None),
    ) == COVERAGE_TARGETS


def test_ut11_38_gate_reads_coverage_json_from_env() -> None:
    """UT11-38 the gate reads `HERNESS_COVERAGE_JSON`, or skips when unset."""
    raw = os.environ.get("HERNESS_COVERAGE_JSON")
    if raw is None:
        pytest.skip("coverage.json not provided")
    report = json.loads(Path(raw).read_text(encoding="utf-8"))
    violations = check_coverage(report)
    assert violations == []
