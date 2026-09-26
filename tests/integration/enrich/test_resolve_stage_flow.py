"""IT03-06: the ``resolve`` stage over real stores (U03-83, flow F03-07, T03-20).

Decisions below the threshold with escalation rows, a pending ``label_check``, and an approved
one that `sync_label_checks` must fold in first. Checks ``enrich.decision`` (``escalated`` and
``review_status`` per design 03 §4.1), ``decision_wide``, spot-check items, the report
counters, the coverage and escalation-share gauges, and that logs and payloads carry no text.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pytest
from structlog.testing import capture_logs
from tests.support.ops_store import OpsStoreHandle

from herness.core.errors import ConfigError
from herness.core.types import Question, QuestionSet
from herness.enrich import resolve
from herness.enrich.cache import CACHE_SCHEMA, DecisionCache, write_part
from herness.enrich.calibrate import CalibrationStore
from herness.enrich.labels import LabelStore
from herness.enrich.layout import EnrichPaths
from herness.enrich.questions import question_fingerprint
from herness.enrich.settings import DecidersSettings, DecisionsConfig
from herness.store.ops import create_review_item, decide_review_item, list_review_items
from herness.store.ops import shared as ops_shared

pytestmark = pytest.mark.integration

_SETTINGS_SQL = Path(__file__).resolve().parents[3] / "herness/model/sql/000_settings.sql"
_QSV = "qs-2026-10-01.1"
_NOW = datetime(2026, 9, 26, 2, tzinfo=UTC)
_STARTED = _NOW - timedelta(hours=2)
_DECIDED = _NOW - timedelta(hours=1)
_LAYA_V = "laya-20260901-1"
_QB = Question.model_validate(
    {
        "id": "q_bool",
        "type": "bool",
        "threshold": 0.7,
        "applies_to": ("incident", "change"),
        "instructions": "Classify this ticket with care, please.",
    }
)
_QS = QuestionSet(version=_QSV, questions=(_QB,))
_FP = question_fingerprint(_QB)
_CFG = DecisionsConfig.model_validate(
    {
        "question_set_version": _QSV,
        "questions": (),
        "change_link": {"use_decider": False},
        "spot_check": {"nightly_rate": 1.0},
    }
)
# record, entity, content hash
_RECORDS = (
    ("inc_1", "incident", "h1"),  # laya above threshold
    ("inc_2", "incident", "h2"),  # laya below, openjev escalation row
    ("inc_3", "incident", "h3"),  # laya below, no escalation row: queued
    ("inc_4", "incident", "h4"),  # laya below, openjev row, pending label_check
    ("inc_5", "incident", "h5"),  # laya below, openjev row, human correction to sync
    ("chg_1", "change", "h6"),  # laya above threshold
)


@dataclass
class _Report:
    decided: int = 0
    escalated: int = 0


@pytest.fixture(autouse=True)
def _no_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    """`decide_review_item` audits under the caller's config; this test needs no audit."""

    def fake(event: str, actor: str, **fields: object) -> None:
        return None

    monkeypatch.setattr(ops_shared, "audit", fake)


@pytest.fixture
def gauges(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, float, dict[str, str]]]:
    seen: list[tuple[str, float, dict[str, str]]] = []

    def record(
        name: str, value: float, *, component: str, labels: dict[str, str] | None = None
    ) -> None:
        assert component == "enrich"
        seen.append((name, value, labels or {}))

    monkeypatch.setattr(resolve, "record_gauge", record)
    return seen


def _warehouse() -> duckdb.DuckDBPyConnection:
    wh = duckdb.connect()
    wh.execute(_SETTINGS_SQL.read_text("utf-8"))
    for entity in ("incident", "change", "problem"):
        wh.execute(f"CREATE TABLE core.{entity} (record_id VARCHAR, opened_at TIMESTAMPTZ)")
    for rid, entity, ch in _RECORDS:
        wh.execute(
            "INSERT INTO enrich.text_redacted VALUES (?, ?, ?, ?)",
            [rid, entity, f"secret body of {rid}", ch],
        )
        opened = _NOW - timedelta(days=10)
        wh.execute(f"INSERT INTO core.{entity} VALUES (?, ?)", [rid, opened])  # noqa: S608
    return wh


def _part(
    paths: EnrichPaths, decider: str, version: str, rows: list[tuple[str, str, float]]
) -> None:
    table = pa.Table.from_pylist(
        [
            {
                "content_hash": ch,
                "question": "q_bool",
                "question_fingerprint": _FP,
                "answer": answer,
                "probability": p,
                "distribution": [(answer, p), ("false" if answer == "true" else "true", 1 - p)],
                "backend_confidence": None,
                "samples": None,
                "decided_at": _DECIDED,
            }
            for ch, answer, p in rows
        ],
        schema=CACHE_SCHEMA,
    )
    write_part(paths.cache_partition(_QSV, decider, version), table)


def _payload(ch: str, rid: str, purpose: str) -> dict[str, object]:
    return {
        "record_id": rid, "content_hash": ch, "question": "q_bool", "question_fingerprint": _FP,
        "question_set_version": _QSV, "answer": "true", "probability": 0.8, "decider": "openjev",
        "decider_version": "v1", "purpose": purpose, "text_ref": "enrich.text_redacted",
    }  # fmt: skip


@pytest.fixture
def run(ops_store: OpsStoreHandle, tmp_path: Path) -> Callable[..., _Report]:
    paths = EnrichPaths(data_root=tmp_path, embedding_path="data/e", laya_current_file="data/c")
    cache, labels = DecisionCache(paths, _QSV), LabelStore(paths, _QSV)
    _part(paths, "laya", _LAYA_V, [
        ("h1", "true", 0.9), ("h2", "true", 0.6), ("h3", "true", 0.6), ("h4", "true", 0.6),
        ("h5", "true", 0.6), ("h6", "true", 0.95),
    ])  # fmt: skip
    _part(paths, "openjev", "v1", [("h2", "false", 0.55), ("h4", "true", 0.8), ("h5", "true", 0.8)])
    create_review_item("label_check", _payload("h4", "inc_4", "gold"), now=_NOW)
    item = create_review_item("label_check", _payload("h5", "inc_5", "spot_check"), now=_NOW)
    note = json.dumps({"answer": "false"})
    decide_review_item(item, "approved", decided_by="a" * 32, note=note, now=_NOW)

    def go(wh: duckdb.DuckDBPyConnection, **kw: Any) -> _Report:
        report = _Report()
        args: dict[str, Any] = {
            "qs": _QS, "cfg": _CFG, "build_id": "b-1", "run_started_at": _STARTED,
            "report": report, "deciders": DecidersSettings(), "cache": cache, "labels": labels,
            "calibration": CalibrationStore(paths), "primaries": {"q_bool": "laya"},
            "versions": {"laya": _LAYA_V, "openjev": "v1", "llm": "v1"}, "now": _NOW,
        }  # fmt: skip
        resolve.run_resolve(wh, **{**args, **kw})
        return report

    return go


def _decisions(wh: duckdb.DuckDBPyConnection) -> dict[str, tuple[Any, ...]]:
    rows = wh.execute(
        "SELECT record_id, answer, decider, decider_version, escalated, review_status, "
        "probability, agreement, question_set_version, content_hash, decided_at "
        "FROM enrich.decision"
    ).fetchall()
    return {r[0]: r[1:] for r in rows}


def _spot_items() -> list[dict[str, object]]:
    match = {"purpose": "spot_check", "question_set_version": _QSV}
    items = list_review_items(kind="label_check", status="pending", payload_match=match)
    return [dict(i.payload) for i in items]


def test_it03_06_escalated_and_review_status_columns(
    run: Callable[..., _Report], gauges: list[tuple[str, float, dict[str, str]]]
) -> None:
    """IT03-06 decisions below threshold with escalation rows: columns per design 03 §4.1."""
    wh = _warehouse()
    with capture_logs() as logs:
        report = run(wh)
    got = _decisions(wh)
    assert set(got) == {"inc_1", "inc_2", "inc_4", "inc_5", "chg_1"}  # inc_3 is queued
    assert got["inc_1"][:5] == ("true", "laya", _LAYA_V, False, "none")
    assert got["inc_2"][:5] == ("false", "openjev", "v1", True, "none")
    assert got["inc_2"][5] == pytest.approx(0.55)  # the escalation decider's confidence
    assert got["inc_4"][:5] == ("true", "openjev", "v1", True, "pending")
    assert got["inc_5"][:6] == ("false", "human", "human", False, "corrected", 1.0)
    assert got["inc_5"][9] == _NOW  # the label time
    assert got["chg_1"][:5] == ("true", "laya", _LAYA_V, False, "none")
    assert all(row[6] is None for row in got.values())  # agreement: ensemble only
    assert {row[7] for row in got.values()} == {_QSV}
    assert got["inc_1"][8:] == ("h1", _DECIDED)  # decided_at is the cache row time
    wide = wh.execute("SELECT * FROM enrich.decision_wide ORDER BY record_id").fetchall()
    assert [(r[0], r[1]) for r in wide] == [
        ("chg_1", "true"), ("inc_1", "true"), ("inc_2", "false"), ("inc_4", "true"),
        ("inc_5", "false"),
    ]  # fmt: skip
    assert (report.decided, report.escalated) == (5, 2)
    assert sorted(gauges) == [
        ("herness_enrich_coverage_ratio", 0.8, {"entity": "incident"}),
        ("herness_enrich_coverage_ratio", 1.0, {"entity": "change"}),
        ("herness_enrich_escalation_share_ratio", 0.4, {}),
    ]
    spot = _spot_items()
    assert sorted(str(p["record_id"]) for p in spot) == ["chg_1", "inc_1", "inc_2", "inc_4"]
    events = {e["event"]: e for e in logs}
    done = events["enrich.resolve.completed"]
    assert (done["decided"], done["escalated"]) == (5, 2)
    assert done["coverage"] == {"change": 1.0, "incident": 0.8}
    created = events["enrich.spot_check.created"]
    assert (created["question"], created["count"], created["purpose"]) == (
        "q_bool",
        4,
        "spot_check",
    )
    dump = json.dumps([logs, spot], default=str)
    assert "secret body" not in dump  # TH03-03: no text in logs or payloads


def test_it03_06_rerun_is_idempotent_for_spot_checks(run: Callable[..., _Report]) -> None:
    """IT03-06 a second build: same decisions, no duplicate spot-check items."""
    first = _warehouse()
    run(first)
    second = _warehouse()
    with capture_logs() as logs:
        run(second)
    before, after = _decisions(first), _decisions(second)
    assert {k: v[:4] for k, v in before.items()} == {k: v[:4] for k, v in after.items()}
    assert after["inc_1"][4] == "pending"  # the spot-check item now opened on it
    assert len(_spot_items()) == 4
    created = next(e for e in logs if e["event"] == "enrich.spot_check.created")
    assert created["count"] == 0


def test_it03_06_bad_question_id_writes_nothing(run: Callable[..., _Report]) -> None:
    """IT03-06 a smuggled question id fails before any write (TH03-19)."""
    bad = QuestionSet.model_construct(
        version=_QSV, questions=(_QB.model_copy(update={"id": 'a"; DROP'}),)
    )
    wh = _warehouse()
    with pytest.raises(ConfigError, match="decision_wide_sql"):
        run(wh, qs=bad)
    assert wh.execute("SELECT count(*) FROM enrich.decision").fetchone() == (0,)
    assert _spot_items() == []
