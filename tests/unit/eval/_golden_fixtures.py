"""Shared fixtures for the golden-suite tests (U11-53 … U11-55, ST11-06, ST11-12).

An in-memory DuckDB warehouse with the `core` display-name tables and one `metrics`
table, plus a truth manifest built from the `test_truth` payload. Spec 11's
`tiny_build` fixture (T11-16) is not built yet, so these tests use this stand-in.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import yaml
from tests.unit.eval.test_truth import _truth_payload

from herness.eval.truth import TruthManifest

BUILD_ID = "20260925-101500-ABCDEF"
T1_TEAM = "servicenow:sys_user_group:9f1"
T3_TEAM = "servicenow:sys_user_group:9f2"
T5_TEAM = "servicenow:sys_user_group:9f3"

_DDL = (
    "CREATE SCHEMA core",
    "CREATE SCHEMA metrics",
    "CREATE TABLE core.team (team_id VARCHAR, name VARCHAR, active BOOLEAN)",
    "CREATE TABLE core.service (service_id VARCHAR, name VARCHAR)",
    "CREATE TABLE core.org (org_id VARCHAR, name VARCHAR)",
    "CREATE TABLE core.work_item (record_id VARCHAR, key VARCHAR)",
    "CREATE TABLE core.incident (record_id VARCHAR, team_id VARCHAR, priority INTEGER)",
    "CREATE TABLE metrics.team_mttr (team_id VARCHAR, org_name VARCHAR, mttr DOUBLE)",
)
_ROWS: tuple[tuple[str, list[tuple[object, ...]]], ...] = (
    (
        "INSERT INTO core.team VALUES (?, ?, true)",
        [(T1_TEAM, "Platform Ops L2"), (T3_TEAM, "Change Crew"), (T5_TEAM, "Payments Core")],
    ),
    (
        "INSERT INTO core.service VALUES (?, ?)",
        [("servicenow:cmdb_ci:5", "Ledger API"), ("servicenow:cmdb_ci:7", "Checkout")],
    ),
    ("INSERT INTO core.org VALUES (?, ?)", [("servicenow:cmn_department:1", "Retail")]),
    ("INSERT INTO core.work_item VALUES (?, ?)", [("jira:issue:20010", "CHK-2001")]),
    (
        "INSERT INTO core.incident VALUES (?, ?, ?)",
        [("i1", T1_TEAM, 1), ("i2", T1_TEAM, 2), ("i3", T5_TEAM, 1)],
    ),
    (
        "INSERT INTO metrics.team_mttr VALUES (?, ?, ?)",
        [(T1_TEAM, "Retail", 12.5), (T5_TEAM, "Finance", 8.0), (T3_TEAM, "Retail", 4.0)],
    ),
)

ENTITIES_SQL = "SELECT team_id, org_name FROM metrics.team_mttr ORDER BY mttr DESC, team_id LIMIT 3"
NUMERIC_SQL = "SELECT count(*) AS value FROM core.incident WHERE priority = 1"


def make_con() -> duckdb.DuckDBPyConnection:
    """A fresh in-memory fixture warehouse."""
    con = duckdb.connect(":memory:")
    for statement in _DDL:
        con.execute(statement)
    for statement, rows in _ROWS:
        con.executemany(statement, rows)
    return con


def truth() -> TruthManifest:
    """The seed-42 tiny truth manifest of `test_truth`."""
    return TruthManifest.model_validate(_truth_payload())


def question(qid: str = "G03", **fields: Any) -> dict[str, Any]:
    """A raw question dict with an entities block unless `expected` is given."""
    raw: dict[str, Any] = {
        "id": qid,
        "pipeline": "chat",
        "question": "Which team has the highest MTTR?",
        "expected": {"entities": {"reference_sql": ENTITIES_SQL, "check": "rank1"}},
    }
    raw.update(fields)
    return raw


def write_suite(path: Path, questions: list[dict[str, Any]], **top: Any) -> Path:
    """Write a version-3 suite file with default tolerance and datasets."""
    doc: dict[str, Any] = {
        "version": 3,
        "defaults": {"tolerance": {"rel": 0.01}, "datasets": ["synthetic", "real"]},
        "questions": questions,
    }
    doc.update(top)
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path
