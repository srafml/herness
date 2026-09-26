"""Tests for herness.core.resilience.loop_policy: loop_signal_policy (impl 08 U08-40; T08-10)
and the ST08-14 unit-part stub loop (TH08-14)."""

from __future__ import annotations

import json

import pytest
from tests.support.ops_store import OpsStoreHandle
from tests.unit.core.resilience.conftest import RecordingTracer

from herness.core import config as c
from herness.core import redact as r
from herness.core.resilience import ProcessState, process_state
from herness.core.resilience.loop_policy import GUARD_STOPS_METRIC, loop_signal_policy
from herness.core.types import LoopSignal, LoopState, Message, TextPart
from herness.store.ops.core import read_all

pytestmark = pytest.mark.unit


@pytest.fixture
def env(
    ops_db: OpsStoreHandle, herness_cfg: c.HernessConfig, test_redactor: r.Redactor
) -> ProcessState:
    """A migrated ops store bound as the backend and the full test config
    (`resilience.loop.stop_on_signal_no` defaults to 2)."""
    del ops_db, herness_cfg, test_redactor
    return process_state()


def _state(loop_signals: int, nudges: int, step: int = 0) -> LoopState:
    first = Message(role="user", parts=[TextPart(text="hi")])
    return LoopState(messages=[first], loop_signals=loop_signals, nudges=nudges, step=step)


def _events(kind: str) -> list[dict[str, object]]:
    rows = read_all("SELECT * FROM resilience_event WHERE kind = ? ORDER BY ts", (kind,))
    return [dict(row) | {"detail": json.loads(row["detail"])} for row in rows]


def _counter(name: str, **labels: str) -> float:
    key = (name, tuple(sorted(labels.items())), "resilience")
    return process_state().metric_buffer.counters.get(key, 0.0)


def test_ut08_45_first_signal_nudges_without_event(env: ProcessState, recording_tracer) -> None:
    """UT08-45: `LoopState(loop_signals=1, nudges=3)` is below `stop_on_signal_no` (2, R-66's
    nudges are ignored) -> `nudge`, no `guard_stop` row and no trace event."""
    state = _state(loop_signals=1, nudges=3)
    signal = LoopSignal(cause="repeat", message="you repeated a call")

    result = loop_signal_policy(state, signal, tracer=recording_tracer)

    assert result == "nudge"
    assert _events("guard_stop") == []
    assert recording_tracer.of("guard_stop") == []


def test_ut08_45_second_signal_stops_with_guard_stop(env: ProcessState) -> None:
    """UT08-45: `LoopState(loop_signals=2, nudges=0)` reaches `stop_on_signal_no` -> `stop`,
    one `guard_stop` row and trace event carrying the tracer's run_id/task_id, `cause` and
    `step`, plus the guard-stops counter."""
    tracer = RecordingTracer(run_id="run_A", task_id="task_1")
    state = _state(loop_signals=2, nudges=0, step=7)
    signal = LoopSignal(cause="no_progress", message="no new evidence")

    result = loop_signal_policy(state, signal, tracer=tracer)

    assert result == "stop"
    rows = _events("guard_stop")
    assert len(rows) == 1
    assert rows[0]["run_id"] == "run_A"
    assert rows[0]["task_id"] == "task_1"
    assert rows[0]["detail"] == {"cause": "no_progress", "step": 7}
    [fields] = tracer.of("guard_stop")
    assert fields == {"cause": "no_progress", "step": 7}
    assert _counter(GUARD_STOPS_METRIC, cause="no_progress") == 1.0


def test_st08_14_stub_loop_stops_on_second_signal(env: ProcessState) -> None:
    """ST08-14 (unit part, TH08-14): a stub loop stands in for a fake LLM repeating the same
    tool call -- each repeat raises the same `repeat` `LoopSignal` -- and drives
    `loop_signal_policy` once per repeat. The first call nudges; the second stops the task,
    writing one `guard_stop` row and trace event carrying the tracer's run_id/task_id. The
    spec 05 `HarnessHooks` integration half of ST08-14 is a T08-10 carry-over."""
    tracer = RecordingTracer(run_id="run_B", task_id="task_9")
    state = _state(loop_signals=0, nudges=0)
    signal = LoopSignal(cause="repeat", message="you repeated a call")

    outcomes = []
    for _ in range(3):  # the stub loop: the same tool call repeats every iteration
        state.loop_signals += 1
        outcomes.append(loop_signal_policy(state, signal, tracer=tracer))
        if outcomes[-1] == "stop":
            break

    assert outcomes == ["nudge", "stop"]
    rows = _events("guard_stop")
    assert len(rows) == 1
    assert rows[0]["run_id"] == "run_B"
    assert rows[0]["task_id"] == "task_9"
    assert rows[0]["detail"]["cause"] == "repeat"
    [fields] = tracer.of("guard_stop")
    assert fields["cause"] == "repeat"
