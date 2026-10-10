"""Private helper of ``cmd_chat.py`` (T09-25 split for the line budget): one terminal chat turn.

``Term`` prints every text through ``safe_terminal_text`` (TH09-15). ``ask`` writes the user row
through ``herness.reports.actions``; ``answer`` takes the mode from the server-side policy
(R-38), folds the ``ChatService`` events with the shared reducer (U09-69) and prints the outcome
of U09-99 "Question", ``defer`` polling included. Store and job modules are imported lazily
(BT09-07).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Final, Protocol

from herness._cli.term import safe_terminal_text
from herness.core import time as clock
from herness.core.errors import HernessError
from herness.core.types import ChatEvent, ChatMode, ToolEvent
from herness.reports.chat_state import TurnState, apply_event, mode_banner
from herness.reports.rules import user_message

if TYPE_CHECKING:
    from rich.console import Console

    from herness.core.config import HernessConfig
    from herness.harness.memory import MemoryStore
    from herness.reports.rules import Actor
    from herness.store.ops.chat import ChatMessageRow

__all__ = ["COMMANDS", "PROMPT", "Answerer", "Term", "answer", "ask", "history", "policy"]

PROMPT: Final = "you> "
HISTORY: Final = 20
COMMANDS: Final = "Commands: /quit, /sessions, /evidence q_…, /correct <text>, /retry."
FAILED: Final = "This answer could not be produced."
RETRY: Final = "Type /retry to try again."
CAPTURED: Final = "Your correction was saved for review."
STOPPED: Final = "Stopped waiting; the answer will appear in this chat later."
QUEUED: Final = "Queued; the answer will appear here later."
TAGS: Final = {"verified": "[verified]", "partial": "[partially verified]"}
UNVERIFIED: Final = "[unverified]"
_FINAL: Final = frozenset({"done", "failed"})


class Answerer(Protocol):
    """``herness.harness.pipelines.chat.ChatService.answer`` (spec 06)."""

    def answer(
        self, session_id: str, text: str, user_ref: str, mode: ChatMode
    ) -> Iterator[ChatEvent]: ...


type Services = tuple[Answerer, MemoryStore]


@dataclass
class Term:
    """One terminal chat: console, actor, config, session and the lazily built services."""

    con: Console
    actor: Actor
    cfg: HernessConfig
    session_id: str
    build: Callable[[HernessConfig], Services]
    services: Services | None = None

    def say(self, text: str, style: str = "") -> None:
        """Print ``text`` made terminal-safe (TH09-15)."""
        self.con.print(safe_terminal_text(text), style=style or None)

    def error(self, what: str, fix: str | None) -> None:
        self.say(f"Error: {what}", "red")
        if fix:
            self.say(f"Fix: {fix}")

    def svc(self) -> Services:
        if self.services is None:
            self.services = self.build(self.cfg)
        return self.services


def policy(now: datetime) -> tuple[ChatMode, datetime | None]:
    """The server-side chat mode (R-38) and, for ``defer``, the next live slot."""
    from herness.core.jobs.chat_policy import chat_next_live_at, chat_policy  # noqa: PLC0415

    mode = chat_policy(now)
    return mode, (chat_next_live_at(now) if mode == "defer" else None)


def show_row(term: Term, row: ChatMessageRow, removed: tuple[str, ...] = ()) -> None:
    """One assistant row: the answer with its tag, removed claims and evidence ids."""
    if row["status"] == "failed":
        term.say(FAILED, "red")
    elif row["status"] != "done":
        term.say(QUEUED if row["status"] == "queued" else "Answering…", "dim")
    else:
        term.say(row["content"])
        term.say(TAGS.get(row["verified"] or "", UNVERIFIED), "dim")
        if row["verified"] == "partial" and removed:
            term.say("Removed claims: " + "; ".join(removed), "dim")
        if row["query_ids"]:
            term.say("Evidence: " + " ".join(row["query_ids"]), "dim")


def history(term: Term) -> None:
    """The last 20 messages: user rows as text, assistant rows with their tag."""
    from herness.store.ops.chat import list_chat_messages  # noqa: PLC0415 - lazy (BT09-07)

    term.say(f"Session {term.session_id}. {COMMANDS}", "dim")
    for row in list_chat_messages(term.session_id, limit=HISTORY):
        if row["role"] == "user":
            term.say(PROMPT + row["content"])
        elif row["role"] == "assistant":
            show_row(term, row)


def ask(term: Term, text: str) -> None:
    """Post the redacted user row (length checked by the action), then answer it."""
    from herness.reports.actions import post_user_message  # noqa: PLC0415 - lazy (BT09-07)

    max_chars = term.cfg.app.chat.max_question_chars
    posted = post_user_message(term.actor, term.session_id, text, max_chars=max_chars,
                               now=clock.now())  # fmt: skip
    answer(term, posted.redacted_text)


def _stream(term: Term, question: str, mode: ChatMode) -> TurnState:
    """Reduce every event; tool events become dim status lines, tokens are only collected."""
    state = TurnState()
    try:
        for event in term.svc()[0].answer(term.session_id, question, term.actor.user_ref, mode):
            accepted = not state.done
            state = apply_event(state, event)
            if accepted and isinstance(event, ToolEvent):
                status = "ok" if event.ok else "failed"
                term.say(f"  {event.name} · {event.query_id or '-'} · {status}", "dim")
    except HernessError as exc:
        what, fix = user_message(exc)
        state = replace(state, error=(type(exc).__name__, what, fix), done=True)
    return state


def _reply(session_id: str) -> ChatMessageRow | None:
    """The assistant row answering the latest user row of the session."""
    from herness.store.ops.chat import find_assistant_message, latest_user_message  # noqa: PLC0415

    latest = latest_user_message(session_id)
    return None if latest is None else find_assistant_message(session_id, latest["message_id"])


def _wait(term: Term) -> None:
    """``defer``: poll until the answer is final; Ctrl+C returns to the prompt (job untouched)."""
    try:
        while (row := _reply(term.session_id)) is None or row["status"] not in _FINAL:
            clock.sleep(term.cfg.app.chat.poll_queued_s)
    except KeyboardInterrupt:
        term.say(STOPPED, "dim")
        return
    show_row(term, row)


def answer(term: Term, question: str) -> None:
    """Mode banner, the streamed answer, then the outcome (U09-99 "Question")."""
    term.svc()  # binds the chain registry that chat_policy reads (R-04)
    mode, eta = policy(clock.now())
    banner = mode_banner(mode, next_live_at=eta, tz=clock.zone(term.cfg.weights.business_timezone))
    if banner is not None:
        term.say(banner[1], "yellow" if banner[0] == "warning" else "cyan")
    state = _stream(term, question, mode)
    if state.error is not None:
        term.error(state.error[1], state.error[2])
        term.say(RETRY)
    elif mode == "defer":
        _wait(term)
    elif not state.done:  # the stream ended with neither `final` nor `error`
        term.say(FAILED, "red")
        term.say(RETRY)
    elif (row := _reply(term.session_id)) is None or row["status"] != "done":
        term.say(FAILED, "red")
    else:
        show_row(term, row, state.removed_claims)
    if state.escalated is not None:
        run_id = state.escalated[0]
        term.say(f"Escalated to run `{run_id}`; the summary will appear in this chat later.")
    if state.correction_memory_id is not None:
        term.say(CAPTURED, "dim")
