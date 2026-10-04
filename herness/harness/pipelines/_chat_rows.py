"""Turn state and the run, task and reply-row writes of one chat turn (impl 06 U06-129).

Private sibling of `herness.harness.pipelines.chat` (T06-25 split, with `_chat_turn`): the
per-turn `Turn` and `TurnEnv`, the plain-text marker rendering (R-16, O06-15), step 5a (the
`chat` run and task), steps 5i-5j (reply row, task and run `done`) and step 6 (the `error`
event, reply `failed`, run ended). No user or answer text is logged (TH06-14).
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Final, Literal

from herness.core.errors import FatalError, HernessError, RetryableError
from herness.core.ids import new_ulid
from herness.core.jobs.tasks import claim_task, complete_task, fail_task
from herness.core.logging import get_logger
from herness.core.numbers import format_number, parse_markers
from herness.core.types import (
    ChatAnswer,
    ChatEvent,
    ChatMode,
    EntityScope,
    ErrorEvent,
    NumberRef,
    TaskBudget,
    TaskSpec,
)  # fmt: skip
from herness.harness.findings import compute_dedup_key
from herness.harness.pipelines.chat_support import CHAT_TOOLS
from herness.harness.swarm.lifecycle import RunRequest, request_config_hash
from herness.store.ops import (
    RunRow,
    insert_run,
    insert_tasks,
    run_write,
    set_run_status,
    update_run_fields,
)
from herness.store.ops.chat import update_chat_message

if TYPE_CHECKING:
    import sqlite3

    from herness.harness.blackboard import Blackboard
    from herness.harness.budget import RunBudget
    from herness.harness.llm.registry import LLMRegistry
    from herness.harness.memory import MemoryStore
    from herness.harness.pipelines.chat import ChatDeps
    from herness.harness.pipelines.settings import PipelinesConfig
    from herness.harness.tracing import Tracer

__all__ = [
    "Turn",
    "TurnEnv",
    "TurnStatus",
    "cleanup",
    "create_run",
    "finish",
    "model_role",
    "render",
]

type TurnStatus = Literal["verified", "partial", "unverified"]

REDACTED: Final = "<redacted>"
_OBJECTIVE: Final = "Answer the chat question."
_CONTENT_MAX: Final = 20_000  # chat_message content cap (impl 09)
_LIVE_RUN: Final = frozenset({"created", "running"})

_log = get_logger("harness.chat")


@dataclass(frozen=True, slots=True)
class TurnEnv:
    """The service collaborators every turn uses."""

    cfg: PipelinesConfig
    llms: LLMRegistry
    memory: MemoryStore
    deps: ChatDeps


@dataclass(slots=True, kw_only=True)
class Turn:
    """One turn: its ids, the stored question, the event sink and the per-turn state."""

    session_id: str
    message_id: str
    user_ref: str
    mode: ChatMode
    question: str
    reply_id: str
    emit: Callable[[ChatEvent | None], None]
    stop: threading.Event
    run_mode: ChatMode = "live"
    run_id: str = ""
    task_id: str = ""
    build_id: str = ""
    claimed: bool = False
    client_key: str = ""
    notice: bool = False
    tracer: Tracer | None = None
    bb: Blackboard | None = None
    escalation: tuple[str, str] | None = None
    lock: asyncio.Lock | None = None
    seen: set[str] = field(default_factory=set)


def render(text: str, numbers: Sequence[NumberRef]) -> str:
    """`text` with each valid marker naming one of `numbers` formatted (R-16, O06-15)."""
    refs = {n.id: n for n in numbers}
    for marker in reversed(parse_markers(text).markers):
        if (ref := refs.get(marker.id)) is not None:
            text = text[: marker.start] + format_number(ref) + text[marker.end :]
    return text


def create_run(env: TurnEnv, t: Turn, budget: TaskBudget) -> None:
    """Step 5a: the `chat` run and its one task in one write, then the task lease."""
    hcfg, now = env.deps.hcfg, env.deps.clock()
    t.run_id, t.task_id = "run_" + new_ulid(), "task_" + new_ulid()
    request = RunRequest(kind="chat", depth="fast", question=REDACTED)
    meta: dict[str, object] = {
        "request": {"kind": "chat", "question": REDACTED},
        "session_id": t.session_id, "user_ref": t.user_ref,
    }  # fmt: skip
    run = RunRow(
        run_id=t.run_id, kind="chat", depth="fast", profile=hcfg.profile, build_id=t.build_id,
        status="running", started_at=now, finished_at=None, token_usage={}, cost_usd=Decimal(0),
        config_hash=request_config_hash(hcfg, request, hcfg.profile), meta=meta,
    )  # fmt: skip
    scope = EntityScope(entity_type="run", entity_ids=[t.run_id])
    spec = TaskSpec(
        task_id=t.task_id, run_id=t.run_id, role="chat", objective=_OBJECTIVE, scope=scope,
        tools=list(CHAT_TOOLS[t.mode]), budget=budget, model_role=model_role(t.mode),
        dedup_key=compute_dedup_key("chat", "general", scope, _OBJECTIVE),
    )  # fmt: skip

    def write(conn: sqlite3.Connection) -> None:
        insert_run(conn, run)
        insert_tasks(conn, [spec], now=now)

    run_write(write, op="chat_create_run")
    t.claimed = claim_task(t.task_id)
    t.tracer = env.deps.tracer_factory(t.run_id, t.build_id)


def model_role(mode: ChatMode) -> str:
    return "chat_off_hours" if mode == "small_model" else "chat"


def finish(
    env: TurnEnv,
    t: Turn,
    answer: ChatAnswer,
    status: TurnStatus,
    latency_ms: int,
    ledger: RunBudget,
) -> None:
    """Steps 5i-5j: the reply row `done`, then the task `done` with the run `done`."""
    meta: dict[str, object] = {
        "reply_to": t.message_id, "mode": t.mode, "model": t.client_key,
        "latency_ms": latency_ms, "numbers": [n.model_dump(mode="json") for n in answer.numbers],
    }  # fmt: skip
    update_chat_message(
        t.reply_id, status="done", content=render(answer.text, answer.numbers)[:_CONTENT_MAX],
        verified=status, run_id=t.run_id, query_ids=list(answer.query_ids), meta=meta,
    )  # fmt: skip
    now = env.deps.clock()

    def writes(conn: sqlite3.Connection) -> None:
        usage = {"chat": ledger.snapshot()}
        update_run_fields(conn, t.run_id, token_usage=usage, cost_usd=ledger.cost_used)
        set_run_status(conn, t.run_id, "done", _LIVE_RUN, now=now)

    result: dict[str, object] = {
        "summary": "", "status": status, "partial": status != "verified",
        "query_ids": list(answer.query_ids), "model": t.client_key,
    }  # fmt: skip
    complete_task(t.task_id, result, writes=writes)


def cleanup(env: TurnEnv, t: Turn, exc: HernessError | None) -> None:
    """Step 6: `error` event (none when the consumer stopped), reply `failed`, run ended."""
    if exc is not None:
        hint = exc.hint or ("Try again later." if isinstance(exc, RetryableError) else None)
        t.emit(ErrorEvent(error_type=type(exc).__name__, message=str(exc), hint=hint))
        _log.error("harness.chat.turn_failed", run_id=t.run_id or None,
                   error_type=type(exc).__name__)  # fmt: skip
    else:
        _log.info("harness.chat.turn_stopped", run_id=t.run_id or None)
    try:
        update_chat_message(t.reply_id, status="failed")
        if t.run_id:
            to, now = ("failed" if exc is not None else "canceled"), env.deps.clock()
            run_write(lambda conn: set_run_status(conn, t.run_id, to, _LIVE_RUN, now=now),
                      op="chat_end_run")  # fmt: skip
        if t.claimed:
            err = exc if exc is not None else FatalError("chat turn stopped by its consumer")
            fail_task(t.task_id, err, max_task_attempts=1)
    except HernessError as err2:
        _log.warning("harness.chat.cleanup_failed", run_id=t.run_id or None,
                     error_type=type(err2).__name__)  # fmt: skip
