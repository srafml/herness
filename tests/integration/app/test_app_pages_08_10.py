"""IT09-10, IT09-21, IT09-22, IT09-26, FT09-01, ST09-07, ST09-09: pages 08 - 10 and their actions.

Recommendations, Review Queue and Runs & Traces run as AppTests on a migrated ops store with the
jobs backends bound. Every audit writer (``auth`` in the page gate and the actions,
``review_decision`` in impl 02, ``recommendation_decision`` and ``admin_action`` in the actions)
records into one list. ``services.memory`` is the recording ``FakeMemoryStore`` (impl 09 §11);
IT09-21 swaps in impl 07's real ``MemoryLifecycle`` for ``approve`` / ``reject`` so a
``memory_write`` item is decided by impl 07 in its own transaction (R-33). The CLI halves of
IT09-21 and IT09-22 belong to the CLI cards.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest
from tests.support.app_env import (
    APP_DIR,
    USER_REF_KEY,
    install_session,
    page_services,
    preserved_main,
    ui_config,
)
from tests.support.fake_memory import FakeMemoryStore
from tests.support.ops_store import OpsStoreHandle
from tests.unit.harness.memory._lifecycle_env import make_lifecycle, review_of, row_of, seed_item

from app.common import wh
from herness.core.errors import PermissionDenied, StoreBusy
from herness.core.ids import new_ulid
from herness.core.resilience import ProcessState
from herness.reports import _actions_audit, actions, rules
from herness.reports.rules import user_ref_for
from herness.store.ops import bind_core_backends, core, shared

pytestmark = pytest.mark.integration

TIMEOUT_S = 60
PAGES = APP_DIR / "pages"
RECS = PAGES / "08_Recommendations.py"
QUEUE = PAGES / "09_Review_Queue.py"
RUNS = PAGES / "10_Runs_and_Traces.py"
ADMIN, REVIEWER, VIEWER = "root", "alice", "victor"
STAMP = "2026-10-01T12:00:00.000000Z"
QID = "q_9c106c191cd857d0"
REASON = "Budget approved by the steering group."
TEXT_KINDS = ("title", "subheader", "markdown", "caption", "text", "info", "warning", "success")
QSV = "qs_2026_10"


def ref(name: str) -> str:
    return user_ref_for(name, USER_REF_KEY)


@pytest.fixture(autouse=True)
def _isolated() -> Iterator[None]:
    st.cache_data.clear()
    wh._pool.clear()
    with preserved_main():
        yield
    st.cache_data.clear()
    wh._pool.clear()


@pytest.fixture
def env(
    ops_store: OpsStoreHandle,
    reset_process_state: ProcessState,
    monkeypatch: pytest.MonkeyPatch,
) -> SimpleNamespace:
    """Ops store + jobs backends, services with a recording memory, one audit list."""
    del reset_process_state
    bind_core_backends()
    ui = ui_config(admins=[ADMIN], reviewers=[REVIEWER])
    services = page_services(ops_store.data_root, ui=ui)
    services.memory = FakeMemoryStore()
    services.cfg.decisions = SimpleNamespace(
        question_set_version=QSV,
        questions=(SimpleNamespace(id="root_cause", type="choice",
                                   options={"capacity": "Capacity", "code": "Code change"}),),
    )  # fmt: skip
    audits = install_session(monkeypatch, services, user=REVIEWER)

    def record(event: str, actor: str, **fields: Any) -> None:
        audits.append({"event": event, "actor": actor, **fields})

    for module in (shared, actions, _actions_audit, rules):
        monkeypatch.setattr(module, "audit", record)

    def login(user: str) -> None:
        install_session(monkeypatch, services, user=user)
        from app.common import auth  # noqa: PLC0415 - patched by install_session above

        monkeypatch.setattr(auth, "audit", record)

    login(REVIEWER)
    return SimpleNamespace(
        services=services, audits=audits, login=login, data_root=ops_store.data_root,
        db=ops_store.db_path, memory=services.memory,
    )  # fmt: skip


def _run(path: Path) -> AppTest:
    at = AppTest.from_file(str(path), default_timeout=TIMEOUT_S).run()
    assert not at.exception, [e.value for e in at.exception]
    assert not at.error, [e.value for e in at.error]
    return at


def _texts(at: AppTest) -> list[str]:
    return [str(e.value) for kind in TEXT_KINDS for e in getattr(at.main, kind)]


def _json(at: AppTest) -> list[object]:
    return [json.loads(str(j.value)) for j in at.main.json]


def _events(env: SimpleNamespace, name: str) -> list[dict[str, Any]]:
    return [a for a in env.audits if a["event"] == name]


def _write(sql: str, *rows: tuple[object, ...]) -> None:
    def fn(conn: sqlite3.Connection) -> None:
        conn.executemany(sql, rows)

    core.run_write(fn, op="test_seed")


# --- seeds ---------------------------------------------------------------------------------------

REC_A, REC_B = "rec_" + new_ulid(), "rec_" + new_ulid()
SUMMARY = "Fund the payments initiative [[n1]] <b>now</b>"


def _seed_recs() -> None:
    """REC_A (fund, undecided) and REC_B (org_action, accepted by the reviewer, one outcome)."""
    number = {"id": "n1", "value": 3, "unit": "count", "query_id": QID, "column": "n",
              "row_key": None}  # fmt: skip
    _write(
        "INSERT INTO recommendation (rec_id, run_id, target_type, target_id, summary, kind,"
        " numbers, confidence, confidence_basis, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (REC_A, "run_x", "candidate", "W1", SUMMARY, "fund", json.dumps([number]), 0.7,
         json.dumps({"prior": 0.5}), STAMP),
        (REC_B, "run_x", "team", "T1", "Move on-call to the platform team", "org_action", "[]",
         None, "{}", STAMP),
    )  # fmt: skip
    _write(
        "INSERT INTO decision_log (rec_id, decision, reason, decided_by, decided_at,"
        " effective_at) VALUES (?,?,?,?,?,?)",
        (REC_B, "accepted", "Agreed in review.", ref(REVIEWER), STAMP, STAMP),
    )
    _write(
        "INSERT INTO outcome (outcome_id, rec_id, measurement, measured_at, metric, baseline,"
        " actual, delta, query_id, verdict) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("out_1", REC_B, 1, STAMP, "mtta_minutes", 40.0, 30.0, -10.0, QID, "paid_off"),
    )


# --- Recommendations (U09-80) --------------------------------------------------------------------


def test_it09_10_recommendations_list_filters_and_outcomes(env: SimpleNamespace) -> None:
    """IT09-10 Recommendations: marked summary, confidence JSON, decision by name, outcomes."""
    del env
    _seed_recs()
    at = _run(RECS)
    shown = "\n".join(_texts(at))
    summary = next(str(m.value) for m in at.main.markdown if "payments" in str(m.value))
    assert "<b>" not in summary  # model text sanitised (TH09-02)
    assert f"(#ev-{QID})" in summary  # the marker became an evidence link
    assert {"confidence": 0.7, "confidence_basis": {"prior": 0.5}} in _json(at)
    assert f"accepted by {REVIEWER}" in shown  # display_name(decided_by) from the roster
    frames = "\n".join(d.value.to_string() for d in at.main.dataframe)
    assert "mtta_minutes" in frames
    assert "paid_off" in frames
    assert any(e.label.startswith("Evidence (") for e in at.main.expander)
    at.selectbox(key="recs_state").set_value("undecided").run()
    assert not at.error
    assert [k for k in ("payments", "on-call") if k in "\n".join(_texts(at))] == ["payments"]
    at.selectbox(key="recs_state").set_value(None).run()
    at.selectbox(key="recs_kind").set_value("org_action").run()
    assert "payments" not in "\n".join(_texts(at))
    at.selectbox(key="recs_kind").set_value("fund").run()
    at.selectbox(key="recs_state").set_value("rejected").run()
    assert "No recommendations match the filters." in [i.value for i in at.info]


def test_it09_22_decide_recommendation_via_page(env: SimpleNamespace) -> None:
    """IT09-22 page: Reject with a reason and date → MemoryStore.decide with user_ref; audit."""
    _seed_recs()
    at = _run(RECS)
    at.radio(key=f"rec_decision_{REC_A}").set_value("Reject")
    at.text_area(key=f"rec_reason_{REC_A}").input(REASON)
    at.date_input(key=f"rec_effective_{REC_A}").set_value(date(2026, 11, 1))
    at.button(key=f"rec_submit_{REC_A}").click().run()
    assert not at.exception
    assert not at.error, [e.value for e in at.error]
    effective = datetime(2026, 11, 1, tzinfo=UTC)
    assert env.memory.calls == [
        ("decide", (REC_A, "rejected", REASON, ref(REVIEWER), effective), {})
    ]
    assert _events(env, "recommendation_decision") == [
        {"event": "recommendation_decision", "actor": ref(REVIEWER), "rec_id": REC_A,
         "decision": "rejected", "decided_by": ref(REVIEWER)}
    ]  # fmt: skip
    assert f"Decision recorded for {REC_A}." in [s.value for s in at.success]


def test_ft09_01_decision_store_busy_keeps_form(env: SimpleNamespace) -> None:
    """FT09-01 Recommendations: StoreBusy from spec 07 → error card; the form keeps its values;
    a short reason is refused the same way."""
    _seed_recs()
    env.memory.raises["decide"] = StoreBusy("database is locked")
    at = _run(RECS)
    at.radio(key=f"rec_decision_{REC_A}").set_value("Defer")
    at.text_area(key=f"rec_reason_{REC_A}").input(REASON)
    at.button(key=f"rec_submit_{REC_A}").click().run()
    assert not at.exception
    assert any("Ops store is locked." in e.value for e in at.error), [e.value for e in at.error]
    assert at.radio(key=f"rec_decision_{REC_A}").value == "Defer"
    assert at.text_area(key=f"rec_reason_{REC_A}").value == REASON
    del env.memory.raises["decide"]
    at.text_area(key=f"rec_reason_{REC_A}").input("too short")
    at.button(key=f"rec_submit_{REC_A}").click().run()
    assert any("Reason must be 10 to 1000 characters." in e.value for e in at.error)
    assert at.text_area(key=f"rec_reason_{REC_A}").value == "too short"
    assert [c[0] for c in env.memory.calls] == ["decide"]  # only the busy attempt


# --- Review Queue (U09-81) -----------------------------------------------------------------------

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
CONTENT = "Churn **means** customers <script>alert(1)</script> [docs](http://evil.example)"
RECORD = "servicenow:incident:INC0001"
CONFLICT = "mem_" + new_ulid()


def _item(kind: str, payload: dict[str, object]) -> str:
    return shared.create_review_item(kind, payload, now=NOW)  # type: ignore[arg-type]


def _label(question: str = "root_cause") -> str:
    return _item("label_check", {
        "record_id": RECORD, "content_hash": "h1", "question": question,
        "question_set_version": QSV, "answer": "code", "probability": 0.55,
        "decider": "ensemble", "decider_version": "v1",
    })  # fmt: skip


def _mapping(value: str = "Payments") -> str:
    return _item("mapping_suggestion", {"field": "assignment_group", "value": value,
                                        "target": "T1"})  # fmt: skip


def _weight() -> str:
    return _item("weight_change", {"key": "usd_per_incident_hour", "old": 100, "new": 120})


def _press(at: AppTest, item_id: str, button: str, note: str | None = None) -> AppTest:
    if note is not None:
        at.text_area(key=f"review_note_{item_id}").input(note)
    return at.button(key=f"review_{button}_{item_id}").click().run()


def _buttons(at: AppTest) -> set[str]:
    return {str(b.key) for b in at.button}


def test_it09_10_review_queue_items_by_kind(env: SimpleNamespace) -> None:
    """IT09-10 Review Queue: a tab per kind with counts; each kind's payload rendering."""
    del env
    _item("memory_write", {"memory_id": "mem_" + new_ulid(), "content": CONTENT,
                           "flags": ["instruction_like", "conflict"],
                           "conflicts_with": [CONFLICT]})  # fmt: skip
    known, unknown = _label(), _label("not_in_config")
    _mapping()
    _weight()
    at = _run(QUEUE)
    assert [t.label for t in at.tabs] == [
        "Memory writes (1)", "Label checks (2)", "Mapping suggestions (1)", "Weight changes (1)",
    ]  # fmt: skip
    content = next(str(m.value) for m in at.main.markdown if "Churn" in str(m.value))
    assert "<script>" not in content
    assert "evil.example" not in content
    shown = _texts(at)
    assert "Flag: instruction_like" in shown
    assert "Flag: conflict" in shown
    assert f"Conflicts with: {CONFLICT}" in shown
    assert "Record text unavailable" in shown  # no promoted build
    assert at.selectbox(key=f"review_answer_{known}").options == ["capacity", "code"]
    assert at.text_input(key=f"review_answer_{unknown}").max_chars == 200
    frames = "\n".join(d.value.to_string() for d in at.main.dataframe)
    assert "assignment_group" in frames
    assert "0.55" in frames
    assert {"key": "usd_per_incident_hour", "new": 120, "old": 100} in _json(at)  # OI-07


def test_it09_21_decide_every_kind_via_page(
    env: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT09-21 page: memory_write → MemoryStore.approve/reject, review item decided in impl 07's
    transaction (R-33); other kinds → impl 02 decide_review_item + review_decision audit."""
    life = make_lifecycle(tmp_path, monkeypatch, real_audit=True)  # keeps the env's audit spy
    calls: list[tuple[str, str, str, str | None]] = []

    def approve(memory_id: str, user_ref: str, note: str | None = None) -> None:
        calls.append(("approve", memory_id, user_ref, note))
        life.lifecycle.approve(memory_id, user_ref, note)

    def reject(memory_id: str, user_ref: str, note: str) -> None:
        calls.append(("reject", memory_id, user_ref, note))
        life.lifecycle.reject(memory_id, user_ref, note)

    env.services.memory = SimpleNamespace(approve=approve, reject=reject)
    kept, dropped = seed_item(), seed_item(content="churn means something else")
    kept_item = row_of(kept)["data"]["review_item_id"]
    dropped_item = row_of(dropped)["data"]["review_item_id"]
    label, mapping, refused, weight = _label(), _mapping(), _mapping("Billing"), _weight()
    env.login(ADMIN)
    me = ref(ADMIN)
    at = _run(QUEUE)
    at = _press(at, kept_item, "approve", "Looks right")
    at = _press(at, dropped_item, "reject", "Duplicate of an existing glossary entry")
    at.selectbox(key=f"review_answer_{label}").set_value("capacity")
    at = _press(at, label, "approve")
    at = _press(at, mapping, "approve")
    at = _press(at, refused, "reject", "Wrong team")
    at = _press(at, weight, "approve")
    assert not at.exception
    assert not at.error, [e.value for e in at.error]
    assert calls == [
        ("approve", kept, me, "Looks right"),
        ("reject", dropped, me, "Duplicate of an existing glossary entry"),
    ]
    assert (row_of(kept)["status"], row_of(dropped)["status"]) == ("active", "rejected")
    for item_id, status in ((kept_item, "approved"), (dropped_item, "rejected"),
                            (label, "approved"), (mapping, "approved"), (refused, "rejected"),
                            (weight, "approved")):  # fmt: skip
        item = review_of(item_id)
        assert (item.status, item.decided_by) == (status, me), item_id
    assert review_of(label).note == json.dumps({"answer": "capacity"}, separators=(",", ":"))
    decided = {(a["item_id"], a["status"]) for a in _events(env, "review_decision")}
    assert decided == {(kept_item, "approved"), (dropped_item, "rejected"),
                       (label, "approved"), (mapping, "approved"), (refused, "rejected"),
                       (weight, "approved")}  # fmt: skip
    assert [t.label for t in at.tabs] == [
        "Memory writes (0)", "Label checks (0)", "Mapping suggestions (0)", "Weight changes (0)",
    ]  # fmt: skip


def test_it09_21_reject_needs_a_note(env: SimpleNamespace) -> None:
    """IT09-21 page: Reject without a note → error card next to the form; item stays pending."""
    mapping = _mapping()
    at = _press(_run(QUEUE), mapping, "reject")
    assert any("A note is required to reject." in e.value for e in at.error)
    assert shared.get_review_item(mapping).status == "pending"
    assert _events(env, "review_decision") == []


def test_st09_09_reviewer_cannot_approve_weight_change(env: SimpleNamespace) -> None:
    """ST09-09 a reviewer sees no weight_change buttons and a direct approval is refused."""
    weight, mapping = _weight(), _mapping()
    at = _run(QUEUE)
    assert {f"review_approve_{mapping}", f"review_reject_{mapping}"} <= _buttons(at)
    assert not any(weight in key for key in _buttons(at))
    reviewer = rules.Actor(ref(REVIEWER), "reviewer", "dashboard", display=REVIEWER)
    with pytest.raises(PermissionDenied):
        actions.decide_review(reviewer, item_id=weight, approve=True, memory=env.memory, now=NOW)
    assert shared.get_review_item(weight).status == "pending"
    assert _events(env, "auth")[-1] == {
        "event": "auth", "actor": ref(REVIEWER), "user_ref": ref(REVIEWER), "role": "reviewer",
        "result": "denied",
    }  # fmt: skip
    assert _events(env, "review_decision") == []
    env.login(ADMIN)
    assert f"review_approve_{weight}" in _buttons(_run(QUEUE))  # admins see them


def test_ft09_01_locked_ops_store_keeps_review_form(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FT09-01 ops DB locked by another connection: approving retries per sqlite_write, then an
    error card; the note is kept and the same submit works once the lock is gone."""
    monkeypatch.setattr(core, "_BUSY_TIMEOUT_MS", 20)
    core.reset_connections(path=env.db)
    attempts: list[str] = []
    real = actions.decide_review_item

    def counted(item_id: str, status: str, **kw: Any) -> object:
        attempts.append(status)
        return real(item_id, status, **kw)

    monkeypatch.setattr(actions, "decide_review_item", counted)
    mapping = _mapping()
    at = _run(QUEUE)
    blocker = sqlite3.connect(env.db, isolation_level=None)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        at = _press(at, mapping, "approve", "Matches the CMDB")
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    assert not at.exception
    assert any("Ops store is locked." in e.value for e in at.error), [e.value for e in at.error]
    assert attempts == ["approved"] * 6  # the sqlite_write policy's attempts
    assert at.text_area(key=f"review_note_{mapping}").value == "Matches the CMDB"
    assert shared.get_review_item(mapping).status == "pending"
    at = at.button(key=f"review_approve_{mapping}").click().run()
    assert not at.error, [e.value for e in at.error]
    item = shared.get_review_item(mapping)
    assert (item.status, item.note) == ("approved", "Matches the CMDB")


# --- Runs & Traces (U09-82) ----------------------------------------------------------------------

RUN_LIVE = "run_" + new_ulid()
FAILED_JOB, QUEUED_JOB = "job_" + new_ulid(), "job_" + new_ulid()
PAYLOAD_TEXT = "sampled prompt text"


def _seed_run(data_root: Path) -> None:
    """A running review run with a dead task, findings, an event, two jobs and a trace file."""
    _write(
        "INSERT INTO run (run_id, kind, depth, profile, config_hash, status, started_at,"
        " token_usage, cost_usd) VALUES (?,?,?,?,?,?,?,?,?)",
        (RUN_LIVE, "funding_review", "standard", "local", "c", "running", STAMP,
         json.dumps({"input": 100, "output": 20}), "0.05"),
    )  # fmt: skip
    _write(
        "INSERT INTO task (task_id, run_id, role, spec, status, attempts, last_error, created_at,"
        " updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ("tsk_dead", RUN_LIVE, "analyst", '{"objective": "Rank W1"}', "dead", 3,
         "QueryError: query timed out", STAMP, STAMP),
        ("tsk_done", RUN_LIVE, "writer", '{"objective": "Write"}', "done", 1, None, STAMP, STAMP),
    )  # fmt: skip
    _write(
        "INSERT INTO finding (finding_id, run_id, task_id, author_role, claim, numbers, status,"
        " created_at) VALUES (?,?,?,?,?,?,?,?)",
        ("fnd_1", RUN_LIVE, "tsk_done", "analyst", "c1", "[]", "verified", STAMP),
        ("fnd_2", RUN_LIVE, "tsk_done", "analyst", "c2", "[]", "rejected", STAMP),
    )
    _write(
        "INSERT INTO resilience_event (event_id, ts, kind, component, target, run_id)"
        " VALUES (?,?,?,?,?,?)",
        ("evt_1", STAMP, "breaker_open", "llm", "reasoning", RUN_LIVE),
    )
    error = json.dumps({"class": "ModelUnavailable", "message": "hidden error text"})
    _write(
        "INSERT INTO job (job_id, kind, gpu_class, status, priority, payload, last_error,"
        " attempts, max_attempts, scheduled_for, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (FAILED_JOB, "review", "none", "failed", 50, json.dumps({"run_id": RUN_LIVE}), error,
         3, 3, STAMP, STAMP),
        (QUEUED_JOB, "review", "none", "queued", 50, json.dumps({"run_id": RUN_LIVE}), None,
         0, 3, STAMP, STAMP),
    )  # fmt: skip
    traces = data_root / "traces"
    traces.mkdir(parents=True, exist_ok=True)
    events = [
        {"type": "llm_call", "run_id": RUN_LIVE, "model": "m1", "payload": PAYLOAD_TEXT},
        {"type": "tool_call", "run_id": RUN_LIVE, "tool": "sql", "args": {"q": PAYLOAD_TEXT}},
    ]
    lines = "\n".join(json.dumps(e) for e in events) + "\n"
    (traces / f"{RUN_LIVE}.jsonl").write_text(lines, encoding="utf-8")


def _job_rows() -> list[tuple[object, ...]]:
    return [tuple(r) for r in core.read_all("SELECT job_id, status FROM job ORDER BY job_id")]


def _frames(at: AppTest) -> str:
    return "\n".join(d.value.to_string() for d in at.main.dataframe)


def _trace(at: AppTest) -> list[dict[str, Any]]:
    found = [j for j in _json(at) if isinstance(j, list)]
    assert len(found) == 1, found
    return found[0]  # type: ignore[no-any-return]


def test_it09_10_runs_detail_and_trace(env: SimpleNamespace) -> None:
    """IT09-10 Runs & Traces: run row, tasks with the dead one highlighted, findings, events,
    jobs, trace events without payloads (admins only see them); no actions for a reviewer."""
    _seed_run(env.data_root)
    at = _run(RUNS)
    assert at.selectbox(key="runs_run").value == RUN_LIVE
    frames = _frames(at)
    for text in ("funding_review", "120", "0.05", "tsk_dead", "query timed out", "verified",
                 "breaker_open", FAILED_JOB, "ModelUnavailable"):  # fmt: skip
        assert text in frames, text
    assert "hidden error text" not in frames
    assert "Dead tasks: tsk_dead" in [w.value for w in at.main.warning]
    assert [e["type"] for e in _trace(at)] == ["llm_call", "tool_call"]
    assert PAYLOAD_TEXT not in json.dumps(_trace(at))
    assert not at.button
    at.selectbox(key="trace_type").set_value("tool_call").run()
    assert [e["tool"] for e in _trace(at)] == ["sql"]
    env.login(ADMIN)
    at = _run(RUNS)
    assert PAYLOAD_TEXT in json.dumps(_trace(at))  # include_payload for admins
    assert {"resume_submit", f"job_retry_{FAILED_JOB}", f"job_cancel_{QUEUED_JOB}",
            "report_rerender"} <= _buttons(at)  # fmt: skip


def test_it09_26_admin_resume_retry_cancel_rerender(
    env: SimpleNamespace, report_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT09-26 admin: resume (enqueue_resume), retry, cancel, re-render via the page; spec 08
    functions called; admin_action audit lines; report files re-rendered."""
    from tests.support.report_env import FUND_RUN  # noqa: PLC0415 - the report_env seed run

    from herness.core import jobs  # noqa: PLC0415 - spied below

    _seed_run(env.data_root)
    called: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
    for name in ("enqueue_resume", "retry", "cancel"):
        real = getattr(jobs, name)

        def spy(*a: Any, _name: str = name, _real: Any = real, **kw: Any) -> Any:
            called.append((_name, a, kw))
            return _real(*a, **kw)

        monkeypatch.setattr(jobs, name, spy)
    env.login(ADMIN)
    at = _run(RUNS)
    at.selectbox(key="runs_run").set_value(RUN_LIVE).run()
    at.checkbox(key="resume_retry_dead").check()
    at = at.button(key="resume_submit").click().run()
    assert not at.error, [e.value for e in at.error]
    resumed = next(c for c in called if c[0] == "enqueue_resume")
    assert resumed[1:] == ((RUN_LIVE,), {"force": False, "retry_dead": True})
    resume_job = core.read_one("SELECT job_id, payload FROM job WHERE idem_key = ?",
                               (f"resume:{RUN_LIVE}",))  # fmt: skip
    assert resume_job is not None
    assert f"Resume queued as job {resume_job['job_id']}." in [s.value for s in at.success]
    at = at.button(key=f"job_retry_{FAILED_JOB}").click().run()
    at = at.button(key=f"job_cancel_{QUEUED_JOB}").click().run()
    assert not at.error, [e.value for e in at.error]
    status = {r["job_id"]: r["status"] for r in core.read_all("SELECT job_id, status FROM job")}
    assert (status[FAILED_JOB], status[QUEUED_JOB]) == ("queued", "canceled")
    assert [c[0] for c in called] == ["enqueue_resume", "retry", "cancel"]
    at.selectbox(key="runs_run").set_value(FUND_RUN).run()
    at = at.button(key="report_rerender").click().run()
    assert not at.error, [e.value for e in at.error]
    out = report_env.reports_root / FUND_RUN
    assert (out / "report.html").is_file()
    assert (out / "report.md").is_file()
    assert any("report.html" in s.value for s in at.success)
    lines = [(a["action"], a["target"], a["detail"]) for a in _events(env, "admin_action")]
    assert lines == [
        ("run_resume", RUN_LIVE, "requested"), ("job_retry", FAILED_JOB, "requested"),
        ("job_cancel", QUEUED_JOB, "requested"), ("report_render", FUND_RUN, "requested"),
    ]  # fmt: skip
    assert {a["actor"] for a in _events(env, "admin_action")} == {ref(ADMIN)}


# --- Viewer gating (ST09-07) ---------------------------------------------------------------------


def test_st09_07_viewer_sees_no_actions_and_direct_calls_are_refused(
    env: SimpleNamespace,
) -> None:
    """ST09-07 a viewer gets no action controls on pages 08 - 10; calling each write action
    directly raises PermissionDenied, changes no row and writes an ``auth`` denied line."""
    _seed_recs()
    _seed_run(env.data_root)
    memory_item = _item("memory_write", {"memory_id": "mem_" + new_ulid(), "content": "c"})
    items = (memory_item, _label(), _mapping(), _weight())
    env.login(VIEWER)
    for path in (RECS, QUEUE, RUNS):
        at = _run(path)
        assert not at.button, (path.name, _buttons(at))
        assert not at.text_area
    viewer = rules.Actor(ref(VIEWER), "viewer", "dashboard", display=VIEWER)
    jobs_before = _job_rows()
    before = len(env.audits)
    calls: list[Any] = [
        lambda: actions.decide_recommendation(viewer, rec_id=REC_A, decision="accepted",
                                              reason=REASON, memory=env.memory),
        *(lambda i=i: actions.decide_review(viewer, item_id=i, approve=True, note="ok",
                                            answer="code", memory=env.memory, now=NOW)
          for i in items),
        lambda: actions.job_control(viewer, job_id=QUEUED_JOB, op="cancel"),
        lambda: actions.job_control(viewer, job_id=FAILED_JOB, op="retry"),
        lambda: actions.resume_run(viewer, run_id=RUN_LIVE),
        lambda: actions.rerender_report(viewer, run_id=RUN_LIVE),
    ]  # fmt: skip
    for call in calls:
        with pytest.raises(PermissionDenied):
            call()
    denied = {"event": "auth", "actor": ref(VIEWER), "user_ref": ref(VIEWER), "role": "viewer",
              "result": "denied"}  # fmt: skip
    assert env.audits[before:] == [denied] * len(calls)
    assert env.memory.calls == []
    assert _job_rows() == jobs_before
    assert {shared.get_review_item(i).status for i in items} == {"pending"}
    assert core.read_one("SELECT count(*) AS n FROM decision_log WHERE rec_id = ?",
                         (REC_A,))["n"] == 0  # fmt: skip
    assert not (env.data_root / "reports" / RUN_LIVE).exists()


# --- Review round 1 (M3 stale items, M4 form reset) -----------------------------------------------


def test_it09_22_form_reset_after_decision(env: SimpleNamespace) -> None:
    """IT09-22 after a recorded decision the form is reset, so a second submit without new
    input adds no second decision (decision_log is append-only)."""
    _seed_recs()
    at = _run(RECS)
    at.radio(key=f"rec_decision_{REC_A}").set_value("Reject")
    at.text_area(key=f"rec_reason_{REC_A}").input(REASON)
    at = at.button(key=f"rec_submit_{REC_A}").click().run()
    assert not at.error, [e.value for e in at.error]
    assert at.radio(key=f"rec_decision_{REC_A}").value == "Accept"
    assert at.text_area(key=f"rec_reason_{REC_A}").value in {"", None}
    assert at.date_input(key=f"rec_effective_{REC_A}").value is None
    at = at.button(key=f"rec_submit_{REC_A}").click().run()
    assert any("Reason must be 10 to 1000 characters." in e.value for e in at.error)
    assert [c[0] for c in env.memory.calls] == ["decide"]
    assert len(_events(env, "recommendation_decision")) == 1


def test_it09_21_stale_item_dropped_after_refusal(env: SimpleNamespace) -> None:
    """IT09-21 an item decided elsewhere after the page cached it: the refusal clears the ops
    cache, so the next rerun no longer lists the item."""
    mapping, other = _mapping(), _mapping("Billing")
    at = _run(QUEUE)
    shared.decide_review_item(mapping, "approved", decided_by=ref(ADMIN), note=None, now=NOW)
    at = _press(at, mapping, "approve", "Looks right")
    assert any(f"Item {mapping} was already approved." in e.value for e in at.error)
    at = at.run()
    assert not at.error, [e.value for e in at.error]
    assert "Mapping suggestions (1)" in [t.label for t in at.tabs]
    assert f"review_approve_{mapping}" not in _buttons(at)
    assert f"review_approve_{other}" in _buttons(at)


def test_it09_22_missing_recommendation_dropped_after_refusal(env: SimpleNamespace) -> None:
    """IT09-22 a recommendation gone after the page cached it: NotFound clears the ops cache,
    so the next rerun no longer lists it."""
    del env
    _seed_recs()
    at = _run(RECS)
    _write("DELETE FROM recommendation WHERE rec_id = ?", (REC_A,))
    at.text_area(key=f"rec_reason_{REC_A}").input(REASON)
    at = at.button(key=f"rec_submit_{REC_A}").click().run()
    assert any(f"recommendation {REC_A} not found" in e.value for e in at.error)
    at = at.run()
    assert not at.error, [e.value for e in at.error]
    assert f"rec_submit_{REC_A}" not in _buttons(at)
    assert f"rec_submit_{REC_B}" in _buttons(at)
