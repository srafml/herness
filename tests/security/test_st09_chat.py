"""ST09-02 (chat page), ST09-14, ST09-20, ST09-25: the Chat page under hostile input (T09-19).

AppTests of ``app/pages/11_Chat.py`` on a migrated ops store with a ``FakeChatService``
(``tests.support.chat_page_env``). The memory-content half of ST09-02 is the sanitiser test in
``test_st09_dashboard.py``; the Chat page renders no memory content.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from streamlit.testing.v1.errors import AppTestError
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
from tests.support.fake_chat import all_kinds, final_event
from tests.support.ops_store import OpsStoreHandle

from app.common import chat_ui, data
from herness.core.resilience import ProcessState
from herness.core.types import TokenEvent
from herness.store.ops.chat import create_chat_session, list_chat_messages

pytestmark = pytest.mark.integration

HOSTILE = (
    "See ![](http://x/?d=secret) and [click](javascript:x) or [site](https://evil.example/p) "
    "with $\\frac{a}{b}$ <b>bold</b> :red[alert]"
)
MARKER = "zq-marker-7731 dave.secret@example.com"


@pytest.fixture(autouse=True)
def _isolated() -> Iterator[None]:
    yield from isolated_app()


@pytest.fixture
def env(
    ops_store: OpsStoreHandle,
    reset_process_state: ProcessState,
    monkeypatch: pytest.MonkeyPatch,
) -> SimpleNamespace:
    del reset_process_state
    return chat_page_env(ops_store.data_root, ops_store.db_path, monkeypatch)


def _assert_inert(bodies: list[str]) -> None:
    for body in bodies:
        assert "![" not in body, body
        assert "](http" not in body, body
        assert "](javascript" not in body, body
        assert "<b>" not in body, body
        assert "$" not in body.replace("\\$", ""), body
        assert ":red[" not in body, body


def test_st09_02_chat_answer_and_stream_have_no_image_link_or_latex(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST09-02 hostile Markdown in the streamed tokens and the stored answer: all inert."""
    script = [TokenEvent(text=HOSTILE[:30]), TokenEvent(text=HOSTILE[30:]), final_event(HOSTILE)]
    env.services.chat.scripts = [script]
    at, _session_id = open_chat()
    reruns: list[str] = []
    monkeypatch.setattr(chat_ui.st, "rerun", lambda: reruns.append("rerun"))
    ask(at, "question")  # no rerun: the streamed draft stays on screen
    assert reruns == ["rerun"]
    streamed = texts(at, "markdown")
    assert any("frac" in body for body in streamed)
    _assert_inert(streamed)
    check(at.run())  # the next run renders the stored answer row
    stored = [b for b in texts(at, "markdown") if "frac" in b]
    assert stored, "the stored answer is rendered"
    _assert_inert(texts(at, "markdown"))


def test_st09_14_oversized_question_rejected(env: SimpleNamespace) -> None:
    """ST09-14 a 4001-char question: rejected with an error; nothing stored, nothing asked."""
    at, session_id = open_chat()
    ask(at, "x" * 4001)
    assert "Question must be 1 to 4000 characters." in " ".join(texts(at, "error"))
    assert list_chat_messages(session_id) == []
    assert env.services.chat.calls == []


def test_st09_14_oversized_correction_rejected(env: SimpleNamespace) -> None:
    """ST09-14 a 1001-char correction: rejected; ``propose`` never called."""
    env.services.chat.scripts = [all_kinds()]
    at, _session_id = open_chat()
    ask(at, "question")
    (reply,) = env.services.chat.written
    at.text_area(key=f"fix-text-{reply}").set_value("y" * 1001)
    click(at, "Save correction")
    assert "Correction must be 1 to 1000 characters." in " ".join(texts(at, "error"))
    assert env.services.memory.calls == []


def test_st09_14_oversized_feedback_note_rejected(env: SimpleNamespace) -> None:
    """ST09-14 a 1001-char feedback note: rejected; the answer row keeps no feedback."""
    env.services.chat.scripts = [all_kinds()]
    at, session_id = open_chat()
    ask(at, "question")
    (reply,) = env.services.chat.written
    at.feedback(key=f"fb-thumbs-{reply}").set_value(1)
    at.text_input(key=f"fb-note-{reply}").set_value("n" * 1001)
    click(at, "Send feedback")
    assert "Note must be at most 1000 characters." in " ".join(texts(at, "error"))
    row = next(r for r in list_chat_messages(session_id) if r["message_id"] == reply)
    assert (row["feedback"], row["feedback_note"]) == (None, None)


def test_st09_14_double_submit_second_refused(env: SimpleNamespace) -> None:
    """ST09-14 a second submit while the turn is in flight: refused, no second user row.

    The page disables the input of a session in flight; ``run_turn``'s own guard (the warning)
    is UT09-97.
    """
    env.services.chat.scripts = [all_kinds()]
    at, session_id = open_chat()
    at.session_state["chat_inflight"] = {session_id}
    check(at.run())
    assert at.chat_input[0].disabled
    with pytest.raises(AppTestError, match="disabled chat_input"):
        ask(at, "second question")
    assert list_chat_messages(session_id) == []
    assert env.services.chat.calls == []


def test_st09_20_marker_absent_from_logs_and_stored_text_redacted(env: SimpleNamespace) -> None:
    """ST09-20 a unique marker with an e-mail: not in any INFO+ log; stored text redacted."""
    env.services.chat.scripts = [all_kinds()]
    at, session_id = open_chat()
    with capture_logs() as logs:
        ask(at, f"Question {MARKER}")
    events = [e for e in logs if e.get("log_level") in {"info", "warning", "error", "critical"}]
    assert {"app.chat.message_posted", "app.chat.turn_completed"} <= {e["event"] for e in events}
    dumped = repr(events)
    assert "zq-marker-7731" not in dumped
    assert "dave.secret@example.com" not in dumped
    (user_row,) = [r for r in list_chat_messages(session_id) if r["role"] == "user"]
    assert user_row["content"] == "Question zq-marker-7731 [EMAIL]"
    assert env.services.chat.calls[0][1] == "Question zq-marker-7731 [EMAIL]"
    raw = rows_of(env.db, "SELECT count(*) FROM chat_message WHERE content LIKE ?",
                  "%dave.secret@example.com%")  # fmt: skip
    assert raw == [(0,)]


def test_st09_25_two_users_disjoint_sessions_no_cached_chat_data(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST09-25 Alice and Bob side by side: disjoint session lists; chat reads never cached."""
    env.services.chat.scripts = [all_kinds()]
    at_alice, alice_session = open_chat()
    ask(at_alice, "alice question")
    install_session(monkeypatch, env.services, user="bob")
    at_bob, bob_session = open_chat()
    ask(at_bob, "bob question")
    bob_ref = at_bob.session_state["chat_session_id"]
    assert bob_ref == bob_session != alice_session
    (bob_option,) = at_bob.sidebar.selectbox[0].options
    assert "bob question" in bob_option
    install_session(monkeypatch, env.services, user="alice")
    check(at_alice.run())
    (alice_option,) = at_alice.sidebar.selectbox[0].options
    assert "alice question" in alice_option
    # a row written behind the page's back shows on the very next run: no cache in between
    ((alice_ref,),) = rows_of(
        env.db, "SELECT user_ref FROM chat_session WHERE session_id = ?", alice_session
    )
    create_chat_session(alice_ref, now=datetime(2030, 1, 1, tzinfo=UTC))
    check(at_alice.run())
    assert len(at_alice.sidebar.selectbox[0].options) == 2
    assert not [name for name in data.OPS_READ_NAMES if "chat" in name]
