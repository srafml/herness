"""Change Health page (impl 09 U09-78; design §5.4): change metrics and change-caused incidents.

Metrics are the catalog's ``change`` domain read through ``metrics.values``. The change-caused
incident list shows identifiers, priorities, times and link scores only, never ticket text
(TH09-11).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import streamlit as st

from app.common._pages_05_07 import cell, choose, metrics_block, options, period, rows
from app.common.widgets import PageContext, block, page

NO_LINKS: Final = "No incident-change links in this build."
NO_INCIDENTS: Final = "No change-caused incidents for these filters."
INCIDENT_COLUMNS: Final = ("number", "opened_at", "priority", "service_id", "team_id",
                           "change_id", "method")  # fmt: skip


@dataclass(frozen=True)
class _Filters:
    service_id: str | None = None
    team_id: str | None = None
    period: str = "month"

    def entity(self) -> dict[str, str | None]:
        """``metrics.values`` entity filter: the service when chosen, else the team."""
        if self.service_id is not None:
            return {"entity_type": "service", "entity_id": self.service_id}
        if self.team_id is not None:
            return {"entity_type": "team", "entity_id": self.team_id}
        return {}


def _filters() -> _Filters:
    services = options("entities.services", "service_id")
    teams = options("entities.teams", "team_id")
    left, middle, right = st.columns(3)
    with left:
        service = choose("Service", services, key="change_service")
    with middle:
        team = choose("Team", teams, key="change_team")
    with right:
        chosen_period = period(key="change_period")
    return _Filters(service, team, chosen_period)


def _metrics(chosen: _Filters) -> None:
    if chosen.service_id is not None and chosen.team_id is not None:
        st.caption("Metrics follow the service filter when a service and a team are chosen.")
    metrics_block("change", chosen.period, **chosen.entity())


def _caused(chosen: _Filters) -> None:
    counts = rows("change.link_counts")
    if counts:
        st.dataframe([{"method": cell(r["method"]), "links": r["links"]} for r in counts],
                     hide_index=True)  # fmt: skip
    else:
        st.info(NO_LINKS)
    found = rows("change.caused_incidents", service_id=chosen.service_id, team_id=chosen.team_id)
    if not found:
        st.caption(NO_INCIDENTS)
        return
    st.dataframe(
        [{**{c: cell(r[c]) for c in INCIDENT_COLUMNS}, "score": r["score"]} for r in found],
        hide_index=True,
    )


def body(ctx: PageContext) -> None:
    """Filters, change metrics and change-caused incidents, each block isolated."""
    del ctx
    st.title("Change Health")
    st.subheader("Filters")
    chosen = _Filters()
    with block("Filters"):
        chosen = _filters()
    st.subheader("Metrics")
    with block("Metrics"):
        _metrics(chosen)
    st.subheader("Change-caused incidents")
    with block("Change-caused incidents"):
        _caused(chosen)


page("Change Health", body)
