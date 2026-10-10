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

from app.common import wh
from herness.core.errors import StoreBusy
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
