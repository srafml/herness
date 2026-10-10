"""Review Queue page (impl 09 U09-81; design §5.4): pending review items by kind, decisions.

Payloads are untrusted: memory content goes through ``safe_markdown``; record text, questions
and field values are plain text or dataframe cells; flags are constant captions; a
``weight_change`` payload is a key/value table (``st.json``; OI-07 default). Approve / Reject
are rendered only for roles that pass ``decide_review``'s own check for the item's kind
(reviewer+, admin for ``weight_change``; TH09-07, ST09-09), and decide only through that
action (impl 02 / impl 07 write the ``review_decision`` audit line; TH09-19).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final, cast

import streamlit as st

from app.common import data, wh
from app.common._pages_08_10 import act, allowed, cell, flash, review_action
from app.common.sanitize import safe_markdown
from app.common.widgets import CELL_MAX, PageContext, block, page
from herness.core import time as clock
from herness.reports.actions import decide_review
from herness.store.ops.shared import ReviewItem

KINDS: Final = ("memory_write", "label_check", "mapping_suggestion", "weight_change")
TAB_LABELS: Final = {
    "memory_write": "Memory writes", "label_check": "Label checks",
    "mapping_suggestion": "Mapping suggestions", "weight_change": "Weight changes",
}  # fmt: skip
FLAGS: Final = ("instruction_like", "conflict", "unverified_numbers")
NO_ITEMS: Final = "No pending items."
NO_TEXT: Final = "Record text unavailable"
ANSWER_MAX: Final = 200
TEXT_MAX: Final = 4000
CONFLICTS_SHOWN: Final = 10
_FIXED_ANSWERS: Final = {"bool": ("true", "false"), "score": ("0", "1", "2", "3")}


def _memory_write(payload: Mapping[str, object]) -> None:
    st.markdown(safe_markdown(str(payload.get("content") or "")))
    flags = payload.get("flags")
    for flag in FLAGS:
        if isinstance(flags, list) and flag in flags:
            st.caption(f"Flag: {flag}")
    conflicts = payload.get("conflicts_with")
    if isinstance(conflicts, list) and conflicts:
        ids = ", ".join(str(c)[:64] for c in conflicts[:CONFLICTS_SHOWN])
        st.caption(safe_markdown(f"Conflicts with: {ids}"))


def _record_text(record_id: object) -> str:
    """The redacted record text (``review.label_text``), or "Record text unavailable"."""
    if not isinstance(record_id, str) or not record_id:
        return NO_TEXT
    try:
        found = wh.query("review.label_text", record_id=record_id).table.to_pylist()
    except wh.NoCurrentBuild:
        return NO_TEXT
    text = found[0].get("text") if found else None
    return str(text)[:TEXT_MAX] if text else NO_TEXT


def _answers(ctx: PageContext, payload: Mapping[str, object]) -> list[str] | None:
    """The allowed answers of the item's question in ``cfg.decisions``; None when not found."""
    decisions = ctx.services.cfg.decisions
    if payload.get("question_set_version") != decisions.question_set_version:
        return None
    question = next((q for q in decisions.questions if q.id == payload.get("question")), None)
    if question is None:
        return None
    if question.type in _FIXED_ANSWERS:
        return list(_FIXED_ANSWERS[question.type])
    return sorted(question.options) if question.options else None


def _label_check(payload: Mapping[str, object]) -> None:
    st.text(_record_text(payload.get("record_id")))
    st.dataframe(
        [{"question": cell(payload.get("question")), "answer": cell(payload.get("answer")),
          "probability": cell(payload.get("probability")),
          "decider": cell(payload.get("decider"))}],
        hide_index=True,
    )  # fmt: skip


def _answer_input(item: ReviewItem, answers: Sequence[str] | None) -> str | None:
    key = f"review_answer_{item.item_id}"
    if answers is None:
        return st.text_input("Correct answer", max_chars=ANSWER_MAX, key=key)
    current = item.payload.get("answer")
    index = answers.index(current) if isinstance(current, str) and current in answers else 0
    choice: str | None = st.selectbox("Correct answer", list(answers), index=index, key=key)
    return choice


def _form(ctx: PageContext, item: ReviewItem, answers: Sequence[str] | None) -> None:
    """Approve / Reject with a note (required to reject); keyed widgets keep their values."""
    item_id = item.item_id
    with st.form(f"review_form_{item_id}"):
        answer = _answer_input(item, answers) if item.kind == "label_check" else None
        note = st.text_area("Note (required to reject)", max_chars=1000,
                            key=f"review_note_{item_id}")  # fmt: skip
        left, right = st.columns(2)
        with left:
            approve = st.form_submit_button("Approve", key=f"review_approve_{item_id}")
        with right:
            reject = st.form_submit_button("Reject", key=f"review_reject_{item_id}")
    if not (approve or reject):
        return
    act(
        "Decision",
        lambda: decide_review(
            ctx.actor, item_id=item_id, approve=approve, note=note or None, answer=answer,
            memory=ctx.services.memory, now=clock.now(),
        ),
        lambda status: f"Item {item_id} {status}.",
        reset=(f"review_answer_{item_id}", f"review_note_{item_id}"),
    )  # fmt: skip


def _item(ctx: PageContext, item: ReviewItem) -> None:
    payload = item.payload
    answers: list[str] | None = None
    with st.container(border=True):
        st.caption(f"Item {item.item_id} · created {item.created_at:%Y-%m-%d %H:%M} UTC")
        if item.kind == "memory_write":
            _memory_write(payload)
        elif item.kind == "label_check":
            _label_check(payload)
            answers = _answers(ctx, payload)
        elif item.kind == "mapping_suggestion":
            st.dataframe([{"field": str(k)[:CELL_MAX], "value": cell(v)}
                          for k, v in payload.items()], hide_index=True)  # fmt: skip
        else:
            st.json(dict(payload))  # weight_change: schema undefined (OI-07)
        if allowed(ctx.actor, review_action(item.kind)):
            _form(ctx, item, answers)


def body(ctx: PageContext) -> None:
    """One tab per kind with its pending count; each item isolated in its own block."""
    st.title("Review Queue")
    flash()
    pending: dict[str, list[ReviewItem]] = {}
    with block("Review items"):
        pending = {
            kind: cast("list[ReviewItem]",
                       data.ops_read("list_review_items", kind=kind, status="pending"))
            for kind in KINDS
        }  # fmt: skip
    if not pending:
        return
    tabs = st.tabs([f"{TAB_LABELS[kind]} ({len(pending[kind])})" for kind in KINDS])
    for tab, kind in zip(tabs, KINDS, strict=True):
        with tab:
            if not pending[kind]:
                st.info(NO_ITEMS)
            for item in pending[kind]:
                with block(f"Item {item.item_id}"):
                    _item(ctx, item)


page("Review Queue", body, needs_warehouse=False)
