"""Stage ``dq`` of ``build_pipeline`` (impl 02 U02-102, T02-20; design 02 §4.8; TH02-17).

Private sibling of ``herness.model.build`` (impl 02 §2 module-map note), split off for the
400-line budget of ``build.py``. ``build`` imports this module; this module reaches
``build`` only inside functions, so there is no import cycle at module level.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from pydantic import JsonValue

from herness.core import time as clock
from herness.model import meta
from herness.model.dq import DqOutcome, evaluate_gate
from herness.model.errors import DqGateFailed
from herness.model.refdata import register_prev_row_counts
from herness.store import warehouse

if TYPE_CHECKING:
    from herness.model.build import StageStatus, _BuildRun
    from herness.store.layout import DataLayout


def _prev_row_counts(build_id: str, layout: DataLayout) -> dict[str, int] | None:
    """``meta.build.row_counts`` of the ``CURRENT`` build when it is another build (step 2).

    None without ``CURRENT``, when ``CURRENT`` names this build, or when the stored value is
    not a JSON object; ``register_prev_row_counts`` filters the entries.
    """
    current = warehouse.read_current(layout=layout)
    if current is None or current == build_id:
        return None
    with warehouse.open_readonly(current, layout=layout) as old:
        row = old.execute("SELECT CAST(row_counts AS VARCHAR) FROM meta.build").fetchone()
    stored = json.loads(row[0]) if row is not None and row[0] is not None else None
    return stored if isinstance(stored, dict) else None


def _result(outcome: DqOutcome) -> dict[str, JsonValue]:
    return {
        "checks": outcome.checks,
        "failed_errors": list(outcome.failed_errors),
        "failed_warnings": list(outcome.failed_warnings),
    }


def stage_dq(run: _BuildRun) -> StageStatus:
    """Stage ``dq``: run 900-999, refresh the row counts and evaluate the gate (U02-102).

    A gate that does not pass marks the build ``failed`` and raises ``DqGateFailed`` naming
    the failed ``error`` checks; ``CURRENT`` is never touched. The outcome is kept in the
    job result under ``dq``.
    """
    from herness.model import build  # noqa: PLC0415 - build imports this module

    con = build._connection(run)
    register_prev_row_counts(con, _prev_row_counts(run.build_id, run.layout))
    if build._sql(run, 900, 999) == "yield":
        return "yield"
    run.row_counts = meta.collect_row_counts(con, ["core", "enrich", "metrics", "score"])
    meta.update_build_row(con, row_counts=run.row_counts)
    outcome = evaluate_gate(con, build_id=run.build_id)
    run.result["dq"] = _result(outcome)
    if not outcome.passed:
        meta.update_build_row(con, status="failed", finished_at=clock.now())
        raise DqGateFailed(run.build_id, outcome.failed_errors)
    con.execute("CHECKPOINT")
    return "done"
