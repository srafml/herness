"""Action Levers page (impl 09 U09-76; design §5.4): ranked levers per team or service.

A viewer page over the current build; every number comes from the ``levers.list`` named query.
The rationale is ``fill_rationale(template, params)`` (the U09-15 placeholder rule), shown as
dataframe text, never Markdown.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Final, cast

import pyarrow as pa
import streamlit as st

from app.common import wh
from app.common.widgets import PageContext, block, evidence, page
from herness.reports._data import fill_rationale

ALL: Final = "All"
ENTITY_TYPES: Final = (ALL, "team", "service")
ENTITIES: Final = {"team": ("entities.teams", "team_id"),
                   "service": ("entities.services", "service_id")}  # fmt: skip
SHOWN: Final = ("entity_type", "entity_id", "metric", "target_kind", "current_value",
                "target_value", "delta_usd")  # fmt: skip
type Row = Mapping[str, object]


def _ids(raw: object) -> list[str]:
    return [str(q) for q in raw] if isinstance(raw, list | tuple) else []


def _selected(state: object, count: int) -> int:
    """The selected row index; the top row when nothing (or nothing valid) is selected."""
    rows = getattr(getattr(state, "selection", None), "rows", None)
    picked = int(rows[0]) if rows else 0
    return picked if 0 <= picked < count else 0


def _params(raw: object) -> dict[str, object]:
    """``template_params`` (JSON text or a mapping) as a dict; anything else is empty."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, RecursionError):
            return {}
    return {str(k): v for k, v in raw.items()} if isinstance(raw, Mapping) else {}


def _rationale(row: Row) -> str:
    template = row["rationale_template"]
    return (
        "" if template is None else fill_rationale(str(template), _params(row["template_params"]))
    )


def _filters() -> dict[str, str | None]:
    """Entity type and entity; ``None`` values mean "all"."""
    left, right = st.columns(2)
    entity_type = left.selectbox("Entity type", ENTITY_TYPES) or ALL
    if entity_type == ALL:
        right.selectbox("Entity", [ALL], disabled=True)
        return {"entity_type": None, "entity_id": None}
    name, key = ENTITIES[entity_type]
    names = {str(r[key]): f"{r['name']} ({r[key]})" for r in wh.query(name).table.to_pylist()}
    entity = right.selectbox("Entity", [ALL, *names], format_func=lambda k: names.get(k, k))
    return {"entity_type": entity_type, "entity_id": None if entity == ALL else entity}


def _levers(params: Mapping[str, str | None]) -> Row | None:
    """Levers ranked by ``delta_usd`` with single-row selection; returns the selected row."""
    result = wh.query("levers.list", **params)
    rows = cast("list[Row]", result.table.to_pylist())
    if not rows:
        st.caption("No action levers for this selection.")
        return None
    shown = [
        {**{c: r[c] for c in SHOWN}, "rationale": _rationale(r),
         "unconfirmed": "Unconfirmed weights" if r["unconfirmed"] else ""}
        for r in rows
    ]  # fmt: skip
    state = st.dataframe(
        pa.Table.from_pylist(shown),
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key="action_levers",
    )
    if result.truncated:
        st.caption("Showing the first rows only.")
    return rows[_selected(state, len(rows))]


def body(ctx: PageContext) -> None:
    """Filters, the ranked levers and the selected lever's evidence, each block isolated."""
    del ctx
    st.title("Action Levers")
    st.subheader("Filters")
    params: dict[str, str | None] = {"entity_type": None, "entity_id": None}
    with block("Filters"):
        params = _filters()
    st.subheader("Levers")
    selected: Row | None = None
    with block("Levers"):
        selected = _levers(params)
    if selected is not None:
        with block("Evidence"):
            evidence(_ids(selected["query_ids"]))


page("Action Levers", body)
