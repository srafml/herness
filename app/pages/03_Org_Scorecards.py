"""Org Scorecards page (impl 09 U09-75; design §5.4): peer scores, a monthly trend, evidence.

A viewer page over the current build; every number comes from a named query (``org.*``).
``_score_table`` takes an optional extra column: the coverage column (T09-15b, user decision
2026-10-10 1a) comes from ``org.coverage``, the covered-weight fraction of design 04 §5.8 step 7.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Final, cast

import pyarrow as pa
import streamlit as st

from app.common import wh
from app.common.widgets import PageContext, block, evidence, page

ALL: Final = "All"
ENTITY_TYPES: Final = ("team", "service", "org")
NAMES: Final = {"team": ("entities.teams", "team_id"), "service": ("entities.services",
                "service_id"), "org": ("org.list", "org_id")}  # fmt: skip
SHOWN: Final = ("rank", "entity_id", "value", "peer_median", "z_score", "sample_size",
                "composite", "peer_group", "flags")  # fmt: skip
TREND_PERIOD: Final = "month"
COVERAGE_WARN: Final = 0.8  # warning badge below this; the §5.8 null threshold (0.5) is unchanged
COVERAGE_HELP: Final = "Scores are normalised by covered metric weight (coverage is that share)."
type Row = Mapping[str, object]
type ExtraColumn = tuple[str, Callable[[Row], object]]


def _ids(raw: object) -> list[str]:
    return [str(q) for q in raw] if isinstance(raw, list | tuple) else []


def _selected(state: object, count: int) -> int:
    """The selected row index; the top row when nothing (or nothing valid) is selected."""
    rows = getattr(getattr(state, "selection", None), "rows", None)
    picked = int(rows[0]) if rows else 0
    return picked if 0 <= picked < count else 0


def _filters() -> dict[str, str | None] | None:
    """Entity type, org subtree and metric; ``None`` when the type has no scores."""
    orgs = {str(r["org_id"]): f"{r['name']} ({r['org_id']})"
            for r in wh.query("org.list").table.to_pylist()}  # fmt: skip
    left, middle, right = st.columns(3)
    entity_type = left.selectbox("Entity type", ENTITY_TYPES) or ENTITY_TYPES[0]
    org = middle.selectbox("Org subtree", [ALL, *orgs], format_func=lambda k: orgs.get(k, k))
    rows = wh.query("org.metrics", entity_type=entity_type).table.to_pylist()
    metrics = [str(r["metric"]) for r in rows]
    if not metrics:
        st.caption("No scores for this entity type.")
        return None
    metric = right.selectbox("Metric", metrics) or metrics[0]
    return {"entity_type": entity_type, "org_id": None if org == ALL else org, "metric": metric}


def _names(entity_type: str) -> dict[str, str]:
    name, key = NAMES[entity_type]
    return {str(r[key]): str(r["name"]) for r in wh.query(name).table.to_pylist()}


def _score_table(rows: Sequence[Row], names: Mapping[str, str],
                 extra: ExtraColumn | None = None) -> object:  # fmt: skip
    """The peer table with single-row selection; ``extra`` adds one column after the name."""
    shown: list[dict[str, object]] = []
    for row in rows:
        cells = {c: row[c] for c in SHOWN}
        cells["name"] = names.get(str(row["entity_id"]), "")
        if extra is not None:
            cells[extra[0]] = extra[1](row)
        cells["unconfirmed"] = "Unconfirmed weights" if row["unconfirmed"] else ""
        shown.append(cells)
    order = ["rank", "entity_id", "name", *([extra[0]] if extra else []), *SHOWN[2:],
             "unconfirmed"]  # fmt: skip
    return st.dataframe(
        pa.Table.from_pylist(shown),
        hide_index=True,
        column_order=order,
        on_select="rerun",
        selection_mode="single-row",
        key="org_scores",
    )


def _coverage(entity_type: str) -> dict[str, float | None]:
    rows = wh.query("org.coverage", entity_type=entity_type).table.to_pylist()
    return {str(r["entity_id"]): r["coverage"] for r in rows}


def _coverage_badge(value: object) -> None:
    """A coloured badge beside the score (orange below ``COVERAGE_WARN``) and the help line."""
    if not isinstance(value, float):
        return
    colour = "green" if value >= COVERAGE_WARN else "orange"
    st.markdown(f":{colour}-badge[Coverage {value:.0%}]")
    st.caption(COVERAGE_HELP)


def _peer_table(params: Mapping[str, str | None]) -> Row | None:
    result = wh.query("org.scores", **params)
    rows = cast("list[Row]", result.table.to_pylist())
    if not rows:
        st.caption("No scores match these filters.")
        return None
    entity_type = str(params["entity_type"])
    coverage = _coverage(entity_type)
    state = _score_table(rows, _names(entity_type),
                         ("coverage", lambda r: coverage.get(str(r["entity_id"]))))  # fmt: skip
    if result.truncated:
        st.caption("Showing the first rows only.")
    chosen = rows[_selected(state, len(rows))]
    _coverage_badge(coverage.get(str(chosen["entity_id"])))
    return chosen


def _trend(row: Row) -> None:
    rows = wh.query("org.trend", metric=row["metric"], entity_type=row["entity_type"],
                    entity_id=row["entity_id"], period=TREND_PERIOD).table.to_pylist()  # fmt: skip
    st.caption(f"{row['metric']} · {row['entity_type']} {row['entity_id']} · last 8 months")
    points = [{"period_start": r["period_start"], "value": r["value"]}
              for r in reversed(rows) if r["value"] is not None]  # fmt: skip
    if not points:
        st.caption("No monthly values for this entity.")
        return
    st.line_chart(pa.Table.from_pylist(points), x="period_start", y="value")


def body(ctx: PageContext) -> None:
    """Filters, the peer table, the selected row's trend and evidence, each block isolated."""
    del ctx
    st.title("Org Scorecards")
    st.subheader("Filters")
    params: dict[str, str | None] | None = None
    with block("Filters"):
        params = _filters()
    if params is None:
        return
    st.subheader("Peer table")
    selected: Row | None = None
    with block("Peer table"):
        selected = _peer_table(params)
    if selected is None:
        return
    st.subheader("Trend")
    with block("Trend"):
        _trend(selected)
    with block("Evidence"):
        evidence(_ids(selected["query_ids"]))


page("Org Scorecards", body)
