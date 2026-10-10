"""Delivery Health page (impl 09 U09-79; design §5.4): delivery metrics by project and team.

Metrics are the catalog's ``delivery`` domain read through ``metrics.values``; the project
filter restricts work-item rows to that project (U09-63), the team filter selects team rows.
"""

from __future__ import annotations

from dataclasses import dataclass

import streamlit as st

from app.common._pages_05_07 import choose, metrics_block, options, period
from app.common.widgets import PageContext, block, page


@dataclass(frozen=True)
class _Filters:
    project: str | None = None
    team_id: str | None = None
    period: str = "month"


def _filters() -> _Filters:
    projects = options("funding.projects", "project", "project")
    teams = options("entities.teams", "team_id")
    left, middle, right = st.columns(3)
    with left:
        project = choose("Project", projects, key="delivery_project")
    with middle:
        team = choose("Team", teams, key="delivery_team")
    with right:
        chosen_period = period(key="delivery_period")
    return _Filters(project, team, chosen_period)


def _metrics(chosen: _Filters) -> None:
    team = {} if chosen.team_id is None else {"entity_type": "team", "entity_id": chosen.team_id}
    metrics_block("delivery", chosen.period, project=chosen.project, **team)


def body(ctx: PageContext) -> None:
    """Filters and delivery metrics, each block isolated."""
    del ctx
    st.title("Delivery Health")
    st.subheader("Filters")
    chosen = _Filters()
    with block("Filters"):
        chosen = _filters()
    st.subheader("Metrics")
    with block("Metrics"):
        _metrics(chosen)


page("Delivery Health", body)
