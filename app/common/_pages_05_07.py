"""Private helpers of pages 05 - 07 (impl 09 U09-77 - U09-79): reads, filters, metric blocks.

Every value comes from a named query (U09-63). Text cells are plain strings cut to ``CELL_MAX``
characters and shown only in dataframes or select options, never as Markdown (TH09-02).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

import pyarrow as pa
import streamlit as st

from app.common import wh
from app.common.sanitize import safe_markdown
from app.common.widgets import CELL_MAX
from herness.metrics.catalog import load_catalog

ALL: Final = "All"
PERIODS: Final = ("week", "month", "quarter")
NO_VALUES: Final = "No metric values for these filters."
METRIC_COLUMNS: Final = ("metric", "entity_type", "entity_id", "period_start", "value",
                         "sample_size", "unit", "flags")  # fmt: skip
type Row = Mapping[str, object]


def rows(name: str, /, **params: object) -> list[Row]:
    """The rows of named query ``name`` on the current build."""
    return list(wh.query(name, **params).table.to_pylist())


def cell(value: object, limit: int = CELL_MAX) -> str:
    """A plain-text cell: lists joined with ``, ``, ``—`` for null, cut to ``limit``."""
    if value is None:
        return "—"
    if isinstance(value, list | tuple):
        value = ", ".join(str(item) for item in value)
    return str(value)[:limit]


def options(name: str, id_key: str, label_key: str = "name") -> list[tuple[str, str]]:
    """``(id, label)`` pairs of a named query for a filter."""
    return [(str(r[id_key]), cell(r[label_key])) for r in rows(name)]


def choose(label: str, pairs: Sequence[tuple[str, str]], *, key: str) -> str | None:
    """A select box with ``All`` first; returns the chosen id (``None`` for all)."""
    names = dict(pairs)

    def show(value: str | None) -> str:
        if value is None:
            return ALL
        name = names.get(value, value)
        return name if name == value else f"{name} ({value})"[:CELL_MAX]

    choice: str | None = st.selectbox(label, [None, *names], format_func=show, key=key)
    return choice


def period(*, key: str) -> str:
    """The period filter (``week``, ``month``, ``quarter``; default ``month``)."""
    return str(st.selectbox("Period", PERIODS, index=1, key=key))


def metric_names(domain: str) -> tuple[str, ...]:
    """Enabled catalog metrics of ``domain`` (``MetricDef.domain``), sorted.

    ``load_catalog`` is memoised per process (T04-24), so no ``st.cache_data`` layer here: clearing
    Streamlit caches no longer forces a reload.
    """
    catalog = load_catalog()
    return tuple(n for n in catalog.names() if catalog.get(n).domain == domain)


def metrics_block(domain: str, period_: str, **filters: str | None) -> None:
    """``metrics.values`` for the domain's metrics: one table, then a line chart per metric."""
    names = metric_names(domain)
    found = rows("metrics.values", metrics=list(names), period=period_, **filters) if names else []
    if not found:
        st.info(NO_VALUES)
        return
    st.dataframe(
        [{c: (r[c] if c in {"value", "sample_size"} else cell(r[c])) for c in METRIC_COLUMNS}
         for r in found],
        hide_index=True,
    )  # fmt: skip
    for metric in names:
        series = [
            {"period_start": r["period_start"], "value": r["value"],
             "entity": cell(f"{r['entity_type']}:{r['entity_id']}")}
            for r in found if r["metric"] == metric and r["value"] is not None
        ]  # fmt: skip
        if series:
            st.caption(safe_markdown(metric))
            st.line_chart(pa.Table.from_pylist(series), x="period_start", y="value", color="entity")
