"""Recommendations page (impl 09 U09-80; design §5.4): filters, list, outcomes, decisions.

Summaries are model text rendered through ``render_marked_markdown`` (TH09-02); confidence
fields go to ``st.json``. The decide form is shown only to roles that pass
``decide_recommendation``'s own role check and writes only through that action, which audits
the decision with ``user_ref`` (TH09-07, TH09-19). Its keyed widgets keep their values when the
action is refused or fails (FT09-01).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Final, Literal, cast

import streamlit as st

from app.common import data
from app.common._pages_08_10 import Row, act, allowed, cell, choose, flash, numbers, rows
from app.common.auth import display_name
from app.common.sanitize import render_marked_markdown, safe_markdown
from app.common.widgets import PageContext, block, evidence, page
from herness.reports.actions import decide_recommendation

REVIEW_KINDS: Final = ("funding_review", "org_review")
KINDS: Final = ("fund", "org_action")
STATES: Final = ("undecided", "accepted", "rejected", "deferred")
CHOICES: Final[Mapping[str, Literal["accepted", "rejected", "deferred"]]] = {
    "Accept": "accepted", "Reject": "rejected", "Defer": "deferred",
}  # fmt: skip
NO_RECS: Final = "No recommendations match the filters."


@dataclass(frozen=True)
class _Filters:
    run_id: str | None = None
    kind: str | None = None
    state: str | None = None


def _filters() -> _Filters:
    runs = [str(r["run_id"]) for r in rows("ui_list_runs", limit=100) if r["kind"] in REVIEW_KINDS]
    left, middle, right = st.columns(3)
    with left:
        run_id = choose("Run", runs, key="recs_run")
    with middle:
        kind = choose("Kind", KINDS, key="recs_kind")
    with right:
        state = choose("Decision state", STATES, key="recs_state")
    return _Filters(run_id, kind, state)


def _decision_line(decision: Row | None, ctx: PageContext) -> str:
    if decision is None:
        return "Undecided"
    who = display_name(str(decision["decided_by"]), ctx.services.roster)
    return safe_markdown(f"{decision['decision']} by {who} at {decision['decided_at']}")


def _outcomes(found: list[Row]) -> None:
    if not found:
        st.caption("No outcomes measured yet.")
        return
    st.dataframe(
        [
            {"measurement": o["measurement"], "metric": cell(o["metric"]),
             "baseline": o["baseline"], "actual": o["actual"], "delta": o["delta"],
             "verdict": cell(o["verdict"]), "measured_at": cell(o["measured_at"])}
            for o in found
        ],
        hide_index=True,
    )  # fmt: skip
    evidence([str(o["query_id"]) for o in found if o["query_id"]])


def _decide(ctx: PageContext, rec_id: str) -> None:
    """The decide form (reviewer+); widget state is keyed per recommendation."""
    with st.form(f"rec_form_{rec_id}"):
        choice = st.radio("Decision", list(CHOICES), key=f"rec_decision_{rec_id}", horizontal=True)
        reason = st.text_area("Reason (10 to 1000 characters)", key=f"rec_reason_{rec_id}")
        picked = st.date_input("Effective date (optional)", value=None,
                               key=f"rec_effective_{rec_id}")  # fmt: skip
        sent = st.form_submit_button("Submit decision", key=f"rec_submit_{rec_id}")
    if not sent:
        return
    effective = picked if isinstance(picked, date) else None
    act(
        "Decision",
        lambda: decide_recommendation(
            ctx.actor, rec_id=rec_id, decision=CHOICES[choice or "Accept"], reason=reason or "",
            effective_at=effective, memory=ctx.services.memory,
        ),
        lambda _: f"Decision recorded for {rec_id}.",
        reset=(f"rec_decision_{rec_id}", f"rec_reason_{rec_id}", f"rec_effective_{rec_id}"),
    )  # fmt: skip


def _recommendation(ctx: PageContext, rec: Row, decision: Row | None, found: list[Row]) -> None:
    rec_id = str(rec["rec_id"])
    with st.container(border=True):
        st.markdown(render_marked_markdown(str(rec["summary"]), numbers(rec["numbers"])))
        st.caption(safe_markdown(
            f"{rec['kind']} · {rec['target_type']} {rec['target_id']} · run {rec['run_id']}"
            f" · {rec_id}"
        ))  # fmt: skip
        st.json({"confidence": rec["confidence"], "confidence_basis": rec["confidence_basis"]})
        st.caption(_decision_line(decision, ctx))
        _outcomes(found)
        if allowed(ctx.actor, "decide_recommendation"):
            _decide(ctx, rec_id)


def _list(ctx: PageContext, chosen: _Filters) -> None:
    recs = rows("ui_list_recommendations", run_id=chosen.run_id, kind=chosen.kind, limit=500)
    ids = tuple(str(r["rec_id"]) for r in recs)
    decisions = cast("Mapping[str, Row]", data.ops_read("ui_latest_decisions", rec_ids=ids))
    outcomes = cast("Mapping[str, list[Row]]", data.ops_read("ui_list_outcomes", rec_ids=ids))

    def state(rec_id: str) -> str:
        found = decisions.get(rec_id)
        return "undecided" if found is None else str(found["decision"])

    shown = [r for r in recs if chosen.state in {None, state(str(r["rec_id"]))}]
    if not shown:
        st.info(NO_RECS)
        return
    st.caption(f"{len(shown)} recommendations")
    for rec in shown:
        rec_id = str(rec["rec_id"])
        _recommendation(ctx, rec, decisions.get(rec_id), outcomes.get(rec_id, []))


def body(ctx: PageContext) -> None:
    """Filters, then the list with each recommendation's outcomes and decide form."""
    st.title("Recommendations")
    flash()
    st.subheader("Filters")
    chosen = _Filters()
    with block("Filters"):
        chosen = _filters()
    st.subheader("List")
    with block("List"):
        _list(ctx, chosen)


page("Recommendations", body, needs_warehouse=False)
