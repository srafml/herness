"""Funding Ranking page (impl 09 U09-73; design §5.4): ranked funding candidates and detail.

A viewer page over the current build. Every number comes from a named query; ``title`` is shown
only as ``redact_text(title)`` (design §9.3, D9; TH09-11); recommendation summaries and verified
finding claims are model text rendered through ``render_marked_markdown`` (TH09-02).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final, cast

import pyarrow as pa
import streamlit as st
from pydantic import ValidationError

from app.common import data, wh
from app.common.sanitize import render_marked_markdown
from app.common.widgets import PageContext, block, evidence, page
from herness.core import redact
from herness.core.logging import get_logger
from herness.core.types import NumberRef

ALL: Final = "All"
TYPES: Final = (ALL, "epic", "feature", "initiative", "cluster_fix")
SORTS: Final = ("priority", "wsjf")
UNCONFIRMED: Final = "Unconfirmed weights"
TOP_N_MAX: Final = 500
SHOWN: Final = ("rank", "candidate_id", "candidate_type", "title", "priority", "wsjf",
                "annual_pain_usd", "addressable_pain_usd", "expected_reduction", "n_incidents",
                "confidence", "strategic_weight", "effort_cost_usd", "project")  # fmt: skip
ATTRIBUTION: Final = ("tier", "record_id", "record_kind", "weight", "share", "pain_usd",
                      "quality")  # fmt: skip
type Row = Mapping[str, object]
_log = get_logger("app.page")


def _title(raw: object) -> str:
    """``title`` through ``redact_text``; a failed redaction shows ``[redacted]`` (TH09-11)."""
    out = redact.redact_text("" if raw is None else str(raw))
    return "[redacted]" if out is None else out


def _ids(raw: object) -> list[str]:
    return [str(q) for q in raw] if isinstance(raw, list | tuple) else []


def _numbers(raw: object, source: str) -> list[NumberRef]:
    """The stored ``numbers`` list as ``NumberRef`` values; malformed items are skipped and logged
    (DEBUG, position and error count only: the item may hold model text)."""
    refs: list[NumberRef] = []
    for index, item in enumerate(raw if isinstance(raw, list) else ()):
        try:
            refs.append(NumberRef.model_validate(item))
        except ValidationError as exc:
            _log.debug("app.funding.number_dropped", source=source, index=index,
                       errors=exc.error_count())  # fmt: skip
    return refs


def _selected(state: object, count: int) -> int:
    """The selected row index; the top row when nothing (or nothing valid) is selected."""
    rows = getattr(getattr(state, "selection", None), "rows", None)
    picked = int(rows[0]) if rows else 0
    return picked if 0 <= picked < count else 0


def _filters(ctx: PageContext) -> dict[str, object]:
    """Filter widgets; returns the ranking query parameters plus the chosen sort."""
    projects = [str(r["project"]) for r in wh.query("funding.projects").table.to_pylist()]
    orgs = {str(r["org_id"]): f"{r['name']} ({r['org_id']})"
            for r in wh.query("org.list").table.to_pylist()}  # fmt: skip
    default_n = min(max(int(ctx.services.cfg.app.reports.top_n), 1), TOP_N_MAX)
    left, middle, right = st.columns(3)
    kind = left.selectbox("Candidate type", TYPES)
    project = middle.selectbox("Project", [ALL, *projects])
    org = right.selectbox("Org", [ALL, *orgs], format_func=lambda key: orgs.get(key, key))
    left, middle, right = st.columns(3)
    confidence = left.slider("Min confidence", 0.0, 1.0, 0.0, step=0.05)
    top_n = middle.number_input("Top N", 1, TOP_N_MAX, default_n, step=1)
    sort = right.radio("Sort", SORTS, horizontal=True)
    return {
        "candidate_type": None if kind == ALL else kind,
        "project": None if project == ALL else project,
        "org_id": None if org == ALL else org,
        "min_confidence": confidence or None,  # 0 keeps rows with no confidence
        "top_n": int(top_n),
        "sort": sort or SORTS[0],
    }


def _ranking(params: Mapping[str, object]) -> Row | None:
    """The ranked candidates with single-row selection; returns the selected row."""
    query = {k: v for k, v in params.items() if k != "sort"}
    result = wh.query(f"funding.ranking_by_{params['sort']}", **query)
    rows = result.table.to_pylist()
    if not rows:
        st.caption("No candidates match these filters.")
        return None
    shown = [
        {**{c: _title(r[c]) if c == "title" else r[c] for c in SHOWN},
         "flag": UNCONFIRMED if r["unconfirmed"] else ""}
        for r in rows
    ]  # fmt: skip
    state = st.dataframe(pa.Table.from_pylist(shown), hide_index=True, on_select="rerun",
                         selection_mode="single-row", key="funding_ranking")  # fmt: skip
    if result.truncated:
        st.caption("Showing the first rows only.")
    return cast("Row", rows[_selected(state, len(rows))])


def _attribution(candidate_id: str) -> None:
    rows = wh.query("funding.attribution", candidate_id=candidate_id).table.to_pylist()
    if not rows:
        st.caption("No attributed records.")
        return
    st.dataframe(pa.Table.from_pylist([{c: r[c] for c in ATTRIBUTION} for r in rows]),
                 hide_index=True)  # fmt: skip


def _recommendations(candidate_id: str) -> None:
    recs = cast("Sequence[Row]", data.ops_read("ui_list_recommendations", target_id=candidate_id))
    if not recs:
        st.caption("No recommendations for this candidate.")
    for rec in recs:
        st.markdown(
            render_marked_markdown(str(rec["summary"]), _numbers(rec["numbers"], "recommendation"))
        )


def _findings(candidate_id: str) -> None:
    found = data.ops_read(
        "ui_list_findings_for_entity", entity_type="candidate", entity_id=candidate_id
    )
    findings = cast("Sequence[Row]", found)
    if not findings:
        st.caption("No verified findings for this candidate.")
    for finding in findings:
        st.markdown(
            render_marked_markdown(str(finding["claim"]), _numbers(finding["numbers"], "finding"))
        )


def _detail(row: Row) -> None:
    candidate_id = str(row["candidate_id"])
    st.text(f"{candidate_id} · {_title(row['title'])}")
    for name, render in (("Attribution", _attribution), ("Recommendations", _recommendations),
                         ("Verified findings", _findings)):  # fmt: skip
        st.caption(name)
        with block(name):
            render(candidate_id)
    with block("Evidence"):
        evidence(_ids(row["query_ids"]))


def body(ctx: PageContext) -> None:
    """Filters, the ranking and the selected candidate's detail, each block isolated."""
    st.title("Funding Ranking")
    params: dict[str, object] = {"top_n": ctx.services.cfg.app.reports.top_n, "sort": SORTS[0]}
    st.subheader("Filters")
    with block("Filters"):
        params = _filters(ctx)
    st.subheader("Ranking")
    selected: Row | None = None
    with block("Ranking"):
        selected = _ranking(params)
    st.subheader("Detail")
    if selected is None:
        st.caption("Select a candidate to see its detail.")
    else:
        _detail(selected)


page("Funding Ranking", body)
