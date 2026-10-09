"""Table G of impl 11 §10 as `GateCheck` rows (private sibling of `tools/phase_gate.py`).

Split off for the 300-line budget of `phase_gate.py`, which re-exports `GateCheck` and `GATES`.
Selector entries: test IDs (pytest and bench checks), other pytest arguments, or a cli argument
list; `{out}` stands for the gate's out dir and `$VAR` for an environment variable.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from typing import Final, Literal

type Kind = Literal["pytest", "cli", "bench"]

PYTEST: Final = ("uv", "run", "pytest")
HERNESS: Final = ("uv", "run", "herness")
PYTHON: Final = ("uv", "run", "python", "tools/phase_gate.py")
_COV: Final = "{out}/data/reports/gates/coverage.json"
_COV_RUN: Final = (*PYTEST, "-m", "(unit or integration or fault) and not gpu and not slow", "-q")


@dataclasses.dataclass(frozen=True, slots=True)
class GateCheck:
    """One Table G row (or one kind of a two-kind row)."""

    check_id: str
    phase: int
    description: str
    kind: Kind
    selector: tuple[str, ...]
    timeout_s: int


def _g(
    check_id: str, description: str, kind: Kind, selector: Sequence[str], timeout_s: int
) -> GateCheck:
    phase = int(check_id[1 : check_id.index(".")])
    return GateCheck(check_id, phase, description, kind, tuple(selector), timeout_s)


def _ids(prefix: str, *numbers: int) -> tuple[str, ...]:
    return tuple(f"{prefix}-{number:02d}" for number in numbers)


_EVAL: Final = (*HERNESS, "eval", "--inline")


def _coverage(check_id: str, what: str, *prefixes: str) -> tuple[GateCheck, GateCheck]:
    """A fresh coverage JSON of the CI selection, then U11-32 on the groups of `prefixes`."""
    run = (*_COV_RUN, "--cov", f"--cov-report=json:{_COV}")
    check = (*PYTHON, "--check-coverage", _COV, *prefixes)
    return _g(check_id, f"{what}: fresh coverage JSON", "cli", run, 1200), _g(
        check_id, f"{what}: U11-32 targets", "cli", check, 120
    )


# fmt: off
_TABLE_G: Final = (
    _g("G1.1", "generator targets and determinism", "bench",
       ("BT11-01", "BT11-02", "BT11-03", "BT11-07", "IT11-01", "IT11-02"), 3600),
    _g("G1.2", "files connector ingests the generated inbox", "pytest", ("IT01-01",), 600),
    _g("G1.3", "full build < 10 min, DQ < 1 min, < 15 GB; lake_small snapshot; dedupe, "
       "drift, blue/green", "bench",
       ("BT02-01", "BT02-02", "BT02-03", "IT02-21", "IT01-03", "IT01-04", "IT02-32"), 3600),
    *_coverage("G1.4", "coverage targets for core and store", "herness/core/", "herness/store/"),
    _g("G2.1", "metrics_tiny covers every catalog metric", "pytest", ("UT11-38",), 1200),
    *_coverage("G2.1", "metrics coverage >= 95 %", "herness/metrics/"),
    _g("G2.2", "run_scoring < 5 min on full", "bench", ("BT04-06",), 1800),
    _g("G2.3", "planted truths T1, T2, T2c, T4, T5 from spec 04 outputs", "pytest",
       (*_ids("IT11", 5, 6, 7, 8), "IT04-08"), 3600),
    _g("G2.4", "meta.evidence re-runs reproduce result_hash", "pytest", ("IT04-01",), 1200),
    _g("G3.1", "mock golden run in CI", "bench", ("ET11-01", "BT11-06"), 900),
    _g("G3.2", "real local golden, standard depth, >= 30 questions, 0 unsupported numbers",
       "cli", (*_EVAL, "--suite", "golden", "--profile", "local", "--depth", "standard"), 14400),
    _g("G3.3", "spec 08 F1-F7, F11 plus X1, X2, X6, X7", "pytest",
       (*_ids("FT08", 1, 2, 3, 4, 5, 6, 7, 11), *_ids("FT11", 1, 2, 6, 7)), 3600),
    _g("G3.4", "spec 06 T1-T16; spec 07 integration 1-3, 5, 6; SQL guard nightly; resume "
       "after kill", "pytest",
       (*_ids("IT06", *range(1, 16)), *_ids("IT07", 1, 2, 3, 5, 6),
        *_ids("ST05", 3, 4, 5), "--hypothesis-profile=nightly"), 7200),
    _g("G4.1", "T2 ARI >= 0.80 and T3 links on full", "bench",
       ("BT11-05", "IT03-12", "IT03-13"), 7200),
    _g("G4.2", "gold set, eval.json cross-check, decider comparison, coverage, escalation, "
       "nightly enrichment", "bench", ("ET11-03", "IT11-25", "BT03-06", "BT03-10"), 7200),
    _g("G4.3", "X3 and X4", "pytest", _ids("FT11", 3, 4), 1800),
    _g("G5.1", "reports render with the evidence appendix; dashboard targets on full",
       "bench", ("BT09-01", "BT09-05"), 7200),
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
