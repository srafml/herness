"""Unit tests for ``load_report_data`` (impl 09 U09-15; UT09-79, TH09-11).

The warehouse is a local temp DuckDB file holding only the ``meta``, ``score`` and ``metrics``
tables and columns ``_data`` reads (names and types from impl 02 ``000_settings.sql`` and the
impl 04 scoring signatures); ``tiny_build`` (T11-17) replaces it once it exists. Ops rows go
into the real migrated ``ops_store`` fixture; the process redactor is a fixed-key test redactor.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest
from tests.support.report_drafts import (
    FINDING_ID,
    RUN_ID,
    ULID,
    draft_dict,
    make_draft,
    number_ref,
    paragraph,
    recommendation,
)

from herness.core import redact as r
from herness.core import time as clock
from herness.core.errors import QueryError, ReportContractError
from herness.core.redact_directory import NameDirectory
from herness.core.settings import RedactionConfig
from herness.reports import _data, _data_slots, charts
from herness.reports._data import ReportData, load_report_data
from herness.reports._evidence import EvidenceCollector
from herness.reports.contract import SECTION_IDS, UncitedHit
from herness.store import ops
from herness.store.ops import core

pytestmark = pytest.mark.unit

_T0 = datetime(2026, 9, 26, 10, 0, tzinfo=UTC)
_NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
_EMAIL = "sentinel.person@example.org"
_PRIOR_RUN = f"run_{ULID[:-1]}A"
_PRIOR_REC = f"rec_{ULID[:-1]}A"
_OLD_REC = f"rec_{ULID[:-1]}B"


def _q(n: int) -> str:
    return f"q_{n:016x}"


QA, QB, QC, QD, QE, QF, QG, QH, QI, QJ, QK, QL, QM, QN = (_q(n) for n in range(0xA1, 0xAF))

_WH_DDL = (
    "CREATE SCHEMA meta", "CREATE SCHEMA score", "CREATE SCHEMA metrics",
    "CREATE TABLE meta.build (build_id VARCHAR, source_watermarks JSON)",
    "CREATE TABLE meta.dq_result (check_name VARCHAR, severity VARCHAR, value DOUBLE,"
    " threshold DOUBLE, passed BOOLEAN)",
    "CREATE TABLE score.funding (candidate_id VARCHAR, title VARCHAR, confidence DOUBLE,"
    " effort_cost_usd DECIMAL(18,2), priority DOUBLE, wsjf DOUBLE, rank BIGINT,"
    " unconfirmed BOOLEAN, query_ids VARCHAR[])",
    "CREATE TABLE score.org (entity_type VARCHAR, entity_id VARCHAR, metric VARCHAR,"
    " value DOUBLE, peer_group VARCHAR, peer_median DOUBLE, z_score DOUBLE, sample_size BIGINT,"
    " composite DOUBLE, rank BIGINT, flags VARCHAR[], query_ids VARCHAR[])",
    "CREATE TABLE score.portfolio (scenario VARCHAR, budget_usd DECIMAL(18,2),"
    " candidate_id VARCHAR, selected BOOLEAN, order_rank INTEGER,"
    " expected_impact_usd DECIMAL(18,2), solver_status VARCHAR, query_ids VARCHAR[])",
    "CREATE TABLE score.action_lever (entity_type VARCHAR, entity_id VARCHAR, metric VARCHAR,"
    " target_kind VARCHAR, delta_usd DECIMAL(18,2), rationale_template VARCHAR,"
    " template_params JSON, unconfirmed BOOLEAN, query_ids VARCHAR[])",
    "CREATE TABLE metrics.metric_value (metric VARCHAR, entity_type VARCHAR,"
    " entity_id VARCHAR, period VARCHAR, period_start DATE, value DOUBLE)",
)  # fmt: skip

_ORG_ROWS = [
    ("team", "t1", "composite", None, "team:all", None, None, 5, 0.9, 1, [], [QK]),
    ("team", "t1", "mttr", 4.5, "team:all", 3.0, 1.25, 12, 0.9, 1, ["small"], [QK]),
    ("team", "t2", "mttr", 2.0, "team:all", 3.0, -0.5, 9, 0.2, 2, [], [_q(0xE1)]),
]


def _seed_warehouse(con: duckdb.DuckDBPyConnection) -> None:
    for ddl in _WH_DDL:
        con.execute(ddl)
    marks = {"servicenow": "2026-09-20T08:00:00Z", "jira": "2026-09-21T09:30:00+00:00",
             "files": "not a timestamp", "nested": {"x": 1}}  # fmt: skip
    con.execute("INSERT INTO meta.build VALUES ('b1', ?)", [json.dumps(marks)])
    con.executemany(
        "INSERT INTO meta.dq_result VALUES (?, ?, ?, ?, ?)",
        [("incidents_service_null", "warn", 0.125, 0.1, False), ("rows_ok", "error", 0, 0, True)],
    )
    con.executemany(
        "INSERT INTO score.funding VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("epic-1", f"Cache rewrite for {_EMAIL}", 0.8, "1000.00", 0.75, 2.5, 1, True,
             [QF, _q(0xF1)]),
            ("epic-2", "Queue retry", None, "500.00", None, 1.0, 2, False, [QG]),
        ],
    )  # fmt: skip
    con.executemany("INSERT INTO score.org VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", _ORG_ROWS)
    con.executemany(
        "INSERT INTO score.portfolio VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("base", "2000.00", "epic-1", True, 1, "3000.00", "OPTIMAL", [QJ]),
            ("base", "2000.00", "epic-2", False, None, "100.00", "OPTIMAL", [QJ]),
            ("tight", "100.00", "epic-2", False, None, "100.00", "INFEASIBLE", [QJ]),
        ],
    )
    params = {"entity_name": "Payments", "metric_label": "MTTR", "current_value": 4.5,
              "target_value": 3}  # fmt: skip
    con.execute(
        "INSERT INTO score.action_lever VALUES ('team', 't1', 'mttr', 'peer_median', 1500.00,"
        " '{entity_name} cuts {metric_label} from {current_value} to {target_value}: {bogus}',"
        " ?, true, ?)",
        [json.dumps(params), [QL]],
    )
    series = [("mttr", "team", "t1", "month", date(2026, m, 1), float(m)) for m in range(1, 11)]
    series.append(("mttr", "team", "t1", "week", date(2026, 9, 1), 99.0))
    con.executemany("INSERT INTO metrics.metric_value VALUES (?, ?, ?, ?, ?, ?)", series)


@pytest.fixture
def wh(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    """A read-only connection to a seeded temp warehouse (stand-in for ``tiny_build``)."""
    path = tmp_path / "wh-b1.duckdb"
    with duckdb.connect(str(path)) as con:
        _seed_warehouse(con)
    con = duckdb.connect(str(path), read_only=True)
    try:
        yield con
    finally:
        con.close()


@pytest.fixture
def redactor(monkeypatch: pytest.MonkeyPatch) -> r.Redactor:
    """A process redactor with a fixed key (no config secret needed)."""
    directory = NameDirectory.from_files(None, ("Jane Doe",), None)
    red = r.Redactor(RedactionConfig(directory_file=None), bytes(range(32)), directory)
    monkeypatch.setattr(r._State, "redactor", red)
    return red


def _write(sql: str, rows: Sequence[Sequence[object]]) -> None:
    def fn(conn: sqlite3.Connection) -> None:
        conn.executemany(sql, rows)

    core.run_write(fn, op="test_seed")


def _ts(dt: datetime) -> str:
    return clock.format_utc(dt)


def _seed_ops(status: str = "done") -> ops.RunRow:
    usage = {"input": 10, "output": 5, "by_role": {"writer": {"calls": 2}, "analyst": {}}}
    _write(
        "INSERT INTO run (run_id, kind, depth, profile, config_hash, build_id, status,"
        " started_at, token_usage, cost_usd) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(RUN_ID, "funding_review", "standard", "local", "cfg1", "b1", status, _ts(_T0),
          json.dumps(usage), "1.25"),
         (_PRIOR_RUN, "funding_review", "fast", "local", "cfg0", "b0", "done",
          _ts(_T0 - timedelta(days=100)), "{}", None)],
    )  # fmt: skip
    spec = json.dumps({"dedup_key": "k1", "objective": "o"})
    _write(
        "INSERT INTO task (task_id, run_id, role, spec, status, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?)",
        [("tsk_1", RUN_ID, "analyst", spec, "done", _ts(_T0), _ts(_T0))],
    )
    finding = (
        FINDING_ID,
        RUN_ID,
        "tsk_1",
        "analyst",
        "Pain is [[n1]]",
        "candidate",
        "epic-1",
        json.dumps([number_ref(query_id=QA)]),
        json.dumps([QB]),
        "verified",
        _ts(_T0),
    )
    _write(
        "INSERT INTO finding (finding_id, run_id, task_id, author_role, claim, entity_type,"
        " entity_id, numbers, query_ids, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [finding],
    )  # fmt: skip
    _write(
        "INSERT INTO recommendation (rec_id, run_id, target_type, target_id, summary, kind,"
        " numbers, created_at) VALUES (?,?,?,?,?,?,?,?)",
        [(_PRIOR_REC, _PRIOR_RUN, "epic", "epic-9", "Saved [[n1]]", "fund",
          json.dumps([number_ref(query_id=_q(0xC1))]), _ts(_T0 - timedelta(days=90))),
         (_OLD_REC, _PRIOR_RUN, "epic", "epic-8", "Old [[n9]]", "fund", '{"not": "a list"}',
          _ts(_T0 - timedelta(days=150)))],
    )  # fmt: skip
    _write(
        "INSERT INTO decision_log (rec_id, decision, decided_by, decided_at, effective_at)"
        " VALUES (?,?,?,?,?)",
        [(_PRIOR_REC, "accepted", "u1", _ts(_T0 - timedelta(days=80)),
          _ts(_T0 - timedelta(days=80)))],
    )  # fmt: skip
    _write(
        "INSERT INTO outcome (outcome_id, rec_id, measurement, measured_at, metric, baseline,"
        " actual, delta, query_id, verdict) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [("out_1", _PRIOR_REC, 1, _ts(_T0 - timedelta(days=20)), "mttr", 4.0, 3.0, -1.0, QM,
          "paid_off")],
    )  # fmt: skip
    run = ops.get_run(RUN_ID)
    assert run is not None
    return run


def _custom(**overrides: Any) -> dict[str, Any]:
    return {
        "scenario": {"name": "custom 900"}, "budget_usd": "900", "solver_status": "OPTIMAL",
        "rows": [
            {"candidate_id": "epic-2", "selected": True, "order_rank": 2,
             "expected_impact_usd": "50", "query_ids": [QI], "extra": 1},
            {"candidate_id": "epic-1", "selected": True, "order_rank": 1,
             "expected_impact_usd": "700", "query_ids": [QH, QI]},
            {"candidate_id": "epic-3", "selected": False, "order_rank": None,
             "expected_impact_usd": "1", "query_ids": [_q(0xD1)]},
        ],
        "ignored": True,
    } | overrides  # fmt: skip


def _full_draft(**overrides: Any) -> Any:
    rec = recommendation(
        1, headline="Fund [[n1]] now", summary="It saves [[n2]] a year",
        numbers=[number_ref(query_id=QD), number_ref("n2", "usd", "1200.50", QE)],
    )  # fmt: skip
    org_rec = recommendation(2, kind="org_action", numbers=[number_ref(query_id=QD),
                             number_ref("n2", "usd", "1.00", QE)])  # fmt: skip
    sections = [
        {"id": "executive_summary", "title": "Summary",
         "paragraphs": [paragraph(numbers=[number_ref(query_id=QA)])]},
        {"id": "recommendations", "title": "Recs",
         "paragraphs": [paragraph("Top [[n1]] 99 items", numbers=[number_ref(query_id=QC)])]},
        {"id": "retrospective", "title": "Retro", "paragraphs": [paragraph("Looking back.",
         numbers=[], finding_ids=[])]},
    ]  # fmt: skip
    dead = [
        {
            "task_id": "tsk_9",
            "role": "analyst",
            "objective": "dig",
            "last_error": f"boom for {_EMAIL}\r\nsecond line",
        },
        {"task_id": "tsk_8", "role": "skeptic", "objective": "check", "last_error": None},
    ]
    base = draft_dict(
        sections=sections, recommendations=[rec, org_rec], portfolio_custom=[_custom()],
        query_ids=[QA, QN], dead_tasks=dead, banners=["hybrid_fallback"],
        contested=[FINDING_ID], removed=[{"where": "sections[9]", "reason": "gate2"}],
        flags={"notes": ["n"]},
    )  # fmt: skip
    return make_draft(**(base | overrides))


def _load(run: ops.RunRow, draft: Any, wh: duckdb.DuckDBPyConnection, **kw: Any) -> tuple[
        ReportData, EvidenceCollector]:  # fmt: skip
    collector = EvidenceCollector()
    args: dict[str, Any] = {"top_n": 10, "now": _NOW} | kw
    data = load_report_data(run, draft, wh_con=wh, collector=collector, **args)
    return data, collector


def test_ut09_79_collector_order(ops_store: object, wh: duckdb.DuckDBPyConnection,
                                 redactor: r.Redactor) -> None:  # fmt: skip
    """UT09-79 query ids enter the collector in outline order, then draft.query_ids."""
    data, collector = _load(_seed_ops(), _full_draft(), wh)
    assert collector.ordered_ids() == [
        QA, QB,  # slot 2: paragraph number, then its finding's query ids
        QC,  # slot 3 text block
        QD, QE, QF,  # card rec-1: headline, summary, then score.funding extras
        QG,  # funding table (epic-1 uses QF again, epic-2 QG)
        QH, QI,  # slot 4: custom block cells (selected rows' first ids) ...
        _q(0xD1),  # ... then every id of every row, unselected epic-3 included
        QJ,  # score.portfolio
        QK, _q(0xE1),  # slot 5: top teams t1, t2 (no scorecard entity in ranked_entities)
        QL,  # slot 6 levers
        _q(0xC1), QM,  # slot 7: prior rec summary number, outcome row
        QN,  # draft.query_ids not yet seen
    ]  # fmt: skip
    assert collector.used_by(QF) == ["rec-1", "funding_table"]
    assert collector.used_by(QN) == ["query_ids[1]"]
    # linked numbers: slot 2 (1), slot 3 text (1; the uncited-free "99" is not a number),
    # card text (2) + extras (4), funding table (epic-1: 4; epic-2: wsjf, effort = 2; its None
    # priority/confidence show "—" unlinked), custom block (2), score.portfolio (1),
    # scorecards (t1 composite: size, composite, rank = 3; t1 mttr 6; t2 mttr 6), lever (1),
    # retro (summary number 1 + baseline, actual, delta 3)
    assert data.numbers_total == 37
    assert len(data.number_query_ids) == 37
    assert set(data.number_query_ids) <= set(collector.ordered_ids())


def test_ut09_79_titles_and_last_error_redacted(ops_store: object, wh: duckdb.DuckDBPyConnection,
                                                redactor: r.Redactor) -> None:  # fmt: skip
    """UT09-79 / TH09-11 funding titles and dead-task errors pass redact_text."""
    data, _ = _load(_seed_ops(), _full_draft(), wh)
    assert data.funding_table is not None
    title = data.funding_table.rows[0][2].text
    assert _EMAIL not in title
    assert title.startswith("Cache rewrite for ")
    assert _EMAIL not in data.charts["funding_priority"]
    role, objective, error = data.caveats.dead_tasks[0]
    assert (role, objective) == ("analyst", "dig")
    assert _EMAIL not in error
    assert error.startswith("boom for ")
    assert "second line" not in error
    assert not error.endswith("\r")  # CRLF errors: first line without the carriage return
    assert data.caveats.dead_tasks[1] == ("skeptic", "check", "")


def test_ut09_79_redaction_failure_shows_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT09-79 a failed redaction (None) never shows the raw text."""
    monkeypatch.setattr(_data_slots, "redact_text", lambda _text: None)
    assert _data_slots.redacted("secret") == "[redacted]"


def test_ut09_79_rationale_filled_unknown_kept(ops_store: object, wh: duckdb.DuckDBPyConnection,
                                               redactor: r.Redactor) -> None:  # fmt: skip
    """UT09-79 lever rationale placeholders filled from template_params, unknown kept."""
    data, _ = _load(_seed_ops(), _full_draft(), wh)
    (lever,) = data.levers
    assert lever.rationale == "Payments cuts MTTR from 4.5 to 3: {bogus}"
    assert lever.card_anchors == ("rec-1", "rec-2")
    assert lever.delta_usd.query_id == QL
    assert lever.unconfirmed is True
    assert (lever.entity_type, lever.entity_id, lever.target_kind) == ("team", "t1", "peer_median")


def test_ut09_79_every_warehouse_sql_has_limit() -> None:
    """UT09-79 every warehouse SQL constant is parameterised text with a LIMIT."""
    consts = {
        f"{mod.__name__}.{name}": value
        for mod in (_data, _data_slots)
        for name, value in vars(mod).items()
        if name.endswith("_SQL") and isinstance(value, str)
    }
    assert len(consts) >= 13
    for name, sql in consts.items():
        assert sql.startswith("SELECT"), name
        assert " LIMIT " in sql, name
        assert re.search(r"\bcore\.", sql) is None, name
        assert "{" not in sql, name


def test_ut09_79_plan_contents(ops_store: object, wh: duckdb.DuckDBPyConnection,
                               redactor: r.Redactor) -> None:  # fmt: skip
    """UT09-79 header, banners, sections, cards, tables, portfolio, scorecards, retro, caveats."""
    run = _seed_ops(status="partial")
    data, _ = _load(run, _full_draft(), wh, unconfirmed_keys=["cost.a"],
                    uncited=[UncitedHit("sections[1].paragraphs[0]", "99", 11, 13)])  # fmt: skip
    head = data.header
    assert head.data_as_of == datetime(2026, 9, 21, 9, 30, tzinfo=UTC)
    assert (head.run_id, head.build_id, head.rendered_at, head.gate2_passed) == (
        RUN_ID, "b1", _NOW, True)  # fmt: skip
    assert [s.text for s in head.title] == ["Funding review"]
    assert data.publishable is True
    assert [b.code for b in data.banners] == [
        "unconfirmed_weights", "dq_warnings", "partial_run", "hybrid_fallback"]  # fmt: skip
    assert list(data.sections) == list(SECTION_IDS)
    rec_block = data.sections["recommendations"][0]
    assert [s.kind for s in rec_block.segments] == ["text", "number", "text", "uncited", "text"]
    assert data.section_titles["executive_summary"] == "Summary"
    (card,) = data.cards
    assert (card.rank, card.kind, card.target_id) == (1, "fund", "epic-1")
    assert dict(card.extras)["priority"].text == "0.75"
    assert dict(card.extras)["confidence"].text == "high"
    assert dict(card.extras)["unconfirmed"].text == "yes"
    assert data.org_table is None
    table = data.funding_table
    assert table is not None
    assert table.marked == [True, False]
    assert table.rows[1][5].text == "—"
    assert table.rows[1][5].query_id is None
    assert table.rows[1][3].query_id is None
    custom, tight, base = data.portfolio_blocks  # score.portfolio by budget_usd
    assert (custom.scenario, custom.custom, custom.budget.text) == ("custom 900", True, "$900")
    assert [row[1].text for row in custom.table.rows] == ["epic-1", "epic-2"]
    assert (base.scenario, len(base.table.rows), tight.solver_status) == ("base", 1, "INFEASIBLE")
    assert all(b.chart_key in data.charts for b in data.portfolio_blocks)


def test_ut09_79_plan_contents_later_slots(ops_store: object, wh: duckdb.DuckDBPyConnection,
                                           redactor: r.Redactor) -> None:  # fmt: skip
    """UT09-79 scorecards, retrospective, caveats, method and run appendix."""
    data, _ = _load(_seed_ops(), _full_draft(), wh, unconfirmed_keys=["cost.a"])
    assert [(s.entity_type, s.entity_id) for s in data.scorecards] == [("team", "t1"),
                                                                      ("team", "t2")]  # fmt: skip
    t1 = data.scorecards[0]
    assert [row[0].text for row in t1.table.rows] == ["composite", "mttr"]
    assert t1.table.rows[1][8].text == "small"
    assert data.charts[t1.spark_keys[1]].startswith("<svg")
    retro = data.retro
    assert [i.rec_id for i in retro.items] == [_PRIOR_REC]
    assert (retro.items[0].decision, retro.verdict_counts, retro.empty_text) == (
        "accepted", {"paid_off": 1}, None)  # fmt: skip
    assert retro.items[0].outcomes.rows[0][4].query_id == QM
    assert [b.where for b in retro.blocks] == ["sections[2].paragraphs[0]"]
    cav = data.caveats
    assert [row[0].text for row in cav.dq_failed.rows] == ["incidents_service_null"]
    assert cav.unmapped_share == "0.125"
    assert (cav.unconfirmed_keys, cav.contested, cav.removed) == (
        ["cost.a"], [FINDING_ID], [("sections[9]", "gate2")])  # fmt: skip
    assert cav.flags == {"notes": ["n"]}
    assert [b.where for b in cav.blocks] == ["caveats[0]"]
    assert data.method.role_calls == {"analyst": 0, "writer": 2}
    app = data.run_appendix
    assert (app.tokens_input, app.tokens_output, app.config_hash) == (10, 5, "cfg1")
    assert app.task_counts == [("analyst", "done", 1)]
    assert app.ranked_entities == [(1, "candidate", "epic-1")]


def test_ut09_79_sparkline_uses_last_eight_months(
    ops_store: object, wh: duckdb.DuckDBPyConnection, redactor: r.Redactor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    """UT09-79 scorecard series: at most 8 monthly points per metric, oldest first."""
    seen: list[list[float | None]] = []
    real = charts.sparkline

    def spy(values: Sequence[float | None], *, title: str) -> str:
        seen.append(list(values))
        return real(values, title=title)

    monkeypatch.setattr(charts, "sparkline", spy)
    _load(_seed_ops(), _full_draft(), wh)
    assert [float(m) for m in range(3, 11)] in seen
    assert all(len(values) <= 8 for values in seen)


def test_ut09_79_org_review(ops_store: object, wh: duckdb.DuckDBPyConnection,
                            redactor: r.Redactor) -> None:  # fmt: skip
    """UT09-79 org review: org table (composite row wins), dot chart, ranked scorecards."""
    run = _seed_ops()
    org_rec = recommendation(
        1,
        kind="org_action",
        target_type="team",
        target_id="t1",
        numbers=[number_ref(query_id=QD), number_ref("n2", "usd", "1", QE)],
    )
    draft = _full_draft(kind="org_review", recommendations=[org_rec], portfolio_custom=[],
                        ranked_entities=[{"rank": 1, "entity_type": "team", "entity_id": "t2"},
                                         {"rank": 2, "entity_type": "candidate",
                                          "entity_id": "x"}])  # fmt: skip
    data, _ = _load(run, draft, wh, top_n=1)
    assert data.funding_table is None
    assert data.org_table is not None
    assert [[c.text for c in row] for row in data.org_table.rows] == [["t1", "0.90", "1"]]
    assert "t1 · mttr" in data.charts["org_z"]
    (card,) = data.cards
    assert (card.kind, card.extras) == ("org_action", ())
    assert [(s.entity_type, s.entity_id) for s in data.scorecards] == [("team", "t2")]


def test_ut09_79_findings_only(ops_store: object, wh: duckdb.DuckDBPyConnection,
                               redactor: r.Redactor) -> None:  # fmt: skip
    """UT09-79 / R-49 findings_only lists the run's verified findings under a fixed heading."""
    run = _seed_ops()
    draft = _full_draft(mode="findings_only", recommendations=[], sections=[
        {"id": "executive_summary", "title": "Summary", "paragraphs": []}])  # fmt: skip
    data, collector = _load(run, draft, wh)
    (block,) = data.sections["executive_summary"]
    assert block.where == f"findings[{FINDING_ID}]"
    assert [s.kind for s in block.segments] == ["text", "number"]
    assert data.section_titles["executive_summary"] == "Verified findings"
    assert collector.ordered_ids()[:2] == [QA, QB]
    assert data.banners[0].code == "dq_warnings"
    assert "findings_only" in [b.code for b in data.banners]
    assert data.cards == []


def test_ut09_79_empty_retrospective(ops_store: object, wh: duckdb.DuckDBPyConnection,
                                     redactor: r.Redactor) -> None:  # fmt: skip
    """UT09-79 no prior recommendation in the window gives the empty-state text; the draft's
    prior_outcomes_commentary follows the retrospective paragraphs."""
    commentary = paragraph("Prior work paid off [[n1]]", numbers=[number_ref(query_id=QB)])
    draft = _full_draft(prior_outcomes_commentary=commentary)
    data, collector = _load(_seed_ops(), draft, wh, outcomes_window_days=(0, 10))
    assert (data.retro.items, data.retro.empty_text) == ([], "No measured outcomes yet.")
    assert [b.where for b in data.retro.blocks] == [
        "sections[2].paragraphs[0]", "prior_outcomes_commentary"]  # fmt: skip
    assert "retrospective" in collector.used_by(QB)
    assert _data_slots.json_object("{not json") == {}
    assert _data_slots.json_object(["a"]) == {}


def test_ut09_79_bad_prior_summary_stays_text(ops_store: object, wh: duckdb.DuckDBPyConnection,
                                              redactor: r.Redactor) -> None:  # fmt: skip
    """UT09-79 a stored summary whose markers do not resolve is shown as plain text."""
    data, _ = _load(_seed_ops(), _full_draft(), wh, outcomes_window_days=(60, 200))
    old = next(i for i in data.retro.items if i.rec_id == _OLD_REC)
    assert [(s.kind, s.text) for s in old.summary] == [("text", "Old [[n9]]")]
    assert old.decision is None


@pytest.mark.parametrize(
    ("custom", "where"),
    [
        ({"budget_usd": None}, "portfolio_custom[0].budget_usd"),
        ({"scenario": 5}, "portfolio_custom[0].scenario"),
        ({"rows": [{"candidate_id": "c"}]}, "portfolio_custom[0].rows[0].selected"),
        ({"rows": [{"candidate_id": "c", "selected": True, "order_rank": 1,
                    "expected_impact_usd": "1"}]}, "portfolio_custom[0].query_ids"),
        ({"rows": []}, "portfolio_custom[0].query_ids"),
    ],
)  # fmt: skip
def test_ut09_79_malformed_portfolio_custom(ops_store: object, wh: duckdb.DuckDBPyConnection,
                                            redactor: r.Redactor, custom: dict[str, Any],
                                            where: str) -> None:  # fmt: skip
    """UT09-79 a malformed portfolio_custom entry raises ReportContractError at its key."""
    raw = {k: v for k, v in _custom(**custom).items() if v is not None}
    with pytest.raises(ReportContractError) as info:
        _load(_seed_ops(), _full_draft(portfolio_custom=[raw]), wh)
    assert info.value.details["where"] == where


def test_ut09_79_portfolio_result_shaped_custom(ops_store: object, wh: duckdb.DuckDBPyConnection,
                                                redactor: r.Redactor) -> None:  # fmt: skip
    """UT09-79 a PortfolioResult-shaped entry (impl 04 U04-77: ids on the result, none on the
    rows) links its cells to the entry's first id and registers every entry id."""
    result = {
        "scenario": "budget 900", "budget_usd": "900", "solver_status": "FEASIBLE",
        "rows": [
            {"candidate_id": "epic-1", "selected": True, "order_rank": 1,
             "expected_impact_usd": "700", "flags": []},
            {"candidate_id": "epic-2", "selected": False, "order_rank": None,
             "expected_impact_usd": "50", "flags": ["over_budget"]},
        ],
        "selected": ["epic-1"], "total_effort_usd": "1000", "total_expected_impact_usd": "700",
        "binding_constraints": ["budget"], "query_ids": [QH, QI], "flags": [],
    }  # fmt: skip
    data, collector = _load(_seed_ops(), _full_draft(portfolio_custom=[result]), wh)
    block = data.portfolio_blocks[0]
    assert (block.scenario, block.custom, block.solver_status) == ("budget 900", True, "FEASIBLE")
    assert [[c.text for c in row[:2]] for row in block.table.rows] == [["1", "epic-1"]]
    assert block.table.rows[0][2].query_id == QH
    ids = collector.ordered_ids()
    assert ids.index(QH) < ids.index(QI) < ids.index(QJ)
    assert collector.used_by(QI) == ["portfolio: budget 900"]


def test_ut09_79_duckdb_error_is_query_error(ops_store: object, redactor: r.Redactor) -> None:
    """UT09-79 a DuckDB error becomes QueryError naming the build."""
    with duckdb.connect(":memory:") as empty, pytest.raises(QueryError) as info:
        _load(_seed_ops(), _full_draft(), empty)
    assert info.value.details["build_id"] == "b1"
    assert "b1" in str(info.value)


def test_ut09_79_missing_watermarks_give_none(ops_store: object, redactor: r.Redactor,
                                              tmp_path: Path) -> None:  # fmt: skip
    """UT09-79 no meta.build row (or NULL watermarks) gives data_as_of None; no DQ banner."""
    with duckdb.connect(str(tmp_path / "w.duckdb")) as con:
        _seed_warehouse(con)
        con.execute("DELETE FROM meta.build")
        con.execute("DELETE FROM meta.dq_result")
        data, _ = _load(_seed_ops(), _full_draft(), con)
    assert data.header.data_as_of is None
    assert data.caveats.unmapped_share is None
    assert "dq_warnings" not in [b.code for b in data.banners]
