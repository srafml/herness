"""Security tests ST06-11, ST06-12, ST06-14 for the chat turn (impl 06 T06-25, U06-129).

ST06-11 (TH06-11): a chat loop that never finishes ends within its budget and timeout with an
`error` or a partial answer, and a consumer that stops reading stops the turn. ST06-12
(TH06-12): `cloud` without the chat approval, or under `local`, is answered by the local
reduced model with no off-network call. ST06-14 (TH06-14): at INFO no log line carries the
user's question or the answer. Setup as in `tests/integration/harness/_chat_service_env.py`.
"""

from __future__ import annotations

import asyncio
import queue
import threading
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
import structlog
from tests.integration.harness import _chat_service_env as cs
from tests.integration.harness._chat_service_env import (
    CLOUD,
    LIVE,
    PLANTED,
    SMALL,
    USER_REF,
    Env,
    answer_script,
    call,
    ref,
    types_of,
    with_policy,
)
from tests.support import loop_standin as ls

from herness.core.errors import ModelUnavailable
from herness.core.types import ErrorEvent, FinalEvent, LLMRequest, LLMResponse, VerificationEvent
from herness.harness.pipelines import _chat_turn
from herness.harness.pipelines import chat as chat_mod
from herness.harness.pipelines.chat_support import NO_VERIFIED_ANSWER
from herness.store.ops import list_chat_messages, read_all

pytestmark = pytest.mark.integration

chat_env = cs.chat_env  # fixture


def _short_budget(env: Env, wall_clock_s: int) -> Any:
    chat = env.cfg.pipelines.pipelines.chat
    budget = chat.budget.model_copy(update={"wall_clock_s": wall_clock_s})
    sections = env.cfg.pipelines.pipelines.model_copy(
        update={"chat": chat.model_copy(update={"budget": budget})}
    )
    pipelines = env.cfg.pipelines.model_copy(update={"pipelines": sections})
    return env.cfg.model_copy(update={"pipelines": pipelines})


def _status(env: Env) -> str:
    rows = [r for r in list_chat_messages(env.session_id) if r["role"] == "assistant"]
    assert len(rows) == 1
    return rows[0]["status"]


def _wait_for(check: Callable[[], bool], timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not check():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.02)


class _HangingClient:
    """A client whose call `n` (0-based) waits until released, then fails or answers."""

    def __init__(self, name: str, hang_at: int, after: list[LLMResponse]) -> None:
        self.name = name
        self.hang_at = hang_at
        self.after = after
        self.release = threading.Event()
        self.requests: list[LLMRequest] = []

    async def acomplete(self, req: LLMRequest) -> LLMResponse:
        self.requests.append(req)
        n = len(self.requests) - 1
        if n == self.hang_at:
            while not self.release.is_set():  # noqa: ASYNC110 - a threading.Event set by the test
                await asyncio.sleep(0.01)
            if not self.after:
                msg = "model released without an answer"
                raise ModelUnavailable(msg)
        return self.after[min(n, len(self.after) - 1)]


# --- ST06-11 budget and timeout --------------------------------------------------------------


def test_st06_11_endless_loop_ends_within_budget(chat_env: Env) -> None:
    """ST06-11 a script that calls a tool forever stops at the chat step budget with a partial
    answer; the number of model calls never exceeds the budget's steps."""
    env = chat_env
    steps = env.cfg.pipelines.pipelines.chat.budget.max_steps

    def forever(n: int, req: LLMRequest) -> LLMResponse:
        del req
        return ls.resp(calls=[call("run_sql", f"c{n}", sql=f"SELECT {n}")])

    client = env.script(LIVE, [])
    client.replies = forever
    env.ask()
    events = env.turn("live")
    assert types_of(events)[-3:] == ["token", "verification", "final"]
    (verification,) = [e for e in events if isinstance(e, VerificationEvent)]
    assert verification.status == "partial"
    (fin,) = [e for e in events if isinstance(e, FinalEvent)]
    assert fin.answer.text == NO_VERIFIED_ANSWER
    assert len(client.requests) <= steps
    assert _status(env) == "done"


def test_st06_11_hung_model_times_out(chat_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """ST06-11 a model call that never returns is cut at the wall clock plus grace: one
    `ModelUnavailable` error event, the reply row and the run `failed`."""
    env = chat_env
    # The backstop fires 20 s into the call while the loop's own 120 s wall clock never does;
    # the first call starts only after the (slow, first-time) prompt build.
    monkeypatch.setattr(_chat_turn, "TURN_GRACE_S", -100)
    hang = _HangingClient(LIVE, 0, [])
    env.registry.clients[LIVE] = hang  # type: ignore[assignment]
    env.ask()
    started = time.monotonic()
    try:
        events = list(env.service().answer(env.session_id, "x", USER_REF, "live"))
    finally:
        hang.release.set()
    assert time.monotonic() - started < 60
    assert len(hang.requests) == 1
    assert types_of(events) == ["mode", "error"]
    err = events[-1]
    assert isinstance(err, ErrorEvent)
    assert (err.error_type, err.message) == ("ModelUnavailable", "chat turn timed out")
    assert _status(env) == "failed"
    (run,) = read_all("SELECT status FROM run", ())
    assert run["status"] == "failed"


def test_st06_11_queue_timeout_stops_the_turn(
    chat_env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST06-11 no event within the turn timeout: the generator yields the timeout error and
    sets the stop flag; the worker then marks the reply row failed."""
    env = chat_env
    monkeypatch.setattr(chat_mod, "QUEUE_GRACE_S", 0)
    hcfg = _short_budget(env, 1)
    hang = _HangingClient(LIVE, 0, [])
    env.registry.clients[LIVE] = hang  # type: ignore[assignment]
    env.ask()
    events = list(env.service(hcfg=hcfg).answer(env.session_id, "x", USER_REF, "live"))
    hang.release.set()
    assert events[-1] == ErrorEvent(
        error_type="ModelUnavailable", message="chat turn timed out", hint="Try again later."
    )
    _wait_for(lambda: _status(env) == "failed")


def test_st06_11_consumer_close_stops_the_turn(chat_env: Env) -> None:
    """ST06-11 a consumer that stops reading sets the stop flag: the loop stops at its next
    step, the run is `canceled` and the reply row `failed`; no further model call is made."""
    env = chat_env
    first = ls.resp(calls=[call("run_sql", sql="SELECT 1")])
    hang = _HangingClient(LIVE, 1, [first, ls.resp(calls=[call("run_sql", "c2", sql="x")])])
    env.registry.clients[LIVE] = hang  # type: ignore[assignment]
    env.ask()
    gen = env.service().answer(env.session_id, "x", USER_REF, "live")
    seen = [next(gen).type for _ in range(3)]
    assert seen == ["mode", "tool", "evidence"]
    gen.close()
    hang.release.set()

    def canceled() -> bool:
        rows = read_all("SELECT status FROM run", ())
        return bool(rows) and rows[0]["status"] == "canceled"

    _wait_for(canceled)
    _wait_for(lambda: _status(env) == "failed")
    assert len(hang.requests) == 2


# --- ST06-12 cloud gate ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("profile", "approved", "egress"),
    [("hybrid", False, True), ("local", True, True), ("hybrid", True, False)],
)
def test_st06_12_cloud_without_approval_is_local(
    chat_env: Env, profile: str, approved: bool, egress: bool
) -> None:
    """ST06-12 `cloud` in `hybrid` without `chat_approved`, in `local`, or with egress off:
    answered as `small_model`; the off-network client is never called."""
    env = chat_env
    hcfg = with_policy(env.cfg, profile=profile, approved=approved, egress=egress)
    cloud = env.script(CLOUD, [])
    small = env.script(SMALL, answer_script("Team One had [[n1]] incidents.", [ref("n1", 40)],
                                            client=SMALL))  # fmt: skip
    env.ask()
    events = list(env.service(hcfg=hcfg).answer(env.session_id, "x", USER_REF, "cloud"))
    assert events[0].type == "mode"
    assert events[0].mode == "small_model"  # type: ignore[union-attr]
    assert cloud.requests == []
    assert len(small.requests) == 3
    assert "cloud" not in env.jobs.asked


def test_st06_12_approved_hybrid_keeps_cloud(chat_env: Env) -> None:
    """ST06-12 control: `hybrid` with egress and the chat approval keeps `cloud`, and the
    off-network client gets no ticket-text tool."""
    env = chat_env
    hcfg = with_policy(env.cfg, profile="hybrid", approved=True, egress=True)
    cloud = env.script(CLOUD, answer_script("Team One had [[n1]] incidents.", [ref("n1", 40)],
                                            client=CLOUD))  # fmt: skip
    env.ask()
    events = list(env.service(hcfg=hcfg).answer(env.session_id, "x", USER_REF, "cloud"))
    assert events[0].mode == "cloud"  # type: ignore[union-attr]
    assert len(cloud.requests) == 3
    names = {t.name for t in cloud.requests[0].tools}
    assert names.isdisjoint({"get_record", "get_cluster", "semantic_search"})


# --- ST06-14 no text in logs -----------------------------------------------------------------


def test_st06_14_no_user_or_answer_text_in_logs(chat_env: Env) -> None:
    """ST06-14 a turn at INFO with a planted name in the question and the answer: no log line
    above DEBUG carries the name, the question or the answer text."""
    env = chat_env
    question = f"Why does {PLANTED} handle so many incidents?"
    answer = f"{PLANTED} handled [[n1]] incidents."
    env.script(LIVE, answer_script(answer, [ref("n1", 40)]))
    env.ask(question)
    with structlog.testing.capture_logs() as logs:
        events = env.turn("live")
    assert types_of(events)[-1] == "final"
    visible = [e for e in logs if e.get("log_level") != "debug"]
    assert any(e["event"] == "harness.chat.turn_completed" for e in visible)
    for event in visible:
        line = repr(event)
        assert PLANTED not in line
        assert "handled" not in line
        assert "incidents?" not in line


class _RecordingQueue(queue.Queue[Any]):
    """`queue.Queue` that records its size and every `get` timeout."""

    made: ClassVar[list[int]] = []
    timeouts: ClassVar[list[float | None]] = []

    def __init__(self, maxsize: int = 0) -> None:
        super().__init__(maxsize)
        self.made.append(maxsize)

    def get(self, block: bool = True, timeout: float | None = None) -> Any:
        self.timeouts.append(timeout)
        return super().get(block, timeout)


def test_st06_11_queue_bound_and_turn_timeout(
    chat_env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST06-11 the event hand-off is bounded at 1,000 events and every wait for the next
    event is `chat.budget.wall_clock_s + 30` seconds (U06-129 step 4, §9 constants)."""
    env = chat_env
    assert (chat_mod.QUEUE_MAX, chat_mod.QUEUE_GRACE_S) == (1000, 30)
    fake = SimpleNamespace(Queue=_RecordingQueue, Empty=queue.Empty, Full=queue.Full)
    monkeypatch.setattr(chat_mod, "queue", fake)
    _RecordingQueue.made.clear()
    _RecordingQueue.timeouts.clear()
    env.script(LIVE, answer_script("Team One had [[n1]] incidents.", [ref("n1", 40)]))
    env.ask()
    events = env.turn("live")
    assert types_of(events)[-1] == "final"
    assert _RecordingQueue.made == [1000]
    wall = env.cfg.pipelines.pipelines.chat.budget.wall_clock_s
    assert _RecordingQueue.timeouts
    assert set(_RecordingQueue.timeouts) == {wall + 30}
