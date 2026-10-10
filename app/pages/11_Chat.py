"""Chat page (impl 09 U09-83; design §5.5): sessions, messages, feedback, corrections, input.

Viewer page with no warehouse requirement. Chat reads go straight to the ops store and are never
cached (TH09-29); every write goes through ``herness.reports.actions``. User rows are plain
text; answers are rendered only through ``render_answer_markdown`` (TH09-02). Sessions are
private: a selected session passes ``require_session_owner`` (TH09-10).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Final, cast

import streamlit as st
from pydantic import ValidationError

from app.common import chat_ui
from app.common.data import after_write
from app.common.sanitize import render_answer_markdown
from app.common.widgets import (
    NO_BUILD,
    PageContext,
    block,
    evidence,
    page,
    user_error,
    verification_badge,
)
from herness.core.errors import HernessError
from herness.core.logging import get_logger
from herness.core.types import NumberRef
from herness.reports import actions
from herness.store.ops.chat import ChatMessageRow, ChatSessionRow, list_chat_messages
from herness.store.ops.chat import list_chat_sessions as _list_sessions

SESSION_KEY: Final = "chat_session_id"
NO_SESSION: Final = "Start a chat with “New chat” in the sidebar."
SAVED: Final = "Saved as pending. A reviewer must approve it before it affects answers or scores."
FAILED: Final = "This answer could not be produced."
ANSWERING: Final = "Answering…"
ENTITY_TYPES: Final = ("", "service", "team", "org", "work_item", "cluster")
LIMIT: Final = "At most 1000 characters."  # enforced by the action: rejected, never cut
_log = get_logger("app.chat")


def _label(row: ChatSessionRow) -> str:
    title = (row["title"] or "Chat")[:40]
    return f"{row['last_active_at']:%Y-%m-%d %H:%M} · {title}"


def _sessions(ctx: PageContext) -> str | None:
    """Sidebar: "New chat" and the actor's own sessions (never cached); the chosen id."""
    with st.sidebar:
        st.subheader("Chats")
        if st.button("New chat", key="chat-new"):
            st.session_state[SESSION_KEY] = actions.start_session(ctx.actor, now=ctx.now)
            after_write()
        labels = {row["session_id"]: _label(row) for row in _list_sessions(ctx.actor.user_ref)}
        ids, current = list(labels), st.session_state.get(SESSION_KEY)
        if current is not None and current not in labels:  # browser-held id: verify, then drop
            del st.session_state[SESSION_KEY]
            actions.require_session_owner(ctx.actor, str(current))  # another user's: not found
        if not ids:
            return None
        chosen = st.selectbox("Session", ids, index=ids.index(current) if current in ids else 0,
                              format_func=labels.__getitem__)  # fmt: skip
    actions.require_session_owner(ctx.actor, chosen)
    st.session_state[SESSION_KEY] = chosen
    return chosen


def _numbers(row: ChatMessageRow) -> list[NumberRef]:
    """``meta.numbers`` as ``NumberRef``; an invalid entry is skipped with a WARNING."""
    raw = row["meta"].get("numbers")
    refs: list[NumberRef] = []
    for item in raw if isinstance(raw, list) else []:
        try:
            refs.append(NumberRef.model_validate(item))
        except ValidationError:
            _log.warning("app.chat.bad_number_ref", message_id=row["message_id"])
    return refs


def _feedback(ctx: PageContext, message_id: str) -> None:
    with st.form(f"fb-{message_id}", border=False):
        thumbs = st.feedback("thumbs", key=f"fb-thumbs-{message_id}")
        note = st.text_input("Note (optional)", help=LIMIT, key=f"fb-note-{message_id}")
        if not st.form_submit_button("Send feedback"):
            return
    if thumbs is None:
        st.warning("Choose thumbs up or down first.")
        return
    actions.set_feedback(
        ctx.actor, message_id, feedback="up" if thumbs == 1 else "down", note=note or None
    )
    after_write()
    st.success("Feedback saved.")


def _correction(ctx: PageContext, session_id: str, reply_to: str, message_id: str) -> None:
    """The "Correct a fact" form (R-33: ``session_id`` and ``source_message_id``)."""
    with st.expander("Correct a fact"), st.form(f"fix-{message_id}"):
        statement = st.text_area("Statement", help=LIMIT, key=f"fix-text-{message_id}")
        kind = st.selectbox("Entity type (optional)", ENTITY_TYPES, key=f"fix-type-{message_id}",
                            format_func=lambda value: value or "none")  # fmt: skip
        entity_id = st.text_input("Entity id (optional)", key=f"fix-id-{message_id}").strip()
        if not st.form_submit_button("Save correction"):
            return
        entity = (cast("actions.EntityType", kind), entity_id) if kind and entity_id else None
        actions.propose_correction(
            ctx.actor, session_id=session_id, statement=statement, entity=entity,
            source_message_id=reply_to, memory=ctx.services.memory, build_id=ctx.build_id,
        )  # fmt: skip
        after_write()
        st.success(SAVED)


def _answer(ctx: PageContext, session_id: str, row: ChatMessageRow) -> None:
    st.markdown(render_answer_markdown(row["content"], _numbers(row)))
    verification_badge(row["verified"])
    if row["query_ids"]:
        evidence(row["query_ids"])
    try:
        _feedback(ctx, row["message_id"])
        reply_to = row["meta"].get("reply_to")
        if isinstance(reply_to, str):
            _correction(ctx, session_id, reply_to, row["message_id"])
    except HernessError as exc:  # a rejected action: Error / Fix lines, the page goes on
        user_error(exc)


def _row(ctx: PageContext, session_id: str, row: ChatMessageRow) -> None:
    if row["role"] != "assistant":
        with st.chat_message("user"):
            st.text(row["content"])
        return
    with st.chat_message("assistant"):
        if row["status"] == "done":
            _answer(ctx, session_id, row)
        elif row["status"] == "queued":
            chat_ui.show_banner(chat_ui.queued_banner(ctx))
        elif row["status"] == "failed":
            st.error(FAILED)
        else:
            st.caption(ANSWERING)


def _queued(rows: Sequence[ChatMessageRow]) -> bool:
    return any(row["role"] == "assistant" and row["status"] == "queued" for row in rows)


def _poll(ctx: PageContext, session_id: str) -> None:
    """The polled messages block: reload the rows; a full rerun once nothing is queued."""
    rows = list_chat_messages(session_id)
    for row in rows:
        _row(ctx, session_id, row)
    if not _queued(rows):
        st.rerun()


def _messages(ctx: PageContext, session_id: str) -> list[ChatMessageRow]:
    rows = list_chat_messages(session_id)
    if _queued(rows):
        st.fragment(run_every=ctx.services.cfg.app.chat.poll_queued_s)(_poll)(ctx, session_id)
    else:
        for row in rows:
            _row(ctx, session_id, row)
    return rows


def _retry(ctx: PageContext, session_id: str, rows: Sequence[ChatMessageRow]) -> None:
    """One "Retry" for the session when its last answer failed (FT09-04)."""
    last_failed = bool(rows) and rows[-1]["role"] == "assistant" and rows[-1]["status"] == "failed"
    wanted = (last_failed or chat_ui.last_error(session_id)) and not chat_ui.in_flight(session_id)
    if wanted and st.button("Retry", key=f"retry-{session_id}"):
        chat_ui.run_turn(ctx, session_id, None)


def _act(action: Callable[..., object], *args: object) -> None:
    """Run a turn or Retry; a rejected one shows Error / Fix lines and the page goes on."""
    try:
        action(*args)
    except HernessError as exc:
        user_error(exc)


def body(ctx: PageContext) -> None:
    """Sessions, messages, the last turn's outcome, Retry and the question input."""
    st.title("Chat")
    if ctx.build_id is None:
        st.info(NO_BUILD)
    session_id = _sessions(ctx)
    if session_id is None:
        st.caption(NO_SESSION)
    rows: list[ChatMessageRow] = []
    with block("Messages"):
        if session_id is not None:
            rows = _messages(ctx, session_id)
            chat_ui.show_outcome(session_id)
    max_chars = ctx.services.cfg.app.chat.max_question_chars
    busy = session_id is None or chat_ui.in_flight(session_id)
    text = st.chat_input("Ask a question about the data", max_chars=max_chars, disabled=busy)
    if session_id is None:
        return
    if text:
        _act(chat_ui.run_turn, ctx, session_id, text)
    _act(_retry, ctx, session_id, rows)  # after the turn: a failed turn offers Retry at once


page("Chat", body, needs_warehouse=False)
