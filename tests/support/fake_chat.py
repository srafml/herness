"""`FakeChatService`: replays scripted `ChatEvent` lists in place of spec 06's `ChatService`.

Impl 09 §11 (T09-19). `answer(session_id, text, user_ref, mode)` records the call and yields
the next script (the last one repeats). With `write_rows=True` it also writes the assistant
row the way spec 06 does (placeholder, then `done` / `failed` / `queued`), so page tests see
real rows; every row it writes is listed in `written`, so a test can tell the UI's writes
from the service's. `all_kinds()` is one script with the eight design kinds plus
`correction_captured` (R-32).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from herness.core.errors import HernessError
from herness.core.types import (
    ChatAnswer,
    ChatEvent,
    ChatMode,
    CorrectionCapturedEvent,
    ErrorEvent,
    EscalatedEvent,
    EvidenceEvent,
    FinalEvent,
    ModeEvent,
    NumberRef,
    TokenEvent,
    ToolEvent,
    VerificationEvent,
    VerificationResult,
)
from herness.store.ops.chat import (
    latest_user_message,
    update_chat_message,
    upsert_assistant_placeholder,
)

__all__ = [
    "ANSWER_TEXT",
    "ESCALATED_RUN_ID",
    "MEMORY_ID",
    "Q1",
    "Q2",
    "RUN_ID",
    "FakeChatService",
    "all_kinds",
    "error_script",
    "final_event",
    "verification",
]  # fmt: skip

Q1 = "q_0123456789abcdef"
Q2 = "q_fedcba9876543210"
RUN_ID = "run_01J9ZQ4Y8M6V3K2N1P0R5T7W9X"
ESCALATED_RUN_ID = "run_01J9ZQ4Y8M6V3K2N1P0R5T7W9Y"
ESCALATED_JOB_ID = "job_01J9ZQ4Y8M6V3K2N1P0R5T7W9Z"
MEMORY_ID = "mem_01J9ZQ4Y8M6V3K2N1P0R5T7W9W"
ANSWER_TEXT = "Team One had 42 incidents."
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def verification() -> VerificationResult:
    """An empty passing `VerificationResult`."""
    return VerificationResult(
        build_id="b1", passed=True, items=[], n_numbers=0, n_failed=0, verified_at=NOW,
        duration_ms=0,
    )  # fmt: skip


def final_event(text: str = ANSWER_TEXT, *, run_id: str = RUN_ID) -> FinalEvent:
    """A `final` event citing one number (42, query `Q1`)."""
    ref = NumberRef(id="n1", value=42, unit="count", query_id=Q1, column="n", row_key=None)
    answer = ChatAnswer(text=text, numbers=[ref], query_ids=[Q1])
    return FinalEvent(answer=answer, run_id=run_id)


def all_kinds(
    mode: ChatMode = "live", *, tokens: Sequence[str] = ("Team One ", "had 42.")
) -> list[ChatEvent]:
    """The eight design kinds in turn order, then `correction_captured` after `final`."""
    return [
        ModeEvent(mode=mode, message=f"mode {mode}"),
        ToolEvent(name="run_sql", query_id=Q1, ok=True),
        EvidenceEvent(query_id=Q1),
        *[TokenEvent(text=t) for t in tokens],
        ToolEvent(name="run_sql", query_id=None, ok=False),
        EvidenceEvent(query_id=Q2),
        VerificationEvent(result=verification(), status="verified", removed_claims=[]),
        EscalatedEvent(run_id=ESCALATED_RUN_ID, job_id=ESCALATED_JOB_ID),
        final_event(),
        CorrectionCapturedEvent(memory_id=MEMORY_ID),
    ]


def error_script(error_type: str = "ModelUnavailable") -> list[ChatEvent]:
    """A turn that fails before any token."""
    return [
        ModeEvent(mode="live", message="mode live"),
        ErrorEvent(error_type=error_type, message="The model is not reachable.",
                   hint="Try again later."),
    ]  # fmt: skip


@dataclass
class FakeChatService:
    """Stand-in for `herness.harness.pipelines.chat.ChatService` (impl 09 §11)."""

    scripts: list[list[ChatEvent]] = field(default_factory=list)
    write_rows: bool = False
    raises: HernessError | None = None  # raised by the generator after the script
    calls: list[tuple[str, str, str, ChatMode]] = field(default_factory=list)
    written: list[str] = field(default_factory=list)

    def answer(
        self, session_id: str, text: str, user_ref: str, mode: ChatMode
    ) -> Iterator[ChatEvent]:
        """Record the call, replay the next script, then raise `raises` when set."""
        self.calls.append((session_id, text, user_ref, mode))
        index = min(len(self.calls), len(self.scripts)) - 1
        events = self.scripts[index] if index >= 0 else []
        reply = self._placeholder(session_id)
        yield from events
        if reply is not None:
            self._finish(reply, events, mode)
        if self.raises is not None:
            raise self.raises

    def _placeholder(self, session_id: str) -> str | None:
        if not self.write_rows:
            return None
        msg = latest_user_message(session_id)
        assert msg is not None, "FakeChatService(write_rows=True) needs a user row"
        row = upsert_assistant_placeholder(session_id, reply_to=msg["message_id"], now=NOW)
        assert row is not None
        if row["message_id"] not in self.written:
            self.written.append(row["message_id"])
        return row["message_id"]

    @staticmethod
    def _finish(reply: str, events: Sequence[ChatEvent], mode: ChatMode) -> None:
        """The row outcome spec 06 writes: queued (defer), failed (error) or done (final)."""
        if mode == "defer":
            update_chat_message(reply, status="queued")
            return
        final = next((e for e in events if isinstance(e, FinalEvent)), None)
        if final is None:
            update_chat_message(reply, status="failed")
            return
        status = next((e.status for e in events if isinstance(e, VerificationEvent)), None)
        numbers = [n.model_dump(mode="json") for n in final.answer.numbers]
        update_chat_message(
            reply, status="done", content=final.answer.text, verified=status or "unverified",
            run_id=final.run_id, query_ids=list(final.answer.query_ids),
            meta={"numbers": numbers},  # merged: the placeholder holds reply_to
        )  # fmt: skip
