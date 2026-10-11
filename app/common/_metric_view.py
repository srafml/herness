"""The per-build metric view of pages 06 and 07 (impl 09 T09-30): table records and chart specs.

``metric_view_data`` turns a ``metrics.values`` table into plain Python values: the table's
records, ids and numbers, and one line-chart Vega-Lite spec per metric (``x = period_start``,
``y = value``, ``color = entity``). The result is cached per build by ``_pages_05_07`` so a
rerun does no chart construction (TH09-11: no ticket text, only catalog metric names and ids).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import altair as alt
import pyarrow as pa

from app.common.widgets import CELL_MAX

METRIC_COLUMNS: Final = ("metric", "entity_type", "entity_id", "period_start", "value",
                         "sample_size", "unit", "flags")  # fmt: skip
NUMERIC: Final = frozenset({"value", "sample_size"})


def cell(value: object, limit: int = CELL_MAX) -> str:
    """A plain-text cell: lists joined with ``, ``, ``—`` for null, cut to ``limit``."""
    if value is None:
        return "—"
    if isinstance(value, list | tuple):
        value = ", ".join(str(item) for item in value)
    return str(value)[:limit]


@dataclass(frozen=True)
class MetricView:
    """Table records and ``(metric, vega_lite_spec)`` pairs of one metric block."""

    records: list[dict[str, object]]
    charts: tuple[tuple[str, dict[str, Any]], ...]


def chart_spec(series: Sequence[Mapping[str, object]]) -> dict[str, Any]:
    """The line chart of one metric: ``period_start`` by ``value``, one line per ``entity``."""
    values = [{**row, "period_start": str(row["period_start"])} for row in series]
    chart = (
        alt.Chart(alt.InlineData(values=values))
        .mark_line()
        .encode(
            x=alt.X("period_start:T", title="period_start"),
            y=alt.Y("value:Q", title="value"),
            color=alt.Color("entity:N", title="entity"),
            tooltip=["period_start:T", "value:Q", "entity:N"],
        )
        .interactive()
    )
    return dict(chart.to_dict())


def metric_view_data(table: pa.Table, names: tuple[str, ...]) -> MetricView:
    """The records and one chart spec per metric (in ``names`` order) of ``table``."""
    found = table.to_pylist()
    records = [{c: (r[c] if c in NUMERIC else cell(r[c])) for c in METRIC_COLUMNS} for r in found]
    charts: list[tuple[str, dict[str, Any]]] = []
    for metric in names:
        series = [
            {"period_start": r["period_start"], "value": r["value"],
             "entity": cell(f"{r['entity_type']}:{r['entity_id']}")}
            for r in found if r["metric"] == metric and r["value"] is not None
        ]  # fmt: skip
        if series:
            charts.append((metric, chart_spec(series)))
    return MetricView(records, tuple(charts))
