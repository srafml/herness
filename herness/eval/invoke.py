"""Chat and review invocation for eval (U11-62, design 11 §5.3.1 steps 3-4; flow F11-07 step 6).

`invoke_chat` runs one question through the chat service; `invoke_review` runs one review
through the swarm and reads its `draft.json` (both modes, R-49) and the run's findings through
the public store API (R-08: no Blackboard). `ReviewCache` shares one review run between the
questions that use it. A pipeline exception never propagates: it becomes
`PipelineOutput.error` (`<class name>: <message>`, secrets scrubbed, message ≤ 500 chars).
The chat mode is the caller's decision (OI-3: `cloud` for `premium`, else `live`).
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

from pydantic import BaseModel, ConfigDict

from herness.core.ids import new_ulid
from herness.core.logging import get_logger
from herness.core.secrets import scrub_secrets
from herness.core.types import (
    ChatAnswer,
    ChatMode,
    ErrorEvent,
    FinalEvent,
    Finding,
    NumberRef,
    Paragraph,
    RecommendationItem,
    ReportDraft,
)
from herness.eval.grading import NumberCarrier, carriers_from, chat_entity_ids
from herness.store.ops import query_findings

if TYPE_CHECKING:
    from herness.core.jobs import JobContext
    from herness.harness.pipelines.chat import ChatService
    from herness.harness.swarm import RunRequest, Swarm

__all__ = ["PipelineOutput", "ReviewCache", "invoke_chat", "invoke_review"]

ERROR_MESSAGE_MAX: Final = 500
FINDINGS_LIMIT: Final = 500  # the store's per-call maximum (U06-43)
EVAL_USER_REF: Final = "eval"
_WITHHELD: Final = "[message withheld]"

_log = get_logger("eval.invoke")


class PipelineOutput(BaseModel):
    """What grading needs from one pipeline invocation (U11-62); `draft_mode` None for chat."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    run_id: str = ""
    final_text: str = ""
    carriers: list[NumberCarrier] = []
    numbers: list[NumberRef] = []
    ranked_ids: list[str] = []
    verified_claims: list[str] = []
    findings: list[Finding] = []
    draft_mode: Literal["full", "findings_only"] | None = None
    latency_s: float = 0.0
    error: str | None = None


def _error_text(kind: str, message: str) -> str:
    """`<kind>: <message>` with known secrets and credential spans masked, message cut."""
    scrubbed = scrub_secrets(None, "error", {"m": message}).get("m")
    text = scrubbed if isinstance(scrubbed, str) else _WITHHELD
    return f"{kind}: {text[:ERROR_MESSAGE_MAX]}"


def _exc_text(err: BaseException) -> str:
    return _error_text(type(err).__name__, str(err))


def _numbers(carriers: Iterable[NumberCarrier]) -> list[NumberRef]:
    return [n for c in carriers for n in c.numbers]


def invoke_chat(
    chat: ChatService,
    text: str,
    *,
    mode: ChatMode,
    now: Callable[[], float],
    name_index: Mapping[str, str] | None = None,
    open_session: Callable[[str], str] | None = None,
) -> PipelineOutput:
    """One chat turn in a fresh session (U11-62); `name_index` (display name → entity id)
    yields `ranked_ids` via `chat_entity_ids`. `open_session(text)` (the U11-62 amendment,
    supplied by the eval runner) creates the `ses_` session holding the redacted user message
    the real `ChatService` answers; without it the session id is `eval_<ulid>`."""
    started = now()
    answer: ChatAnswer | None = None
    run_id = ""
    error: str | None = None
    try:
        session_id = "eval_" + new_ulid() if open_session is None else open_session(text)
        for event in chat.answer(session_id, text, EVAL_USER_REF, mode):
            if isinstance(event, FinalEvent):
                answer, run_id = event.answer, event.run_id
            elif isinstance(event, ErrorEvent):
                error = _error_text(event.error_type, event.message)
    except Exception as err:  # noqa: BLE001 - U11-62: a pipeline exception is never raised
        error = _exc_text(err)
    latency = now() - started
    if answer is None:
        error = error or "NoFinalEvent: the chat turn ended without a final answer"
        return PipelineOutput(run_id=run_id, latency_s=latency, error=error)
    carriers = carriers_from([answer])
    return PipelineOutput(
        run_id=run_id, final_text=answer.text, carriers=carriers, numbers=_numbers(carriers),
        ranked_ids=chat_entity_ids(answer.text, name_index or {}), latency_s=latency,
        error=error,
    )  # fmt: skip


def invoke_review(
    swarm: Swarm, req: RunRequest, *, job: JobContext | None, now: Callable[[], float]
) -> PipelineOutput:
    """Run one review and read its draft and findings (U11-62); never raises."""
    started = now()
    run_id = ""
    try:
        result = asyncio.run(swarm.start(req, job))
        run_id = result.run_id
        if result.draft_path is None:
            error = f"NoDraft: run {run_id} ended {result.status} without a draft"
            return PipelineOutput(run_id=run_id, latency_s=now() - started, error=error)
        text = Path(result.draft_path).read_text(encoding="utf-8")
        draft = ReportDraft.model_validate_json(text)
        found = query_findings(run_id, statuses=None, limit=FINDINGS_LIMIT)
        latency = now() - started
    except Exception as err:  # noqa: BLE001 - U11-62: a pipeline exception is never raised
        return PipelineOutput(run_id=run_id, latency_s=now() - started, error=_exc_text(err))
    if len(found) >= FINDINGS_LIMIT:  # the store caps one read; later findings are not seen
        _log.warning("eval.invoke.findings_truncated", run_id=run_id, limit=FINDINGS_LIMIT)
    return _from_draft(run_id, draft, found, latency)


def _from_draft(
    run_id: str, draft: ReportDraft, found: list[Finding], latency: float
) -> PipelineOutput:
    """Final text, carriers and ranked ids of a `full` or `findings_only` draft (R-49)."""
    if draft.mode == "full":
        texts, carriers, ranked = _full(draft)
    else:
        texts, carriers, ranked = _findings_only(draft, {f.finding_id: f for f in found})
    return PipelineOutput(
        run_id=run_id, final_text="\n".join(texts), carriers=carriers,
        numbers=_numbers(carriers), ranked_ids=ranked,
        verified_claims=[f.claim for f in found if f.status == "verified"], findings=found,
        draft_mode=draft.mode, latency_s=latency,
    )  # fmt: skip


def _full(draft: ReportDraft) -> tuple[list[str], list[NumberCarrier], list[str]]:
    """Title, paragraphs, recommendations, caveats, prior-outcomes commentary; carriers per
    paragraph (commentary included) and recommendation."""
    items: list[Paragraph | RecommendationItem] = [
        p for section in draft.sections for p in section.paragraphs
    ]
    items += draft.recommendations
    poc = draft.prior_outcomes_commentary
    if poc is not None:
        items.append(poc)
    carriers = carriers_from(items)
    body = carriers[:-1] if poc is not None else carriers  # the commentary goes last
    texts = [draft.title, *(c.text for c in body), *draft.caveats]
    if poc is not None:
        texts.append(poc.text)
    ranked = [e.entity_id for e in draft.ranked_entities]
    return texts, carriers, ranked


def _findings_only(
    draft: ReportDraft, by_id: Mapping[str, Finding]
) -> tuple[list[str], list[NumberCarrier], list[str]]:
    """Title, every verified claim the draft holds, caveats; one carrier per finding.

    A Writer-dead draft holds one `executive_summary` paragraph per verified finding whose
    text and numbers are the finding's `claim` and `numbers` (U06-142); entity ids come
    from the stored findings the paragraphs cite, in draft order.
    """
    paragraphs = [p for section in draft.sections for p in section.paragraphs]
    carriers = carriers_from(paragraphs)
    texts = [draft.title, *(c.text for c in carriers), *draft.caveats]
    if draft.ranked_entities:
        ranked = [e.entity_id for e in draft.ranked_entities]
    else:
        cited = (by_id.get(fid) for p in paragraphs for fid in p.finding_ids)
        ranked = list(dict.fromkeys(f.entity_id for f in cited if f is not None))
    return texts, carriers, ranked


@dataclass
class _Slot:
    done: threading.Event = field(default_factory=threading.Event)
    output: PipelineOutput | None = None


type CacheKey = tuple[str, str, str] | tuple[str, str, str, str]


class ReviewCache:
    """In-memory review runs shared by questions (U11-62); one factory call per key.

    Keys are `(pipeline, profile, depth)` for shared runs and `(pipeline, profile, depth,
    question_id)` for framing questions. A lock guards the table; concurrent callers of one
    key wait for the first caller's run and get the same output.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._slots: dict[CacheKey, _Slot] = {}

    def get_or_run(
        self,
        pipeline: str,
        profile: str,
        depth: str,
        framing_question: str | None,
        factory: Callable[[], PipelineOutput],
    ) -> PipelineOutput:
        """The cached output for the key, running `factory` once when it is new."""
        key: CacheKey = (
            (pipeline, profile, depth)
            if framing_question is None
            else (pipeline, profile, depth, framing_question)
        )
        with self._lock:
            slot = self._slots.get(key)
            owner = slot is None
            if slot is None:
                slot = self._slots[key] = _Slot()
        if owner:
            try:
                slot.output = factory()
            except Exception as err:  # noqa: BLE001 - U11-62: a pipeline exception is never raised
                slot.output = PipelineOutput(error=_exc_text(err))
            finally:
                if slot.output is None:  # owner died (BaseException): later callers re-run
                    with self._lock:
                        self._slots.pop(key, None)
                slot.done.set()
        else:
            slot.done.wait()
        if slot.output is None:
            return PipelineOutput(error="ReviewCacheError: the shared run did not complete")
        return slot.output
