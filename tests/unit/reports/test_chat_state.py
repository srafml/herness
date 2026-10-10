"""UT09-61, UT09-62: the chat reducer and banners in ``herness.reports.chat_state`` (OI-09).

T09-25 moved U09-69/U09-70 out of ``app/common/chat_ui.py`` so the terminal chat can use them
(``herness`` never imports ``app``); the dashboard module re-exports the same objects. The full
event tables are exercised through the re-export in ``tests/unit/app/test_app_chat_ui.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tests.support.fake_chat import ESCALATED_RUN_ID, MEMORY_ID, Q1, Q2, RUN_ID, all_kinds

from herness.core import time as clock
from herness.core.types import EvidenceEvent, ToolEvent
from herness.reports import chat_state
from herness.reports.chat_state import CAP, TurnState, apply_event, mode_banner

pytestmark = pytest.mark.unit


def test_ut09_61_reducer_folds_a_whole_turn() -> None:
    """UT09-61 every kind in turn order; ``correction_captured`` accepted after ``done``."""
    state = TurnState()
    for event in all_kinds("cloud"):
        state = apply_event(state, event)
    assert (state.mode, state.verification, state.final_run_id) == ("cloud", "verified", RUN_ID)
    assert state.evidence_ids == (Q1, Q2)
    assert state.escalated is not None
    assert state.escalated[0] == ESCALATED_RUN_ID
    assert (state.done, state.draft, state.correction_memory_id) == (True, "", MEMORY_ID)
    assert apply_event(state, EvidenceEvent(query_id=Q1)) is state  # ignored after done


def test_ut09_61_repeated_evidence_and_cap() -> None:
    """UT09-61 a repeated evidence id is ignored; tools stop at 200 entries."""
    state = apply_event(TurnState(evidence_ids=(Q1,)), EvidenceEvent(query_id=Q1))
    assert state.evidence_ids == (Q1,)
    full = TurnState(tools=(("t", None, True),) * CAP)
    assert apply_event(full, ToolEvent(name="x", query_id=None, ok=True)).tools == full.tools


def test_ut09_62_banners_and_reexport() -> None:
    """UT09-62 live has no banner; defer formats the slot in the zone; app re-exports them."""
    from app.common import chat_ui  # noqa: PLC0415 - the dashboard re-export (OI-09)

    tz = clock.zone("Europe/Berlin")
    assert mode_banner("live", next_live_at=None, tz=tz) is None
    slot = datetime(2026, 1, 5, 8, tzinfo=UTC)
    banner = mode_banner("defer", next_live_at=slot, tz=tz)
    assert banner is not None
    assert banner[0] == "warning"
    assert banner[1].endswith("after 2026-01-05 09:00 CET.")
    queued = mode_banner("defer", next_live_at=None, tz=tz)
    assert queued is not None
    assert queued[1].endswith(f"after {chat_state.NEXT_WINDOW}.")
    small = mode_banner("small_model", next_live_at=None, tz=tz)
    assert small == chat_state.MODE_BANNERS["small_model"]
    exported = (chat_ui.TurnState, chat_ui.apply_event, chat_ui.mode_banner, chat_ui.MODE_BANNERS)
    assert exported == (TurnState, apply_event, mode_banner, chat_state.MODE_BANNERS)
