"""BT09-04 chat time to first token in the dashboard turn driver (marker `gpu`).

Spec target (impl 09 §11): "BT09-04 | Chat time to first token | reasoning model loaded (marker
`gpu`) | 20 questions from the chat golden subset | p95 < 3 s excluding queueing". The tree has
no chat golden subset and no wiring of a loaded reasoning model into ``ChatService`` for the
dashboard, so this bench drives ``run_turn`` (AppTest) with a ``FakeChatService`` that streams
a scripted answer: it measures the UI driver's share of time to first token (the value
``herness_app_chat_first_token_seconds`` records), not the model's. Each repeat asks 20
questions; the gate is the median over ``REPEATS`` of the per-repeat p95.
Run: pytest -m "integration and slow and gpu" tests/bench/test_app_chat_bench.py
"""

from __future__ import annotations

import statistics
from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from streamlit.testing.v1 import AppTest
from tests.support.app_env import preserved_main
from tests.support.bench_stats import REPEATS, report
from tests.support.fake_chat import FakeChatService, all_kinds

from app.common import chat_ui
from herness.reports.actions import PostedMessage
from herness.reports.rules import Actor

pytestmark = [pytest.mark.integration, pytest.mark.slow, pytest.mark.gpu]

QUESTIONS = 20
LIMIT_S = 3.0
SES = "ses_01J9ZQ4Y8M6V3K2N1P0R5T7W9X"  # pragma: allowlist secret
ACTOR = Actor(user_ref="a1" * 16, display="bench", role="viewer", channel="dashboard")


@pytest.fixture(autouse=True)
def _keep_main() -> Iterator[None]:
    with preserved_main():
        yield


def bench_script(*args: object) -> None:
    """AppTest script: one turn, then nothing (the turn reruns the script)."""
    from typing import cast  # noqa: PLC0415 - AppTest runs this function's body as a script

    import streamlit as st  # noqa: PLC0415

    from app.common import chat_ui  # noqa: PLC0415

    ctx, session_id, question = cast("tuple[Any, str, str]", args)
    if not st.session_state.get("ran"):
        st.session_state["ran"] = True
        chat_ui.run_turn(ctx, session_id, question)


def _ctx(chat: FakeChatService) -> SimpleNamespace:
    chat_cfg = SimpleNamespace(max_question_chars=4000, poll_queued_s=15)
    cfg = SimpleNamespace(app=SimpleNamespace(chat=chat_cfg),
                          weights=SimpleNamespace(business_timezone="UTC"))  # fmt: skip
    return SimpleNamespace(actor=ACTOR, services=SimpleNamespace(cfg=cfg, chat=chat),
                           build_id="b1", now=datetime(2026, 10, 1, tzinfo=UTC))  # fmt: skip


def _p95(values: list[float]) -> float:
    return statistics.quantiles(values, n=20, method="inclusive")[18]


def test_bt09_04_chat_time_to_first_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """BT09-04 20 questions per repeat: p95 time to first token < 3 s (UI driver share)."""
    seen: list[float] = []

    def histogram(name: str, value: float, **_: Any) -> None:
        if name == "herness_app_chat_first_token_seconds":
            seen.append(value)

    def post(actor: Actor, session_id: str, text: str, **_: Any) -> PostedMessage:
        return PostedMessage(message_id="msg_" + "1" * 26, redacted_text=text)

    monkeypatch.setattr(chat_ui, "post_user_message", post)
    monkeypatch.setattr(chat_ui, "chat_policy", lambda now: "live")
    monkeypatch.setattr(chat_ui, "after_write", lambda: None)
    monkeypatch.setattr(chat_ui, "record_histogram", histogram)
    monkeypatch.setattr(chat_ui, "record_counter", lambda *a, **k: None)
    p95s: list[float] = []
    for _repeat in range(REPEATS):
        seen.clear()
        for i in range(QUESTIONS):
            chat = FakeChatService([all_kinds(tokens=[f"Answer {i} ", "with 42."])])
            at = AppTest.from_function(bench_script, args=(_ctx(chat), SES, f"Question {i}?"))
            at.run(timeout=30)
            assert not at.exception
        assert len(seen) == QUESTIONS
        p95s.append(_p95(seen))
    median = report("BT09-04", "p95_first_token", p95s, "s", f"< {LIMIT_S}")
    assert median < LIMIT_S
