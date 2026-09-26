"""Tests for herness.harness.gates (U06-29..U06-31): CallGate, build_gates, TaskSlots."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from herness.core.errors import ConfigError
from herness.harness.gates import CallGate, TaskSlots, build_gates
from herness.harness.llm.settings import ClientConfig

pytestmark = pytest.mark.unit


def _client(name: str, max_concurrency: int, reserved: int = 0) -> ClientConfig:
    return ClientConfig.model_validate(
        {
            "name": name,
            "kind": "openai_compat",
            "base_url": "http://localhost:8000/v1",
            "model": "m",
            "context_window": 32768,
            "max_output_tokens": 4096,
            "tokenizer": "estimate",
            "max_concurrency": max_concurrency,
            "chat_reserved_slots": reserved,
            "price_per_mtok": {"input": "0", "output": "0", "cache_read": "0", "cache_write": "0"},
        }
    )


# --- UT06-18 -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ut06_18_gate_bounds_in_flight() -> None:
    """UT06-18 gate 2 with 10 coroutines: max_in_flight == 2; on_wait called 10 times."""
    waits: list[tuple[str, float]] = []
    gate = CallGate("local", 2, on_wait=lambda c, s: waits.append((c, s)))

    async def call() -> None:
        async with gate:
            assert 1 <= gate.in_flight <= 2
            await asyncio.sleep(0.01)

    await asyncio.gather(*(call() for _ in range(10)))
    assert gate.max_in_flight == 2
    assert gate.in_flight == 0
    assert gate.size == 2
    assert len(waits) == 10
    assert all(c == "local" and s >= 0.0 for c, s in waits)
    assert max(s for _, s in waits) > 0.0


@pytest.mark.asyncio
async def test_ut06_18_gate_releases_on_exception() -> None:
    """UT06-18 the slot is released when the call body raises; no on_wait is fine."""
    gate = CallGate("local", 1)
    msg = "boom"
    with pytest.raises(RuntimeError):
        async with gate:
            raise RuntimeError(msg)
    assert gate.in_flight == 0
    async with gate:
        assert gate.in_flight == 1
    assert gate.max_in_flight == 1


@pytest.mark.parametrize("size", [0, -3])
def test_ut06_18_gate_size_below_one_is_config_error(size: int) -> None:
    """UT06-18 a gate size below 1 is a ConfigError naming the client."""
    with pytest.raises(ConfigError, match="gate size must be >= 1: client=c1"):
        CallGate("c1", size)


def test_ut06_18_gate_builds_semaphore_per_event_loop() -> None:
    """UT06-18 a gate is usable from a second asyncio.run (lazy semaphore, no loop binding)."""
    gate = CallGate("local", 1)

    async def once() -> None:
        async with gate:
            await asyncio.sleep(0)

    asyncio.run(once())
    asyncio.run(once())
    assert gate.max_in_flight == 1


# --- UT06-19 -----------------------------------------------------------------------------


def test_ut06_19_review_and_chat_sizes() -> None:
    """UT06-19 clients 6 with reserve 2: review 4, chat 2; reserve 0 in chat gives 6."""
    configs = {"b": _client("b", 6, 2), "a": _client("a", 6, 0)}
    review = build_gates(configs, mode="review")
    chat = build_gates(configs, mode="chat")
    assert list(review) == ["a", "b"]
    assert review["b"].size == 4
    assert chat["b"].size == 2
    assert chat["a"].size == 6
    assert review["a"].size == 6


def test_ut06_19_review_size_never_below_one() -> None:
    """UT06-19 review size is max(1, max_concurrency - chat_reserved_slots)."""
    # ClientConfig itself rejects reserve >= max_concurrency; a stand-in reaches the floor.
    odd: Any = SimpleNamespace(max_concurrency=2, chat_reserved_slots=5)
    assert build_gates({"x": odd}, mode="review")["x"].size == 1
    assert build_gates({"x": _client("x", 3, 2)}, mode="review")["x"].size == 1


@pytest.mark.asyncio
async def test_ut06_19_on_wait_is_passed_to_every_gate() -> None:
    """UT06-19 the on_wait callback reaches every built gate."""
    seen: list[str] = []
    gates = build_gates({"x": _client("x", 1)}, mode="chat", on_wait=lambda c, _s: seen.append(c))
    async with gates["x"]:
        pass
    assert seen == ["x"]


def test_ut06_19_missing_fields_default() -> None:
    """UT06-19 missing max_concurrency counts as 1 and missing chat_reserved_slots as 0."""
    bare: Any = SimpleNamespace()
    assert build_gates({"x": bare}, mode="review")["x"].size == 1
    assert build_gates({"x": bare}, mode="chat")["x"].size == 1


# --- UT06-20 -----------------------------------------------------------------------------


def test_ut06_20_for_run_size() -> None:
    """UT06-20 for_run(6, 1.5) has size 9; for_run never goes below 1."""
    assert TaskSlots.for_run(6, 1.5).free() == 9
    assert TaskSlots.for_run(5, 1.1).free() == 6
    assert TaskSlots.for_run(1, 0.1).free() == 1


@pytest.mark.asyncio
async def test_ut06_20_double_release_is_config_error() -> None:
    """UT06-20 release without a matching acquire raises ConfigError("slot released twice")."""
    slots = TaskSlots(2)
    await slots.acquire()
    assert slots.free() == 1
    slots.release()
    assert slots.free() == 2
    with pytest.raises(ConfigError, match="slot released twice"):
        slots.release()
    assert slots.free() == 2


@pytest.mark.asyncio
async def test_ut06_20_acquire_blocks_when_full() -> None:
    """UT06-20 a third acquire on two slots waits until a release."""
    slots = TaskSlots(2)
    await slots.acquire()
    await slots.acquire()
    assert slots.free() == 0
    waiter = asyncio.create_task(slots.acquire())
    await asyncio.sleep(0.01)
    assert not waiter.done()
    slots.release()
    await asyncio.wait_for(waiter, timeout=1)
    assert slots.free() == 0


@pytest.mark.parametrize("size", [0, -1])
def test_ut06_20_size_below_one_is_config_error(size: int) -> None:
    """UT06-20 a slot count below 1 is a ConfigError."""
    with pytest.raises(ConfigError):
        TaskSlots(size)


def test_ut06_20_release_before_any_acquire_is_config_error() -> None:
    """UT06-20 release on a fresh TaskSlots raises without an event loop."""
    with pytest.raises(ConfigError, match="slot released twice"):
        TaskSlots(1).release()
