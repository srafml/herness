"""Swarm benchmark BT06-03: resume after crash (impl 06 §10, §11.6; T06-28).

Run: pytest -m "fault and slow" tests/bench/test_swarm_resume_bench.py -s
The set-up is FT06-01's (`tests/fault/harness/test_swarm_fault.py`): the T06-22 `drv` run with
every role on the real spec 05 loop over spec 11 `FakeLLMClient` scripts, one task slot, four
analysts (t1 = 2, t2 = 2), a standard funding run killed by the fault plan
`{point: swarm.after_finding_write, action: kill, role: analyst, nth: 2}` (R-40, in-process
`SimulatedKill`), then resumed on a fresh `Swarm` as `herness resume` does. The "job restart"
is that `Swarm.resume` call; the clock runs (`perf_counter`) from the call to the first
successful scheduler `claim_task`. The file's category marker is `fault` (the test plugin
allows one category per file), so it is not in `test_swarm_bench.py`.
"""

from __future__ import annotations

import asyncio
import time
from typing import Final

import pytest
from tests.integration.harness import _swarm_run_env as e
from tests.integration.harness._swarm_run_env import Drv
from tests.support import _f06_swarm as f6
from tests.support.bench_stats import REPEATS, report
from tests.support.fault_env import FaultEnv

from herness.harness.gates import TaskSlots
from herness.harness.swarm import scheduler
from herness.store.ops import core, select_tasks

pytestmark = [pytest.mark.fault, pytest.mark.slow]

drv = e.drv  # fixtures
test_redactor = e.env_mod.test_redactor  # type: ignore[attr-defined]
__all__ = ["drv", "test_redactor"]

BT06_03_MAX_S: Final = 60.0  # scheduling resumes ≤ 60 s after job restart
KILL_RULE: Final = {"point": "swarm.after_finding_write", "action": "kill", "role": "analyst",
                    "nth": 2}  # fmt: skip


def _run_ids() -> set[str]:
    return {str(r["run_id"]) for r in core.read_all("SELECT run_id FROM run", ())}


# --- BT06-03 -----------------------------------------------------------------------------------


@pytest.mark.timeout(600)
def test_bt06_03_resume_schedules_within_60_s_no_done_task_rerun(
    drv: Drv, fault_env: FaultEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BT06-03 FT06-01 set-up: from the fresh `Swarm.resume` call (the job restart) to the
    first scheduler claim ≤ 60 s; zero `done` tasks executed again; the run reaches done."""
    scripts = f6.use_real_agents(drv, monkeypatch)
    f6.install_kill(monkeypatch)
    monkeypatch.setattr(TaskSlots, "for_run", classmethod(lambda cls, *_a: cls(1)))
    drv.set_pipeline(t1=2, t2=2)
    claims: list[float] = []
    claim = scheduler.claim_task

    def timed_claim(task_id: str) -> bool:
        ok = claim(task_id)
        if ok:
            claims.append(time.perf_counter())
        return ok

    monkeypatch.setattr(scheduler, "claim_task", timed_claim)
    resumed: list[float] = []
    for _ in range(REPEATS):
        known = _run_ids()
        fault_env([KILL_RULE])
        f6.run_killed(lambda: drv.swarm().start(drv.request(depth="standard")))
        (run_id,) = _run_ids() - known
        analysts = select_tasks(run_id, roles={"analyst"})
        done = {t.task_id for t in select_tasks(run_id, statuses={"done"})}
        assert (len(analysts), sum(t.status == "running" for t in analysts)) == (4, 1)
        fault_env([])
        claims.clear()
        start = time.perf_counter()
        res = asyncio.run(drv.swarm().resume(run_id))
        assert res.status == "done", res
        assert claims, "no task was claimed after the restart"
        resumed.append(claims[0] - start)
        rerun = {t: n for t in done if (n := scripts.executions[t]) != 1}
        assert rerun == {}, f"done tasks executed again: {rerun}"
    median = report("BT06-03", "resume_to_claim", resumed, "s", f"<= {BT06_03_MAX_S:g} s")
    assert median <= BT06_03_MAX_S, f"scheduling resumed after {median:.3f} s"
