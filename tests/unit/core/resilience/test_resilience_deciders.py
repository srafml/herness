"""Tests for herness.core.resilience.deciders: DeciderChain (impl 08 U08-39; T08-10)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from types import SimpleNamespace

import pytest
from tests.support.ops_store import OpsStoreHandle

import herness.core.resilience.deciders as deciders_mod
from herness.core import config as c
from herness.core import redact as r
from herness.core import registry
from herness.core import time as clock
from herness.core.errors import CircuitOpen, ConfigError
from herness.core.resilience import ProcessState, process_state
from herness.core.resilience.deciders import DeciderChain
from herness.core.types import DecisionInput, DecisionOutput, QuestionSet

pytestmark = pytest.mark.unit

QUESTIONS = QuestionSet(version="qs-2026-01-01", questions=())


@pytest.fixture
def env(
    ops_db: OpsStoreHandle, herness_cfg: c.HernessConfig, test_redactor: r.Redactor
) -> ProcessState:
    """A migrated ops store bound as the backend, the full test config (profile local,
    `security.egress.enabled` false)."""
    del ops_db, herness_cfg, test_redactor
    return process_state()


def _item(record_id: str = "r1") -> DecisionInput:
    return DecisionInput(record_id=record_id, entity="incident", content_hash="0" * 32, text="hi")


def _output(decider: str, record_id: str = "r1") -> DecisionOutput:
    return DecisionOutput(
        record_id=record_id,
        content_hash="0" * 32,
        decider=decider,
        decider_version="v1",
        answers={},
    )


@dataclass
class StubDecider:
    """A `DeciderLike` that raises `error` (if set) or returns fixed `outputs`; counts calls."""

    name: str
    outputs: list[DecisionOutput] = field(default_factory=list)
    error: BaseException | None = None
    calls: int = 0

    def decide(
        self, items: Sequence[DecisionInput], questions: QuestionSet
    ) -> list[DecisionOutput]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.outputs

    def health(self) -> None:
        return None


def _resolve_only(deciders: dict[str, StubDecider]):
    """A `resolve` that fails loudly if asked for a decider the test did not expect."""

    def resolve(name: str) -> StubDecider:
        if name not in deciders:
            msg = f"resolve called unexpectedly for {name!r}"
            raise AssertionError(msg)
        return deciders[name]

    return resolve


def test_ut08_43_crash_retried_skip_then_decide(env: ProcessState, fake_gpu_state) -> None:
    """UT08-43: `laya` raises RuntimeError (classified ModelUnavailable, retried 3x per the
    `decider_local` policy), `openjev` is skipped (gpu reasoning loaded, not `decider`), `llm`
    decides -> `([outputs], [])` (impl 03 T03-14 M5: raw crashes never reach the caller)."""
    fake_gpu_state.healthy.add("vllm-reasoning")  # gpu.loaded defaults to "reasoning"
    laya = StubDecider("laya", error=RuntimeError("boom"))
    outputs = [_output("llm")]
    llm = StubDecider("llm", outputs=outputs)
    chain = DeciderChain(
        ["laya", "openjev", "llm"],
        gpu=fake_gpu_state,
        resolve=_resolve_only({"laya": laya, "llm": llm}),
    )

    decided, deferred = chain.decide([_item()], QUESTIONS)

    assert decided == outputs
    assert deferred == []
    assert laya.calls == 3  # decider_local attempts (config/resilience.yaml)
    assert llm.calls == 1


def test_ut08_44_all_entries_skipped_defers_items(env: ProcessState, fake_gpu_state) -> None:
    """UT08-44: `decider` class loaded, `openjev` unhealthy, order `[openjev, llm]` -- both
    entries are skipped (neither's availability rule is met) -> `([], items)`."""
    fake_gpu_state.loaded = "decider"  # `llm` needs "reasoning"; `openjev` stays unhealthy
    items = [_item("r1"), _item("r2")]
    chain = DeciderChain(["openjev", "llm"], gpu=fake_gpu_state, resolve=_resolve_only({}))

    decided, deferred = chain.decide(items, QUESTIONS)

    assert decided == []
    assert deferred == items


def test_ut08_43_empty_order_rejected(fake_gpu_state) -> None:
    """UT08-43 coverage: an empty `order` is rejected at construction (U08-39: `order` is
    documented non-empty)."""
    with pytest.raises(ConfigError):
        DeciderChain([], gpu=fake_gpu_state)


def test_ut08_43_properties_and_default_resolve(env: ProcessState, fake_gpu_state) -> None:
    """UT08-43 coverage: `order`/`gpu` expose the constructor arguments, and the default
    `resolve` looks the decider up in `herness.core.registry` (U08-39 constructor default)."""
    order = ["laya", "llm"]
    chain = DeciderChain(order, gpu=fake_gpu_state)
    assert chain.order == tuple(order)
    assert chain.gpu is fake_gpu_state

    output = _output("laya")

    class _RegisteredLaya:
        def decide(self, items: Sequence[DecisionInput], questions: QuestionSet) -> list[object]:
            return [output]

        def health(self) -> None:
            return None

    registry.register("decider", "laya")(_RegisteredLaya)
    decided, deferred = DeciderChain(["laya"], gpu=fake_gpu_state).decide([_item()], QUESTIONS)
    assert decided == [output]
    assert deferred == []


def test_ut08_43_jev_needs_egress(env: ProcessState, fake_gpu_state, monkeypatch) -> None:
    """UT08-43 coverage: `jev` availability follows `cfg.security.egress.enabled` only,
    independent of the gpu state."""
    output = _output("jev")
    chain = DeciderChain(
        ["jev"], gpu=fake_gpu_state, resolve=_resolve_only({"jev": StubDecider("jev", [output])})
    )

    def cfg(*, enabled: bool) -> SimpleNamespace:
        egress = SimpleNamespace(enabled=enabled)
        return SimpleNamespace(security=SimpleNamespace(egress=egress))

    monkeypatch.setattr(deciders_mod, "get_config", lambda: cfg(enabled=False))
    assert chain.decide([_item()], QUESTIONS) == ([], [_item()])

    monkeypatch.setattr(deciders_mod, "get_config", lambda: cfg(enabled=True))
    decided, deferred = chain.decide([_item()], QUESTIONS)
    assert decided == [output]
    assert deferred == []


def test_ut08_43_other_error_propagates(env: ProcessState, fake_gpu_state) -> None:
    """UT08-43 coverage: a `HernessError` that is neither `ModelUnavailable` nor `CircuitOpen`
    (here `ConfigError`, not retryable) propagates out of `decide` unchanged (U08-39 step 5,
    and step 3's bare `raise` when the decider already raised a `HernessError`)."""
    laya = StubDecider("laya", error=ConfigError("bad decider state"))
    chain = DeciderChain(["laya"], gpu=fake_gpu_state, resolve=_resolve_only({"laya": laya}))

    with pytest.raises(ConfigError):
        chain.decide([_item()], QUESTIONS)
    assert laya.calls == 1  # ConfigError is not retryable


def test_ut08_43_circuit_open_fallback_moves_on(env: ProcessState, fake_gpu_state) -> None:
    """UT08-43 coverage: a decider raising `CircuitOpen` is never retried (the retry wiring
    excludes it) and the chain moves on to the next entry with `reason="circuit_open"`
    (U08-39 step 5)."""
    fake_gpu_state.healthy.add("vllm-reasoning")
    retry_at = clock.now() + timedelta(seconds=30)
    laya = StubDecider("laya", error=CircuitOpen("open", key="decider:laya", retry_at=retry_at))
    outputs = [_output("llm")]
    llm = StubDecider("llm", outputs=outputs)
    chain = DeciderChain(
        ["laya", "llm"], gpu=fake_gpu_state, resolve=_resolve_only({"laya": laya, "llm": llm})
    )

    decided, deferred = chain.decide([_item()], QUESTIONS)

    assert decided == outputs
    assert deferred == []
    assert laya.calls == 1
