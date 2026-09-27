"""Unit tests for `herness.harness.hooks` (T05-22: U05-61, U05-62, U05-76).

The spec 08 building blocks (`ModelChain`, `complete_validated`, `loop_signal_policy`,
`save_checkpoint`) are replaced by recording fakes; the hooks look them up at call time.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import BaseModel
from structlog.testing import capture_logs
from tests.support.config_tree import write_full_config
from tests.support.fake_clock import FakeClock
from tests.support.harness_fakes import RecordingTracer

from herness.core import config as c
from herness.core import resilience
from herness.core import time as clock
from herness.core.errors import ConfigError, ModelRefused, OutputValidationError
from herness.core.jobs import tasks as jobs_tasks
from herness.core.types import (
    LLMRequest,
    LLMResponse,
    LoopCheckpoint,
    LoopLimits,
    LoopSignal,
    LoopState,
    Message,
    RequestMeta,
    TextPart,
    ToolCall,
    ToolCallPart,
    ToolResultPart,
    Usage,
)
from herness.harness import hooks as h
from herness.harness.gates import CallGate
from herness.harness.llm.base import Done, TextDelta, ToolCallDelta
from herness.harness.llm.tokens import estimate_tokens

pytestmark = pytest.mark.unit

DASH = chr(0x2013)
QID = "q_0123456789abcdef"


# --- helpers ------------------------------------------------------------------------------


def _req(*, schema: bool = False, client: str = "local") -> LLMRequest:
    return LLMRequest(
        client=client,
        messages=[Message(role="user", parts=[TextPart(text="task")])],
        response_schema={"type": "object"} if schema else None,
        response_schema_name="Answer" if schema else None,
        max_output_tokens=100,
        timeout_s=10.0,
        metadata=RequestMeta(
            run_id="run_1", task_id="t1", role="analyst", model_role="main", step=0,
            request_key="t1:0:step",
        ),
    )  # fmt: skip


def _resp(
    *, text: str = "ok", stop: str = "end_turn", parsed: dict[str, object] | None = None
) -> LLMResponse:
    return LLMResponse.model_validate(
        {
            "text": text,
            "tool_calls": [],
            "parsed": parsed,
            "reasoning": [],
            "stop_reason": stop,
            "raw_stop_reason": stop,
            "refusal_category": "policy" if stop == "refusal" else None,
            "usage": Usage(input_tokens=1, output_tokens=1),
            "cost_usd": Decimal(0),
            "client": "local",
            "model": "m",
            "provider": "openai_compat",
            "latency_ms": 1,
            "request_id": None,
        }
    )


class FakeClient:
    """Non-streaming `LLMClient`: returns `resp`, or raises `error`, recording calls."""

    def __init__(self, name: str = "local", resp: LLMResponse | None = None) -> None:
        self.name = name
        self.resp = resp or _resp()
        self.error: Exception | None = None
        self.calls: list[LLMRequest] = []
        self.log: list[str] | None = None
        self.release: asyncio.Event | None = None

    async def acomplete(self, req: LLMRequest) -> LLMResponse:
        self.calls.append(req)
        if self.log is not None:
            self.log.append("inner")
        if self.release is not None:
            await self.release.wait()
        if self.error is not None:
            raise self.error
        return self.resp

    def complete(self, req: LLMRequest) -> LLMResponse:
        raise NotImplementedError


class StreamClient(FakeClient):
    """Streaming fake: each attempt yields its scripted events; `None` means raise."""

    def __init__(self, attempts: list[list[object]]) -> None:
        super().__init__()
        self.attempts = attempts

    async def astream(self, req: LLMRequest) -> AsyncIterator[object]:
        self.calls.append(req)
        for event in self.attempts.pop(0):
            if event is None:
                msg = "stream broke"
                raise ConnectionError(msg)
            yield event


class RecordingGate:
    """`CallGateLike` recording enter/exit into a shared log."""

    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.held = False

    async def __aenter__(self) -> None:
        self.log.append("enter")
        self.held = True

    async def __aexit__(self, *exc: object) -> None:
        self.log.append("exit")
        self.held = False


class FakeRegistry:
    def __init__(self, clients: dict[str, FakeClient]) -> None:
        self.clients = clients

    def client(self, key: str) -> FakeClient:
        return self.clients[key]


def _hooks(**overrides: object) -> h.HarnessHooks:
    args: dict[str, object] = {
        "registry": FakeRegistry({"local": FakeClient()}),
        "gates": {},
        "chain": None,
        "compactor": None,
        "task_id": "t1",
        "phase": "investigate",
        "stop": None,
        "on_text_delta": None,
        "tracer": RecordingTracer(),
    }
    args.update(overrides)
    return h.HarnessHooks(**args)  # type: ignore[arg-type]


def _asst(call_id: str) -> Message:
    call = ToolCall(id=call_id, name="run_sql", arguments={"sql": "SELECT " + "x" * 200})
    return Message(role="assistant", parts=[ToolCallPart(call=call)])


def _tool(call_id: str) -> Message:
    part = ToolResultPart(tool_call_id=call_id, content="row " * 60)
    return Message(role="tool", parts=[part])


def _state(
    groups: int = 0, *, soft: int = 19_420, hard: int = 23_585, budget: int = 27_744
) -> LoopState:
    limits = LoopLimits(context_budget_tokens=budget, soft_tokens=soft, hard_tokens=hard)
    first = Message(role="user", parts=[TextPart(text="the task")])
    state = LoopState.fresh(first, limits=limits, estimator=estimate_tokens)
    for i in range(1, groups + 1):
        state.messages.append(_asst(f"c{i}"))
        state.messages.append(_tool(f"c{i}"))
        if i == 3:
            state.messages.append(Message(role="user", parts=[TextPart(text="n")], kind="nudge"))
    state.step = groups
    return state


# --- UT05-95..UT05-97: GatedClient ---------------------------------------------------------


@pytest.mark.asyncio
async def test_ut05_95_gate_held_only_during_calls() -> None:
    """UT05-95 gate size 1, two agents, 1 s tool: the second call waits less than the tool."""
    gate = CallGate("local", 1)
    first, second = FakeClient(), FakeClient()
    first.release = asyncio.Event()
    in_tool: list[int] = []

    async def agent(client: FakeClient) -> h.GatedClient:
        g = h.GatedClient(client, gate)
        await g.acomplete(_req())
        in_tool.append(gate.in_flight)
        await asyncio.sleep(1.0)  # the tool runs outside the gate
        return g

    task_a = asyncio.create_task(agent(first))
    await asyncio.sleep(0)
    assert gate.in_flight == 1
    first.release.set()
    await asyncio.sleep(0.05)  # agent A is in its tool now
    task_b = asyncio.create_task(agent(second))
    _, gb = await asyncio.gather(task_a, task_b)
    assert in_tool[0] == 0
    assert gb.last_gate_wait_ms < 1000
    assert gate.max_in_flight == 1
    assert gate.in_flight == 0


@pytest.mark.asyncio
async def test_ut05_95_gate_wait_measured_and_name_mirrors_inner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT05-95 last_gate_wait_ms is the time spent entering the gate; name is inner.name."""
    ticks = iter([10.0, 10.25])
    monkeypatch.setattr(clock, "monotonic", lambda: next(ticks))
    g = h.GatedClient(FakeClient("remote"), None)
    assert g.name == "remote"
    assert g.last_gate_wait_ms == 0
    await g.acomplete(_req())
    assert g.last_gate_wait_ms == 250


@pytest.mark.asyncio
async def test_ut05_96_refusal_raises_after_gate_released() -> None:
    """UT05-96 a refusal response raises ModelRefused after the gate was released."""
    log: list[str] = []
    gate = RecordingGate(log)
    client = FakeClient(resp=_resp(stop="refusal"))
    client.log = log
    with pytest.raises(ModelRefused) as info:
        await h.GatedClient(client, gate).acomplete(_req())
    assert log == ["enter", "inner", "exit"]
    assert info.value.category == "policy"


@pytest.mark.asyncio
async def test_ut05_96_inner_error_propagates_with_gate_released() -> None:
    """UT05-96 an inner client error propagates unchanged and the gate is released."""
    log: list[str] = []
    gate = RecordingGate(log)
    client = FakeClient()
    client.error = TimeoutError("slow")
    with pytest.raises(TimeoutError):
        await h.GatedClient(client, gate).acomplete(_req())
    assert log == ["enter", "exit"]
    assert not gate.held


@pytest.mark.asyncio
async def test_ut05_97_stream_reset_before_retry_text() -> None:
    """UT05-97 streaming fails after text; the retry first sends on_text_delta(None)."""
    deltas: list[str | None] = []

    async def sink(text: str | None) -> None:
        deltas.append(text)

    client = StreamClient(
        [[TextDelta("par"), None], [TextDelta("full"), ToolCallDelta("c", "n", "{"), Done(_resp())]]
    )
    ss = h.StreamState()
    g = h.GatedClient(client, None, on_text_delta=sink, stream_state=ss)
    with pytest.raises(ConnectionError):
        await g.acomplete(_req())
    assert ss.emitted
    resp = await g.acomplete(_req())
    assert resp.text == "ok"
    assert deltas == ["par", None, "full"]
    assert ss.emitted


@pytest.mark.asyncio
async def test_ut05_97_no_stream_without_sink_schema_or_capability() -> None:
    """UT05-97 streaming only with a sink, a StreamCapable inner and no response schema."""
    deltas: list[str | None] = []

    async def sink(text: str | None) -> None:
        deltas.append(text)

    streamer = StreamClient([])
    await h.GatedClient(streamer, None, on_text_delta=sink).acomplete(_req(schema=True))
    await h.GatedClient(streamer, None).acomplete(_req())
    plain = FakeClient()
    await h.GatedClient(plain, None, on_text_delta=sink).acomplete(_req())
    assert len(streamer.calls) == 2
    assert len(plain.calls) == 1
    assert deltas == []


@pytest.mark.asyncio
async def test_ut05_97_stream_without_done_is_output_error() -> None:
    """UT05-97 a stream that ends without Done fails with OutputValidationError."""

    async def sink(text: str | None) -> None:
        return None

    g = h.GatedClient(StreamClient([[TextDelta("x")]]), None, on_text_delta=sink)
    with pytest.raises(OutputValidationError):
        await g.acomplete(_req())


def test_ut05_97_complete_outside_and_inside_event_loop() -> None:
    """UT05-97 complete() runs acomplete outside a loop and refuses inside one (U05-25)."""
    g = h.GatedClient(FakeClient(), None)
    assert g.complete(_req()).text == "ok"

    async def inside() -> None:
        with pytest.raises(RuntimeError):
            g.complete(_req())

    asyncio.run(inside())


# --- UT05-98, UT05-99: HarnessHooks.call ---------------------------------------------------


class FakeChain:
    """Records `acomplete` arguments; asks `client_for` for each key and calls it."""

    def __init__(self, keys: Sequence[str]) -> None:
        self.keys = keys
        self.kwargs: dict[str, object] = {}
        self.made: list[h.GatedClient] = []

    async def acomplete(
        self,
        req: LLMRequest,
        *,
        schema: type[BaseModel] | None = None,
        client_for: Callable[[str], h.GatedClient],
        tracer: object = None,
    ) -> tuple[LLMResponse, BaseModel | None]:
        self.kwargs = {"req": req, "schema": schema, "tracer": tracer}
        resp = _resp()
        for key in self.keys:
            g = client_for(key)
            self.made.append(g)
            resp = await g.acomplete(req)
        return resp, None


@pytest.mark.asyncio
async def test_ut05_98_chain_client_for_wraps_with_right_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT05-98 client_for wraps registry clients in GatedClient with their gate; tracer passed."""
    log_a: list[str] = []
    log_b: list[str] = []
    clients = {"a": FakeClient("a"), "b": FakeClient("b")}
    chain = FakeChain(["a", "b"])
    tracer = RecordingTracer()

    async def sink(text: str | None) -> None:
        return None

    hooks = _hooks(
        registry=FakeRegistry(clients),
        gates={"a": RecordingGate(log_a), "b": RecordingGate(log_b)},
        chain=chain,
        tracer=tracer,
        on_text_delta=sink,
    )
    waits = iter([0.0, 0.004, 1.0, 1.003])
    monkeypatch.setattr(clock, "monotonic", lambda: next(waits))
    state = _state()
    req = _req()
    resp, parsed = await hooks.call(clients["a"], req, state, None)
    assert parsed is None
    assert resp.text == "ok"
    assert chain.kwargs == {"req": req, "schema": None, "tracer": tracer}
    assert [g.name for g in chain.made] == ["a", "b"]
    assert all(isinstance(g, h.GatedClient) for g in chain.made)
    assert log_a == ["enter", "exit"]
    assert log_b == ["enter", "exit"]
    assert clients["a"].calls == [req]
    assert clients["b"].calls == [req]
    assert state.gate_wait_ms() == 7
    assert hooks.tracer is tracer


@pytest.mark.asyncio
async def test_ut05_98_chain_without_gate_and_schema_disables_stream() -> None:
    """UT05-98 a key with no gate runs ungated; a schema call never streams."""
    deltas: list[str | None] = []

    async def sink(text: str | None) -> None:
        deltas.append(text)

    streamer = StreamClient([])
    chain = FakeChain(["s"])
    hooks = _hooks(registry=FakeRegistry({"s": streamer}), chain=chain, on_text_delta=sink)
    await hooks.call(streamer, _req(schema=True), _state(), Answer)
    assert chain.kwargs["schema"] is Answer
    assert deltas == []
    assert streamer.calls


class Answer(BaseModel):
    answer: int


@pytest.mark.asyncio
async def test_ut05_99_no_chain_schema_uses_complete_validated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT05-99 without a chain: complete_validated through the gated client, parsed model."""
    seen: dict[str, object] = {}
    reply = {"parsed": {"answer": 42}}

    async def fake_validated(
        client: h.GatedClient, req: LLMRequest, *, tracer: object = None
    ) -> LLMResponse:
        seen.update(client=client, req=req, tracer=tracer)
        await client.acomplete(req)
        return _resp(parsed=reply["parsed"])

    monkeypatch.setattr(resilience, "complete_validated", fake_validated)
    log: list[str] = []
    inner = FakeClient("local")
    tracer = RecordingTracer()
    hooks = _hooks(gates={"local": RecordingGate(log)}, tracer=tracer)
    req = _req(schema=True)
    resp, parsed = await hooks.call(inner, req, _state(), Answer)
    assert parsed == Answer(answer=42)
    assert resp.parsed == {"answer": 42}
    assert isinstance(seen["client"], h.GatedClient)
    assert seen["tracer"] is tracer
    assert log == ["enter", "exit"]
    reply["parsed"] = {"answer": "not a number"}
    with pytest.raises(OutputValidationError):
        await hooks.call(inner, req, _state(), Answer)
    reply["parsed"] = None
    with pytest.raises(OutputValidationError):
        await hooks.call(inner, req, _state(), Answer)


@pytest.mark.asyncio
async def test_ut05_99_no_chain_no_schema_plain_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT05-99 without chain or schema: one gated call, parsed None, gate wait noted."""
    ticks = iter([5.0, 5.002])
    monkeypatch.setattr(clock, "monotonic", lambda: next(ticks))
    inner = FakeClient("local")
    state = _state()
    resp, parsed = await _hooks().call(inner, _req(), state, None)
    assert (resp.text, parsed) == ("ok", None)
    assert inner.calls
    assert state.gate_wait_ms() == 2


@pytest.mark.asyncio
async def test_ut05_99_before_call_returns_request() -> None:
    """UT05-99 before_call returns the request unchanged."""
    req = _req()
    assert await _hooks().before_call(_state(), req) is req


def test_ut05_99_task_id_and_phase_both_or_neither() -> None:
    """UT05-99 task_id and phase must be both set or both None (U05-62 precondition)."""
    with pytest.raises(ConfigError):
        _hooks(task_id="t1", phase=None)
    with pytest.raises(ConfigError):
        _hooks(task_id=None, phase="p")
    assert _hooks(task_id=None, phase=None).tracer is not None


# --- UT05-100: needs_compaction and on_context_pressure ------------------------------------


@dataclass(frozen=True)
class Pressure:
    tokens: int
    soft: int


class FakeCompactor:
    def __init__(self, pressure: Pressure, *, fail: bool = False) -> None:
        self._pressure = pressure
        self.fail = fail
        self.seen: list[LoopState] = []

    def pressure(self, state: LoopState) -> Pressure:
        self.seen.append(state)
        return self._pressure

    async def on_context_pressure(self, state: LoopState) -> list[Message]:
        if self.fail:
            msg = "summary invalid"
            raise OutputValidationError(msg)
        return [state.messages[0]]


@pytest.mark.asyncio
async def test_ut05_100_needs_compaction_uses_compactor_pressure() -> None:
    """UT05-100 with a compactor: tokens >= pressure().soft."""
    state = _state()
    assert await _hooks(compactor=FakeCompactor(Pressure(100, 100))).needs_compaction(state)
    assert not await _hooks(compactor=FakeCompactor(Pressure(99, 100))).needs_compaction(state)


@pytest.mark.asyncio
async def test_ut05_100_needs_compaction_formula_32k_4k() -> None:
    """UT05-100 without a compactor: est >= soft; 32k/4k gives budget 27,744, soft 19,420."""
    limits = LoopLimits.for_client(
        32_768, None, 4_000, no_progress_steps=4, error_streak=3, fixed_tokens=0
    )
    assert (limits.context_budget_tokens, limits.soft_tokens) == (27_744, 19_420)
    first = Message(role="user", parts=[TextPart(text="t")])
    hooks = _hooks()
    below = LoopState.fresh(first, limits=limits, estimator=lambda _m: 19_419)
    at = LoopState.fresh(first, limits=limits, estimator=lambda _m: 19_420)
    assert not await hooks.needs_compaction(below)
    assert await hooks.needs_compaction(at)


@pytest.mark.asyncio
async def test_ut05_100_context_pressure_compactor_then_fallbacks() -> None:
    """UT05-100 compactor result is returned; its OutputValidationError falls back to
    truncation with a WARNING; without a compactor truncation is used directly."""
    state = _state(2)
    ok = _hooks(compactor=FakeCompactor(Pressure(0, 1)))
    assert await ok.on_context_pressure(state) == [state.messages[0]]
    failing = _hooks(compactor=FakeCompactor(Pressure(0, 1), fail=True))
    with capture_logs() as events:
        fallback = await failing.on_context_pressure(state)
    assert fallback == h.truncate_context(state)
    warn = [e for e in events if e["event"] == "harness.loop.truncation_fallback"]
    assert warn == [
        {
            "event": "harness.loop.truncation_fallback",
            "log_level": "warning",
            "component": "harness.loop",
            "task_id": "t1",
            "phase": "investigate",
            "reason": "compactor_failed",
        }
    ]
    assert await _hooks().on_context_pressure(state) == h.truncate_context(state)


# --- UT05-101: on_loop_signal ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_ut05_101_on_loop_signal_delegates_to_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT05-101 on_loop_signal delegates to loop_signal_policy and returns its answer."""
    calls: list[tuple[object, ...]] = []
    answers = iter(["stop", "nudge"])

    def fake_policy(state: LoopState, signal: LoopSignal, *, tracer: object = None) -> str:
        calls.append((state, signal, tracer))
        return next(answers)

    monkeypatch.setattr(resilience, "loop_signal_policy", fake_policy)
    tracer = RecordingTracer()
    hooks = _hooks(tracer=tracer)
    state = _state()
    signal = LoopSignal(cause="repeat", message="m")
    assert await hooks.on_loop_signal(state, signal) == "stop"
    assert await hooks.on_loop_signal(state, signal) == "nudge"
    assert calls == [(state, signal, tracer), (state, signal, tracer)]


# --- UT05-102: after_step checkpoints ------------------------------------------------------


@pytest.fixture
def herness_cfg(tmp_path: Path) -> object:
    c.reset_config()
    yield c.init_config("local", config_dir=write_full_config(tmp_path), env={})
    c.reset_config()


@pytest.fixture
def saves(monkeypatch: pytest.MonkeyPatch) -> list[tuple[object, ...]]:
    recorded: list[tuple[object, ...]] = []

    def fake_save(task_id: str, key: str, value: object, *, writes: object = None) -> None:
        recorded.append((task_id, key, value))

    monkeypatch.setattr(jobs_tasks, "save_checkpoint", fake_save)
    return recorded


@pytest.fixture
def mono(monkeypatch: pytest.MonkeyPatch, fake_clock: FakeClock) -> FakeClock:
    monkeypatch.setattr(clock, "monotonic", lambda: fake_clock.now().timestamp())
    return fake_clock


@pytest.mark.asyncio
@pytest.mark.usefixtures("herness_cfg")
async def test_ut05_102_after_step_interval_then_stop(
    saves: list[tuple[object, ...]], mono: FakeClock
) -> None:
    """UT05-102 three steps within 5 s save once as save_checkpoint(task_id, "loop",
    <LoopCheckpoint fields>); a stop saves then raises CancelledError."""
    flag = {"stop": False}
    hooks = _hooks(stop=lambda: flag["stop"])
    state = _state(2)
    state.query_ids.append(QID)
    for _ in range(3):
        await hooks.after_step(state)
        mono.advance(1.0)
    assert len(saves) == 1
    task_id, key, value = saves[0]
    assert (task_id, key) == ("t1", "loop")
    assert value == state.to_checkpoint()
    cp = LoopCheckpoint.model_validate(value)
    assert cp.query_ids == [QID]
    assert cp.state == {"step": 2, "nudges": 0, "loop_signals": 0}
    flag["stop"] = True
    with pytest.raises(asyncio.CancelledError):
        await hooks.after_step(state)
    assert len(saves) == 2


@pytest.mark.asyncio
@pytest.mark.usefixtures("herness_cfg")
async def test_ut05_102_after_step_saves_after_interval_and_when_stopping(
    saves: list[tuple[object, ...]], mono: FakeClock
) -> None:
    """UT05-102 a save is due once the interval passed, and always when the state stops."""
    hooks = _hooks()
    state = _state(1)
    await hooks.after_step(state)
    mono.advance(4.9)
    await hooks.after_step(state)
    mono.advance(0.1)
    await hooks.after_step(state)
    assert len(saves) == 2
    state._stopping = True
    await hooks.after_step(state)
    assert len(saves) == 3


@pytest.mark.asyncio
async def test_ut05_102_after_step_without_task_id_never_saves(
    saves: list[tuple[object, ...]],
) -> None:
    """UT05-102 no task_id: nothing is saved; a stop still raises CancelledError."""
    hooks = _hooks(task_id=None, phase=None, stop=lambda: True)
    with pytest.raises(asyncio.CancelledError):
        await hooks.after_step(_state(1))
    assert saves == []


# --- UT05-127: truncate_context ------------------------------------------------------------


def _call_ids(messages: Sequence[Message]) -> tuple[set[str], set[str]]:
    calls = {p.call.id for m in messages for p in m.parts if isinstance(p, ToolCallPart)}
    results = {p.tool_call_id for m in messages for p in m.parts if isinstance(p, ToolResultPart)}
    return calls, results


def test_ut05_127_truncate_drops_oldest_whole_groups() -> None:
    """UT05-127 6 tool groups, ids, small soft limit: first message unchanged, one wrapped
    compaction_summary note with every id, oldest groups dropped whole, input unchanged."""
    state = _state(6, soft=900, hard=1_000, budget=1_000)
    state.query_ids.extend(["q_a1", "q_b2"])
    state.finding_ids.append("f_c3")
    before = list(state.messages)
    snapshot = [m.model_dump() for m in state.messages]
    out = h.truncate_context(state)
    assert state.messages == before
    assert [m.model_dump() for m in state.messages] == snapshot
    assert out[0] is state.messages[0]
    summaries = [m for m in out if m.kind == "compaction_summary"]
    assert summaries == [out[1]]
    assert out[1].role == "user"
    note = out[1].parts[0]
    assert isinstance(note, TextPart)
    assert note.text.startswith('<untrusted_data source="truncation"')
    for ident in ("q_a1", "q_b2", "f_c3"):
        assert ident in note.text
    assert "Earlier steps were removed to fit the context." in note.text
    kept = out[2:]
    assert 0 < len([m for m in kept if m.role == "assistant"]) < 6
    assert kept == state.messages[len(state.messages) - len(kept) :]
    assert kept[0].role == "assistant"
    calls, results = _call_ids(kept)
    assert calls == results
    assert estimate_tokens(out) < 900 or len([m for m in kept if m.role == "assistant"]) == 1
    n_dropped = 6 - len([m for m in kept if m.role == "assistant"])
    assert f"Steps removed: 1{DASH}{n_dropped}" in note.text


def test_ut05_127_truncate_keeps_all_when_under_soft() -> None:
    """UT05-127 nothing dropped under the soft limit; the note still lists the ids."""
    state = _state(2)
    state.query_ids.append("q_1")
    out = h.truncate_context(state)
    assert out[2:] == state.messages[1:]
    note = out[1].parts[0]
    assert isinstance(note, TextPart)
    assert "Steps removed: none" in note.text
    assert "q_1" in note.text


def test_ut05_127_truncate_keeps_last_group_and_replaces_old_summary() -> None:
    """UT05-127 at least the newest group stays; an earlier summary is superseded; a
    leading non-assistant message forms its own group and escapes are applied."""
    state = _state(0, soft=10, hard=20, budget=30)
    old = Message(role="user", parts=[TextPart(text="old")], kind="compaction_summary")
    lead = Message(role="user", parts=[TextPart(text="n")], kind="nudge")
    state.messages.extend([old, lead, _asst("c1"), _tool("c1"), _asst("c2"), _tool("c2")])
    state.step = 2
    state.finding_ids.append("f<1>")
    out = h.truncate_context(state)
    assert [m.kind for m in out].count("compaction_summary") == 1
    assert out[2:] == state.messages[-2:]
    note = out[1].parts[0]
    assert isinstance(note, TextPart)
    assert "f&lt;1&gt;" in note.text
    assert f"Steps removed: 1{DASH}1" in note.text


def test_ut05_127_truncate_only_task_message() -> None:
    """UT05-127 a state holding only the task message gets the task plus the note."""
    state = _state(0)
    out = h.truncate_context(state)
    assert len(out) == 2
    assert out[0] is state.messages[0]
    assert out[1].kind == "compaction_summary"
