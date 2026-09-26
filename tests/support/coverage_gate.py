"""Coverage targets and gate (design §4.3, U11-32).

Reads `coverage.py` JSON output and checks each package group of design §4.3 against
its line and (where set) branch coverage target. `herness/enrich/`'s GPU-only paths are
excluded upstream by `pyproject.toml`'s `exclude_lines` (`# pragma: gpu`), so this module
only sums the counters `coverage.py` already reports.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Final

_NO_FILES: Final = "no files"


@dataclass(frozen=True)
class CoverageTarget:
    """One design §4.3 row: prefixes sharing a target, line %, branch % (`None` if unset)."""

    prefixes: tuple[str, ...]
    line: float
    branch: float | None


@dataclass(frozen=True)
class CoverageViolation:
    """One gate failure: `group` name, `measure` (`line`, `branch` or `no files`)."""

    group: str
    measure: str
    actual: float
    target: float


COVERAGE_TARGETS: tuple[CoverageTarget, ...] = (
    CoverageTarget(("herness/metrics/",), 95.0, 90.0),
    CoverageTarget(("herness/core/",), 90.0, 85.0),
    CoverageTarget(("herness/store/", "herness/model/"), 90.0, 80.0),
    CoverageTarget(("herness/harness/",), 85.0, 75.0),
    CoverageTarget(("herness/connectors/",), 85.0, 75.0),
    CoverageTarget(("herness/enrich/",), 80.0, 70.0),
    CoverageTarget(("herness/eval/", "herness/reports/", "herness/cli.py"), 80.0, None),
)


def _group_name(target: CoverageTarget) -> str:
    return ", ".join(prefix.rstrip("/") for prefix in target.prefixes)


def _normalize(path: str) -> str:
    """POSIX repo-relative form of a `coverage.json` file path."""
    return PurePosixPath(path.replace("\\", "/")).as_posix()


def _sum_counters(files: Mapping[str, Any], target: CoverageTarget) -> dict[str, int]:
    totals = {"covered_lines": 0, "num_statements": 0, "covered_branches": 0, "num_branches": 0}
    for raw_path, file_data in files.items():
        if not _normalize(raw_path).startswith(target.prefixes):
            continue
        summary = file_data.get("summary", {})
        for key in totals:
            totals[key] += int(summary.get(key, 0))
    return totals


def check_coverage(report: Mapping[str, Any]) -> list[CoverageViolation]:
    """Violations of design §4.3 in a `coverage.py` JSON report (`files.<path>.summary`)."""
    files: Mapping[str, Any] = report.get("files", {})
    violations: list[CoverageViolation] = []
    for target in COVERAGE_TARGETS:
        group = _group_name(target)
        totals = _sum_counters(files, target)
        if totals["num_statements"] == 0:
            violations.append(CoverageViolation(group, _NO_FILES, 0.0, target.line))
            continue
        line_pct = totals["covered_lines"] / totals["num_statements"] * 100
        if line_pct < target.line:
            violations.append(CoverageViolation(group, "line", round(line_pct, 2), target.line))
        if target.branch is None:
            continue
        if totals["num_branches"] > 0:
            branch_pct = totals["covered_branches"] / totals["num_branches"] * 100
        else:
            branch_pct = 100.0
        if branch_pct < target.branch:
            violations.append(
                CoverageViolation(group, "branch", round(branch_pct, 2), target.branch)
            )
    return violations
