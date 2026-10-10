"""User actions shared by the dashboard and the CLI: role check, validation, audit, write.

Every write a person triggers goes through one of these functions (impl 09 §3.7, U09-34 …
U09-42, U09-101), so both channels run the same server-side role check (TH09-07), the same
validators, redaction of typed text before storage (TH09-12) and the same audit line before
an admin change or a recommendation decision (TH09-19). Typed text never reaches a log line
or an error message; logs carry ids, lengths and ``user_ref`` only (TH09-21).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, cast

from pydantic import JsonValue

from herness.core import jobs
from herness.core import time as clock
from herness.core.audit import audit
from herness.core.config import get_config
from herness.core.errors import NotFound, PolicyViolation, SchemaViolation
from herness.core.ids import canonical_json
from herness.core.jobs.status import FINISHED_RUN
from herness.core.logging import get_logger
from herness.core.redact import redact_text
from herness.core.resilience import retry_call
from herness.core.types import MemoryProposal, Provenance
from herness.reports import rules as r
from herness.reports._actions_audit import (
    Decided,
    admin_change,
    already_finished,
    completed,
    failed,
    memory_decide,
)
from herness.reports.contract import RUN_ID_RE
from herness.reports.render import Format, render_run
from herness.reports.rules import Actor, UserInputError
from herness.store.errors import ReviewItemConflict
from herness.store.ops import (
    append_chat_message,
    create_chat_session,
    decide_review_item,
    get_chat_message,
    get_chat_session,
    get_review_item,
    get_run,
    ui_get_recommendation,
    ui_job_exists,
    update_chat_message,
)

if TYPE_CHECKING:
    from herness.core.types import ReportManifest
    from herness.harness.memory import MemoryStore
    from herness.harness.memory.types import ProposeResult
    from herness.store.ops import ChatSessionRow

__all__ = [
    "PostedMessage", "decide_memory", "decide_recommendation", "decide_review", "job_control",
    "post_user_message", "propose_correction", "require_session_owner", "rerender_report",
    "resume_run", "set_feedback", "start_session",
]  # fmt: skip

type EntityType = Literal["service", "team", "org", "work_item", "cluster"]

_chat_log = get_logger("app.chat")
_auth_log = get_logger("app.auth")
_UNSAFE: Final = "Your message could not be processed safely."
_REVIEW_ACTION: Final = {
    "weight_change": "review_decide_weight_change",
    "memory_write": "memory_decide",
}
_ENTITY_ID_MAX: Final = 200


@dataclass(frozen=True)
class PostedMessage:
    """The stored user row (U09-34): its id and the redacted text the turn must use."""

    message_id: str
    redacted_text: str


def _redacted(text: str) -> str:
    """``redact_text(text)``; fail closed: no result → PolicyViolation, text never kept."""
    try:
        out = redact_text(text)
    except Exception as exc:
        raise PolicyViolation(_UNSAFE) from exc
    if out is None:
        raise PolicyViolation(_UNSAFE)
    return out


def _session_not_found(session_id: str) -> NotFound:
    return NotFound(f"chat session {session_id} not found")


# --- U09-42 require_session_owner ----------------------------------------------------------


def require_session_owner(actor: Actor, session_id: str) -> ChatSessionRow:
    """The session row when ``actor`` owns it; another user's session looks missing (TH09-10)."""
    r.check_id(r.SESSION_ID_RE, session_id, "session")
    row = get_chat_session(session_id)
    if row is None:
        raise _session_not_found(session_id)
    if row["user_ref"] != actor.user_ref:
        try:
            # rules.py precedent (T09-02 ruling): impl 10's `auth` schema has no `action` field.
            audit("auth", actor.user_ref, user_ref=actor.user_ref, role=actor.role, result="denied")
        except Exception as exc:  # noqa: BLE001 - an audit failure is logged; the refusal stands
            _auth_log.error(
                "app.auth.audit_failed", action="chat_session", error_type=type(exc).__name__
            )
        raise _session_not_found(session_id)
    return row


# --- U09-34 start_session, post_user_message --------------------------------------------------


def start_session(actor: Actor, *, now: datetime) -> str:
    """Create a chat session owned by ``actor.user_ref``; returns its ``session_id``."""
    r.require_role(actor, "chat")
    return retry_call("sqlite_write", create_chat_session, actor.user_ref, now=now)


def post_user_message(
    actor: Actor, session_id: str, text: str, *, max_chars: int, now: datetime
) -> PostedMessage:
    """Insert the redacted ``user`` row of a turn (design §5.5 steps 1 and 2); text never logged."""
    r.require_role(actor, "chat")
    question = r.validate_question(text, max_chars=max_chars)
    require_session_owner(actor, session_id)
    redacted = _redacted(question)
    message_id = retry_call(
        "sqlite_write", append_chat_message, session_id, "user", redacted, now=now
    )
    if message_id is None:
        raise _session_not_found(session_id)
    _chat_log.info(
        "app.chat.message_posted", session_id=session_id, message_id=message_id,
        chars=len(question), channel=actor.channel,
    )  # fmt: skip
    return PostedMessage(message_id=message_id, redacted_text=redacted)


# --- U09-35 set_feedback ----------------------------------------------------------------------


def set_feedback(
    actor: Actor, message_id: str, *, feedback: Literal["up", "down"], note: str | None = None
) -> None:
    """Store thumbs and an optional redacted note on an answer row of the actor's session."""
    r.require_role(actor, "chat_feedback")
    r.check_id(r.MESSAGE_ID_RE, message_id, "message")
    msg = get_chat_message(message_id)
    missing = NotFound(f"chat message {message_id} not found")
    if msg is None:
        raise missing
    try:  # owner first: another user's message (any kind) looks missing, its session not echoed
        require_session_owner(actor, msg["session_id"])
    except NotFound:
        raise missing from None
    if msg["role"] != "assistant":
        text = "Feedback applies to answers only."
        raise UserInputError(text)
    clean = r.validate_note(note, required=False)
    stored = None if clean is None else _redacted(clean)
    retry_call(
        "sqlite_write", update_chat_message, message_id, feedback=feedback, feedback_note=stored
    )
    _chat_log.info(
        "app.chat.feedback_saved", message_id=message_id, feedback=feedback,
        has_note=stored is not None,
    )  # fmt: skip


# --- U09-36 propose_correction ----------------------------------------------------------------


def propose_correction(  # noqa: PLR0913 - keyword parameters fixed by U09-36
    actor: Actor,
    *,
    session_id: str,
    statement: str,
    entity: tuple[EntityType, str] | None = None,
    source_message_id: str,
    memory: MemoryStore,
    build_id: str | None = None,
) -> ProposeResult:
    """The "Correct a fact" form → one pending semantic memory proposal via chat (TH09-26)."""
    r.require_role(actor, "chat_correction")
    fact = r.validate_correction(statement)
    r.check_id(r.MESSAGE_ID_RE, source_message_id, "message")
    if entity is not None and not 1 <= len(entity[1]) <= _ENTITY_ID_MAX:
        text = f"Entity id must be 1 to {_ENTITY_ID_MAX} characters."
        raise UserInputError(text)
    require_session_owner(actor, session_id)
    msg = get_chat_message(source_message_id)
    if msg is None or msg["session_id"] != session_id or msg["role"] != "user":
        text = "The message to correct is not in this chat."
        raise UserInputError(text)
    entities: list[JsonValue] = [] if entity is None else [{"type": entity[0], "id": entity[1]}]
    provenance = Provenance(
        author_type="human", author_role=None, author_ref=actor.user_ref, run_id=None,
        task_id=None, session_id=session_id, source_message_id=source_message_id,
        build_id=build_id, via="chat",
    )  # fmt: skip
    proposal = MemoryProposal(
        layer="semantic", kind="user_correction", content=fact, confidence=0.5,
        data={"statement": fact, "effective_date": None, "suggested_action": "none",
              "entities": entities},
        provenance=provenance,
    )  # fmt: skip
    result = memory.propose(proposal)
    _chat_log.info("app.chat.correction_proposed", memory_id=result.memory_id, status=result.status)
    return result


# --- U09-37 decide_recommendation -------------------------------------------------------------


def decide_recommendation(
    actor: Actor,
    *,
    rec_id: str,
    decision: Literal["accepted", "rejected", "deferred"],
    reason: str,
    effective_at: date | None = None,
    memory: MemoryStore,
) -> None:
    """Record a human decision on a recommendation; the audit line is written first."""
    r.require_role(actor, "decide_recommendation")
    r.check_id(r.REC_ID_RE, rec_id, "recommendation")
    why = r.validate_reason(reason)
    if ui_get_recommendation(rec_id) is None:
        text = f"recommendation {rec_id} not found"
        raise NotFound(text)
    eff = None if effective_at is None else datetime.combine(effective_at, time(0), UTC)
    # impl 10 schema {rec_id, decision, decided_by}: no reason_len, no detail (impl 09 note).
    audit("recommendation_decision", actor.user_ref, rec_id=rec_id, decision=decision,
          decided_by=actor.user_ref)  # fmt: skip
    try:
        memory.decide(rec_id, decision, why, actor.user_ref, eff)
    except Exception as exc:
        failed(actor, "decide_recommendation", rec_id, exc, line=False)
        raise
    completed(actor, "decide_recommendation", rec_id=rec_id, decision=decision)


# --- U09-38 decide_review, U09-101 decide_memory ----------------------------------------------


def _stored_note(kind: str, approve: bool, note: str | None, answer: str | None) -> str | None:
    """The note impl 02 stores; a label_check approval stores ``{"answer": ...}`` (spec 03)."""
    if kind != "label_check" or not approve:
        return note
    if not (answer or "").strip():
        text = "Choose the correct answer to approve a label check."
        raise UserInputError(text)
    return canonical_json({"answer": r.validate_answer(answer or "")})


def decide_review(  # noqa: PLR0913 - keyword parameters fixed by U09-38
    actor: Actor,
    *,
    item_id: str,
    approve: bool,
    note: str | None = None,
    answer: str | None = None,
    memory: MemoryStore,
    now: datetime,
) -> Decided:
    """Approve or reject a pending review item; the role needed depends on its kind."""
    r.check_id(r.ITEM_ID_RE, item_id, "review item")
    item = get_review_item(item_id)
    if item.status != "pending":
        text = f"Item {item_id} was already {item.status}."
        raise UserInputError(text)
    action = _REVIEW_ACTION.get(item.kind, "review_decide")
    r.require_role(actor, action)
    is_memory = item.kind == "memory_write"
    clean = r.validate_note(
        note, required=not approve, max_chars=500 if approve and is_memory else 1000
    )
    if is_memory:
        memory_id = item.payload.get("memory_id")
        if not isinstance(memory_id, str):
            msg = f"review item {item_id} has no memory_id"
            raise SchemaViolation(msg)
        refusal = f"Item {item_id} is no longer pending."
        status = memory_decide(memory, memory_id, actor, clean, approve=approve, refusal=refusal)
    else:
        stored = _stored_note(item.kind, approve, clean, answer)
        status = "approved" if approve else "rejected"
        try:
            retry_call("sqlite_write", decide_review_item, item_id, status,
                       decided_by=actor.user_ref, note=stored, now=now)  # fmt: skip
        except ReviewItemConflict:
            text = f"Item {item_id} was already decided."
            raise UserInputError(text) from None
    completed(actor, action, item_id=item_id, kind=item.kind, status=status)
    return status


def decide_memory(
    actor: Actor, *, memory_id: str, approve: bool, note: str | None = None, memory: MemoryStore
) -> Decided:
    """Approve or reject a memory item by id (R-33); impl 07 decides the linked review item."""
    r.require_role(actor, "memory_decide")
    r.check_id(r.MEMORY_ID_RE, memory_id, "memory")
    clean = r.validate_note(note, required=not approve, max_chars=500 if approve else 1000)
    refusal = f"Memory item {memory_id} is not pending."
    status = memory_decide(memory, memory_id, actor, clean, approve=approve, refusal=refusal)
    completed(actor, "memory_decide", memory_id=memory_id, status=status)
    return status


# --- U09-39 job_control, U09-40 resume_run, U09-41 rerender_report ----------------------------


def job_control(actor: Actor, *, job_id: str, op: Literal["cancel", "retry"]) -> str:
    """Cancel or retry a job after the ``admin_action`` request line (DD-19); ``not_active``
    is an outcome (completed log), a raised spec 08 error a ``failed:`` line."""
    action = "job_cancel" if op == "cancel" else "job_retry"
    r.require_role(actor, action)
    r.check_id(r.JOB_ID_RE, job_id, "job")
    if not ui_job_exists(job_id):
        text = f"job {job_id} not found"
        raise NotFound(text)

    def change() -> str:
        if op == "cancel":
            return str(jobs.cancel(job_id))
        jobs.retry(job_id)
        return "queued"

    result = admin_change(actor, action, job_id, change)
    completed(actor, action, job_id=job_id, result=result)
    return result


def resume_run(actor: Actor, *, run_id: str, retry_dead: bool = False, force: bool = False) -> str:
    """Re-enqueue an unfinished run; idempotent through impl 08's ``resume:<run_id>`` key."""
    r.require_role(actor, "run_resume")
    r.check_id(RUN_ID_RE, run_id, "run")
    run = get_run(run_id)
    if run is None:
        text = f"run {run_id} not found"
        raise NotFound(text)
    if run.status in FINISHED_RUN and not force:  # refused before the line (review I1)
        raise already_finished(run_id, run.status)

    def change() -> str:
        res = jobs.enqueue_resume(run_id, force=force, retry_dead=retry_dead)
        if res.job_id is None:  # finished between get_run and enqueue
            raise already_finished(run_id, res.run_status)
        return res.job_id

    job_id = admin_change(actor, "run_resume", run_id, change)
    completed(actor, "run_resume", run_id=run_id, job_id=job_id)
    return job_id


def rerender_report(
    actor: Actor, *, run_id: str, formats: Sequence[str] | None = None,
    out_dir: Path | None = None, strict: bool | None = None, top_n: int | None = None,
) -> ReportManifest:  # fmt: skip
    """Render a run's report: any viewer on the CLI, admins (audited) on the dashboard."""
    action = "report_render" if actor.channel == "cli" else "report_rerender"
    r.require_role(actor, action)
    r.check_id(RUN_ID_RE, run_id, "run")
    settings = get_config().app.reports
    chosen = cast("Sequence[Format]", settings.formats if formats is None else formats)
    strict_numbers = settings.strict_numbers if strict is None else strict

    def change() -> ReportManifest:
        return render_run(run_id, chosen, out_dir, strict_numbers, clock.now(), top_n=top_n)

    if actor.channel == "dashboard":
        return admin_change(actor, "report_render", run_id, change)
    return change()
