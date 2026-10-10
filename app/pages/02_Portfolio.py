"""Portfolio page (impl 09 U09-74; design §5.4): portfolio scenarios, their rows and a chart.

A viewer page over the current build. Every number comes from a named query (``portfolio.*``);
``title`` is shown only as ``redact_text(title)`` (design §9.3, D9; TH09-11).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Final

import pyarrow as pa
import streamlit as st

from app.common import wh
from app.common.widgets import PageContext, block, evidence, page
from herness.core import redact
from herness.reports._format import format_value

NO_COMPARE: Final = "None"
SHOWN: Final = ("order_rank", "candidate_id", "title", "selected", "expected_impact_usd",
                "effort_cost_usd", "flags")  # fmt: skip
X: Final = "cumulative_effort_usd"
Y: Final = "cumulative_impact_usd"
type Row = Mapping[str, object]


def _title(raw: object) -> str:
    """``title`` through ``redact_text``; a failed redaction shows ``[redacted]`` (TH09-11)."""
    out = redact.redact_text("" if raw is None else str(raw))
    return "[redacted]" if out is None else out


def _rows(scenario: str) -> list[Row]:
    rows: list[Row] = wh.query("portfolio.rows", scenario=scenario).table.to_pylist()
    return rows


def _usd(value: object) -> Decimal:
    """A cell as a ``Decimal``; NULL (no estimate) counts as 0."""
    return Decimal(str(value)) if isinstance(value, int | float | Decimal) else Decimal(0)


def _scenario() -> tuple[Row, Row | None] | None:
    """The scenario select and the optional "Compare with" select."""
    scenarios = {str(r["scenario"]): r for r in wh.query("portfolio.scenarios").table.to_pylist()}
    if not scenarios:
        st.caption("No portfolio scenarios in this build.")
        return None
    left, right = st.columns(2)
    name = left.selectbox("Scenario", list(scenarios)) or next(iter(scenarios))
    others = [s for s in scenarios if s != name]
    other = right.selectbox("Compare with", [None, *others],
                            format_func=lambda s: NO_COMPARE if s is None else s)  # fmt: skip
    return scenarios[name], (None if other is None else scenarios[other])


def _table(scenario: Row) -> None:
    st.caption(f"Solver status: {scenario['solver_status']}")
    rows = _rows(str(scenario["scenario"]))
    if not rows:
        st.caption("No candidates in this scenario.")
        return
    shown = [{c: _title(r[c]) if c == "title" else r[c] for c in SHOWN} for r in rows]
    st.dataframe(pa.Table.from_pylist(shown), hide_index=True)


def _curve(scenario: Row) -> list[dict[str, object]]:
    """Cumulative impact against cumulative effort over the selected rows by ``order_rank``."""
    effort = impact = Decimal(0)
    points: list[dict[str, object]] = [{"scenario": str(scenario["scenario"]), X: 0.0, Y: 0.0}]
    for row in _rows(str(scenario["scenario"])):
        if row["selected"] is True:
            effort += _usd(row["effort_cost_usd"])
            impact += _usd(row["expected_impact_usd"])
            points.append({"scenario": str(scenario["scenario"]), X: float(effort),
                           Y: float(impact)})  # fmt: skip
    return points


def _chart(scenarios: Sequence[Row]) -> None:
    curves = [_curve(s) for s in scenarios]
    for scenario in scenarios:
        st.caption(f"Budget ({scenario['scenario']}): {format_value(_budget(scenario), 'usd')}")
    if all(len(curve) == 1 for curve in curves):
        st.caption("No selected candidates to chart.")
        return
    points = [point for curve in curves for point in curve]
    st.line_chart(pa.Table.from_pylist(points), x=X, y=Y, color="scenario")
    st.caption("Candidates without an effort estimate count as 0.")


def _budget(scenario: Row) -> Decimal | None:
    value = scenario["budget_usd"]
    return None if value is None else _usd(value)


def _query_ids(scenario: Row) -> list[str]:
    raw = [r["query_ids"] for r in _rows(str(scenario["scenario"]))]
    return [str(q) for ids in raw if isinstance(ids, list | tuple) for q in ids]


def body(ctx: PageContext) -> None:
    """Scenario selection, the scenario table, the cumulative chart and evidence."""
    del ctx
    st.title("Portfolio")
    st.subheader("Scenario")
    picked: tuple[Row, Row | None] | None = None
    with block("Scenario"):
        picked = _scenario()
    if picked is None:
        return
    scenario, other = picked
    st.subheader("Table")
    with block("Table"):
        _table(scenario)
    st.subheader("Chart")
    with block("Chart"):
        _chart([scenario] if other is None else [scenario, other])
    with block("Evidence"):
        evidence(_query_ids(scenario))


page("Portfolio", body)
