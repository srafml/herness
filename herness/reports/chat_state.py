"""Chat turn reducer and chat-hours banners shared by the dashboard and the terminal chat.

Impl 09 U09-69 and U09-70, moved here from ``app/common/chat_ui.py`` by T09-25 (OI-09 default:
``herness`` never imports ``app``, so the CLI could not use them there); ``app.common.chat_ui``
re-exports every name. Pure: no rendering and no I/O.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Final, Literal
from zoneinfo import ZoneInfo

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

__all__ = [
    "CAP", "MODE_BANNERS", "NEXT_WINDOW", "BannerKind", "TurnState", "apply_event", "mode_banner",
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
CAP: Final = 200  # entries kept in ``tools`` and ``evidence_ids``


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
