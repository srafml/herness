"""FT09-04: the chat model is unavailable, then Retry answers the same user row (F09-04 step 6).

The ``FakeChatService`` first emits an ``error`` event (``ModelUnavailable``) and marks the
answer row ``failed`` like spec 06; the second call answers. Retry inserts no user row.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from tests.support.chat_page_env import ask, chat_page_env, click, isolated_app, open_chat, texts
from tests.support.fake_chat import all_kinds, error_script
from tests.support.ops_store import OpsStoreHandle

from app.common import chat_ui
from herness.core.errors import ConfigError
from herness.core.resilience import ProcessState
from herness.store.ops.chat import list_chat_messages

pytestmark = pytest.mark.fault


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


def test_ft09_04_model_unavailable_retry_reuses_user_row(env: SimpleNamespace) -> None:
    """FT09-04 ``error`` (ModelUnavailable), click Retry: no new user row; same row answered."""
    chat = env.services.chat
    chat.scripts = [error_script("ModelUnavailable"), all_kinds()]
    at, session_id = open_chat()
    ask(at, "How many incidents? mail eve@example.com")
    assert "The model is not reachable.\n\nTry again later." in texts(at, "error")
    assert "This answer could not be produced." in texts(at, "error")
    (reply,) = chat.written
    assert [r["status"] for r in list_chat_messages(session_id)] == ["done", "failed"]
    click(at, "Retry")
    rows = list_chat_messages(session_id)
    assert [(r["role"], r["status"]) for r in rows] == [("user", "done"), ("assistant", "done")]
    assert chat.written == [reply]  # the same answer row, re-requested
    assert [c[1] for c in chat.calls] == ["How many incidents? mail [EMAIL]"] * 2
    assert not [b for b in at.button if b.label == "Retry"]
    assert not [e for e in texts(at, "error") if "not reachable" in e]


def test_ft09_04_policy_failure_after_post_offers_retry(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FT09-04 the chat policy fails after the user row is stored: Retry, no second user row."""
    chat = env.services.chat
    chat.scripts = [all_kinds()]

    def broken(now: object) -> str:
        msg = "chat chain has no local client"
        raise ConfigError(msg)

    monkeypatch.setattr(chat_ui, "chat_policy", broken)
    at, session_id = open_chat()
    ask(at, "How many incidents?")
    assert at.error, "the failure is shown"
    assert [r["role"] for r in list_chat_messages(session_id)] == ["user"]
    assert [b.label for b in at.button if b.label == "Retry"] == ["Retry"]
    monkeypatch.setattr(chat_ui, "chat_policy", lambda now: "live")
    click(at, "Retry")
    rows = list_chat_messages(session_id)
    assert [(r["role"], r["status"]) for r in rows] == [("user", "done"), ("assistant", "done")]
    assert [c[1] for c in chat.calls] == ["How many incidents?"]
