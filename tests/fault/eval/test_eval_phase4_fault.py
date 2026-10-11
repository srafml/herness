"""Phase 4 fault cases X3 and X4 (impl 11 §11: FT11-03, FT11-04; T11-36).

Both drive the impl 08 JSON fault plan (R-40, `HERNESS_ENV=test`, the `fault_env` fixture); every
rule is validated by `FaultRule.model_validate` (no copied point list).

* FT11-03 (X3): the real `OpenJevDecider` talks over loopback HTTP to a `StubDeciderServer`
  named `openjev`. The rule `decider.batch kill_service:openjev` is
  scoped to the chain's own call (`model=openjev`), so its 2nd hit is the 2nd chunk: the fault
  hook POSTs `/__control/kill` to the stub URL read from `HERNESS_STUB_SERVICES`. The decide
  stages and the cache are the production ones; the LLM decider is the scripted stand-in of
  FT03-01 (its role is the receiving side of the escalation).
* FT11-04 (X4): `embed.batch kill` in a 5,000-text stage with the production flush window
  (20 batches of 128 = 2,560 texts). `os.kill` is replaced by a `BaseException`, as in FT03-03,
  so the "process" dies without taking pytest with it.

Carry-over: switch the plan writer to `tests.support.faults.write_fault_plan` after T11-33 merges.
"""

from __future__ import annotations

import dataclasses
import json
import os
import socket
from collections import Counter
from datetime import timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from tests.support.build_harness import FakeJobContext
from tests.support.fault_env import FaultEnv
from tests.support.stub_decider import StubDeciderServer, content_hash_of
from tests.unit.enrich._decide_stage_support import (
    NOW,
    QS,
    QSV,
    VERSIONS,
    DecideEnv,
    Report,
    ScriptedDecider,
    decide_env,
    decisions_cfg,
    rid,
    warehouse,
)
from tests.unit.enrich._embed_support import (
    FakeEncoder,
    Rec,
    embed_warehouse,
    stored,
    use_batch_size,
    use_store,
)
from tests.unit.enrich._embed_support import Report as EmbedReport

from herness.core.resilience import ProcessState
from herness.core.resilience.faults import FaultRule
from herness.core.types import GpuClass, ServiceName
from herness.enrich import decide_stage as st
from herness.enrich.cache_maint import compact
from herness.enrich.deciders.openjev import OpenJevDecider
from herness.enrich.embed_stage import FLUSH_BATCHES, run_embed_stage
from herness.enrich.settings import OpenJevSettings

pytestmark = pytest.mark.fault

__all__ = ["decide_env"]  # the fixture is used by name

_RECORDS = 6
_CHUNK = 2
_BATCH = 128
_TEXTS = 5000
_BOUND = FLUSH_BATCHES * _BATCH  # 2,560: the texts of one flush window
_LABELS = {"q_bool": "true", "q_choice": "a", "q_score": "2"}


def _rule(**fields: object) -> dict[str, object]:
    """One plan rule, validated by the impl 08 model (R-40)."""
    return FaultRule.model_validate(fields).model_dump(exclude_none=True)


class _Killed(BaseException):
    """The simulated process kill (not an Exception, so no stage code catches it)."""


def _text(n: int) -> str:
    return f"ticket text of {rid(n)}"


def _write_labels(path: Path) -> None:
    """Oracle labels for the six records (the stub keys them by `sha256(text)[:32]`)."""
    rows = [
        (content_hash_of(_text(n)), q, a)
        for n in range(1, _RECORDS + 1)
        for q, a in _LABELS.items()
    ]
    columns = ["content_hash", "question", "answer"]
    pq.write_table(pa.table({c: [r[i] for r in rows] for i, c in enumerate(columns)}), path)


class _ProbeGpu:
    """`GpuStateReader`: `openjev` is healthy while the stub's listener still accepts."""

    def __init__(self, stub: StubDeciderServer) -> None:
        self._address = stub.server_address

    def loaded_class(self) -> GpuClass:
        return "decider"

    def service_healthy(self, name: ServiceName) -> bool:
        if name != "openjev":
            return False
        try:
            socket.create_connection(self._address, timeout=1).close()
        except OSError:
            return False
        return True


def _register(monkeypatch: pytest.MonkeyPatch, stub: StubDeciderServer) -> None:
    """`HERNESS_STUB_SERVICES` as impl 08 reads it: an inline JSON object name -> URL.

    Spec note (DD11-04, still open): impl 11 U11-44 has `StubHTTPServer.start` merge the
    name into a file named by this variable, which `faults._stub_url` cannot parse (it takes
    the variable itself as JSON), so the stub is registered here instead.
    """
    assert stub.service_name == "openjev"
    monkeypatch.setenv("HERNESS_STUB_SERVICES", json.dumps({stub.service_name: stub.base_url}))


def _keys(env: DecideEnv) -> Counter[tuple[str, str, str]]:
    return Counter((r["decider"], r["content_hash"], r["question"]) for r in env.rows())


def _night(env: DecideEnv, stub: StubDeciderServer, llm: ScriptedDecider) -> int:
    """One decide-escalate run (real OpenJev over the stub) plus the LLM phase."""
    teacher = OpenJevDecider(
        OpenJevSettings(base_url=stub.base_url, concurrency=1),
        api_key=None, image_tag="0.4.0", samples=None,
    )  # fmt: skip
    cfg = decisions_cfg()
    args = dataclasses.replace(
        env.resolve_args(cfg), versions={**VERSIONS, "openjev": teacher.version}
    )
    records = [
        (rid(n), "incident", content_hash_of(_text(n)), NOW - timedelta(hours=200 - n))
        for n in range(1, _RECORDS + 1)
    ]
    deferred = st.run_decide_escalate(
        warehouse(records),
        teacher=teacher,
        qs=QS,
        resolve_args=args,
        pairs=(),
        cache=env.cache,
        cfg=cfg,
        ctx=FakeJobContext(),
        report=Report(),  # type: ignore[arg-type]
        gpu=_ProbeGpu(stub),
    )
    return st.run_llm_escalation(
        deferred,
        llm=llm,  # type: ignore[arg-type]
        qs=QS,
        cache=env.cache,
        cap=20,
        ctx=FakeJobContext(),
        report=Report(),  # type: ignore[arg-type]
    )


def test_ft11_03_stub_openjev_killed_escalation_moves_to_llm(
    decide_env: DecideEnv,
    fault_env: FaultEnv,
    tmp_path: Path,
    reset_process_state: ProcessState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FT11-03 kill_service:openjev on the stub: escalation moves to the LLM decider; the rerun
    adds no duplicate cache rows."""
    del reset_process_state
    monkeypatch.setattr(st, "DECIDE_CHUNK", _CHUNK)
    labels = tmp_path / "truth_labels.parquet"
    _write_labels(labels)
    fault_env([_rule(point="decider.batch", action="kill_service:openjev", nth=2, model="openjev")])
    llm = ScriptedDecider("llm")
    with StubDeciderServer("oracle", labels) as stub:
        _register(monkeypatch, stub)
        assert _night(decide_env, stub, llm) == 4
        assert stub.calls == _CHUNK  # chunk 1 answered over HTTP; chunk 2 hit the killed stub
        assert not _ProbeGpu(stub).service_healthy("openjev")  # killed through /__control/kill
    assert llm.calls == [["inc_004", "inc_003", "inc_002", "inc_001"]]  # queue order kept
    first = _keys(decide_env)
    assert Counter(d for d, _, _ in first) == {"openjev": 6, "llm": 12}  # 2 and 4 records x 3
    assert max(first.values()) == 1

    rerun_llm = ScriptedDecider("llm")
    monkeypatch.delenv("HERNESS_STUB_SERVICES")  # the stub would read it as a file path
    with StubDeciderServer("oracle", labels) as restarted:  # OpenJev is back for the rerun
        _register(monkeypatch, restarted)
        assert _night(decide_env, restarted, rerun_llm) == 0
        assert (restarted.calls, rerun_llm.calls) == (0, [])  # everything is already final
    assert _keys(decide_env) == first
    compact(decide_env.paths, QSV)
    after = _keys(decide_env)
    assert after == first
    assert max(after.values()) == 1


def test_ft11_04_embed_batch_kill_rerun_reembeds_at_most_one_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault_env: FaultEnv
) -> None:
    """FT11-04 embed.batch kill in the 2nd flush window: the rerun re-embeds at most 2,560
    texts and every ticket ends with exactly one vector."""
    store = use_store(tmp_path, monkeypatch)
    use_batch_size(monkeypatch, _BATCH)
    recs = [Rec("incident", f"INC{i:05d}", f"ticket text {i:05d}") for i in range(_TEXTS)]
    wh = embed_warehouse(tmp_path / "wh.duckdb", recs)

    def killed(_pid: int, _sig: int) -> None:
        raise _Killed

    monkeypatch.setattr(os, "kill", killed)
    last_of_window_two = 2 * FLUSH_BATCHES  # the 40th batch: 19 batches of window 2 were lost
    fault_env([_rule(point="embed.batch", action="kill", nth=last_of_window_two)])
    first = FakeEncoder()
    with pytest.raises(_Killed):
        run_embed_stage(wh, encoder=first, ctx=FakeJobContext(), report=EmbedReport())  # type: ignore[arg-type]
    assert len(first.inputs) == (last_of_window_two - 1) * _BATCH
    assert store.count("ticket_embedding") == _BOUND  # only window 1 was flushed

    second, report = FakeEncoder(), EmbedReport()
    run_embed_stage(wh, encoder=second, ctx=FakeJobContext(), report=report)  # type: ignore[arg-type]
    assert len(second.inputs) <= _BOUND  # the Expected column of FT11-04
    assert len(second.inputs) == _TEXTS - _BOUND
    assert report.embedded == len(second.inputs)
    assert store.count("ticket_embedding") == _TEXTS
    assert set(stored(store)) == {rec.record_id for rec in recs}
