"""IT09-12, IT09-13: the Chat page over a real migrated ops store with a ``FakeChatService``.

The chat policy is fixed per test (``chat_ui.chat_policy``); tests load no redaction key, so
``herness.reports.actions.redact_text`` is replaced by an e-mail masker. The fake writes the
assistant row like spec 06 (``write_rows=True``) and lists every row it wrote, so the UI's own
writes are the rest: exactly one user row per turn, no assistant row and no ``chat`` job.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import pytest
import streamlit as st
from structlog.testing import capture_logs
from tests.support.app_env import install_session
from tests.support.chat_page_env import (
    ask,
    chat_page_env,
    check,
    click,
    isolated_app,
    open_chat,
    rows_of,
    texts,
)
from tests.support.fake_chat import ESCALATED_RUN_ID, MEMORY_ID, Q1, all_kinds
from tests.support.fake_clock import FIXTURE_START, FakeClock
from tests.support.ops_store import OpsStoreHandle

from app.common import chat_ui
from herness.core.resilience import ProcessState
from herness.core.types import ChatMode
from herness.reports import actions
from herness.store.ops.chat import list_chat_messages, update_chat_message

pytestmark = pytest.mark.integration

QUESTION = "How many incidents did Team One have? Mail me at bob@example.com"
REDACTED_QUESTION = "How many incidents did Team One have? Mail me at [EMAIL]"
SAVED = "Saved as pending. A reviewer must approve it before it affects answers or scores."
DEFER_PREFIX = "The GPU is running scheduled work. Your question is queued"
MODES: tuple[ChatMode, ...] = ("live", "small_model", "defer", "cloud")


@pytest.fixture(autouse=True)
def _isolated() -> Iterator[None]:
    yield from isolated_app()


@pytest.fixture
def env(
    ops_store: OpsStoreHandle,
    reset_process_state: ProcessState,
    monkeypatch: pytest.MonkeyPatch,
) -> SimpleNamespace:
    """Alice's chat page on a migrated ops store; the policy mode is ``env.mode[0]``."""
    del reset_process_state
    return chat_page_env(ops_store.data_root, ops_store.db_path, monkeypatch)


@pytest.mark.parametrize("mode", MODES)
def test_it09_12_one_user_row_per_turn_no_assistant_row_no_job(
    env: SimpleNamespace, mode: ChatMode
) -> None:
    """IT09-12 each mode: the UI writes one redacted user row per turn, no answer, no job."""
    env.mode[0] = mode
    env.services.chat.scripts = [all_kinds(mode)]
    at, session_id = open_chat()
    ask(at, QUESTION)
    ask(at, "Second question")
    rows = list_chat_messages(session_id)
    users = [r for r in rows if r["role"] == "user"]
    assert [r["content"] for r in users] == [REDACTED_QUESTION, "Second question"]
    assistants = [r["message_id"] for r in rows if r["role"] == "assistant"]
    assert assistants == env.services.chat.written  # every answer row is the service's
    assert rows_of(env.db, "SELECT count(*) FROM job WHERE kind = 'chat'") == [(0,)]
    calls = env.services.chat.calls
    assert [(c[0], c[1], c[3]) for c in calls] == [
        (session_id, REDACTED_QUESTION, mode), (session_id, "Second question", mode),
    ]  # fmt: skip
    assert all(QUESTION not in t for t in texts(at, "text", "markdown", "caption"))
    if mode == "defer":
        assert [w.value for w in at.warning if str(w.value).startswith(DEFER_PREFIX)]
    else:
        assert ":green-badge[Verified]" in texts(at, "markdown")
        assert "Evidence (1 queries)" in [e.label for e in at.expander]
        assert Q1 in texts(at, "subheader")


def test_it09_12_turn_outcome_escalation_and_correction_notice(env: SimpleNamespace) -> None:
    """IT09-12 after the rerun: the escalated run id and the separate correction notice."""
    env.services.chat.scripts = [all_kinds()]
    at, _session_id = open_chat()
    ask(at, QUESTION)
    assert f"Escalated to a review run: `{ESCALATED_RUN_ID}`." in texts(at, "info")
    assert chat_ui.CORRECTION_NOTICE in texts(at, "caption")
    answer = [t for t in texts(at, "markdown") if "incidents" in t]
    assert answer == ["Team One had [42](#ev-q_0123456789abcdef) incidents."]
    assert MEMORY_ID not in "\n".join(texts(at, "markdown", "caption", "info", "text"))


def test_it09_12_browser_state_cannot_choose_the_mode(env: SimpleNamespace) -> None:
    """IT09-12 (R-38) the mode comes only from ``chat_policy``: session keys cannot force cloud."""
    env.services.chat.scripts = [all_kinds()]
    at, _session_id = open_chat()
    for key in ("chat_mode", "mode", "cloud"):
        at.session_state[key] = "cloud"
    ask(at, QUESTION)
    assert [call[3] for call in env.services.chat.calls] == ["live"]


def test_it09_12_feedback_changes_only_feedback_columns(env: SimpleNamespace) -> None:
    """IT09-12 thumbs down with a note: only ``feedback`` and the redacted note change."""
    env.services.chat.scripts = [all_kinds()]
    at, session_id = open_chat()
    ask(at, QUESTION)
    (reply,) = env.services.chat.written
    before = {k: v for k, v in _row(session_id, reply).items() if not k.startswith("feedback")}
    at.feedback(key=f"fb-thumbs-{reply}").set_value(0)
    at.text_input(key=f"fb-note-{reply}").set_value("wrong, ask carol@example.com")
    click(at, "Send feedback")
    after = _row(session_id, reply)
    assert (after["feedback"], after["feedback_note"]) == ("down", "wrong, ask [EMAIL]")
    assert {k: v for k, v in after.items() if not k.startswith("feedback")} == before
    assert "Feedback saved." in texts(at, "success")


def _row(session_id: str, message_id: str) -> dict[str, Any]:
    return dict(next(r for r in list_chat_messages(session_id) if r["message_id"] == message_id))


def test_it09_12_correction_calls_propose_with_session_and_source(env: SimpleNamespace) -> None:
    """IT09-12 "Correct a fact": ``propose`` with ``session_id`` and the user row (R-33)."""
    env.services.chat.scripts = [all_kinds()]
    at, session_id = open_chat()
    ask(at, QUESTION)
    (reply,) = env.services.chat.written
    user_row = next(r for r in list_chat_messages(session_id) if r["role"] == "user")
    at.text_area(key=f"fix-text-{reply}").set_value("Team One had 40 incidents.")
    at.selectbox(key=f"fix-type-{reply}").set_value("team")
    at.text_input(key=f"fix-id-{reply}").set_value("T1")
    click(at, "Save correction")
    assert SAVED in texts(at, "success")
    ((name, (proposal,), _kw),) = env.services.memory.calls
    assert name == "propose"
    assert proposal.provenance.session_id == session_id
    assert proposal.provenance.source_message_id == user_row["message_id"]
    assert proposal.data["entities"] == [{"type": "team", "id": "T1"}]


def test_it09_12_wrong_owner_session_selection_not_found(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT09-12 another user's session id in the browser state: "not found", no data (TH09-10)."""
    env.services.chat.scripts = [all_kinds()]
    at, session_id = open_chat()
    ask(at, QUESTION)
    install_session(monkeypatch, env.services, user="mallory")
    audits: list[dict[str, Any]] = []
    monkeypatch.setattr(actions, "audit", lambda event, actor, **f: audits.append(f))
    at.session_state["chat_session_id"] = session_id
    check(at.run())
    assert any(f"chat session {session_id} not found" in e for e in texts(at, "error"))
    assert [a["result"] for a in audits] == ["denied"]  # require_session_owner ran and refused
    assert not at.chat_message
    shown = "\n".join(texts(at, "text", "markdown", "caption", "info", "warning"))
    assert "incidents" not in shown
    assert not at.sidebar.selectbox
    check(at.run())  # the refused id was dropped: the page works again for Mallory
    assert not at.error
    assert "chat_session_id" not in at.session_state


def test_it09_13_fragment_reload_shows_answer(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT09-13 a queued row polled by the fragment; flipped to done, the answer shows."""
    seen: list[object] = []
    real: Callable[..., Any] = st.fragment

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs.get("run_every"))
        return real(*args, **kwargs)

    monkeypatch.setattr(st, "fragment", spy)
    env.mode[0] = "defer"
    env.services.chat.scripts = [all_kinds("defer")]
    with FakeClock(FIXTURE_START) as fc:
        at, _session_id = open_chat()
        ask(at, QUESTION)
        (reply,) = env.services.chat.written
        banner = DEFER_PREFIX + " and the answer will appear here after 2026-09-01 04:00 CEST."
        assert banner in texts(at, "warning")
        assert seen
        assert set(seen) == {15}
        fc.advance(15)
        update_chat_message(reply, status="done", content="Answered later.", verified="partial")
        check(at.run())
    assert "Answered later." in texts(at, "markdown")
    assert ":orange-badge[Partially verified]" in texts(at, "markdown")
    assert not [w for w in texts(at, "warning") if w.startswith(DEFER_PREFIX)]


def test_it09_12_bad_number_ref_skipped_and_feedback_needs_thumbs(env: SimpleNamespace) -> None:
    """IT09-12 a bad ``meta.numbers`` entry is skipped with a WARNING; feedback needs thumbs."""
    env.services.chat.scripts = [all_kinds()]
    at, session_id = open_chat()
    ask(at, QUESTION)
    (reply,) = env.services.chat.written
    update_chat_message(reply, meta={"numbers": [{"id": "bad"}]})
    with capture_logs() as logs:
        check(at.run())
    warned = [e for e in logs if e["event"] == "app.chat.bad_number_ref"]
    assert [e["message_id"] for e in warned] == [reply]
    click(at, "Send feedback")
    assert "Choose thumbs up or down first." in texts(at, "warning")
    assert _row(session_id, reply)["feedback"] is None
