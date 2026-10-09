"""Phase gate definitions and runner (U11-73; design 11 §10.4, impl 11 §10 Table G).

`uv run python tools/phase_gate.py --phase <N> [--dry-run] [--out-dir DIR]` runs every check of
phase N as a subprocess with its timeout and writes
`<out dir>/data/reports/gates/phase<N>-<YYYYMMDD-HHMMSS>.json` (each check's exit code,
duration and log path; logs sit in the folder of the same name). A `pytest` check runs
`uv run pytest --select-test-ids=<ids> -q`; selector entries that are not test IDs (paths,
`-m`, `--hypothesis-profile=nightly`) pass through as pytest arguments. A `bench` check runs
the same with `HERNESS_BENCH_GATE=1` (regression rule of U11-51), `HERNESS_BENCH_FULL=1` (the
opt-in full-scale benches) and `HERNESS_BENCH_DIR=<out dir>/data/bench`. A `cli` check runs
its selector as the argument list; `herness eval` carries `--inline` (R-45), and `$VAR`
references are expanded from the environment (`PHASE_GATE_COMPARE_PROFILE`,
`PHASE_GATE_COMPARE_RUNS` for phase 7: no `HERNESS_` prefix, which impl 10 reads as config).
Exit codes (R-73): 0 every check passed, 1 a check failed or the report could not be written,
2 usage error (including an unknown phase).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final, Literal

from herness.core import time as clock
from herness.core.logging import configure_logging, get_logger

_log = get_logger("phase_gate")

type Kind = Literal["pytest", "cli", "bench"]

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
PYTEST: Final = ("uv", "run", "pytest")
HERNESS: Final = ("uv", "run", "herness")
TIMEOUT_EXIT: Final = 124
NOT_FOUND_EXIT: Final = 127
_TEST_ID: Final = re.compile(r"^[A-Z]{2,3}\d{2}-\d{2,3}$")
_BENCH_ENV: Final = {"HERNESS_BENCH_GATE": "1", "HERNESS_BENCH_FULL": "1"}


@dataclasses.dataclass(frozen=True, slots=True)
class GateCheck:
    """One Table G row (or one kind of a two-kind row)."""

    check_id: str
    phase: int
    description: str
    kind: Kind
    selector: tuple[str, ...]
    timeout_s: int


@dataclasses.dataclass(frozen=True, slots=True)
class CheckResult:
    """The outcome of one executed check."""

    check_id: str
    kind: Kind
    command: tuple[str, ...]
    exit_code: int
    duration_s: float
    log_path: Path


@dataclasses.dataclass(frozen=True, slots=True)
class GateReport:
    """A phase run: every check's result and the report file."""

    phase: int
    path: Path
    results: tuple[CheckResult, ...]

    @property
    def passed(self) -> bool:
        return all(result.exit_code == 0 for result in self.results)


def _g(
    check_id: str, description: str, kind: Kind, selector: Sequence[str], timeout_s: int
) -> GateCheck:
    phase = int(check_id[1 : check_id.index(".")])
    return GateCheck(check_id, phase, description, kind, tuple(selector), timeout_s)


def _ids(prefix: str, *numbers: int) -> tuple[str, ...]:
    return tuple(f"{prefix}-{number:02d}" for number in numbers)


_EVAL: Final = (*HERNESS, "eval", "--inline")

# fmt: off
_TABLE_G: Final = (
    _g("G1.1", "generator targets and determinism", "bench",
       ("BT11-01", "BT11-02", "BT11-03", "BT11-07", "IT11-01", "IT11-02"), 3600),
    _g("G1.2", "files connector ingests the generated inbox", "pytest", ("IT01-01",), 600),
    _g("G1.3", "full build < 10 min, DQ < 1 min, < 15 GB; lake_small snapshot; dedupe, "
       "drift, blue/green", "bench",
       ("BT02-01", "BT02-02", "BT02-03", "IT02-21", "IT01-03", "IT01-04", "IT02-32"), 3600),
    _g("G1.4", "coverage targets for core and store", "pytest", ("UT11-38",), 1200),
    _g("G2.1", "metrics_tiny covers every catalog metric; metrics coverage >= 95 %", "pytest",
       ("UT11-38",), 1200),
    _g("G2.2", "run_scoring < 5 min on full", "bench", ("BT04-06",), 1800),
    _g("G2.3", "planted truths T1, T2, T2c, T4, T5 from spec 04 outputs", "pytest",
       (*_ids("IT11", 5, 6, 7, 8), "IT04-08"), 3600),
    _g("G2.4", "meta.evidence re-runs reproduce result_hash", "pytest", ("IT04-01",), 1200),
    _g("G3.1", "mock golden run in CI", "pytest", ("ET11-01", "BT11-06"), 900),
    _g("G3.2", "real local golden, standard depth, >= 30 questions, 0 unsupported numbers",
       "cli", (*_EVAL, "--suite", "golden", "--depth", "standard"), 14400),
    _g("G3.3", "spec 08 F1-F7, F11 plus X1, X2, X6, X7", "pytest",
       (*_ids("FT08", 1, 2, 3, 4, 5, 6, 7, 11), *_ids("FT11", 1, 2, 6, 7)), 3600),
    _g("G3.4", "spec 06 T1-T16; spec 07 integration 1-3, 5, 6; SQL guard nightly; resume "
       "after kill", "pytest",
       (*_ids("IT06", *range(1, 16)), *_ids("IT07", 1, 2, 3, 5, 6),
        *_ids("ST05", 3, 4, 5), "--hypothesis-profile=nightly"), 7200),
    _g("G4.1", "T2 ARI >= 0.80 and T3 links on full", "pytest", ("BT11-05",), 7200),
    _g("G4.2", "gold set, eval.json cross-check, decider comparison, coverage, escalation, "
       "nightly enrichment", "pytest", ("ET11-03", "IT11-25", "BT03-06", "BT03-10"), 7200),
    _g("G4.3", "X3 and X4", "pytest", _ids("FT11", 3, 4), 1800),
    _g("G5.1", "reports render with the evidence appendix; dashboard targets on full",
       "pytest", ("BT09-01", "BT09-05"), 7200),
    _g("G5.1", "chat golden subset G01-G23", "cli", (*_EVAL, "--ids", "G01..G23"), 7200),
    _g("G5.2", "redaction corpus thresholds", "pytest", ("IT10-02",), 600),
    _g("G6.1", "cassette tests incl. 429 and breaker; two syncs add zero rows; DQ", "pytest",
       ("IT01-02", "IT01-03", "FT08-04", "FT08-05", "IT02-29"), 3600),
    _g("G6.2", "real-data golden subset meets the floor; fixtures PII scan clean", "pytest",
       ("ET11-05", "ST11-01"), 14400),
    _g("G7.1", "--compare across profiles and depths", "cli",
       (*_EVAL, "--suite", "golden", "--compare", "$PHASE_GATE_COMPARE_PROFILE"), 28800),
    _g("G7.2", "model or LoRA upgrade not worse on the golden set", "cli",
       (*_EVAL, "--compare-runs", "$PHASE_GATE_COMPARE_RUNS"), 14400),
)
# fmt: on

GATES: Final[dict[int, tuple[GateCheck, ...]]] = {
    phase: tuple(check for check in _TABLE_G if check.phase == phase) for phase in range(1, 8)
}


def command_for(check: GateCheck) -> tuple[list[str], dict[str, str]]:
    """The argument list and extra environment for `check`."""
    if check.kind == "cli":
        return [os.path.expandvars(arg) for arg in check.selector], {}
    ids = [entry for entry in check.selector if _TEST_ID.fullmatch(entry)]
    extra = [entry for entry in check.selector if not _TEST_ID.fullmatch(entry)]
    argv = [*PYTEST, f"--select-test-ids={','.join(ids)}", "-q", *extra]
    return argv, dict(_BENCH_ENV) if check.kind == "bench" else {}


def _execute(check: GateCheck, *, repo_root: Path, out_dir: Path, log_path: Path) -> CheckResult:
    argv, extra_env = command_for(check)
    env = {**os.environ, **extra_env}
    if check.kind == "bench":
        env["HERNESS_BENCH_DIR"] = str(out_dir / "data" / "bench")
    started = time.monotonic()
    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"$ {' '.join(argv)}\n")
        log.flush()
        try:
            done = subprocess.run(  # noqa: S603 - argument list from GATES, no shell
                argv,
                cwd=repo_root,
                env=env,
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
    doc: dict[str, Any] = {
        "schema": 1,
        "phase": phase,
        "passed": all(result.exit_code == 0 for result in results),
        "checks": [
            {
                "check_id": result.check_id,
                "kind": result.kind,
                "command": list(result.command),
                "exit_code": result.exit_code,
                "duration_s": round(result.duration_s, 3),
                "log": result.log_path.as_posix(),
            }
            for result in results
        ],
    }
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def run_gate(phase: int, *, repo_root: Path, out_dir: Path) -> GateReport:
    """Run every check of `phase` (KeyError for a phase with no gate); write the report.

    Raises OSError when the report folder or file cannot be written.
    """
    checks = GATES[phase]
    stamp = clock.now().strftime("%Y%m%d-%H%M%S")
    reports = out_dir / "data" / "reports" / "gates"
    logs = reports / f"phase{phase}-{stamp}"
    logs.mkdir(parents=True, exist_ok=True)
    results: list[CheckResult] = []
    for index, check in enumerate(checks, start=1):
        log_path = logs / f"{index:02d}-{check.check_id}-{check.kind}.log"
        result = _execute(check, repo_root=repo_root, out_dir=out_dir, log_path=log_path)
        _log.info(
            "phase_gate.check.done",
            check_id=check.check_id,
            kind=check.kind,
            exit_code=result.exit_code,
            duration_s=round(result.duration_s, 3),
        )
        results.append(result)
    path = reports / f"phase{phase}-{stamp}.json"
    _write_report(path, phase, results)
    return GateReport(phase, path, tuple(results))


def _dry_run(phase: int) -> None:
    for check in GATES[phase]:
        argv, extra_env = command_for(check)
        env = "".join(f"{key}={value} " for key, value in extra_env.items())
        sys.stdout.write(
            f"{check.check_id} [{check.kind}, timeout {check.timeout_s} s] {check.description}\n"
            f"    {env}{' '.join(argv)}\n"
        )


def _parse(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="phase_gate", description="Run a phase gate.")
    parser.add_argument("--phase", type=int, required=True, help="phase number (1-7)")
    parser.add_argument("--dry-run", action="store_true", help="list the checks, run nothing")
    parser.add_argument("--out-dir", type=Path, default=REPO_ROOT, help="report root")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry; exit codes per R-73 (0 pass, 1 findings or runner failure, 2 usage)."""
    try:
        ns = _parse(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as exc:  # argparse: usage error (2) or --help (0)
        return exc.code if isinstance(exc.code, int) else 2
    if ns.phase not in GATES:
        sys.stderr.write(f"phase_gate: no gate for phase {ns.phase}; phases: 1-7\n")
        return 2
    if ns.dry_run:
        _dry_run(ns.phase)
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
