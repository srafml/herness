"""Swarm benchmarks BT06-01 … BT06-10 (impl 06 §10, §11.6; T06-28).

Run here (FakeLLMClient scripts, no model):
    pytest -m "integration and slow" tests/bench/test_swarm_bench.py -s -k "bt06_01 or bt06_02"
    pytest -m "fault and slow" tests/bench/test_swarm_resume_bench.py -s
Owed on the target PC (each row skips unless `HERNESS_BENCH_FULL=1` and the row's service
variables are set; the herness config of the process names the 5M build and the profile):
    HERNESS_BENCH_FULL=1 HERNESS_SYNTH_FULL_ROOT=<seed 42 full root> HERNESS_BENCH_GPU=1
    HERNESS_BENCH_LLM_PROFILE=local [HERNESS_BENCH_PREMIUM=1]
    pytest -m slow tests/bench/test_swarm_bench.py -s -k "bt06_04 or ... or bt06_10"

BT06-01 runs the standard `funding_review` of the T06-22 `drv` set-up (real driver, steps,
scheduler, Blackboard and ops store; every role on the real spec 05 loop over spec 11
`FakeLLMClient` scripts, `tests.support._f06_swarm`), not `small_build`'s warehouse: the
synthetic builds carry no `score.*` layer, which the funding plan context and the Writer's
recommendation read (owner T02-22 / T11-17; T06-26 deviation 1). The run has the task count
of a standard funding run on `small_build` (`M_must` 10 must-cover + `K_candidates` 25), and
every model call waits 120 ms (12 s x0.01). BT06-03 (marker `fault`, a file category of its
own) is in `test_swarm_resume_bench.py`.
"""

from __future__ import annotations

import asyncio
import os
import statistics
import subprocess
import sys
import threading
import time
from typing import Any, Final

import pytest
from tests.bench import _swarm_bench_env as sb
from tests.integration.harness import _swarm_run_env as e
from tests.integration.harness._swarm_run_env import Drv
from tests.support import _f06_swarm as f6
from tests.support.bench_stats import REPEATS, report
from tests.unit.harness import _blackboard_env as bbe

from herness.core import config as c
from herness.core import time as clock
from herness.core.types import ErrorEvent, FinalEvent
from herness.harness.llm.registry import LLMRegistry
from herness.harness.memory import MemoryStore
from herness.harness.pipelines.chat import ChatService
from herness.harness.swarm import handler
from herness.harness.swarm.lifecycle import RunRequest, RunResult
from herness.harness.verifier import Verifier
from herness.store.ops import core, select_tasks
from herness.store.ops.chat import append_chat_message, create_chat_session

pytestmark = [pytest.mark.integration, pytest.mark.slow]

drv = e.drv  # fixtures
test_redactor = e.env_mod.test_redactor  # type: ignore[attr-defined]
bb_env = bbe.bb_env
__all__ = ["bb_env", "drv", "test_redactor"]

MIN: Final = 60.0
BT06_01_MAX_PCT: Final = 2.0  # scheduler + blackboard time < 2 % of wall time
BT06_01_T1: Final = 10  # must-cover analyst tasks (standard M_must)
BT06_01_T2: Final = 25  # candidate analyst tasks (standard K_candidates)
BT06_02_POSTS: Final = 1_000
BT06_02_TASKS: Final = 6
BT06_02_P95_MS: Final = 50.0  # p95 < 50 ms
BT06_04_P50_S: Final = 30.0  # p50 ≤ 30 s
BT06_04_P95_S: Final = 90.0  # p95 ≤ 90 s
BT06_04_VERIFY_S: Final = 5.0  # `verify_answer` ≤ 5 s
BT06_05_RANGE_S: Final = (30 * MIN, 90 * MIN)  # 30-90 min end to end
BT06_06_MAX_S: Final = 20 * MIN  # ≤ 20 min
BT06_07_RANGE_S: Final = (6 * 60 * MIN, 12 * 60 * MIN)  # 6-12 h
BT06_08_PREMIUM_S: Final = (5 * MIN, 15 * MIN)  # premium 5-15 min
BT06_08_HYBRID_EXTRA_S: Final = 10 * MIN  # hybrid ≤ local + 10 min
BT06_08_HYBRID_USD: Final = 15  # and ≤ $15
BT06_09_MIN_UTIL_PCT: Final = 70.0  # ≥ 70 % average
BT06_10_MAX_S: Final = 5 * MIN  # ≤ 5 min
CHAT_QUESTIONS: Final = (
    "Which team had the most incidents last quarter?",
    "What is the incident trend of the payments service?",
    "Which funding candidate has the highest expected impact?",
    "How many changes failed last month?",
)
CHAT_TURNS: Final = 20


def _run_ids() -> set[str]:
    return {str(r["run_id"]) for r in core.read_all("SELECT run_id FROM run", ())}


# --- BT06-01 -----------------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="BT06-01 measured 6.68 % and 8.02 % (dev medians, loaded host) vs < 2 % of wall;"
    " owner T06-08 (Blackboard post) / T06-14 (scheduler pass)",
)
@pytest.mark.timeout(600)
def test_bt06_01_scheduler_and_blackboard_under_2_percent_of_wall(
    drv: Drv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BT06-01 standard funding run, fake LLM 12 s x0.01 per call: scheduler + blackboard
    time (union of the timed intervals) < 2 % of the run's wall time (median of REPEATS)."""
    scripts = f6.use_real_agents(drv, monkeypatch)
    sb.use_slow_clients(drv.llms, scripts, monkeypatch)
    drv.set_pipeline(t1=BT06_01_T1, t2=BT06_01_T2)
    over = sb.Overhead()
    over.install(monkeypatch)
    share, upper, walls = [], [], []
    for i in range(REPEATS + 1):  # run 0 is the warm-up
        over.reset()
        start = time.perf_counter()
        res = asyncio.run(drv.swarm().start(drv.request(depth="standard")))
        wall = time.perf_counter() - start
        assert res.status == "done", res
        assert len(select_tasks(res.run_id, roles={"analyst"})) >= BT06_01_T1 + BT06_01_T2
        if i:
            share.append(100 * over.union_s() / wall)
            upper.append(100 * over.sum_s() / wall)
            walls.append(wall)
            parts = {t: f"{100 * over.union_s(t) / wall:.2f}" for t in sb.TAGS}
            sys.stderr.write(f"BT06-01 repeat {i} share by part (%): {parts}\n")
    report("BT06-01", "wall", walls, "s", "info")
    report("BT06-01", "sum_share", upper, "%", "info (upper bound)")
    median = report("BT06-01", "share", share, "%", f"< {BT06_01_MAX_PCT:g} %")
    assert median < BT06_01_MAX_PCT, f"scheduler + blackboard {median:.3f} % of wall"


# --- BT06-02 -----------------------------------------------------------------------------------


def _post_all(env: bbe.BbEnv, offset: int) -> list[float]:
    """1,000 posts from 6 concurrent tasks (one analyst task each); per-post seconds."""
    task_ids = [bbe.add_running_task(env.run_id, n=offset + k) for k in range(BT06_02_TASKS)]
    shares = [len(range(k, BT06_02_POSTS, BT06_02_TASKS)) for k in range(BT06_02_TASKS)]
    seconds: list[float] = []

    def post(task_id: str, i: int) -> None:
        number = {"id": "n1", "value": i, "unit": "count", "query_id": env.ops_qid,
                  "column": "incidents", "row_key": None}  # fmt: skip
        start = time.perf_counter()
        env.bb.post(env.ctx(task_id=task_id), **bbe.args(env.ops_qid, numbers=[number]))
        seconds.append(time.perf_counter() - start)

    async def poster(task_id: str, n: int) -> None:
        for i in range(n):
            await asyncio.to_thread(post, task_id, i)

    async def main() -> None:
        await asyncio.gather(*(poster(t, n) for t, n in zip(task_ids, shares, strict=True)))

    asyncio.run(main())
    return seconds


@pytest.mark.timeout(600)
def test_bt06_02_blackboard_post_p95_under_50_ms(bb_env: bbe.BbEnv) -> None:
    """BT06-02 1,000 `post_finding` posts from 6 concurrent tasks into a tmp SQLite WAL ops
    store through the real Blackboard (one writer thread): p95 per-post latency < 50 ms."""
    row = core.read_one("PRAGMA journal_mode", ())
    assert row is not None
    assert str(row["journal_mode"]).lower() == "wal"
    p95s: list[float] = []
    for rep in range(REPEATS):
        seconds = _post_all(bb_env, offset=100 * (rep + 1))
        assert len(seconds) == BT06_02_POSTS
        p95s.append(1000 * statistics.quantiles(seconds, n=100)[94])
    count = core.read_one("SELECT count(*) AS n FROM finding WHERE run_id = ?", (bb_env.run_id,))
    assert count is not None
    assert count["n"] == REPEATS * BT06_02_POSTS
    median = report("BT06-02", "p95_post", p95s, "ms", f"< {BT06_02_P95_MS:g} ms")
    assert median < BT06_02_P95_MS, f"blackboard post p95 {median:.2f} ms"


# --- owed on the target PC: BT06-04 … BT06-10 -------------------------------------------------


def _review(kind: str, depth: str, profile: str, **kw: Any) -> tuple[float, RunResult]:
    """One review on the process config (5M build, real models) via the composition root."""
    swarm = handler.build_swarm_from_config(c.get_config())
    try:
        req = RunRequest.model_validate({"kind": kind, "depth": depth, "profile": profile} | kw)
        start = time.perf_counter()
        res = asyncio.run(swarm.start(req))
        seconds = time.perf_counter() - start
    finally:
        swarm.close()
    assert res.status == "done", res
    sys.stderr.write(f"{kind} {depth} {profile}: {seconds:.1f} s, ${res.cost_usd}\n")
    return seconds, res


def _profile() -> str:
    return os.environ[sb.PROFILE_ENV]


@sb.full_gate(sb.PROFILE_ENV, sb.SYNTH_ENV)
@pytest.mark.gpu
def test_bt06_04_chat_answer_local_p50_p95_and_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    """BT06-04 chat answers on real `local-30b`, synthetic 5M: p50 ≤ 30 s, p95 ≤ 90 s;
    `verify_answer` ≤ 5 s (slowest call)."""
    cfg = c.get_config()
    svc = ChatService(cfg.pipelines, LLMRegistry(cfg.models, profile=cfg.profile),
                      MemoryStore.from_config(cfg))  # fmt: skip
    verify_s: list[float] = []
    verify = Verifier.verify_answer

    def timed(self: Verifier, *args: Any, **kwargs: Any) -> Any:
        start = time.perf_counter()
        try:
            return verify(self, *args, **kwargs)
        finally:
            verify_s.append(time.perf_counter() - start)

    monkeypatch.setattr(Verifier, "verify_answer", timed)
    turns: list[float] = []
    for i in range(CHAT_TURNS):
        question = CHAT_QUESTIONS[i % len(CHAT_QUESTIONS)]
        session = create_chat_session("bench", now=clock.now())
        assert append_chat_message(session, "user", question, now=clock.now()) is not None
        start = time.perf_counter()
        events = list(svc.answer(session, question, "bench", "live"))
        turns.append(time.perf_counter() - start)
        assert not any(isinstance(ev, ErrorEvent) for ev in events), events
        assert any(isinstance(ev, FinalEvent) for ev in events)
    p50, p95 = statistics.median(turns), statistics.quantiles(turns, n=100)[94]
    sys.stderr.write(f"BT06-04 p50 {p50:.1f} s p95 {p95:.1f} s verify max {max(verify_s):.2f} s\n")
    assert p50 <= BT06_04_P50_S
    assert p95 <= BT06_04_P95_S
    assert max(verify_s) <= BT06_04_VERIFY_S


@sb.full_gate(sb.PROFILE_ENV, sb.GPU_ENV, sb.SYNTH_ENV)
@pytest.mark.gpu
def test_bt06_05_standard_review_local_30_to_90_min() -> None:
    """BT06-05 standard review local, synthetic 5M, one 24 GB GPU: 30-90 min end to end."""
    seconds, _ = _review("funding_review", "standard", _profile())
    assert BT06_05_RANGE_S[0] <= seconds <= BT06_05_RANGE_S[1], f"{seconds / MIN:.1f} min"


@sb.full_gate(sb.PROFILE_ENV, sb.GPU_ENV, sb.SYNTH_ENV)
@pytest.mark.gpu
def test_bt06_06_fast_review_local_under_20_min() -> None:
    """BT06-06 fast review local, same set-up: ≤ 20 min."""
    seconds, _ = _review("funding_review", "fast", _profile())
    assert seconds <= BT06_06_MAX_S, f"{seconds / MIN:.1f} min"


@sb.full_gate(sb.PROFILE_ENV, sb.GPU_ENV, sb.SYNTH_ENV)
@pytest.mark.gpu
def test_bt06_07_deep_review_local_with_large_stage_6_to_12_h() -> None:
    """BT06-07 deep review local incl. the large stage (manual, overnight): 6-12 h."""
    seconds, _ = _review("funding_review", "deep", _profile())
    assert BT06_07_RANGE_S[0] <= seconds <= BT06_07_RANGE_S[1], f"{seconds / 3600:.2f} h"


@sb.full_gate(sb.PREMIUM_ENV, sb.PROFILE_ENV, sb.SYNTH_ENV)
def test_bt06_08_premium_5_to_15_min_hybrid_within_local_plus_10_min_and_15_usd() -> None:
    """BT06-08 synthetic 5M, approved synth policy (manual): premium standard 5-15 min;
    hybrid standard ≤ local + 10 min and ≤ $15."""
    premium_s, _ = _review("funding_review", "standard", "premium")
    local_s, _ = _review("funding_review", "standard", _profile())
    hybrid_s, hybrid = _review("funding_review", "standard", "hybrid")
    assert BT06_08_PREMIUM_S[0] <= premium_s <= BT06_08_PREMIUM_S[1], f"{premium_s / MIN:.1f}"
    assert hybrid_s <= local_s + BT06_08_HYBRID_EXTRA_S, f"{hybrid_s:.0f} vs {local_s:.0f} s"
    assert hybrid.cost_usd <= BT06_08_HYBRID_USD, f"${hybrid.cost_usd}"


def _sample_gpu(stop: threading.Event, out: list[tuple[str | None, float]]) -> None:
    """`nvidia-smi` utilization every 1 s, tagged with the newest run's status."""
    argv = ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"]
    while not stop.wait(1.0):
        done = subprocess.run(argv, capture_output=True, text=True, timeout=10, check=True)  # noqa: S603 - fixed argv
        row = core.read_one("SELECT status FROM run ORDER BY started_at DESC LIMIT 1", ())
        status = None if row is None else str(row["status"])
        out.append((status, float(done.stdout.splitlines()[0])))


@sb.full_gate(sb.PROFILE_ENV, sb.GPU_ENV, sb.SYNTH_ENV)
@pytest.mark.gpu
def test_bt06_09_gpu_utilization_during_fan_out_at_least_70_percent() -> None:
    """BT06-09 the BT06-05 run with `nvidia-smi` sampled every 1 s: ≥ 70 % average GPU
    utilization over the fan-out (samples while the run is `running`)."""
    samples: list[tuple[str | None, float]] = []
    stop = threading.Event()
    sampler = threading.Thread(target=_sample_gpu, args=(stop, samples), daemon=True)
    sampler.start()
    try:
        _review("funding_review", "standard", _profile())
    finally:
        stop.set()
        sampler.join(timeout=15)
    fan_out = [util for status, util in samples if status == "running"]
    assert fan_out, "no sample during the fan-out"
    average = statistics.fmean(fan_out)
    sys.stderr.write(f"BT06-09 average utilization {average:.1f} % over {len(fan_out)} s\n")
    assert average >= BT06_09_MIN_UTIL_PCT


@sb.full_gate(sb.PROFILE_ENV, sb.GPU_ENV, sb.SYNTH_ENV)
@pytest.mark.gpu
def test_bt06_10_chat_mini_swarm_under_5_min() -> None:
    """BT06-10 chat mini swarm, synthetic 5M, local: ≤ 5 min. The run is the escalation
    request of U06-134 (`fast`, the question, `pipelines.chat.escalation` budget override)."""
    escalation = dict(c.get_config().pipelines.pipelines.chat.escalation)
    seconds, _ = _review("funding_review", "fast", _profile(), question=CHAT_QUESTIONS[2],
                         budget_override=escalation)  # fmt: skip
    assert seconds <= BT06_10_MAX_S, f"{seconds / MIN:.1f} min"
