"""UT09-61, UT09-62, UT09-97, UT09-98: chat reducer, chat-hours banners and the turn driver.

``run_turn`` runs inside ``AppTest.from_function`` scripts with the store-facing names of
``app.common.chat_ui`` replaced (no ops store), a ``FakeChatService`` and a fixed chat policy.
A script calls ``run_turn`` once per AppTest session (a streamed turn ends in ``st.rerun()``).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from streamlit.testing.v1 import AppTest
from structlog.testing import capture_logs
from tests.support.app_env import preserved_main
from tests.support.fake_chat import (
    ESCALATED_RUN_ID,
    MEMORY_ID,
    Q1,
    Q2,
    RUN_ID,
    FakeChatService,
    all_kinds,
    error_script,
    final_event,
    verification,
)

from app.common import chat_ui
from app.common.chat_ui import (
    BUSY,
    CORRECTION_NOTICE,
    MODE_BANNERS,
    TurnState,
    apply_event,
    mode_banner,
)
from herness.core.errors import ModelUnavailable, PolicyViolation
from herness.core.types import (
    CorrectionCapturedEvent,
    ErrorEvent,
    EscalatedEvent,
    EvidenceEvent,
    ModeEvent,
    TokenEvent,
    ToolEvent,
    VerificationEvent,
)
from herness.reports.actions import PostedMessage
from herness.reports.rules import Actor

pytestmark = pytest.mark.unit

SES = "ses_01J9ZQ4Y8M6V3K2N1P0R5T7W9X"  # pragma: allowlist secret
USER_REF = "a1" * 16
ACTOR = Actor(user_ref=USER_REF, display="alice", role="viewer", channel="dashboard")
BERLIN = ZoneInfo("Europe/Berlin")
SMALL = "Outside chat hours: answering with a reduced model; answers may be less thorough."
CLOUD = "Answered by an off-network model under the approved data policy."
DEFER = ("The GPU is running scheduled work. Your question is queued and the answer will "
         "appear here after {}.")  # fmt: skip


@pytest.fixture(autouse=True)
def _keep_main() -> Iterator[None]:
    with preserved_main():
        yield


# --- UT09-61 reducer --------------------------------------------------------------------------


def test_ut09_61_each_event_kind_changes_state_per_table() -> None:
    """UT09-61 each event kind: the U09-69 table state change; input state unchanged."""
    s0 = TurnState()
    s1 = apply_event(s0, ModeEvent(mode="small_model", message="reduced"))
    assert (s1.mode, s1.mode_message) == ("small_model", "reduced")
    assert s0 == TurnState()  # pure: input unchanged
    s2 = apply_event(apply_event(s1, TokenEvent(text="ab")), TokenEvent(text="c"))
    assert s2.draft == "abc"
    assert apply_event(s2, TokenEvent(text="")) is s2  # empty text ignored
    s3 = apply_event(s2, ToolEvent(name="run_sql", query_id=Q1, ok=True))
    s3 = apply_event(s3, ToolEvent(name="lookup", query_id=None, ok=False))
    assert s3.tools == (("run_sql", Q1, True), ("lookup", None, False))
    s4 = apply_event(apply_event(s3, EvidenceEvent(query_id=Q1)), EvidenceEvent(query_id=Q2))
    assert apply_event(s4, EvidenceEvent(query_id=Q1)) is s4  # appended only when new
    assert s4.evidence_ids == (Q1, Q2)
    s5 = apply_event(
        s4, VerificationEvent(result=verification(), status="partial", removed_claims=["x"])
    )
    assert (s5.verification, s5.removed_claims) == ("partial", ("x",))
    s6 = apply_event(s5, EscalatedEvent(run_id=ESCALATED_RUN_ID, job_id="job_" + "1" * 26))
    assert s6.escalated == (ESCALATED_RUN_ID, "job_" + "1" * 26)
    s7 = apply_event(s6, final_event())
    assert (s7.final_run_id, s7.draft, s7.done) == (RUN_ID, "", True)
    err = apply_event(s2, ErrorEvent(error_type="ModelUnavailable", message="m", hint="h"))
    assert (err.error, err.done, err.draft) == (("ModelUnavailable", "m", "h"), True, "abc")


def test_ut09_61_events_after_done_are_ignored() -> None:
    """UT09-61 after ``done`` every event but ``correction_captured`` returns the same state."""
    done = apply_event(TurnState(), final_event())
    later = [
        ModeEvent(mode="cloud", message="m"), TokenEvent(text="late"),
        ToolEvent(name="t", query_id=None, ok=True), EvidenceEvent(query_id=Q1),
        VerificationEvent(result=verification(), status="verified", removed_claims=[]),
        EscalatedEvent(run_id=ESCALATED_RUN_ID, job_id="job_" + "1" * 26), final_event(),
        ErrorEvent(error_type="X", message="m", hint=None),
    ]  # fmt: skip
    for event in later:
        assert apply_event(done, event) is done
    failed = apply_event(TurnState(), ErrorEvent(error_type="X", message="m", hint=None))
    assert apply_event(failed, TokenEvent(text="late")) is failed


def test_ut09_61_tools_and_evidence_capped_at_200() -> None:
    """UT09-61 ``tools`` and ``evidence_ids`` keep at most 200 entries."""
    state = TurnState()
    for i in range(205):
        state = apply_event(state, ToolEvent(name="t", query_id=None, ok=True))
        state = apply_event(state, EvidenceEvent(query_id=f"q_{i:016x}"))
    assert len(state.tools) == 200
    assert len(state.evidence_ids) == 200


# --- UT09-62 banners --------------------------------------------------------------------------


def test_ut09_62_banner_texts_per_mode() -> None:
    """UT09-62 each mode: exact texts; ``live`` has no banner."""
    assert mode_banner("live", next_live_at=None, tz=BERLIN) is None
    assert mode_banner("small_model", next_live_at=None, tz=BERLIN) == ("info", SMALL)
    assert mode_banner("cloud", next_live_at=None, tz=BERLIN) == ("info", CLOUD)
    assert set(MODE_BANNERS) == {"live", "small_model", "defer", "cloud"}


def test_ut09_62_defer_with_and_without_eta_tz_formatting() -> None:
    """UT09-62 defer: ``YYYY-MM-DD HH:MM <tz abbreviation>`` in ``tz``, else the next window."""
    winter = datetime(2026, 1, 5, 17, 30, tzinfo=UTC)
    summer = datetime(2026, 7, 5, 6, 0, tzinfo=UTC)
    assert mode_banner("defer", next_live_at=winter, tz=BERLIN) == (
        "warning", DEFER.format("2026-01-05 18:30 CET"))  # fmt: skip
    assert mode_banner("defer", next_live_at=summer, tz=BERLIN) == (
        "warning", DEFER.format("2026-07-05 08:00 CEST"))  # fmt: skip
    assert mode_banner("defer", next_live_at=winter, tz=ZoneInfo("UTC")) == (
        "warning", DEFER.format("2026-01-05 17:30 UTC"))  # fmt: skip
    assert mode_banner("defer", next_live_at=None, tz=BERLIN) == (
        "warning", DEFER.format("the next chat window"))  # fmt: skip


# --- run_turn harness -------------------------------------------------------------------------


def _ctx(chat: FakeChatService) -> SimpleNamespace:
    cfg = SimpleNamespace(
        app=SimpleNamespace(chat=SimpleNamespace(max_question_chars=4000, poll_queued_s=15)),
        weights=SimpleNamespace(business_timezone="Europe/Berlin"),
    )
    services = SimpleNamespace(cfg=cfg, chat=chat)
    return SimpleNamespace(actor=ACTOR, services=services, build_id="b1",
                           now=datetime(2026, 10, 1, 12, tzinfo=UTC))  # fmt: skip


class Store:
    """The store-facing names ``run_turn`` uses, recording calls."""

    def __init__(self, latest: str | None = "stored question") -> None:
        self.posted: list[tuple[str, str, int]] = []
        self.owner_checks: list[str] = []
        self.latest = latest
        self.cleared = 0
        self.metrics: list[tuple[str, dict[str, str]]] = []

    def install(self, mp: pytest.MonkeyPatch, mode: str = "live") -> None:
        def post(actor: Actor, session_id: str, text: str, *, max_chars: int, now: Any) -> Any:
            self.posted.append((session_id, text, max_chars))
            if len(text) > max_chars:
                msg = "too long"
                raise PolicyViolation(msg)
            return PostedMessage(message_id="msg_" + "1" * 26, redacted_text="[REDACTED] q")

        def latest(session_id: str) -> dict[str, str] | None:
            return None if self.latest is None else {"content": self.latest}

        def metric(name: str, value: float, *, component: str, labels: dict[str, str]) -> None:
            self.metrics.append((name, labels))

        mp.setattr(chat_ui, "post_user_message", post)
        mp.setattr(chat_ui, "latest_user_message", latest)
        mp.setattr(chat_ui, "require_session_owner", lambda a, s: self.owner_checks.append(s))
        mp.setattr(chat_ui, "chat_policy", lambda now: mode)
        mp.setattr(chat_ui, "chat_next_live_at", lambda now: datetime(2026, 1, 5, 17, tzinfo=UTC))
        mp.setattr(chat_ui, "after_write", lambda: setattr(self, "cleared", self.cleared + 1))
        mp.setattr(chat_ui, "record_counter", metric)
        mp.setattr(chat_ui, "record_histogram", metric)


def turn_script(*args: object) -> None:
    """AppTest script: one ``run_turn`` per session (the turn reruns the script)."""
    from typing import cast  # noqa: PLC0415 - AppTest runs this function's body as a script

    import streamlit as st  # noqa: PLC0415

    from app.common import chat_ui  # noqa: PLC0415
    from herness.core.errors import HernessError  # noqa: PLC0415

    ctx, session_id, text, busy = cast("tuple[Any, str, str | None, bool]", args)
    if busy:
        st.session_state["chat_inflight"] = {session_id}
    if not st.session_state.get("ran"):
        st.session_state["ran"] = True
        try:
            state = chat_ui.run_turn(ctx, session_id, text)
            st.text(f"state done={state.done}")
        except HernessError as exc:
            st.text(f"raised {type(exc).__name__}: {exc.message}")
    else:
        chat_ui.show_outcome(session_id)
        st.text("after rerun")


def _run(ctx: Any, text: str | None, *, busy: bool = False) -> AppTest:
    at = AppTest.from_function(turn_script, args=(ctx, SES, text, busy), default_timeout=30)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def _texts(at: AppTest) -> list[str]:
    return [str(e.value) for e in at.text]


# --- UT09-97 ----------------------------------------------------------------------------------


def test_ut09_97_turn_in_flight_warns_and_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT09-97 a turn already in flight: warning, no user row, no answer request."""
    store, chat = Store(), FakeChatService([all_kinds()])
    store.install(monkeypatch)
    at = _run(_ctx(chat), "second question", busy=True)
    assert [w.value for w in at.warning] == [BUSY]
    assert store.posted == []
    assert chat.calls == []
    assert "state done=False" in _texts(at)


def test_ut09_97_retry_without_user_row_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT09-97 retry with no user row: ``UserInputError("Nothing to retry.")``; owner checked."""
    store, chat = Store(latest=None), FakeChatService([all_kinds()])
    store.install(monkeypatch)
    with capture_logs() as logs:
        at = _run(_ctx(chat), None)
    assert "raised UserInputError: Nothing to retry." in _texts(at)
    assert store.owner_checks == [SES]
    assert chat.calls == []
    assert at.session_state["chat_inflight"] == set()  # cleared in `finally`
    failed = [e for e in logs if e["event"] == "app.chat.turn_failed"]
    assert failed == [{"event": "app.chat.turn_failed", "session_id": SES, "component": "app.chat",
                       "error_type": "UserInputError", "log_level": "warning"}]  # fmt: skip


def test_ut09_97_text_turn_posts_once_and_streams_redacted_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT09-97 a turn: one post, the redacted text and the policy mode go to ``answer``."""
    store, chat = Store(), FakeChatService([all_kinds("small_model")])
    store.install(monkeypatch, mode="small_model")
    with capture_logs() as logs:
        at = _run(_ctx(chat), "raw question")
    assert store.posted == [(SES, "raw question", 4000)]
    assert chat.calls == [(SES, "[REDACTED] q", USER_REF, "small_model")]
    assert "after rerun" in _texts(at)  # the streamed turn reran the script
    assert store.cleared == 1
    assert at.session_state["chat_inflight"] == set()
    completed = next(e for e in logs if e["event"] == "app.chat.turn_completed")
    assert completed["mode"] == "small_model"
    assert completed["final_run_id"] == RUN_ID
    assert completed["verification"] == "verified"
    assert completed["n_evidence"] == 2
    names = [name for name, _ in store.metrics]
    assert names == ["herness_app_chat_first_token_seconds", "herness_app_chat_turns_total"]
    assert store.metrics[1][1] == {"mode": "small_model", "result": "ok"}


def test_ut09_97_retry_reuses_latest_user_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT09-97 retry: no post; the latest user row's content is answered again."""
    store, chat = Store(), FakeChatService([all_kinds()])
    store.install(monkeypatch)
    _run(_ctx(chat), None)
    assert store.posted == []
    assert chat.calls == [(SES, "stored question", USER_REF, "live")]


def test_ut09_97_pre_stream_error_propagates_without_rerun(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT09-97 a rejected question (redaction / validation) reaches the page; no stream."""
    store, chat = Store(), FakeChatService([all_kinds()])
    store.install(monkeypatch)
    ctx = _ctx(chat)
    ctx.services.cfg.app.chat.max_question_chars = 5
    at = _run(ctx, "much too long")
    assert "raised PolicyViolation: too long" in _texts(at)
    assert chat.calls == []
    assert store.metrics == [("herness_app_chat_turns_total", {"mode": "none", "result": "error"})]


def test_ut09_97_generator_error_becomes_error_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT09-97 a ``HernessError`` raised by the stream becomes the ``error`` state."""
    store = Store()
    chat = FakeChatService([[TokenEvent(text="part")]], raises=ModelUnavailable("down"))
    store.install(monkeypatch)
    with capture_logs() as logs:
        at = _run(_ctx(chat), "q")
    assert "after rerun" in _texts(at)
    assert at.error, "the kept outcome shows the error after the rerun"
    assert chat_ui.last_error.__name__  # exported helper
    failed = next(e for e in logs if e["event"] == "app.chat.turn_failed")
    assert failed["error_type"] == "ModelUnavailable"


def test_ut09_97_error_event_shown_with_hint_after_rerun(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT09-97 an ``error`` event: message and hint in the error box kept across the rerun."""
    store, chat = Store(), FakeChatService([error_script()])
    store.install(monkeypatch)
    at = _run(_ctx(chat), "q")
    assert [e.value for e in at.error] == ["The model is not reachable.\n\nTry again later."]
    assert store.metrics[-1] == (
        "herness_app_chat_turns_total",
        {"mode": "live", "result": "error"},
    )


def test_ut09_97_defer_banner_and_escalation_shown(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT09-97 defer: the queued banner with the next slot; escalation shows the run id."""
    store, chat = Store(), FakeChatService([all_kinds("defer")])
    store.install(monkeypatch, mode="defer")
    calls: list[str] = []
    monkeypatch.setattr(chat_ui.st, "rerun", lambda: calls.append("rerun"))  # stay in this run
    at = _run(_ctx(chat), "q")
    assert calls == ["rerun"]
    assert [w.value for w in at.warning] == [DEFER.format("2026-01-05 18:00 CET")]
    assert f"Escalated to a review run: `{ESCALATED_RUN_ID}`." in [i.value for i in at.info]
    assert chat.calls[0][3] == "defer"
    assert "Draft — verifying numbers…" in [c.value for c in at.caption]
    (status,) = at.status
    assert (status.label, status.proto.expanded, status.state) == ("Running queries…", False,
                                                                  "complete")  # fmt: skip
    assert [t.value for t in status.text] == [f"run_sql · {Q1} · ok", "run_sql · - · failed"]


# --- UT09-98 ----------------------------------------------------------------------------------


def test_ut09_98_correction_after_final_reduces_and_renders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT09-98 ``final`` then ``correction_captured``: done kept, id set, separate caption."""
    state = apply_event(TurnState(), final_event())
    state = apply_event(state, CorrectionCapturedEvent(memory_id=MEMORY_ID))
    assert (state.done, state.correction_memory_id, state.final_run_id) == (True, MEMORY_ID, RUN_ID)
    store = Store()
    chat = FakeChatService([[TokenEvent(text="Team One had 42."), final_event(),
                             CorrectionCapturedEvent(memory_id=MEMORY_ID)]])  # fmt: skip
    store.install(monkeypatch)
    monkeypatch.setattr(chat_ui.st, "rerun", lambda: None)  # keep the streamed answer visible
    at = _run(_ctx(chat), "q")
    captions = [c.value for c in at.caption]
    assert captions[-1] == CORRECTION_NOTICE
    answer = [m.value for m in at.markdown if "Team One" in str(m.value)]
    assert answer == ["Team One had 42\\."]
    assert all("correction" not in str(m.value).lower() for m in at.markdown)


def test_ut09_97_cache_clear_failure_never_masks_the_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT09-97 ``after_write`` failing in ``finally``: logged by type; the turn still completes."""
    store, chat = Store(), FakeChatService([all_kinds()])
    store.install(monkeypatch)

    def broken() -> None:
        msg = "cache unavailable"
        raise RuntimeError(msg)

    monkeypatch.setattr(chat_ui, "after_write", broken)
    with capture_logs() as logs:
        at = _run(_ctx(chat), "q")
    assert "after rerun" in _texts(at)
    events = [e["event"] for e in logs]
    assert "app.chat.cache_clear_failed" in events
    assert "app.chat.turn_completed" in events
