"""Chat page AppTest helpers (T09-19): environment, "New chat", asking, row and text reads.

``chat_page_env`` gives a migrated ops store, Alice's session, a ``FakeChatService`` that writes
the assistant row like spec 06, an e-mail masking redaction double and a fixed chat policy
(``env.mode[0]``). Used by the page tests (IT09-12, IT09-13), the fault test FT09-04 and the
chat security tests.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest
from tests.support.app_env import APP_DIR, install_session, page_services, preserved_main
from tests.support.fake_chat import FakeChatService

from app.common import chat_ui, wh
from herness.core.types import ChatMode
from herness.reports import actions
from herness.store.ops import bind_core_backends

__all__ = [
    "PAGE", "ask", "chat_page_env", "check", "click", "isolated_app", "mask_email", "open_chat",
    "rows_of", "texts",
]  # fmt: skip

PAGE = APP_DIR / "pages" / "11_Chat.py"
TIMEOUT_S = 60


def mask_email(text: str | None) -> str | None:
    """The redaction double: e-mail addresses become ``[EMAIL]``."""
    return None if text is None else re.sub(r"\S+@\S+", "[EMAIL]", text)


def isolated_app() -> Iterator[None]:
    """Clear the Streamlit caches and the warehouse pool; keep ``__main__``."""
    st.cache_data.clear()
    wh._pool.clear()
    with preserved_main():
        yield
    st.cache_data.clear()
    wh._pool.clear()


def chat_page_env(data_root: Any, db: Any, mp: pytest.MonkeyPatch) -> SimpleNamespace:
    """Alice's session on the migrated ops store at ``data_root`` (see the module doc)."""
    bind_core_backends()
    services = page_services(data_root)
    services.chat = FakeChatService(write_rows=True)
    audits = install_session(mp, services)
    mp.setattr(actions, "redact_text", mask_email)
    mode: list[ChatMode] = ["live"]
    mp.setattr(chat_ui, "chat_policy", lambda now: mode[0])
    mp.setattr(chat_ui, "chat_next_live_at", lambda now: now + timedelta(hours=2))
    return SimpleNamespace(db=db, services=services, mode=mode, audits=audits)


def check(at: AppTest) -> AppTest:
    assert not at.exception, [e.value for e in at.exception]
    return at


def open_chat() -> tuple[AppTest, str]:
    """Load the page and click "New chat"; returns the AppTest and the new session id."""
    at = check(AppTest.from_file(str(PAGE), default_timeout=TIMEOUT_S).run())
    check(at.button(key="chat-new").click().run())
    return at, str(at.session_state["chat_session_id"])


def ask(at: AppTest, text: str) -> AppTest:
    return check(at.chat_input[0].set_value(text).run())


def click(at: AppTest, label: str) -> AppTest:
    return check(next(b for b in at.button if b.label == label).click().run())


def rows_of(db: object, sql: str, *params: object) -> list[tuple[Any, ...]]:
    con = sqlite3.connect(str(db))
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def texts(at: AppTest, *kinds: str) -> list[str]:
    return [str(e.value) for kind in kinds for e in getattr(at, kind)]
