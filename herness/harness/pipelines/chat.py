"""Chat entry point and the deferred `chat` job (impl 06 U06-127 … U06-129, U06-136).

`ChatService.answer` streams one turn as `ChatEvent`s (design 06 §5.13): it reserves the one
assistant row of the user message, defers the turn to a `chat` job, or runs it on a worker
thread with its own event loop (`_chat_turn.run_turn`) and hands the events over through a
bounded queue. `ChatService` is the only writer of assistant `chat_message` rows. The model sees
only the stored, redacted message text; no user or answer text is logged above DEBUG (TH06-14).
"""

from __future__ import annotations

import asyncio
import queue
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol

from herness.core import time as clock
from herness.core.config import HernessConfig, get_config
from herness.core.egress import cloud_chat_allowed
from herness.core.errors import ConfigError, NotFound
from herness.core.jobs import queue as job_queue
from herness.core.jobs.chat_policy import chat_model_profile
from herness.core.logging import get_logger
from herness.core.resilience import metrics as process_metrics
from herness.core.types import ChatEvent, ChatMode, ErrorEvent, JobOutcome, ModeEvent
from herness.harness.llm.registry import LLMRegistry
from herness.harness.memory import MemoryStore, get_memory_store
from herness.harness.pipelines._chat_rows import cleanup
from herness.harness.pipelines._chat_turn import Turn, TurnEnv, run_turn
from herness.harness.pipelines.chat_support import MODE_MESSAGES
from herness.harness.swarm.escalation import JobsFacade
from herness.harness.tracing import Tracer
from herness.harness.verifier import Verifier
from herness.harness.warehouse import WarehousePool
from herness.store.layout import data_layout
from herness.store.ops import evidence as evidence_ops
from herness.store.ops import query_verified_findings_recent
from herness.store.ops.chat import (
    find_assistant_message,
    get_chat_message,
    get_chat_session,
    latest_user_message,
    update_chat_message,
    upsert_assistant_placeholder,
)
from herness.store.warehouse import read_current

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from herness.core.jobs.ports import JobContext
    from herness.core.types import (
        ChatAnswer,
        OpsHandle,
        VectorHandle,
        VectorHit,
        VerificationResult,
        WarehouseHandle,
    )
    from herness.harness.pipelines.settings import PipelinesConfig
    from herness.harness.swarm.tools import PastReader

__all__ = ["ChatDeps", "ChatService", "chat_job_handler"]

QUEUE_MAX: Final = 1000
QUEUE_GRACE_S: Final = 30  # the turn timeout is `chat.budget.wall_clock_s` plus this
_PUT_POLL_S: Final = 0.2
_TIMED_OUT: Final = "chat turn timed out"
_RETRY_HINT: Final = "Try again later."

_log = get_logger("harness.chat")


class ChatJobs(JobsFacade, Protocol):
    """The spec 08 `jobs` surface a turn uses: `enqueue` (U08-47) and `chat_model_profile`."""

    def chat_model_profile(self, mode: ChatMode, depth: str = "fast") -> str | None: ...


class AnswerVerifier(Protocol):
    """Spec 05 `Verifier.verify_answer` (U05-67)."""

    def verify_answer(self, answer: ChatAnswer, build_id: str) -> VerificationResult: ...


class WarehouseSource(Protocol):
    """`WarehousePool.get`: the read-only handle of one build."""

    def get(self, build_id: str) -> WarehouseHandle: ...


class ChatMetrics(Protocol):
    """Spec 08 component metrics (U08-19, U08-20)."""

    def record_counter(
        self, name: str, value: float = 1.0, *, component: str,
        labels: Mapping[str, str] | None = None,
    ) -> None: ...  # fmt: skip

    def record_histogram(
        self, name: str, value: float, *, component: str, labels: Mapping[str, str] | None = None
    ) -> None: ...


class _QueueJobs:
    """The process job queue as `ChatJobs`."""

    enqueue = staticmethod(job_queue.enqueue)
    chat_model_profile = staticmethod(chat_model_profile)


class _UnboundVectors:
    """Fail-closed `VectorHandle` until the composition root binds a ticket index (spec note)."""

    def search_tickets(
        self, vector: Sequence[float], k: int, *, entity: str | None, service_id: str | None
    ) -> list[VectorHit]:
        del vector, k, entity, service_id
        msg = "ticket vector search is not bound in this process"
        raise NotFound(msg)


@dataclass(frozen=True, slots=True, kw_only=True)
class ChatDeps:
    """Collaborators of `ChatService` (U06-127, D06-25, D06-27: no store handle).

    `data_policy_allows_cloud(profile)` is true only under the U06-129 step 3 rule (R-38).
    `ops`, `vectors` and `past_reader` are the `ToolContext` handles and the chat
    `list_findings` backend, which the U06-127 field list does not name (spec note).
    """

    hcfg: HernessConfig
    verifier: AnswerVerifier
    warehouses: WarehouseSource
    tracer_factory: Callable[[str, str], Tracer]
    clock: Callable[[], datetime]
    metrics: ChatMetrics
    jobs: ChatJobs
    current_build: Callable[[], str | None]
    data_policy_allows_cloud: Callable[[str], bool]
    ops: OpsHandle
    vectors: VectorHandle
    past_reader: PastReader

    @classmethod
    def from_config(cls, hcfg: HernessConfig) -> ChatDeps:
        """The process collaborators built from `hcfg` (composition default, U06-136 step 3)."""
        pool = WarehousePool(data_layout(cfg=hcfg).warehouse, hcfg.models.harness.sql)

        def tracer_factory(run_id: str, build_id: str) -> Tracer:
            return Tracer.for_run(run_id, build_id=build_id, run_kind="chat")

        def allows_cloud(profile: str) -> bool:
            return profile == hcfg.profile and cloud_chat_allowed(hcfg)

        return cls(
            hcfg=hcfg, verifier=Verifier(evidence_ops, pool, hcfg.models.harness.verifier),
            warehouses=pool, tracer_factory=tracer_factory, clock=clock.now,
            metrics=process_metrics, jobs=_QueueJobs(), current_build=read_current,
            data_policy_allows_cloud=allows_cloud, ops=evidence_ops, vectors=_UnboundVectors(),
            past_reader=query_verified_findings_recent,
        )  # fmt: skip


class ChatService:
    """Chat entry point used by spec 09 (U06-127, design 06 §3.7)."""

    def __init__(
        self,
        cfg: PipelinesConfig,
        llms: LLMRegistry,
        memory: MemoryStore,
        *,
        deps: ChatDeps | None = None,
    ) -> None:
        self._deps = deps if deps is not None else ChatDeps.from_config(get_config())
        self._env = TurnEnv(cfg=cfg, llms=llms, memory=memory, deps=self._deps)
        self._timeout_s = cfg.pipelines.chat.budget.wall_clock_s + QUEUE_GRACE_S

    def answer(
        self, session_id: str, text: str, user_ref: str, mode: ChatMode
    ) -> Iterator[ChatEvent]:
        """Answer the latest user message of the session (U06-128).

        `text` is accepted for the interface and not used: the model sees the stored,
        redacted message content, so unredacted text never reaches a model or the log.
        """
        del text
        msg = latest_user_message(session_id)
        if msg is None:
            yield ErrorEvent(error_type="NotFound", message="no user message to answer", hint=None)
            return
        yield from self._answer_message(session_id, msg["message_id"], user_ref, mode)

    def _answer_message(
        self, session_id: str, message_id: str, user_ref: str, mode: ChatMode
    ) -> Iterator[ChatEvent]:
        """One chat turn for a known user message (U06-129 steps 1-4)."""
        reply = upsert_assistant_placeholder(
            session_id, reply_to=message_id, now=self._deps.clock()
        )
        msg = get_chat_message(message_id)
        if reply is None or msg is None:
            yield ErrorEvent(
                error_type="NotFound", message="message not found in session", hint=None
            )
            return
        if reply["status"] == "done":
            return  # idempotent re-run of a deferred job
        if mode == "defer":
            yield self._defer(session_id, message_id, reply["message_id"])
            return
        if mode == "cloud" and not self._deps.data_policy_allows_cloud(self._deps.hcfg.profile):
            mode = "small_model"
        yield ModeEvent(mode=mode, message=MODE_MESSAGES[mode])
        events: queue.Queue[ChatEvent | None] = queue.Queue(maxsize=QUEUE_MAX)
        stop = threading.Event()

        def emit(event: ChatEvent | None) -> None:
            while not stop.is_set():
                try:
                    events.put(event, timeout=_PUT_POLL_S)
                except queue.Full:
                    continue
                return

        turn = Turn(
            session_id=session_id, message_id=message_id, user_ref=user_ref, mode=mode,
            question=msg["content"], reply_id=reply["message_id"], emit=emit, stop=stop,
        )  # fmt: skip
        worker = threading.Thread(target=self._work, args=(turn,), name="chat-turn", daemon=True)
        worker.start()
        try:
            yield from self._drain(events, stop)
        finally:
            stop.set()  # a consumer that stopped reading stops the turn

    def _drain(
        self, events: queue.Queue[ChatEvent | None], stop: threading.Event
    ) -> Iterator[ChatEvent]:
        while True:
            try:
                event = events.get(timeout=self._timeout_s)
            except queue.Empty:
                stop.set()
                yield ErrorEvent(
                    error_type="ModelUnavailable", message=_TIMED_OUT, hint=_RETRY_HINT
                )
                return
            if event is None:
                return
            yield event

    def _work(self, turn: Turn) -> None:
        """The worker thread: one event loop for the turn, then the end-of-stream sentinel."""
        try:
            asyncio.run(run_turn(self._env, turn))
        except Exception as exc:  # noqa: BLE001 - a bug must not leave the consumer waiting
            _log.error("harness.chat.turn_crashed", error_type=type(exc).__name__)
            turn.emit(ErrorEvent(error_type="InternalError", message="chat turn failed", hint=None))
            if not turn.done:  # step 6 cleanup: no row, run or task lease left open
                cleanup(self._env, turn, None, crashed=True)
        finally:
            turn.emit(None)

    def _defer(self, session_id: str, message_id: str, reply_id: str) -> ModeEvent:
        """U06-129 step 2: one `chat` job per user message (idempotency key, R-41)."""
        job_id = self._deps.jobs.enqueue(
            "chat", {"session_id": session_id, "message_id": message_id},
            gpu_class="reasoning", priority=None, idem_key=f"chat:{session_id}:{message_id}",
        )  # fmt: skip
        meta: dict[str, object] = {"reply_to": message_id, "mode": "defer", "job_id": job_id}
        update_chat_message(reply_id, status="queued", meta=meta)
        _log.info("harness.chat.turn_deferred", session_id=session_id, job_id=job_id)
        return ModeEvent(mode="defer", message=MODE_MESSAGES["defer"])


def _service_from_config() -> ChatService:
    """`ChatService` from `get_config()` (U06-136 step 3; the composition root binds the
    jobs and ops backends, the tool registry and the chain registry first)."""
    hcfg = get_config()
    llms = LLMRegistry(hcfg.models, profile=hcfg.profile)
    return ChatService(hcfg.pipelines, llms, get_memory_store(), deps=ChatDeps.from_config(hcfg))


def chat_job_handler(ctx: JobContext) -> JobOutcome:
    """Spec 08 `chat` job handler for deferred turns (U06-136).

    A second run of the same job finds the reply row `done` and makes no model call. The
    result reads the reply row: its run id and its verification status (`failed` rows give
    the row status).
    """
    payload = ctx.job.payload
    session_id, message_id = payload.get("session_id"), payload.get("message_id")
    if set(payload) != {"session_id", "message_id"} or not (
        isinstance(session_id, str) and isinstance(message_id, str)
    ):
        msg = "chat job payload needs exactly session_id and message_id"
        raise ConfigError(msg)
    session = get_chat_session(session_id)
    if session is None:
        msg = "chat session not found"
        raise NotFound(msg)
    service = _service_from_config()
    for _event in service._answer_message(session_id, message_id, session["user_ref"], "live"):
        pass
    row = find_assistant_message(session_id, message_id)
    run_id = None if row is None else row["run_id"]
    status = (
        None if row is None else (row["verified"] if row["status"] == "done" else row["status"])
    )
    return JobOutcome(status="done", result={"run_id": run_id, "status": status})
