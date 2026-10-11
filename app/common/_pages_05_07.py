"""Private helpers of pages 05 - 07 (impl 09 U09-77 - U09-79): reads, filters, metric blocks.

Every value comes from a named query (U09-63). Text cells are plain strings cut to ``CELL_MAX``
characters and shown only in dataframes or select options, never as Markdown (TH09-02). The
metric table and charts of pages 06 and 07 are cached per build (T09-30, ``_metric_view``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

import pyarrow as pa
import streamlit as st

from app.common import wh
from app.common._metric_view import METRIC_COLUMNS, MetricView, cell, metric_view_data
from app.common.sanitize import safe_markdown
from app.common.widgets import CELL_MAX
from herness.metrics.catalog import load_catalog

__all__ = [
    "ALL", "METRIC_COLUMNS", "NO_VALUES", "PERIODS", "MetricView", "Row", "cell", "choose",
    "metric_names", "metric_view", "metrics_block", "options", "period", "rows",
]  # fmt: skip

ALL: Final = "All"
PERIODS: Final = ("week", "month", "quarter")
NO_VALUES: Final = "No metric values for these filters."
type Row = Mapping[str, object]


def rows(name: str, /, **params: object) -> list[Row]:
    """The rows of named query ``name`` on the current build."""
    return list(wh.query(name, **params).table.to_pylist())


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


@st.cache_data(show_spinner=False, max_entries=64)
def _cached_view(
    build_id: str,
    domain: str,
    period_: str,
    names: tuple[str, ...],
    filters: tuple[tuple[str, str | None], ...],
    _table: pa.Table,
) -> MetricView:
    del build_id, domain, period_, filters  # key parts only: they determine ``_table``
    return metric_view_data(_table, names)


def metric_view(domain: str, period_: str, filters: Mapping[str, str | None]) -> MetricView:
    """The table records and chart specs of the domain's ``metrics.values``, cached per build.

    The key is the build id, domain, period, metric names and filters; the table is not hashed.
    """
    names = metric_names(domain)
    if not names:
        return MetricView([], ())
    result = wh.query("metrics.values", metrics=list(names), period=period_, **filters)
    key = tuple(sorted(filters.items()))
    return _cached_view(result.build_id, domain, period_, names, key, _table=result.table)


def metrics_block(domain: str, period_: str, **filters: str | None) -> None:
    """``metrics.values`` for the domain's metrics: one table, then a line chart per metric."""
    view = metric_view(domain, period_, filters)
    if not view.records:
        st.info(NO_VALUES)
        return
    st.dataframe(view.records, hide_index=True)
    for metric, spec in view.charts:
        st.caption(safe_markdown(metric))
        st.vega_lite_chart(spec, use_container_width=True)
