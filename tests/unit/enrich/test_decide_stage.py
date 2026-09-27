"""Tests for the decide stages (impl 03 U03-84 ... U03-87; T03-21).

UT03-79 `build_inputs`, UT03-80 `run_decide_primary`, UT03-81 `run_decide_escalate` (real
`resolve_frame`, `escalation_queue` and `DeciderChain` over a migrated ops store) and UT03-82
`run_llm_escalation`. Deciders are `ScriptedDecider` fakes; the job context is the impl 11
`FakeJobContext`.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from structlog.testing import capture_logs
from tests.support.build_harness import FakeJobContext
from tests.unit.enrich._decide_stage_support import (
    NOW,
    QS,
    QSV,
    DecideEnv,
    FakeGpu,
    Report,
    ScriptedDecider,
    decide_env,
    decisions_cfg,
    digest,
    incidents,
    rid,
    warehouse,
)

from herness.core.errors import (
    AuthError,
    ConfigError,
    EgressBlocked,
    FatalError,
    ModelUnavailable,
    SchemaViolation,
)
from herness.core.types import DecisionInput
from herness.enrich import decide_stage as st
from herness.enrich.cache import DecisionCache
from herness.enrich.gpu import YieldRequested
from herness.enrich.resolve import QueueItem

pytestmark = pytest.mark.unit

__all__ = ["decide_env"]  # the fixture is used by name

_ALL = frozenset({"q_bool", "q_choice", "q_score", "change_caused_pair"})
_LAYA_PRIMARY = {
    "q_bool": "laya",
    "q_choice": "openjev",
    "q_score": "laya",
    "change_caused_pair": "openjev",
}


def _asked(chunks: list[list[DecisionInput]]) -> dict[str, tuple[str, ...] | None]:
    return {item.record_id: item.question_ids for chunk in chunks for item in chunk}


def _registered(wh: Any) -> set[str]:
    return {row[0] for row in wh.execute("SELECT view_name FROM duckdb_views()").fetchall()}


# --- UT03-79 build_inputs ---


def test_ut03_79_only_missing_questions_asked_chunk_size_respected(decide_env: DecideEnv) -> None:
    """UT03-79 cache with some keys: only missing questions asked; chunk size respected."""
    records = incidents(5)
    records.append(("chg_001", "change", digest(99), NOW))
    wh = warehouse(records)
    decide_env.write_rows(
        "laya",
        "v1",
        [
            (digest(1), "q_bool", "true", 0.9),  # cached: not asked again
            (digest(2), "q_bool", "true", 0.9),
            (digest(2), "q_choice", "a", 0.9),  # fully cached
        ],
    )
    decide_env.write_rows("laya", "v0", [(digest(3), "q_bool", "true", 0.9)])  # other version
    chunks = list(
        st.build_inputs(
            wh,
            decider="laya",
            version="v1",
            qs=QS,
            question_ids=frozenset({"q_bool", "q_choice", "change_caused_pair"}),
            cache=decide_env.cache,
            chunk=2,
        )
    )
    assert [len(chunk) for chunk in chunks] == [2, 2, 1]
    assert _asked(chunks) == {
        rid(1): ("q_choice",),
        rid(3): ("q_bool", "q_choice"),
        rid(4): ("q_bool", "q_choice"),
        rid(5): ("q_bool", "q_choice"),
        "chg_001": ("q_choice",),  # q_bool does not apply to changes
    }
    hashes = [item.content_hash for chunk in chunks for item in chunk]
    assert hashes == sorted(hashes)  # equal texts adjacent
    assert all(item.text == f"ticket text of {item.record_id}" for c in chunks for item in c)
    assert not {"_bi_have", "_bi_questions"} & _registered(wh)


def test_ut03_79_stale_fingerprint_is_missing(decide_env: DecideEnv) -> None:
    """UT03-79 a row with another fingerprint does not count as cached; pairs never asked."""
    wh = warehouse(incidents(1))
    decide_env.write_rows("laya", "v1", [(digest(1), "q_bool", "true", 0.9)])
    stale = QS.model_copy(
        update={"questions": (QS.get("q_bool").model_copy(update={"fingerprint": "f" * 16}),)}
    )
    chunks = list(
        st.build_inputs(
            wh, decider="laya", version="v1", qs=stale, question_ids=_ALL, cache=decide_env.cache
        )
    )
    assert _asked(chunks) == {rid(1): ("q_bool",)}
    only_pair = st.build_inputs(
        wh,
        decider="laya",
        version="v1",
        qs=QS,
        question_ids=frozenset({"change_caused_pair"}),
        cache=decide_env.cache,
    )
    assert list(only_pair) == []


def test_ut03_79_bootstrap_filter_and_errors(tmp_path: Any) -> None:
    """UT03-79 bootstrap keeps records opened since `since`; bad arguments and SQL errors."""
    cache = DecisionCache(_paths(tmp_path), QSV)
    wh = warehouse(incidents(4))  # opened NOW - 199 ... NOW - 196 hours
    since = NOW - timedelta(hours=197)
    chunks = list(
        st.build_inputs(
            wh,
            decider="laya",
            version="v1",
            qs=QS,
            question_ids=_ALL,
            cache=cache,
            record_filter_sql="bootstrap",
            since=since,
        )
    )
    assert sorted(_asked(chunks)) == [rid(3), rid(4)]
    with pytest.raises(ConfigError):
        next(
            st.build_inputs(
                wh,
                decider="laya",
                version="v1",
                qs=QS,
                question_ids=_ALL,
                cache=cache,
                record_filter_sql="bootstrap",
            )
        )
    with pytest.raises(ConfigError):
        next(
            st.build_inputs(
                wh, decider="laya", version="v1", qs=QS, question_ids=_ALL, cache=cache, chunk=0
            )
        )
    wh.execute("DROP TABLE core.problem")
    with pytest.raises(SchemaViolation, match=r"^build_inputs: CatalogException$"):
        next(
            st.build_inputs(
                wh,
                decider="laya",
                version="v1",
                qs=QS,
                question_ids=_ALL,
                cache=cache,
                record_filter_sql="bootstrap",
                since=since,
            )
        )
    assert not {"_bi_have", "_bi_questions"} & _registered(wh)


def _paths(tmp_path: Any) -> Any:
    from herness.enrich.layout import EnrichPaths  # noqa: PLC0415

    return EnrichPaths(data_root=tmp_path, embedding_path="data/e", laya_current_file="data/c")


# --- UT03-80 run_decide_primary ---


def _primary(
    env: DecideEnv,
    laya: ScriptedDecider | None,
    ctx: FakeJobContext,
    report: Report,
    primaries: dict[str, str] = _LAYA_PRIMARY,
) -> None:
    wh = warehouse(incidents(5))
    st.run_decide_primary(
        wh,
        laya=laya,
        qs=QS,
        primaries=primaries,  # type: ignore[arg-type]
        cache=env.cache,
        ctx=ctx,
        report=report,
    )  # type: ignore[arg-type]


def test_ut03_80_laya_degraded_is_skipped(decide_env: DecideEnv) -> None:
    """UT03-80 Laya degraded (None) -> skipped; no Laya-primary question -> skipped."""
    report = Report()
    _primary(decide_env, None, FakeJobContext(), report)
    assert (report.status, report.note) == ("skipped", "laya_degraded")
    laya, report = ScriptedDecider("laya", version="v1"), Report()
    _primary(decide_env, laya, FakeJobContext(), report, {"q_bool": "openjev"})
    assert (report.status, report.note, laya.loaded, laya.calls) == ("skipped", "no_work", 0, [])
    assert decide_env.rows() == []


def test_ut03_80_yield_after_chunk_one_after_a_flush(
    decide_env: DecideEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-80 fake Laya with yield after chunk 1: YieldRequested after a flush; rerun resumes."""
    monkeypatch.setattr(st, "DECIDE_CHUNK", 2)
    laya, ctx, report = (
        ScriptedDecider("laya", version="v1"),
        FakeJobContext(yield_after=0),
        Report(),
    )
    with pytest.raises(YieldRequested) as info:
        _primary(decide_env, laya, ctx, report)
    assert info.value.stage == "decide-primary"
    assert laya.calls == [[rid(1), rid(2)]]
    assert (laya.loaded, laya.unloaded, ctx.heartbeats) == (1, 1, ["decide-primary"])
    rows = decide_env.rows()
    assert len(rows) == 4  # 2 records x 2 questions
    assert {r["decider"] for r in rows} == {"laya"}
    again, report = ScriptedDecider("laya", version="v1"), Report()
    _primary(decide_env, again, FakeJobContext(), report)
    assert again.calls == [[rid(3), rid(4)], [rid(5)]]
    assert (report.status, report.decided, len(decide_env.rows())) == ("done", 3, 10)


def test_ut03_80_timeout_marks_degraded_and_errors_counted(
    decide_env: DecideEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-80 item errors counted; ModelUnavailable (timeout) -> degraded, earlier rows kept."""
    monkeypatch.setattr(st, "DECIDE_CHUNK", 2)

    def fail(items: Any) -> BaseException | None:
        return ModelUnavailable("laya timeout") if items[0].record_id == rid(3) else None

    laya = ScriptedDecider("laya", version="v1", fail=fail, item_errors={rid(2): 1})
    report = Report()
    with capture_logs() as logs:
        _primary(decide_env, laya, FakeJobContext(), report)
    assert (report.status, report.note, report.decided, report.failed) == (
        "degraded",
        "laya_degraded",
        1,
        1,
    )
    assert laya.unloaded == 1
    assert {r["content_hash"] for r in decide_env.rows()} == {digest(1)}
    degraded = [e for e in logs if e["event"] == "enrich.stage.degraded"]
    assert degraded == [
        {
            "component": "enrich.decide",
            "event": "enrich.stage.degraded",
            "stage": "decide-primary",
            "error_class": "ModelUnavailable",
            "log_level": "warning",
        }
    ]


def test_ut03_80_fatal_propagates_and_unloads(decide_env: DecideEnv) -> None:
    """UT03-80 FatalError (OOM at batch 1) fails the stage; Laya is still unloaded."""
    laya = ScriptedDecider("laya", version="v1", fail=lambda _items: FatalError("cuda oom"))
    with pytest.raises(FatalError):
        _primary(decide_env, laya, FakeJobContext(), Report())
    assert laya.unloaded == 1


# --- UT03-81 run_decide_escalate ---


def _escalate(  # noqa: PLR0913 - test helper mirrors the stage keywords
    env: DecideEnv,
    teacher: ScriptedDecider | None,
    *,
    cap: int = 100,
    records: int = 5,
    pairs: tuple[DecisionInput, ...] = (),
    ctx: FakeJobContext | None = None,
    report: Report | None = None,
    primaries: dict[str, str] | None = None,
) -> tuple[list[QueueItem], Report]:
    cfg = decisions_cfg(max_rows_per_night=cap)
    report = report or Report()
    wh = warehouse(incidents(records))
    out = st.run_decide_escalate(
        wh,
        teacher=teacher,
        qs=QS,
        resolve_args=env.resolve_args(cfg, primaries),  # type: ignore[arg-type]
        pairs=pairs,
        cache=env.cache,
        cfg=cfg,
        ctx=ctx or FakeJobContext(),  # type: ignore[arg-type]
        report=report,
        gpu=FakeGpu(teacher),  # type: ignore[arg-type]
    )
    return out, report


def _pair(n: int) -> DecisionInput:
    return DecisionInput(
        record_id=f"pair_{n}",
        entity="incident",
        content_hash=digest(500 + n),
        text=f"pair text {n}",
        question_ids=("change_caused_pair",),
    )


def test_ut03_81_unavailable_chunk_two_cap_three(
    decide_env: DecideEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-81 teacher ModelUnavailable on chunk 2, cap 3: chunk 1 cached, rest deferred,
    cap honoured, `enrich.decide.escalation_capped` logged."""
    monkeypatch.setattr(st, "DECIDE_CHUNK", 2)

    def fail(items: Any) -> BaseException | None:
        return ModelUnavailable("openjev 503") if items[0].record_id == rid(3) else None

    teacher = ScriptedDecider("openjev", fail=fail)
    with capture_logs() as logs:
        deferred, report = _escalate(decide_env, teacher, cap=3)
    # queue order: newest first -> inc_005, inc_004 (chunk 1), inc_003 (chunk 2, lost)
    assert teacher.calls[0] == [rid(5), rid(4)]
    assert {tuple(c) for c in teacher.calls[1:]} == {(rid(3),)}  # retried by the chain policy
    assert [(d.record_id, d.question_ids) for d in deferred] == [
        (rid(3), ("q_bool", "q_choice", "q_score"))
    ]
    assert {r["content_hash"] for r in decide_env.rows()} == {digest(5), digest(4)}
    assert (report.escalated, report.failed, report.status, report.note) == (
        2,
        0,
        "degraded",
        "openjev_unavailable",
    )
    capped = [e for e in logs if e["event"] == "enrich.decide.escalation_capped"]
    assert capped == [
        {
            "component": "enrich.decide",
            "event": "enrich.decide.escalation_capped",
            "queued": 3,
            "cap": 3,
            "log_level": "info",
        }
    ]
    assert all("ticket text" not in str(e) for e in logs)


def test_ut03_81_no_teacher_defers_queue_and_pairs(decide_env: DecideEnv) -> None:
    """UT03-81 teacher None: the queue plus the (capped) pairs come back deferred."""
    deferred, report = _escalate(decide_env, None, records=2, pairs=(_pair(1), _pair(2), _pair(3)))
    assert [d.record_id for d in deferred] == [rid(2), rid(1), "pair_1", "pair_2"]
    assert deferred[-1].question_ids == ("change_caused_pair",)
    assert (report.status, report.note) == ("skipped", "teacher_unavailable")
    assert decide_env.rows() == []


def test_ut03_81_pairs_sent_after_records_and_item_errors_retried_once(
    decide_env: DecideEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-81 pairs follow the queue; an item error is retried once in the next chunk, a
    second error defers the item (records first, then pairs); yield checked per chunk."""
    monkeypatch.setattr(st, "DECIDE_CHUNK", 3)
    teacher = ScriptedDecider("openjev", item_errors={rid(2): 1, "pair_1": 2})
    ctx = FakeJobContext()
    deferred, report = _escalate(decide_env, teacher, records=2, pairs=(_pair(1),), ctx=ctx)
    assert teacher.calls == [[rid(2), rid(1), "pair_1"], [rid(2), "pair_1"]]
    assert [d.record_id for d in deferred] == ["pair_1"]
    assert (report.escalated, report.failed, report.status) == (2, 1, "done")
    assert ctx.heartbeats == ["decide-escalate", "decide-escalate"]
    assert {r["content_hash"] for r in decide_env.rows()} == {digest(1), digest(2)}


def test_ut03_81_below_gate_teacher_rows_go_to_llm(
    decide_env: DecideEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-81 a queued question the teacher already answered below the gate is not re-sent;
    it is merged with the record's lost questions in the deferred item."""
    decide_env.write_rows("openjev", "v1", [(digest(1), "q_bool", "true", 0.55)])
    teacher = ScriptedDecider("openjev", fail=lambda _items: ModelUnavailable("down"))
    deferred, report = _escalate(decide_env, teacher, records=1)
    assert teacher.calls[0] == [rid(1)]
    assert [(d.record_id, d.question_ids) for d in deferred] == [
        (rid(1), ("q_bool", "q_choice", "q_score"))
    ]
    teacher = ScriptedDecider("openjev")
    deferred, _ = _escalate(decide_env, teacher, records=1)
    assert [(d.record_id, d.question_ids) for d in deferred] == [(rid(1), ("q_bool",))]
    assert len(teacher.calls) == 1
    assert report.escalated == 0


@pytest.mark.parametrize(
    ("error", "note", "event"),
    [
        (AuthError("401"), "openjev_auth", "enrich.decider.auth_failed"),
        (EgressBlocked("guard"), "openjev_blocked", "enrich.decider.egress_blocked"),
    ],
)
def test_ut03_81_auth_or_egress_stops_sending(
    decide_env: DecideEnv,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    note: str,
    event: str,
) -> None:
    """UT03-81 AuthError / EgressBlocked: sending stops, everything left is deferred (ERROR)."""
    monkeypatch.setattr(st, "DECIDE_CHUNK", 2)

    def fail(items: Any) -> BaseException | None:
        return error if items[0].record_id == rid(3) else None

    teacher = ScriptedDecider("openjev", fail=fail)
    with capture_logs() as logs:
        deferred, report = _escalate(decide_env, teacher, pairs=(_pair(1),))
    assert len(teacher.calls) == 2
    assert [d.record_id for d in deferred] == [rid(3), rid(2), rid(1), "pair_1"]
    assert (report.status, report.note, report.escalated) == ("degraded", note, 2)
    assert [e["log_level"] for e in logs if e["event"] == event] == ["error"]


def test_ut03_81_yield_flushes_then_raises(
    decide_env: DecideEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-81 a yield request after a chunk flushes the answers, then raises."""
    monkeypatch.setattr(st, "DECIDE_CHUNK", 2)
    teacher = ScriptedDecider("openjev")
    with pytest.raises(YieldRequested) as info:
        _escalate(decide_env, teacher, ctx=FakeJobContext(yield_after=0))
    assert info.value.stage == "decide-escalate"
    assert {r["content_hash"] for r in decide_env.rows()} == {digest(5), digest(4)}


# --- UT03-82 run_llm_escalation ---


def _items(count: int) -> list[QueueItem]:
    return [
        QueueItem(rid(n), "incident", digest(n), f"text {n}", ("q_bool", "q_score"))
        for n in range(1, count + 1)
    ]


def _llm(
    env: DecideEnv,
    deferred: list[QueueItem],
    llm: ScriptedDecider | None,
    *,
    cap: int,
    ctx: FakeJobContext | None = None,
    report: Report | None = None,
) -> int:
    return st.run_llm_escalation(
        deferred,
        llm=llm,
        qs=QS,
        cache=env.cache,
        cap=cap,  # type: ignore[arg-type]
        ctx=ctx or FakeJobContext(),
        report=report or Report(),
    )  # type: ignore[arg-type]


def test_ut03_82_cap_twenty_of_thirty_order_kept(decide_env: DecideEnv) -> None:
    """UT03-82 30 deferred, cap 20, fake LLM: 20 answered, order kept."""
    llm, report = ScriptedDecider("llm"), Report()
    assert _llm(decide_env, _items(30), llm, cap=20, report=report) == 20
    assert llm.calls == [[rid(n) for n in range(1, 21)]]
    rows = decide_env.rows()
    assert len(rows) == 40
    assert {r["samples"] for r in rows} == {3}
    assert {r["decider"] for r in rows} == {"llm"}
    assert report.decided == 20


def test_ut03_82_unavailable_stops_and_none_is_degraded(
    decide_env: DecideEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-82 no LLM -> 0; ModelUnavailable on chunk 2 stops, the rest stay cache misses."""
    assert _llm(decide_env, _items(3), None, cap=20) == 0
    monkeypatch.setattr(st, "LLM_CHUNK", 5)

    def fail(items: Any) -> BaseException | None:
        return ModelUnavailable("vllm down") if items[0].record_id == rid(6) else None

    llm, report = ScriptedDecider("llm", fail=fail), Report()
    with capture_logs() as logs:
        assert _llm(decide_env, _items(12), llm, cap=20, report=report) == 5
    assert len(llm.calls) == 2
    assert (report.status, report.note) == ("degraded", "llm_unavailable")
    warn = [e for e in logs if e["event"] == "enrich.decider.unavailable"]
    assert warn[0]["left"] == 7
    assert warn[0]["log_level"] == "warning"
    assert len(decide_env.rows()) == 10


def test_ut03_82_yield_after_chunk(decide_env: DecideEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT03-82 a yield request after chunk 1 flushes its answers, then raises."""
    monkeypatch.setattr(st, "LLM_CHUNK", 5)
    with pytest.raises(YieldRequested) as info:
        _llm(
            decide_env,
            _items(12),
            ScriptedDecider("llm"),
            cap=20,
            ctx=FakeJobContext(yield_after=0),
        )
    assert info.value.stage == "reasoning"
    assert len(decide_env.rows()) == 10
