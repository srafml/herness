"""Clusters page (impl 09 U09-77; design §5.4): filters, cluster list and one cluster's detail.

Cluster labels, root causes and top terms are derived text; member text comes only from
``enrich.text_redacted`` (``clusters.members``). All of it is shown as plain, length-bounded
dataframe cells, never as Markdown (TH09-11, TH09-02).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import streamlit as st

from app.common._pages_05_07 import Row, cell, choose, options, rows
from app.common.widgets import PageContext, block, page

NO_CLUSTERS: Final = "No clusters match the filters."
TEXT_MAX: Final = 1000


@dataclass(frozen=True)
class _Filters:
    service_id: str | None = None
    root_cause: str | None = None
    min_size: int = 1


def _filters() -> _Filters:
    services = options("entities.services", "service_id")
    causes = options("clusters.root_causes", "root_cause_category", "root_cause_category")
    left, middle, right = st.columns(3)
    with left:
        service = choose("Service", services, key="clusters_service")
    with middle:
        cause = choose("Root cause", causes, key="clusters_root_cause")
    with right:
        size = st.number_input("Min size", min_value=1, value=1, step=1, key="clusters_min_size")
    return _Filters(service, cause, int(size))


def _list(chosen: _Filters) -> list[Row]:
    found = rows("clusters.list", service_id=chosen.service_id, root_cause=chosen.root_cause,
                 min_size=chosen.min_size)  # fmt: skip
    if not found:
        st.info(NO_CLUSTERS)
        return []
    st.dataframe(
        [
            {"cluster": cell(r["cluster_id"]), "label": cell(r["label"]),
             "root_cause": cell(r["root_cause_category"]), "size": r["size"],
             "first_seen": cell(r["first_seen"]), "last_seen": cell(r["last_seen"]),
             "top_terms": cell(r["top_terms"])}
            for r in found
        ],
        hide_index=True,
    )  # fmt: skip
    return found


def _detail(found: list[Row]) -> None:
    if not found:
        st.caption("Choose filters that match a cluster to see its detail.")
        return
    by_id = {str(r["cluster_id"]): r for r in found}
    chosen = st.selectbox(
        "Cluster", list(by_id), key="clusters_detail",
        format_func=lambda c: f"{c[:64]} ({by_id[c]['size']} incidents)",
    )  # fmt: skip
    row = by_id[chosen or next(iter(by_id))]
    cluster_id = str(row["cluster_id"])
    st.dataframe([{"label": cell(row["label"]), "top_terms": cell(row["top_terms"])}],
                 hide_index=True)  # fmt: skip
    by_month = rows("clusters.size_over_time", cluster_id=cluster_id)
    if by_month:
        st.bar_chart([{"month": cell(r["month"], 7), "incidents": r["incidents"]}
                      for r in by_month], x="month", y="incidents")  # fmt: skip
    else:
        st.caption("No incidents by month for this cluster.")
    members = rows("clusters.members", cluster_id=cluster_id)
    st.dataframe(
        [
            {"number": cell(m["number"]), "opened_at": cell(m["opened_at"]),
             "priority": cell(m["priority"]), "text": cell(m["text"], TEXT_MAX),
             "membership_prob": m["membership_prob"]}
            for m in members
        ],
        hide_index=True,
    )  # fmt: skip


def body(ctx: PageContext) -> None:
    """Filters, the cluster list and the chosen cluster's detail, each block isolated."""
    del ctx
    st.title("Clusters")
    st.subheader("Filters")
    chosen = _Filters()
    with block("Filters"):
        chosen = _filters()
    st.subheader("Cluster list")
    found: list[Row] = []
    with block("Cluster list"):
        found = _list(chosen)
    st.subheader("Cluster detail")
    with block("Cluster detail"):
        _detail(found)


page("Clusters", body)
