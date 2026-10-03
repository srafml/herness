"""Escalation from chat to a mini swarm and its summary message (impl 06 U06-134, U06-135).

`escalate_to_review` turns a chat question into a `fast` review run with the chat escalation
`budget_override` and one `review` job; `post_escalation_summary` posts the finished run's
executive summary into the chat session as one assistant turn. Both are idempotent: the run is
found by its chat source, the job by a deterministic idempotency key, and the summary message by
`meta.escalation_run_id`. Text is redacted before it is stored and never logged (TH06-17: the
session and message ids come from the chat binding and are validated before use).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Mapping
from functools import partial
from typing import TYPE_CHECKING, Final, Literal, Protocol

from herness.core.errors import PolicyViolation, SchemaViolation
from herness.core.logging import get_logger
from herness.core.numbers import format_number, parse_markers
from herness.core.redact import redact_text
from herness.harness.swarm.lifecycle import RunRequest, create_run_record
from herness.store.ops import find_run_by_escalation, run_write, update_run_fields
from herness.store.warehouse import read_current

if TYPE_CHECKING:
    import sqlite3
    from datetime import datetime

    from pydantic import JsonValue

    from herness.core.config import HernessConfig
    from herness.core.types import EntityScope, GpuClass, JobKind, Paragraph, ReportDraft
    from herness.harness.blackboard import Blackboard
    from herness.store.ops import ChatMessageRow, RunRow

__all__ = ["JobsFacade", "escalate_to_review", "post_escalation_summary"]

_ULID: Final = "[0-9A-HJKMNP-TV-Z]{26}"
_SESSION_ID: Final = re.compile(f"ses_{_ULID}")
_MESSAGE_ID: Final = re.compile(f"msg_{_ULID}")
_MAX_QUESTION: Final = 2_000  # RunRequest.question max_length
_MAX_CONTENT: Final = 20_000  # chat_message content cap (impl 09)
_SUMMARY_SECTION: Final = "executive_summary"
_PRIORITY: Final = 75

_log = get_logger("harness.chat")


class JobsFacade(Protocol):
    """The job-queue surface escalation needs.

    Spec note: impl 06 names `JobsFacade` but no spec defines it; this Protocol mirrors
    `herness.core.jobs.queue.enqueue`, so the queue module itself satisfies it.
    """

    def enqueue(  # noqa: PLR0913 - mirrors design 08 §3.4 enqueue (R-41)
        self,
        kind: JobKind,
        payload: dict[str, JsonValue],
        gpu_class: GpuClass,
        priority: int | None = None,
        scheduled_for: datetime | None = None,
        *,
        max_attempts: int | None = None,
        idem_key: str | None = None,
    ) -> str: ...


def _check_ids(session_id: str, message_id: str) -> None:
    """Both ids must be well-formed before they enter an idempotency key or a query."""
    if _SESSION_ID.fullmatch(session_id) is None or _MESSAGE_ID.fullmatch(message_id) is None:
        msg = "escalation needs a valid session id and message id"
        raise SchemaViolation(msg)


def _redacted(text: str, what: str) -> str:
    """Redacted `text`; a failed redaction fails closed with a message naming no text."""
    safe = redact_text(text)
    if safe is None:
        msg = f"{what} failed redaction"
        raise PolicyViolation(msg)
    return safe


async def escalate_to_review(  # noqa: PLR0913 - U06-134 signature
    bb: Blackboard,
    cfg: HernessConfig,
    *,
    session_id: str,
    message_id: str,
    question: str,
    kind: Literal["funding_review", "org_review"],
    focus: EntityScope | None,
    jobs: JobsFacade,
    now: datetime,
) -> tuple[str, str]:
    """Start a mini swarm for a chat message; returns `(run_id, job_id)` (U06-134).

    A retry for the same message returns the existing run and job; a run left without
    `meta.job_id` (stopped before the enqueue) gets its job instead of a second run.
    """
    _check_ids(session_id, message_id)
    run = find_run_by_escalation(session_id, message_id)
    if run is not None and isinstance(known := run.meta.get("job_id"), str):
        return run.run_id, known
    if run is None:
        req = RunRequest(
            kind=kind,
            depth="fast",
            question=_redacted(question, "escalation question")[:_MAX_QUESTION],
            focus=focus,
            budget_override=dict(cfg.pipelines.pipelines.chat.escalation),
        )
        source = {"session_id": session_id, "message_id": message_id}
        create = partial(
            create_run_record, bb, cfg, req,
            job_id=None, escalated_from=source, now=now, current_build=read_current,
        )  # fmt: skip
        run = await asyncio.to_thread(create)  # it waits on the writer thread itself
    run_id = run.run_id
    enqueue = partial(
        jobs.enqueue, "review", {"run_id": run_id, "resume": True}, gpu_class="reasoning",
        priority=_PRIORITY, idem_key=f"escalate:{session_id}:{message_id}",
    )  # fmt: skip
    job_id = await bb.run_on_writer(enqueue)

    def patch(conn: sqlite3.Connection) -> None:
        update_run_fields(conn, run_id, meta_patch={"job_id": job_id})

    await bb.run_on_writer(lambda: run_write(patch, op="swarm_escalation_job"))
    _log.info(
        "harness.chat.escalated",
        run_id=run_id, job_id=job_id, session_id=session_id, message_id=message_id, kind=kind,
    )  # fmt: skip
    return run_id, job_id


def _escalation_session(run: RunRow) -> str | None:
    """The chat session the run was escalated from, when well-formed."""
    source = run.meta.get("escalated_from")
    session_id = source.get("session_id") if isinstance(source, Mapping) else None
    if isinstance(session_id, str) and _SESSION_ID.fullmatch(session_id) is not None:
        return session_id
    return None


def _summary(draft: ReportDraft | None) -> Paragraph | None:
    """First paragraph of `executive_summary` of a full draft (R-49), else None."""
    if draft is None or draft.mode != "full":
        return None
    for section in draft.sections:
        if section.id == _SUMMARY_SECTION and section.paragraphs:
            return section.paragraphs[0]
    return None


def _render(p: Paragraph) -> str:
    """`p.text` with each valid marker naming one of `p.numbers` formatted (R-16)."""
    refs = {n.id: n for n in p.numbers}
    text = p.text
    for marker in reversed(parse_markers(text).markers):
        if (ref := refs.get(marker.id)) is not None:
            text = text[: marker.start] + format_number(ref) + text[marker.end :]
    return text


def post_escalation_summary(
    run: RunRow,
    draft: ReportDraft | None,
    *,
    append_message: Callable[..., str],
    find_message: Callable[..., ChatMessageRow | None],
) -> str | None:
    """Post the run's executive summary as one assistant turn; returns its message id (U06-135).

    `find_message(session_id, run_id)` returns the assistant row whose `meta.escalation_run_id`
    is the run; when present its id is returned and nothing is written. A run without a valid
    chat source returns None.
    """
    session_id = _escalation_session(run)
    if session_id is None:
        return None
    if (existing := find_message(session_id, run.run_id)) is not None:
        return existing["message_id"]
    p = _summary(draft)
    if p is None:
        text = f"The review finished without an executive summary; see run {run.run_id}."
    else:
        text = _render(p)
    content = _redacted(text, "escalation summary")[:_MAX_CONTENT]
    numbers = [] if p is None else p.numbers
    message_id = append_message(
        session_id,
        role="assistant",
        content=content,
        status="done",
        verified="verified" if draft is not None and draft.verification.passed else "partial",
        run_id=run.run_id,
        query_ids=sorted(n.query_id for n in numbers),
        meta={
            "escalation_run_id": run.run_id,
            "mode": "escalation",
            "numbers": [n.model_dump(mode="json") for n in numbers],
        },
    )
    _log.info("harness.chat.escalation_posted", run_id=run.run_id, message_id=message_id)
    return message_id
