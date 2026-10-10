"""Renderer test environment (impl 09 T09-11): fixture `report_env`, `ReportEnv`, seed data.

`report_env` gives each test a fresh migrated ops store (`ops_store`), a copy of the T11-17
`tiny_build` warehouse under the fixed build id `BUILD_ID`, the seeded ops rows the committed
drafts `tests/fixtures/reports/draft_funding.json` and `draft_org.json` cite, and those drafts
written as `<data>/reports/<run_id>/draft.json`. Titles and error lines pass a fixed-key test
redactor.

Deviation (recorded in the T09-11 report): `tiny_build` runs T02-22's fake enrich/score hooks,
so its warehouse has no `score.*` tables and no `metrics.metric_value`; its `meta.build`,
`meta.dq_result` and `meta.evidence` rows carry the build time (future-timestamp DQ values,
`executed_at`, build-id-keyed query ids). `prepare_warehouse` therefore copies the build file
under `BUILD_ID`, points `meta.build` at that id, pins `meta.dq_result` to fixed rows and adds
a small fixed score layer (impl 04 table shapes) and two `meta.evidence` rows, so goldens are
byte-stable across cold builds, days and machines. The tiny build's own tables stay as built.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import duckdb
import pytest
from tests.support.builds import BuildHandle

from herness.core import ids
from herness.core import redact as r
from herness.core import time as clock
from herness.core.redact_directory import NameDirectory
from herness.core.settings import RedactionConfig
from herness.core.types import ReportManifest
from herness.store import ops
from herness.store.ops import core

__all__ = [
    "BUILD_ID",
    "FIXTURES",
    "FO_RUN",
    "FUND_RUN",
    "GOLDEN",
    "NOW",
    "OPS_EVIDENCE",
    "ORG_RUN",
    "REPO",
    "WH_EVIDENCE",
    "Evidence",
    "ReportEnv",
    "fixture_draft",
    "insert_evidence",
    "install_redactor",
    "number",
    "prepare_warehouse",
    "report_env",
    "report_warehouse",
    "seed_ops",
]  # fmt: skip

REPO: Final = Path(__file__).resolve().parents[2]
FIXTURES: Final = REPO / "tests" / "fixtures" / "reports"
GOLDEN: Final = FIXTURES / "golden"
BUILD_ID: Final = "20260926-100000-000001"  # digits only: no PII-scan PHONE hit
T0: Final = datetime(2026, 9, 26, 10, 0, tzinfo=UTC)
NOW: Final = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


def _ulid(n: int) -> str:
    return f"01JA{n:022d}"


PRIOR_RUN, FUND_RUN, ORG_RUN, FO_RUN = (f"run_{_ulid(n)}" for n in range(4))
F1, F2, F3, F4, F5 = (f"fnd_{_ulid(n)}" for n in range(11, 16))
REC1, REC2, REC3, PRIOR_REC = (f"rec_{_ulid(n)}" for n in range(21, 25))
_EXECUTED: Final = "2026-09-26T09:30:00.000000Z"


@dataclasses.dataclass(frozen=True, slots=True)
class Evidence:
    """One ops `evidence` row; its query id recomputes from sql, params and `BUILD_ID`."""

    sql: str
    params: dict[str, object]
    sample: list[dict[str, str]]

    @property
    def qid(self) -> str:
        return ids.query_id(self.sql, self.params, BUILD_ID)


OPS_EVIDENCE: Final = (
    Evidence("SELECT count(*) AS n FROM core.incident WHERE team_id = $team", {"team": "t1"},
             [{"team": "t1", "n": "42"}]),
    Evidence("SELECT effort_cost_usd FROM score.funding WHERE candidate_id = $c",
             {"c": "epic-1"}, [{"candidate_id": "epic-1", "effort_cost_usd": "1200.50"}]),
    Evidence("SELECT entity_id, value FROM score.org WHERE metric = $m AND entity_id = $e",
             {"m": "mttr", "e": "t2"}, [{"entity_id": "<b onmouseover=x>", "value": "4.5"}]),
    Evidence("SELECT count(*) AS n FROM core.change WHERE outcome = $o", {"o": "failed"},
             [{"n": "7"}]),
    Evidence("SELECT count(*) AS n FROM core.incident WHERE priority <= $p", {"p": 2}, []),
)  # fmt: skip
WH_EVIDENCE: Final[Mapping[str, tuple[str, str]]] = {
    "q_00000000000000f1": ("SELECT * FROM score.funding ORDER BY rank",
                           '[{"candidate_id": "epic-1", "rank": "1"}]'),
    "q_00000000000000f2": ("SELECT * FROM score.org ORDER BY rank", '[{"entity_id": "t1"}]'),
}  # fmt: skip
QW1, QW2 = WH_EVIDENCE

_SCORE_DDL: Final = (
    "CREATE SCHEMA IF NOT EXISTS score", "CREATE SCHEMA IF NOT EXISTS metrics",
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
_ORG_ROWS: Final = (
    ("team", "t1", "composite", None, "team:all", None, None, 5, 0.75, 1, [], [QW2]),
    ("team", "t1", "mttr", 4.5, "team:all", 3.0, 1.25, 12, 0.75, 1, ["small"], [QW2]),
    ("team", "t2", "mttr", 2.0, "team:all", 3.0, -0.5, 9, 0.25, 2, [], [QW2]),
)  # fmt: skip


def prepare_warehouse(source: Path, dest: Path) -> None:
    """Copy ``source`` to ``dest`` and pin it to ``BUILD_ID`` with the fixed rows (module doc)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, dest)
    with duckdb.connect(str(dest)) as con:
        marks = json.dumps({"servicenow": "2026-09-25T08:00:00Z", "jira": "2026-09-25T09:30:00Z"})
        con.execute("UPDATE meta.build SET build_id = ?, source_watermarks = ?", [BUILD_ID, marks])
        con.execute("DELETE FROM meta.dq_result")
        con.execute(
            "INSERT INTO meta.dq_result (check_name, severity, value, threshold, passed)"
            " VALUES ('incidents_service_null', 'warn', 0.125, 0.1, false),"
            " ('rows_ok', 'error', 0, 0, true)"
        )
        for ddl in _SCORE_DDL:
            con.execute(ddl)
        _seed_scores(con)
        for qid, (sql, sample) in WH_EVIDENCE.items():
            con.execute(
                "INSERT INTO meta.evidence (query_id, sql, params, result_hash, row_count,"
                " result_sample, executed_at, producer) VALUES (?, ?, '{}', ?, 1, ?,"
                " TIMESTAMPTZ '2026-09-26 09:00:00+00', 'scoring')",
                [qid, sql, "0" * 64, sample],
            )
        con.execute("CHECKPOINT")


def _seed_scores(con: duckdb.DuckDBPyConnection) -> None:
    con.executemany(
        "INSERT INTO score.funding VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [("epic-1", "Cache rewrite", 0.8, "1200.50", 0.75, 2.5, 1, True, [QW1]),
         ("epic-2", "Queue retry", None, "500.00", 0.5, 1.0, 2, False, [QW1])],
    )  # fmt: skip
    con.executemany("INSERT INTO score.org VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", list(_ORG_ROWS))
    con.executemany(
        "INSERT INTO score.portfolio VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [("base", "2000.00", "epic-1", True, 1, "3000.00", "OPTIMAL", [QW1]),
         ("base", "2000.00", "epic-2", False, None, "100.00", "OPTIMAL", [QW1])],
    )  # fmt: skip
    params = json.dumps({"entity_name": "Payments", "current_value": 4.5, "target_value": 3})
    con.execute(
        "INSERT INTO score.action_lever VALUES ('team', 't1', 'mttr', 'peer_median', 1500.00,"
        " '{entity_name} cuts MTTR from {current_value} to {target_value}', ?, true, ?)",
        [params, [QW2]],
    )
    series = [("mttr", "team", "t1", "month", date(2026, m, 1), float(m)) for m in range(1, 10)]
    series += [("mttr", "team", "t2", "month", date(2026, m, 1), 2.0) for m in range(6, 10)]
    con.executemany("INSERT INTO metrics.metric_value VALUES (?, ?, ?, ?, ?, ?)", series)


# --- ops rows -------------------------------------------------------------------------------


def _write(sql: str, rows: Sequence[Sequence[object]]) -> None:
    def fn(conn: sqlite3.Connection) -> None:
        conn.executemany(sql, rows)

    core.run_write(fn, op="test_seed")


def _ts(dt: datetime) -> str:
    return clock.format_utc(dt)


def number(ref_id: str, qid: str, value: object = 42, unit: str = "count") -> dict[str, Any]:
    """One ``NumberRef`` dict."""
    return {"id": ref_id, "value": value, "unit": unit, "query_id": qid, "column": "n",
            "row_key": {"team": "t1"}}  # fmt: skip


def _runs() -> None:
    usage = json.dumps({"input": 1200, "output": 340,
                        "by_role": {"analyst": {"calls": 3}, "writer": {"calls": 1}}})  # fmt: skip
    rows = [(FUND_RUN, "funding_review", "done", T0, usage, "0.42"),
            (ORG_RUN, "org_review", "partial", T0, usage, "0.40"),
            (FO_RUN, "funding_review", "done", T0, usage, "0.10"),
            (PRIOR_RUN, "funding_review", "done", T0 - timedelta(days=100), "{}",
             None)]  # fmt: skip
    _write(
        "INSERT INTO run (run_id, kind, depth, profile, config_hash, build_id, status,"
        " started_at, token_usage, cost_usd) VALUES (?,?,'standard','local',"
        " 'cfg_00000000000000aa',?,?,?,?,?)",
        [(run, kind, BUILD_ID, status, _ts(at), use, cost)
         for run, kind, status, at, use, cost in rows],
    )  # fmt: skip
    _write(
        "INSERT INTO task (task_id, run_id, role, spec, status, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?)",
        [(f"tsk_{n}", run, role, json.dumps({"dedup_key": f"k{n}", "objective": "o"}), "done",
          _ts(T0), _ts(T0))
         for n, (run, role) in enumerate([(FUND_RUN, "analyst"), (FUND_RUN, "analyst"),
                                          (FUND_RUN, "writer"), (ORG_RUN, "analyst")])],
    )  # fmt: skip


def _findings() -> None:
    e1, _e2, _e3, e4, e5 = (e.qid for e in OPS_EVIDENCE)
    claims = [(F1, FUND_RUN, "Incidents reached [[n1]] this quarter", e1, [e4]),
              (F2, ORG_RUN, "Team mean time to repair is [[n1]] hours", e1, [e5]),
              (F3, FO_RUN, "Incidents reached [[n1]] this quarter", e1, [e4]),
              (F4, FO_RUN, "Failed changes numbered [[n1]]", e4, [e4]),
              (F5, FO_RUN, "High priority incidents numbered [[n1]]", e5, [e5])]  # fmt: skip
    _write(
        "INSERT INTO finding (finding_id, run_id, task_id, author_role, claim, entity_type,"
        " entity_id, numbers, query_ids, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(fid, run, "tsk_0", "analyst", claim, "team", "t1", json.dumps([number("n1", qid)]),
          json.dumps(qids), "verified", _ts(T0)) for fid, run, claim, qid, qids in claims],
    )  # fmt: skip


def _recommendations() -> None:
    e1 = OPS_EVIDENCE[0].qid
    nums = json.dumps([number("n1", e1)])
    rows = [(REC1, FUND_RUN, "epic", "epic-1", "Fund it", "fund", T0),
            (REC2, FUND_RUN, "epic", "epic-2", "Fund it too", "fund", T0),
            (REC3, ORG_RUN, "team", "t2", "Coach the team", "org_action", T0),
            (PRIOR_REC, PRIOR_RUN, "epic", "epic-9", "Saved [[n1]] incidents", "fund",
             T0 - timedelta(days=90))]  # fmt: skip
    _write(
        "INSERT INTO recommendation (rec_id, run_id, target_type, target_id, summary, kind,"
        " numbers, created_at) VALUES (?,?,?,?,?,?,?,?)",
        [(*row[:6], nums, _ts(row[6])) for row in rows],
    )
    at = _ts(T0 - timedelta(days=80))
    _write(
        "INSERT INTO decision_log (rec_id, decision, decided_by, decided_at, effective_at)"
        " VALUES (?,?,?,?,?)",
        [(PRIOR_REC, "accepted", "u1", at, at)],
    )
    _write(
        "INSERT INTO outcome (outcome_id, rec_id, measurement, measured_at, metric, baseline,"
        " actual, delta, query_id, verdict) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [("out_1", PRIOR_REC, 1, _ts(T0 - timedelta(days=20)), "mttr", 4.0, 3.0, -1.0,
          OPS_EVIDENCE[3].qid, "paid_off")],
    )  # fmt: skip


def seed_ops() -> None:
    """The runs, tasks, findings, recommendations and evidence rows the fixture drafts cite."""
    _runs()
    _findings()
    _recommendations()
    insert_evidence(OPS_EVIDENCE)


def insert_evidence(evidence: Sequence[Evidence]) -> None:
    """Ops ``evidence`` rows of ``FUND_RUN`` on ``BUILD_ID`` (trusted: ids recompute)."""
    _write(
        "INSERT INTO evidence (query_id, run_id, build_id, sql, result_hash, params,"
        " result_sample, row_count, duration_ms, executed_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(e.qid, FUND_RUN, BUILD_ID, e.sql, "0" * 64, json.dumps(e.params),
          json.dumps(e.sample), len(e.sample), 12, _EXECUTED) for e in evidence],
    )  # fmt: skip


# --- fixtures ---------------------------------------------------------------------------------


def install_redactor(patch: pytest.MonkeyPatch | None = None) -> r.Redactor:
    """A process redactor with a fixed key (no config secret, deterministic output)."""
    directory = NameDirectory.from_files(None, ("Jane Doe",), None)
    red = r.Redactor(RedactionConfig(directory_file=None), bytes(range(32)), directory)
    if patch is None:
        r._State.redactor = red
    else:
        patch.setattr(r._State, "redactor", red)
    return red


def fixture_draft(name: str) -> dict[str, Any]:
    """A committed draft fixture (``funding`` or ``org``) as a dict."""
    text = (FIXTURES / f"draft_{name}.json").read_text(encoding="utf-8")
    loaded: dict[str, Any] = json.loads(text)
    return loaded


@dataclasses.dataclass(frozen=True, slots=True)
class ReportEnv:
    """The data root, the build file and helpers of one render test."""

    data_root: Path
    warehouse: Path

    @property
    def reports_root(self) -> Path:
        return self.data_root / "reports"

    def write_draft(self, draft: Mapping[str, Any]) -> Path:
        """Write ``draft`` as ``<data>/reports/<run_id>/draft.json``."""
        path = self.reports_root / str(draft["run_id"]) / "draft.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((json.dumps(draft, indent=2) + "\n").encode())
        return path

    def render(self, run_id: str, formats: Sequence[str] = ("html", "md"),
               **kwargs: Any) -> ReportManifest:  # fmt: skip
        """``render_run`` with the fixed ``NOW`` unless ``now`` is given."""
        from herness.reports.render import render_run  # noqa: PLC0415 - after config setup

        kwargs.setdefault("now", NOW)
        return render_run(run_id, formats, **kwargs)  # type: ignore[arg-type]

    def out(self, run_id: str, name: str) -> bytes:
        return (self.reports_root / run_id / name).read_bytes()


@pytest.fixture(scope="session")
def report_warehouse(tiny_build: BuildHandle, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The prepared warehouse file, built once per session from ``tiny_build``."""
    dest = tmp_path_factory.mktemp("report_wh") / f"wh-{BUILD_ID}.duckdb"
    prepare_warehouse(tiny_build.warehouse, dest)
    return dest


@pytest.fixture
def report_env(
    ops_store: Any, report_warehouse: Path, monkeypatch: pytest.MonkeyPatch
) -> ReportEnv:
    """Seeded ops store, the warehouse copy and both fixture drafts (module doc)."""
    install_redactor(monkeypatch)
    data_root = Path(ops_store.data_root)
    wh = data_root / "warehouse" / f"wh-{BUILD_ID}.duckdb"
    wh.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(report_warehouse, wh)
    seed_ops()
    env = ReportEnv(data_root, wh)
    for name in ("funding", "org"):
        env.write_draft(fixture_draft(name))
    assert ops.get_run(FUND_RUN) is not None
    return env
