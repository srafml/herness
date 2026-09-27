"""The DQ gate over ``meta.dq_result`` (impl 02 U02-93, U02-94; design 02 §4.8; TH02-17).

``error`` checks that failed block promotion; failed ``warn`` checks are logged and kept in
``meta.dq_result`` for reports. The gate fails closed: a table with no rows, or a row whose
severity or result is not one of the known values, is a ``SchemaViolation`` rather than a
pass. Rows of every producer count (this spec's ``dq900`` rows and spec 04 invariants).
"""

from __future__ import annotations

import dataclasses
from typing import Final

import duckdb

from herness.core.errors import SchemaViolation
from herness.core.logging import get_logger

_SELECT: Final = "SELECT check_name, severity, passed FROM meta.dq_result ORDER BY check_name"
_SEVERITIES: Final = frozenset({"error", "warn"})

_log = get_logger("model.dq")


@dataclasses.dataclass(frozen=True, slots=True)
class DqOutcome:
    """Result of the DQ gate (U02-93): ``passed`` iff no failed error and at least one check."""

    passed: bool
    failed_errors: tuple[str, ...]
    failed_warnings: tuple[str, ...]
    checks: int

    def __post_init__(self) -> None:
        if self.passed != (self.failed_errors == () and self.checks > 0):
            msg = "DqOutcome.passed must equal (no failed errors and checks > 0)"
            raise ValueError(msg)


def _rows(con: duckdb.DuckDBPyConnection) -> list[tuple[object, ...]]:
    try:
        return con.execute(_SELECT).fetchall()
    except duckdb.Error as exc:
        msg = "cannot read DQ results"
        raise SchemaViolation(msg, error_type=type(exc).__name__) from None


def evaluate_gate(con: duckdb.DuckDBPyConnection, *, build_id: str | None = None) -> DqOutcome:
    """Decide promotion from ``meta.dq_result`` on the build connection (U02-94).

    Raises SchemaViolation when there are no rows (fail closed, TH02-17) or a row has an
    unknown severity or a NULL result. Logs ``model.dq.evaluated`` and one
    ``model.dq.check_failed`` per failed check (ERROR for ``error``, WARNING for ``warn``).
    """
    rows = _rows(con)
    if not rows:
        msg = "no DQ results for build"
        raise SchemaViolation(msg, build_id=build_id)
    errors: list[str] = []
    warnings: list[str] = []
    for check_name, severity, passed in rows:
        if severity not in _SEVERITIES or not isinstance(passed, bool):
            msg = "invalid DQ result row"
            raise SchemaViolation(msg, build_id=build_id)
        if not passed:
            (errors if severity == "error" else warnings).append(str(check_name))
    outcome = DqOutcome(
        passed=not errors,
        failed_errors=tuple(errors),
        failed_warnings=tuple(warnings),
        checks=len(rows),
    )
    for name in errors:
        _log.error("model.dq.check_failed", build_id=build_id, check_name=name, severity="error")
    for name in warnings:
        _log.warning("model.dq.check_failed", build_id=build_id, check_name=name, severity="warn")
    _log.info(
        "model.dq.evaluated",
        build_id=build_id,
        checks=outcome.checks,
        failed_errors=len(errors),
        failed_warnings=len(warnings),
    )
    return outcome
