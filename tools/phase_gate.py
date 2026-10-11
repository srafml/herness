"""Phase gate definitions and runner (U11-73; design 11 §10.4, impl 11 §10 Table G).

`uv run python tools/phase_gate.py --phase <N> [--dry-run] [--out-dir DIR]` runs each check of
phase N as a subprocess with its timeout and writes `<out dir>/data/reports/gates/
phase<N>-<YYYYMMDD-HHMMSS>.json` (exit code, duration, log path and problems per check; logs in
the folder of the same name). `pytest`: `uv run pytest --select-test-ids=<ids> -q` plus the
selector's non-ID entries; `bench`: the same with `HERNESS_BENCH_GATE=1`, `HERNESS_BENCH_FULL=1`
and `HERNESS_BENCH_DIR=<out dir>/data/bench` (U11-51); `cli`: the selector as argument list
(`herness eval` with `--inline`, R-45). `{out}` stands for the out dir; `$VAR` is expanded from
the environment (`PHASE_GATE_COMPARE_*`: no `HERNESS_` prefix, which impl 10 reads as config).
Before running, the test IDs are resolved by one `--collect-only --collect-test-ids` pass; a
check with an unknown ID, an unset `$VAR` or a cli `--ids a,b,...` naming a question the golden
suite lacks fails without running (exit code 3, listed by `--dry-run`; `herness eval` itself
is not needed). `--check-coverage JSON PREFIX...` applies U11-32 to the groups of the prefixes.
Exit codes (R-73): 0 every check passed (or `--dry-run`), 1 a check failed or the report could
not be written, 2 usage error (including an unknown phase). Per-check codes in the report: the
command's own, 3 unresolved, 124 timeout, 127 cannot start.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

from herness.core import time as clock
from herness.core.logging import configure_logging, get_logger
from tools._phase_gate_table import GATES, PYTEST, GateCheck, Kind

__all__ = ["GATES", "GateCheck", "GateReport", "command_for", "main", "run_gate"]

_log = get_logger("phase_gate")
REPO_ROOT: Final = Path(__file__).resolve().parents[1]
UNRESOLVED_EXIT: Final = 3
TIMEOUT_EXIT: Final = 124
NOT_FOUND_EXIT: Final = 127
_GOLDEN: Final = "tests/eval/golden.yaml"  # what `--suite golden` resolves to (config/eval.yaml)
_TEST_ID: Final = re.compile(r"^[A-Z]{2,3}\d{2}-\d{2,3}$")
_VAR: Final = re.compile(r"\$(\w+)")
_BENCH_ENV: Final = {"HERNESS_BENCH_GATE": "1", "HERNESS_BENCH_FULL": "1"}


@dataclasses.dataclass(frozen=True, slots=True)
class CheckResult:
    """The outcome of one executed check."""

    check_id: str
    kind: Kind
    command: tuple[str, ...]
    exit_code: int
    duration_s: float
    log_path: Path
    problems: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True, slots=True)
class GateReport:
    """A phase run: every check's result and the report file."""

    phase: int
    path: Path
    results: tuple[CheckResult, ...]

    @property
    def passed(self) -> bool:
        return all(result.exit_code == 0 for result in self.results)


def command_for(check: GateCheck, out_dir: Path) -> tuple[list[str], dict[str, str]]:
    """The argument list and extra environment for `check`."""
    args = [os.path.expandvars(a.replace("{out}", out_dir.as_posix())) for a in check.selector]
    if check.kind == "cli":
        return args, {}
    ids = [arg for arg in args if _TEST_ID.fullmatch(arg)]
    argv = [*PYTEST, f"--select-test-ids={','.join(ids)}", "-q"]
    argv += [arg for arg in args if not _TEST_ID.fullmatch(arg)]
    bench_dir = {"HERNESS_BENCH_DIR": (out_dir / "data" / "bench").as_posix()}
    return argv, {**_BENCH_ENV, **bench_dir} if check.kind == "bench" else {}


def collect_test_ids(
    repo_root: Path, index_path: Path, *, paths: Sequence[str] | None = None
) -> frozenset[str] | None:
    """Every test ID under `paths` (default: the whole tree; U11-31 `--collect-test-ids`),
    or None when collection fails."""
    argv = [*PYTEST, "--collect-only", "-q", f"--collect-test-ids={index_path}", *(paths or ())]
    try:
        subprocess.run(  # noqa: S603 - fixed argument list, no shell
            argv, cwd=repo_root, capture_output=True, timeout=1800, check=False
        )
        return frozenset(json.loads(index_path.read_text(encoding="utf-8"))["ids"])
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return None


def _golden_ids() -> frozenset[str]:
    """The question ids of the golden suite (`tests/eval/golden.yaml`); none if unreadable."""
    from herness.core.errors import ConfigError  # noqa: PLC0415
    from herness.eval.golden import load_suite  # noqa: PLC0415

    try:
        return frozenset(q.id for q in load_suite(REPO_ROOT / _GOLDEN).questions)
    except (ConfigError, OSError):
        return frozenset()


def _question_problems(check: GateCheck) -> list[str]:
    """A cli check with `--ids a,b,...` names only questions of the golden suite."""
    if check.kind != "cli" or "--ids" not in check.selector[:-1]:
        return []
    named = check.selector[check.selector.index("--ids") + 1].split(",")
    golden = _golden_ids()
    return [f"unknown question id {name}" for name in named if name not in golden]


def problems(check: GateCheck, known: frozenset[str] | None) -> list[str]:
    """Why `check` cannot run: unset `$VAR`s, unknown golden question ids and test IDs no
    collected test carries."""
    found = [f"unset {v}" for v in _VAR.findall(" ".join(check.selector)) if v not in os.environ]
    found += _question_problems(check)
    ids = [entry for entry in check.selector if _TEST_ID.fullmatch(entry)]
    if ids and known is None:
        found.append("test-ID collection failed")
    elif ids and known is not None:
        found += [f"unknown test ID {entry}" for entry in ids if entry not in known]
    return found


def _execute(check: GateCheck, *, repo_root: Path, out_dir: Path, log_path: Path) -> CheckResult:
    argv, extra_env = command_for(check, out_dir)
    started = time.monotonic()
    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"$ {' '.join(argv)}\n")
        log.flush()
        try:
            done = subprocess.run(  # noqa: S603 - argument list from GATES, no shell
                argv,
                cwd=repo_root,
                env={**os.environ, **extra_env},
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=check.timeout_s,
                check=False,
            )
            code = done.returncode
        except subprocess.TimeoutExpired:
            code = TIMEOUT_EXIT
            log.write(f"\nphase_gate: timed out after {check.timeout_s} s\n")
        except OSError as exc:
            code = NOT_FOUND_EXIT
            log.write(f"\nphase_gate: cannot start: {type(exc).__name__}\n")
    duration = time.monotonic() - started
    return CheckResult(check.check_id, check.kind, tuple(argv), code, duration, log_path)


def _write_report(path: Path, phase: int, results: Sequence[CheckResult]) -> None:
    checks = [
        {
            "check_id": r.check_id,
            "kind": r.kind,
            "command": list(r.command),
            "exit_code": r.exit_code,
            "duration_s": round(r.duration_s, 3),
            "log": r.log_path.as_posix(),
            "problems": list(r.problems),
        }
        for r in results
    ]
    passed = all(r.exit_code == 0 for r in results)
    doc: dict[str, Any] = {"schema": 1, "phase": phase, "passed": passed, "checks": checks}
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _known_ids(
    checks: Sequence[GateCheck], repo_root: Path, folder: Path, paths: Sequence[str] | None
) -> frozenset[str] | None:
    if any(_TEST_ID.fullmatch(entry) for check in checks for entry in check.selector):
        return collect_test_ids(repo_root, folder / "test-ids.json", paths=paths)
    return frozenset()


def run_gate(
    phase: int, *, repo_root: Path, out_dir: Path, collect_paths: Sequence[str] | None = None
) -> GateReport:
    """Run every check of `phase` (KeyError for a phase with no gate); write the report.
    `collect_paths` limits the test-ID collection (default: the whole tree).

    Raises OSError when the report folder or file cannot be written.
    """
    checks = GATES[phase]
    stamp = clock.now().strftime("%Y%m%d-%H%M%S")
    reports = out_dir / "data" / "reports" / "gates"
    logs = reports / f"phase{phase}-{stamp}"
    logs.mkdir(parents=True, exist_ok=True)
    (reports / "coverage.json").unlink(missing_ok=True)  # never gate on a stale file
    known = _known_ids(checks, repo_root, logs, collect_paths)
    results: list[CheckResult] = []
    for index, check in enumerate(checks, start=1):
        log_path = logs / f"{index:02d}-{check.check_id}-{check.kind}.log"
        found = problems(check, known)
        if found:
            log_path.write_text("".join(f"phase_gate: {p}\n" for p in found), encoding="utf-8")
            cmd = tuple(command_for(check, out_dir)[0])
            result = CheckResult(
                check.check_id, check.kind, cmd, UNRESOLVED_EXIT, 0.0, log_path, tuple(found)
            )
        else:
            result = _execute(check, repo_root=repo_root, out_dir=out_dir, log_path=log_path)
        _log.info("phase_gate.check.done", check_id=check.check_id, exit_code=result.exit_code)
        results.append(result)
    path = reports / f"phase{phase}-{stamp}.json"
    _write_report(path, phase, results)
    return GateReport(phase, path, tuple(results))


def _dry_run(phase: int, out_dir: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        known = _known_ids(GATES[phase], REPO_ROOT, Path(tmp), None)
    for check in GATES[phase]:
        argv, extra_env = command_for(check, out_dir)
        env = "".join(f"{key}={value} " for key, value in extra_env.items())
        sys.stdout.write(
            f"{check.check_id} [{check.kind}, timeout {check.timeout_s} s] {check.description}\n"
            f"    {env}{' '.join(argv)}\n"
        )
        for problem in problems(check, known):
            sys.stdout.write(f"    UNRESOLVED: {problem}\n")


def check_coverage_file(path: Path, prefixes: Sequence[str]) -> int:
    """U11-32 on a coverage.py JSON, limited to the groups holding `prefixes`: 0 or 1."""
    from tests.support.coverage_gate import COVERAGE_TARGETS, check_coverage  # noqa: PLC0415

    picked = [t for t in COVERAGE_TARGETS if set(t.prefixes) & set(prefixes)]
    groups = {", ".join(prefix.rstrip("/") for prefix in t.prefixes) for t in picked}
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"coverage: cannot read {path.as_posix()}: {type(exc).__name__}\n")
        return 1
    bad = [v for v in check_coverage(report) if v.group in groups]
    for v in bad:
        sys.stderr.write(f"coverage: {v.group} {v.measure} {v.actual} < {v.target}\n")
    return 1 if bad or not groups else 0


def _parse(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="phase_gate", description="Run a phase gate.")
    parser.add_argument("--phase", type=int, help="phase number (1-7)")
    parser.add_argument("--dry-run", action="store_true", help="list the checks, run nothing")
    parser.add_argument("--out-dir", type=Path, default=REPO_ROOT, help="report root")
    parser.add_argument("--check-coverage", nargs="+", metavar="JSON PREFIX")
    ns = parser.parse_args(argv)
    if (ns.phase is None) == (ns.check_coverage is None):
        parser.error("give exactly one of --phase and --check-coverage")
    return ns


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry; exit codes per R-73 (0 pass, 1 findings or runner failure, 2 usage)."""
    try:
        ns = _parse(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as exc:  # argparse: usage error (2) or --help (0)
        return exc.code if isinstance(exc.code, int) else 2
    if ns.check_coverage is not None:
        json_path, *prefixes = ns.check_coverage
        return check_coverage_file(Path(json_path), prefixes)
    if ns.phase not in GATES:
        sys.stderr.write(f"phase_gate: no gate for phase {ns.phase}; phases: 1-7\n")
        return 2
    if ns.dry_run:
        _dry_run(ns.phase, ns.out_dir)
        return 0
    configure_logging("INFO")
    try:
        report = run_gate(ns.phase, repo_root=REPO_ROOT, out_dir=ns.out_dir)
    except OSError as exc:
        _log.error("phase_gate.report.failed", phase=ns.phase, error_type=type(exc).__name__)
        sys.stderr.write(f"phase_gate: cannot write the report: {type(exc).__name__}\n")
        return 1
    failed = [result.check_id for result in report.results if result.exit_code != 0]
    status = f"FAILED ({', '.join(failed)})" if failed else "passed"
    sys.stdout.write(f"phase {ns.phase} gate {status}; report {report.path.as_posix()}\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
