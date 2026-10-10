"""IT09-10 for pages 01 - 04 (T09-16): title redaction, the funding detail, the portfolio chart.

The page-list tests (tests/integration/app/test_app_pages.py) cover "no exception, key elements"
and IT09-11 for these pages; this file adds the card's acceptance check "titles pass through the
redaction test double (asserted)" and drives the blocks the planted build leaves empty.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest
from tests.support.app_env import (
    APP_DIR,
    REDACTED_TITLE,
    install_redaction_double,
    install_session,
    page_services,
    plant_scored_build,
    preserved_main,
)
from tests.support.ops_store import OpsStoreHandle

from app.common import wh
from herness.core.resilience import ProcessState
from herness.store.ops import bind_core_backends, core

pytestmark = pytest.mark.integration

TIMEOUT_S = 60
FUNDING = APP_DIR / "pages" / "01_Funding_Ranking.py"
PORTFOLIO = APP_DIR / "pages" / "02_Portfolio.py"
RAW_TITLES = ("PAY-1", "PAY-2")  # score.funding titles of the planted build
STAMP = "2026-10-01T12:00:00.000000Z"
REC_SUMMARY = "Fund the payments initiative"
CLAIM = "Incidents on W1 dropped"


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
    """A migrated ops store, a reviewer session, the redaction double and a scored build."""
    del reset_process_state
    bind_core_backends()
    install_session(monkeypatch, page_services(ops_store.data_root))
    titles = install_redaction_double(monkeypatch)
    build = plant_scored_build(ops_store.data_root / "warehouse")
    db = ops_store.data_root / "warehouse" / f"wh-{build}.duckdb"
    return SimpleNamespace(titles=titles, db=db)


def _values(at: AppTest) -> list[str]:
    kinds = ("title", "subheader", "markdown", "caption", "text", "info", "warning", "success")
    return [str(e.value) for kind in kinds for e in getattr(at.main, kind)]


def _page_frames(at: AppTest) -> str:
    """The page's own tables: dataframes outside the evidence expanders.

    Evidence samples are stored ``meta.evidence.result_sample`` cells shown by the T09-15
    evidence widget, not titles this page renders (see the T09-16 report).
    """
    inside = {id(d.proto) for e in at.main.expander for d in e.dataframe}
    return "\n".join(d.value.to_string() for d in at.main.dataframe if id(d.proto) not in inside)


def _run(path: Path) -> AppTest:
    at = AppTest.from_file(str(path), default_timeout=TIMEOUT_S).run()
    assert not at.exception
    assert not at.error, [e.value for e in at.error]
    return at


@pytest.mark.parametrize("path", [FUNDING, PORTFOLIO], ids=lambda p: p.name)
def test_it09_10_titles_pass_redaction(env: SimpleNamespace, path: Path) -> None:
    """IT09-10 Funding Ranking / Portfolio titles go through ``redact_text`` (TH09-11, D9)."""
    at = _run(path)
    assert set(RAW_TITLES) <= set(env.titles)
    frames = _page_frames(at)
    assert REDACTED_TITLE in frames
    shown = "\n".join([frames, *_values(at)])
    for raw in RAW_TITLES:
        assert raw not in shown, raw
    if path == FUNDING:
        assert "Unconfirmed weights" in frames  # both planted rows have unconfirmed = true


def _seed_detail() -> None:
    """One recommendation and one verified finding about candidate ``W1``."""
    ref = {"id": "n1", "value": 3, "unit": "count", "query_id": "q_9c106c191cd857d0",
           "column": "n_incidents", "row_key": None}  # fmt: skip
    numbers = json.dumps([ref])

    def fn(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO recommendation (rec_id, run_id, target_type, target_id, summary, kind,"
            " numbers, created_at) VALUES (?,?,?,?,?,?,?,?)",
            ("rec_1", "run_1", "candidate", "W1", f"{REC_SUMMARY} [[n1]]", "fund", numbers, STAMP),
        )
        conn.execute(
            "INSERT INTO finding (finding_id, run_id, task_id, author_role, claim, entity_type,"
            " entity_id, numbers, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("fnd_1", "run_1", "task_1", "analyst", f"{CLAIM} [[n1]] <b>x</b>", "candidate",
             "W1", numbers, "verified", STAMP),
        )  # fmt: skip

    core.run_write(fn, op="test_seed_detail")


def test_it09_10_funding_detail(env: SimpleNamespace) -> None:
    """IT09-10 Funding detail: attribution, recommendation summary, finding claim, evidence."""
    del env
    _seed_detail()
    at = _run(FUNDING)
    shown = _values(at)
    for key in ("Attribution", "Recommendations", "Verified findings"):
        assert key in shown, key
    marked = [str(m.value) for m in at.main.markdown]
    assert any(REC_SUMMARY in m and "[unresolved]" not in m for m in marked), marked
    claim = next(m for m in marked if "Incidents on W1" in m)
    assert "<b>" not in claim  # model text is sanitised (TH09-02)
    assert "record_id" in _page_frames(at)  # the attribution table
    assert any(e.label.startswith("Evidence (") for e in at.main.expander)


def test_it09_10_portfolio_chart_compare(env: SimpleNamespace) -> None:
    """IT09-10 Portfolio: selected rows chart cumulative impact; "Compare with" adds a series."""
    with duckdb.connect(str(env.db)) as con:
        con.execute(
            "UPDATE score.portfolio SET selected = true,"
            " order_rank = CASE candidate_id WHEN 'W1' THEN 1 ELSE 2 END"
        )
    at = _run(PORTFOLIO)
    assert "No selected candidates to chart." not in _values(at)
    assert len(at.get("vega_lite_chart")) == 1
    compare = next(s for s in at.selectbox if s.label == "Compare with")
    at = compare.set_value("lean").run()
    assert not at.exception
    assert not at.error, [e.value for e in at.error]
    captions = [str(c.value) for c in at.main.caption]
    assert sum(c.startswith("Budget (") for c in captions) == 2, captions
    assert "Solver status: OPTIMAL" in captions
    assert len(at.get("vega_lite_chart")) == 1  # one chart, a series per scenario


ORG = APP_DIR / "pages" / "03_Org_Scorecards.py"
LEVERS = APP_DIR / "pages" / "04_Action_Levers.py"


def test_it09_10_org_scorecards_peer_and_trend(env: SimpleNamespace) -> None:
    """IT09-10 Org Scorecards: the peer table, the selected row's monthly trend and evidence."""
    with duckdb.connect(str(env.db)) as con:  # the planted monthly values are NULL
        con.execute(
            "UPDATE metrics.metric_value SET value = 0.25 WHERE metric = 'change_failure_rate'"
            " AND entity_type = 'team' AND entity_id = 'T1' AND period = 'month'"
        )
    at = _run(ORG)
    frames = _page_frames(at)
    for column in ("peer_median", "z_score", "composite"):
        assert column in frames, column
    captions = [str(c.value) for c in at.main.caption]
    assert any(c.endswith("last 8 months") for c in captions), captions
    assert len(at.get("vega_lite_chart")) == 1
    assert any(e.label.startswith("Evidence (") for e in at.main.expander)
