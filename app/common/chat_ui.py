"""Chat turn reducer, chat-hours banners and the dashboard turn driver (impl 09 U09-69 - U09-71).

``apply_event`` folds the ``ChatEvent`` stream of spec 06 into a ``TurnState``; ``mode_banner``
gives the chat-hours banner shared with the terminal chat; ``run_turn`` drives one turn on the
script thread. The UI writes only the user row (``post_user_message``); ``ChatService`` writes
the assistant row and, for ``defer``, the job (design §5.5). The chat mode comes only from the
server-side ``chat_policy`` (R-38): nothing in the session state can choose it. Streamed tokens
are escaped per character (TH09-02); logs carry ids, lengths and modes only, never chat text
(TH09-21).
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Final, Literal, cast
from zoneinfo import ZoneInfo

import streamlit as st
from streamlit.errors import StreamlitAPIException

from app.common.data import after_write
from app.common.sanitize import escape_markdown_chunk, safe_markdown
from herness.core import time as clock
from herness.core.errors import HernessError
from herness.core.jobs.chat_policy import chat_next_live_at, chat_policy
from herness.core.logging import get_logger
from herness.core.resilience.metrics import record_counter, record_histogram
from herness.core.types import (
    ChatEvent,
    ChatMode,
    CorrectionCapturedEvent,
    ErrorEvent,
    EscalatedEvent,
    EvidenceEvent,
    FinalEvent,
    ModeEvent,
    TokenEvent,
    ToolEvent,
    VerificationEvent,
)
from herness.reports.actions import post_user_message, require_session_owner
from herness.reports.rules import UserInputError, user_message
from herness.store.ops.chat import latest_user_message

if TYPE_CHECKING:
    from app.common.widgets import PageContext

__all__ = [
    "BUSY", "CORRECTION_NOTICE", "MODE_BANNERS", "TurnState", "apply_event", "business_tz",
    "in_flight", "last_error", "mode_banner", "queued_banner", "run_turn", "show_banner",
    "show_outcome",
]  # fmt: skip

type BannerKind = Literal["info", "warning"]

MODE_BANNERS: Final[Mapping[ChatMode, tuple[BannerKind, str] | None]] = {
    "live": None,
    "small_model": ("info", "Outside chat hours: answering with a reduced model; "
                            "answers may be less thorough."),
    "defer": ("warning", "The GPU is running scheduled work. Your question is queued and the "
                         "answer will appear here after {slot}."),
    "cloud": ("info", "Answered by an off-network model under the approved data policy."),
}  # fmt: skip
NEXT_WINDOW: Final = "the next chat window"
CORRECTION_NOTICE: Final = (
    "Your correction was saved for review. It will not affect answers until a reviewer approves it."
)
BUSY: Final = "A question is already being answered in this chat."
NOTHING_TO_RETRY: Final = "Nothing to retry."
DRAFT: Final = "Draft — verifying numbers…"
RUNNING: Final = "Running queries…"
CAP: Final = 200  # entries kept in ``tools`` and ``evidence_ids``
INFLIGHT_KEY: Final = "chat_inflight"
OUTCOME_KEY: Final = "chat_outcome"
_NAME_MAX: Final = 64
_log = get_logger("app.chat")


@dataclass(frozen=True)
class TurnState:
    """What one chat turn has produced so far (U09-69; design §3.3)."""

    mode: str | None = None
    mode_message: str | None = None
    draft: str = ""
    tools: tuple[tuple[str, str | None, bool], ...] = ()
    evidence_ids: tuple[str, ...] = ()
    verification: str | None = None
    removed_claims: tuple[str, ...] = ()
    escalated: tuple[str, str] | None = None
    final_run_id: str | None = None
    error: tuple[str, str, str | None] | None = None
    done: bool = False
    correction_memory_id: str | None = None  # R-32


def _capped[T](items: tuple[T, ...], item: T) -> tuple[T, ...]:
    """``items`` plus ``item`` while under the cap of 200 entries."""
    return items if len(items) >= CAP else (*items, item)


def _reduce(state: TurnState, event: ChatEvent) -> TurnState:  # noqa: PLR0911 - one per kind
    """The U09-69 table for the eight design kinds (a turn that is not done)."""
    match event:
        case ModeEvent(mode=mode, message=message):
            return replace(state, mode=mode, mode_message=message)
        case TokenEvent(text=text):
            return replace(state, draft=state.draft + text) if text else state
        case ToolEvent(name=name, query_id=query_id, ok=ok):
            return replace(state, tools=_capped(state.tools, (name, query_id, ok)))
        case EvidenceEvent(query_id=query_id) if query_id not in state.evidence_ids:
            return replace(state, evidence_ids=_capped(state.evidence_ids, query_id))
        case VerificationEvent(status=status, removed_claims=removed):
            return replace(state, verification=status, removed_claims=tuple(removed))
        case EscalatedEvent(run_id=run_id, job_id=job_id):
            return replace(state, escalated=(run_id, job_id))
        case FinalEvent(run_id=run_id):
            return replace(state, final_run_id=run_id, draft="", done=True)
        case ErrorEvent(error_type=error_type, message=message, hint=hint):
            return replace(state, error=(error_type, message, hint), done=True)
    return state  # a repeated evidence id


def apply_event(state: TurnState, event: ChatEvent) -> TurnState:
    """The next state; events after ``done`` are ignored except ``correction_captured``."""
    if isinstance(event, CorrectionCapturedEvent):
        return replace(state, correction_memory_id=event.memory_id)
    if state.done:
        return state
    return _reduce(state, event)


def mode_banner(
    mode: ChatMode, *, next_live_at: datetime | None, tz: ZoneInfo
) -> tuple[BannerKind, str] | None:
    """The chat-hours banner of ``mode`` (design §5.5 step 7); ``None`` for ``live``."""
    banner = MODE_BANNERS[mode]
    if banner is None or mode != "defer":
        return banner
    kind, text = banner
    if next_live_at is None:
        return kind, text.format(slot=NEXT_WINDOW)
    local = next_live_at.astimezone(tz)
    return kind, text.format(slot=f"{local:%Y-%m-%d %H:%M} {local.tzname()}")


def show_banner(banner: tuple[BannerKind, str] | None) -> None:
    """``st.info`` / ``st.warning`` with the constant banner text."""
    if banner is not None:
        (st.info if banner[0] == "info" else st.warning)(banner[1])


def business_tz(ctx: PageContext) -> ZoneInfo:
    return clock.zone(ctx.services.cfg.weights.business_timezone)


def queued_banner(ctx: PageContext) -> tuple[BannerKind, str] | None:
    """The ``defer`` banner of a queued answer, with the next live slot (F09-05 step 2)."""
    return mode_banner("defer", next_live_at=chat_next_live_at(clock.now()), tz=business_tz(ctx))


def _inflight() -> set[str]:
    return cast("set[str]", st.session_state.setdefault(INFLIGHT_KEY, set()))


def in_flight(session_id: str) -> bool:
    """Whether a turn of ``session_id`` is running in this browser session."""
    return session_id in _inflight()


def _outcomes() -> dict[str, TurnState]:
    return cast("dict[str, TurnState]", st.session_state.setdefault(OUTCOME_KEY, {}))


def last_error(session_id: str) -> bool:
    """Whether the last turn of the session (kept across the rerun) ended in an error."""
    state = _outcomes().get(session_id)
    return state is not None and state.error is not None


def show_outcome(session_id: str, state: TurnState | None = None) -> None:
    """Below the answer: the error box, the escalation and the correction notice (R-32).

    Without ``state``, the last turn of the session kept across the rerun (step 8) is shown.
    """
    state = state if state is not None else _outcomes().get(session_id)
    if state is None:
        return
    if state.error is not None:
        _type, message, hint = state.error
        st.error(safe_markdown(message if hint is None else f"{message}\n\n{hint}"))
    if state.escalated is not None:
        st.info(f"Escalated to a review run: `{state.escalated[0]}`.")
        with contextlib.suppress(StreamlitAPIException):  # page missing in a test run
            st.page_link("pages/10_Runs_and_Traces.py", label="Open Runs & Traces")
    if state.correction_memory_id is not None:
        st.caption(CORRECTION_NOTICE)


def _question(ctx: PageContext, session_id: str, text: str | None) -> str:
    """Step 3: insert the redacted user row, or (retry) reuse the latest user row."""
    if text is not None:
        max_chars = ctx.services.cfg.app.chat.max_question_chars
        posted = post_user_message(ctx.actor, session_id, text, max_chars=max_chars, now=ctx.now)
        return posted.redacted_text
    require_session_owner(ctx.actor, session_id)
    row = latest_user_message(session_id)
    if row is None:
        raise UserInputError(NOTHING_TO_RETRY)
    return row["content"]


def _metric(name: str, value: float, labels: dict[str, str], *, counter: bool) -> None:
    with contextlib.suppress(Exception):  # a metric failure never breaks a turn
        if counter:
            record_counter(name, value, component="app", labels=labels)
        else:
            record_histogram(name, value, component="app", labels=labels)


@dataclass
class _Turn:
    """The mutable state of one ``run_turn`` call, shared with its stream generator."""

    ctx: PageContext
    session_id: str
    mode: ChatMode | None = None
    state: TurnState = TurnState()
    started: float = 0.0


def _events(turn: _Turn, question: str, status: object) -> Iterator[str]:
    """Steps 5 and 6: reduce every event; escaped tokens out, tool lines into the status box."""
    mode = cast("ChatMode", turn.mode)
    first = True
    answer = turn.ctx.services.chat.answer(turn.session_id, question, turn.ctx.actor.user_ref, mode)
    try:
        for event in answer:
            accepted = not turn.state.done
            turn.state = apply_event(turn.state, event)
            if accepted and isinstance(event, ToolEvent):
                with cast("contextlib.AbstractContextManager[object]", status):
                    st.text(f"{event.name[:_NAME_MAX]} · {event.query_id or '-'} · "
                            f"{'ok' if event.ok else 'failed'}")  # fmt: skip
            elif accepted and isinstance(event, TokenEvent) and event.text:
                if first:
                    first = False
                    seconds = clock.monotonic() - turn.started
                    _metric("herness_app_chat_first_token_seconds", seconds, {"mode": mode},
                            counter=False)  # fmt: skip
                yield escape_markdown_chunk(event.text)
    except HernessError as exc:
        what, fix = user_message(exc)
        turn.state = replace(turn.state, error=(type(exc).__name__, what, fix), done=True)


def _stream(turn: _Turn, question: str) -> None:
    """Steps 4 and 5: the mode from the server-side policy, then the streamed answer."""
    now = clock.now()
    turn.mode = chat_policy(now)
    eta = chat_next_live_at(now) if turn.mode == "defer" else None
    with st.chat_message("user"):
        st.text(question)
    with st.chat_message("assistant"):
        show_banner(mode_banner(turn.mode, next_live_at=eta, tz=business_tz(turn.ctx)))
        st.caption(DRAFT)
        status = st.status(RUNNING, expanded=False)
        st.write_stream(_events(turn, question, status))
        status.update(state="error" if turn.state.error is not None else "complete")
        show_outcome(turn.session_id, turn.state)  # the Retry button follows the rerun (page)


def _log_turn(turn: _Turn, error_type: str | None) -> None:
    """Step 8: ``app.chat.turn_completed`` / ``turn_failed`` and the turns counter."""
    state = turn.state
    error_type = error_type or (state.error[0] if state.error is not None else None)
    mode = turn.mode or "none"
    if error_type is None:
        _log.info(
            "app.chat.turn_completed", session_id=turn.session_id, mode=mode,
            final_run_id=state.final_run_id, verification=state.verification,
            n_evidence=len(state.evidence_ids),
            latency_ms=int((clock.monotonic() - turn.started) * 1000),
        )  # fmt: skip
    else:
        _log.warning("app.chat.turn_failed", session_id=turn.session_id, error_type=error_type)
    result = "ok" if error_type is None else "error"
    _metric("herness_app_chat_turns_total", 1.0, {"mode": mode, "result": result}, counter=True)


def run_turn(ctx: PageContext, session_id: str, text: str | None) -> TurnState:
    """One chat turn (U09-71); ``text=None`` retries the latest user row without a new one.

    Errors before the stream (validation, ownership, redaction, chat policy) propagate to the
    page; the stream's own failure becomes the ``error`` state. After a streamed turn the page
    reruns, so it reloads the assistant row ``ChatService`` wrote.
    """
    inflight = _inflight()
    if session_id in inflight:
        st.warning(BUSY)
        return TurnState()
    inflight.add(session_id)
    turn = _Turn(ctx, session_id, started=clock.monotonic())
    failed: str | None = None
    question: str | None = None
    try:
        question = _question(ctx, session_id, text)
        _stream(turn, question)
    except BaseException as exc:  # also a rerun or stop cutting the turn short: never "ok"
        failed = type(exc).__name__
        if isinstance(exc, Exception) and question is not None and turn.state.error is None:
            turn.state = replace(turn.state, error=(failed, *user_message(exc)), done=True)  # Retry
        raise
    finally:
        inflight.discard(session_id)
        _outcomes()[session_id] = turn.state
        try:
            after_write()
        except Exception as exc:  # noqa: BLE001 - never mask the turn's own outcome
            _log.warning("app.chat.cache_clear_failed", error_type=type(exc).__name__)
        _log_turn(turn, failed)
    st.rerun()  # reached only after a streamed turn; returns outside a script run
    return turn.state  # type: ignore[unreachable]  # st.rerun is typed NoReturn
