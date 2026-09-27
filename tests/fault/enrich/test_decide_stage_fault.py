"""Fault tests for the decide stages (impl 03 FT03-01, FT03-03; T03-21).

Both run the impl 08 fault plan (R-40, `HERNESS_ENV=test`, the `fault_env` fixture):

* FT03-01: `kill_service:openjev` at the second `decider.batch` call. Without
  `HERNESS_STUB_SERVICES` the fault hook calls `process_state().kill_service_hook`, which here
  stops the stub teacher; its `FakeGpu` then reports `openjev` unhealthy, as the worker's GPU
  state would after a real container kill.
* FT03-03: `kill` at the second `enrich.after_batch_write`. The hook's `os.kill` is replaced
  by a `BaseException` that unwinds the stage (no process is killed); the rerun uses a fresh
  decider, as a restarted job would.
"""

from __future__ import annotations

import os
from collections import Counter

import pytest
from tests.support.build_harness import FakeJobContext
from tests.support.fault_env import FaultEnv
from tests.unit.enrich._decide_stage_support import (
    QS,
    QSV,
    DecideEnv,
    FakeGpu,
    Report,
    ScriptedDecider,
    decide_env,
    decisions_cfg,
    incidents,
    warehouse,
)

from herness.core.resilience import ProcessState
from herness.enrich import decide_stage as st
from herness.enrich.cache_maint import compact

pytestmark = pytest.mark.fault

__all__ = ["decide_env"]  # the fixture is used by name


class _Killed(BaseException):
    """The simulated process kill (not an Exception, so no stage code catches it)."""


def _keys(env: DecideEnv) -> Counter[tuple[str, str, str]]:
    return Counter((r["decider"], r["content_hash"], r["question"]) for r in env.rows())


def _night(env: DecideEnv, teacher: ScriptedDecider, llm: ScriptedDecider) -> int:
    """One decide-escalate run plus the reasoning-phase LLM escalation; records answered."""
    cfg = decisions_cfg()
    wh = warehouse(incidents(6))
    deferred = st.run_decide_escalate(
        wh,
        teacher=teacher,  # type: ignore[arg-type]
        qs=QS,
        resolve_args=env.resolve_args(cfg),
        pairs=(),
        cache=env.cache,
        cfg=cfg,
        ctx=FakeJobContext(),  # type: ignore[arg-type]
        report=Report(),
        gpu=FakeGpu(teacher),
    )
    return st.run_llm_escalation(
        deferred,
        llm=llm,  # type: ignore[arg-type]
        qs=QS,
        cache=env.cache,
        cap=20,
        ctx=FakeJobContext(),  # type: ignore[arg-type]
        report=Report(),
    )


def test_ft03_01_openjev_killed_mid_escalation(
    decide_env: DecideEnv,
    fault_env: FaultEnv,
    reset_process_state: ProcessState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FT03-01 kill_service:openjev mid-escalation: the remaining queue goes to the LLM
    decider; a rerun leaves no duplicate cache keys, before and after compaction."""
    monkeypatch.setattr(st, "DECIDE_CHUNK", 2)
    monkeypatch.delenv("HERNESS_STUB_SERVICES", raising=False)
    teacher, llm = ScriptedDecider("openjev"), ScriptedDecider("llm")
    reset_process_state.kill_service_hook = teacher.kill
    fault_env([{"point": "decider.batch", "action": "kill_service:openjev", "nth": 2}])
    assert _night(decide_env, teacher, llm) == 4
    assert not teacher.alive
    assert teacher.calls[0] == ["inc_006", "inc_005"]  # chunk 1 answered before the kill
    assert llm.calls == [["inc_004", "inc_003", "inc_002", "inc_001"]]  # queue order kept
    first = _keys(decide_env)
    assert Counter(d for d, _, _ in first) == {"openjev": 6, "llm": 12}  # 2 and 4 records x 3

    rerun_teacher, rerun_llm = ScriptedDecider("openjev"), ScriptedDecider("llm")
    assert _night(decide_env, rerun_teacher, rerun_llm) == 0
    assert (rerun_teacher.calls, rerun_llm.calls) == ([], [])  # everything already final
    assert _keys(decide_env) == first
    assert max(first.values()) == 1
    compact(decide_env.paths, QSV)
    after = _keys(decide_env)
    assert after == first
    assert max(after.values()) == 1


def test_ft03_03_kill_after_batch_write_during_laya(
    decide_env: DecideEnv, fault_env: FaultEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FT03-03 enrich.after_batch_write -> kill during Laya: the rerun resumes from the last
    flushed part (at most one checkpoint of rework)."""
    monkeypatch.setattr(st, "DECIDE_CHUNK", 7)  # smaller than a checkpoint, as in production

    def killed(_pid: int, _sig: int) -> None:
        raise _Killed

    monkeypatch.setattr(os, "kill", killed)
    fault_env([{"point": "enrich.after_batch_write", "action": "kill", "nth": 2}])
    checkpoint = st.CHECKPOINT_CALLS * 1 * 1  # call_batch 1 (test config) x 1 Laya question
    primaries = {"q_bool": "laya"}
    laya = ScriptedDecider("laya", version="v1")
    with pytest.raises(_Killed):
        st.run_decide_primary(
            warehouse(incidents(100)),
            laya=laya,  # type: ignore[arg-type]
            qs=QS,
            primaries=primaries,
            cache=decide_env.cache,
            ctx=FakeJobContext(),  # type: ignore[arg-type]
            report=Report(),
        )
    assert len(decide_env.rows()) == 2 * checkpoint  # two flushed parts survive the kill

    rerun, report = ScriptedDecider("laya", version="v1"), Report()
    st.run_decide_primary(
        warehouse(incidents(100)),
        laya=rerun,  # type: ignore[arg-type]
        qs=QS,
        primaries=primaries,
        cache=decide_env.cache,
        ctx=FakeJobContext(),  # type: ignore[arg-type]
        report=report,
    )
    keys = _keys(decide_env)
    assert len(keys) == 100
    assert max(keys.values()) == 1
    rework = laya.decided + rerun.decided - 100
    assert 0 <= rework <= checkpoint
    assert rerun.decided == 100 - 2 * checkpoint
