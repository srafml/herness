"""IT03-07: the deep-mode ensemble flow over real stores (U03-88, U03-89, F03-08, T03-22).

Laya rows (stage 3) → `resolve_frame` → `ensemble_band` → the OpenJev and LLM members' band
rows written through the cache writer (stages 4 and 6 stand-ins: the members "ran") →
`run_ensemble_pool` → `run_resolve` with ``versions["ensemble"]``, on a migrated tmp ops store.
Checks ensemble rows, ``agreement`` filled only for ensemble decisions, and disagreement items
at most the cap; logs and payloads carry no ticket text.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest
from structlog.testing import capture_logs
from tests.support.ops_store import OpsStoreHandle

from herness.core.types import Answer, DecisionOutput, Question, QuestionSet
from herness.enrich import ensemble_stage, resolve
from herness.enrich.cache import DecisionCache
from herness.enrich.calibrate import CalibrationStore
from herness.enrich.labels import LabelStore
from herness.enrich.layout import EnrichPaths
from herness.enrich.settings import DecidersSettings, DecisionsConfig
from herness.store.ops import list_review_items

pytestmark = pytest.mark.integration

_SETTINGS_SQL = Path(__file__).resolve().parents[3] / "herness/model/sql/000_settings.sql"
_QSV = "qs-2026-10-01.1"
_NOW = datetime(2026, 9, 27, 2, tzinfo=UTC)
_LAYA = ("laya", "laya-20260901-1")
_MEMBERS = (_LAYA, ("openjev", "v1"), ("llm", "v1"))
_VERSIONS = dict(_MEMBERS)
_OPTIONS = {"a": "Option A.", "b": "Option B.", "c": "Option C."}
_QS = QuestionSet(
    version=_QSV,
    questions=tuple(
        Question.model_validate({"id": qid, "type": qtype, "threshold": 0.5,
            "instructions": "Classify this ticket with care, please.",
            **({"options": _OPTIONS} if qtype == "choice" else {})})
        for qid, qtype in (("q_choice", "choice"), ("q_bool", "bool"))
    ),
)  # fmt: skip
_CFG = DecisionsConfig.model_validate({"question_set_version": _QSV, "questions": (),
    "change_link": {"use_decider": False}, "ensemble": {"disagreement_review_cap": 1}})  # fmt: skip
_SPLIT = {"laya": {"a": 0.6, "b": 0.2, "c": 0.2}, "openjev": {"a": 0.25, "b": 0.5, "c": 0.25},
          "llm": {"a": 0.3, "b": 0.3, "c": 0.4}}  # fmt: skip  # agreement 1/3
_AGREE = {"a": 0.7, "b": 0.15, "c": 0.15}
_SURE = {"a": 0.96, "b": 0.02, "c": 0.02}
_TRUE = {"true": 0.97, "false": 0.03}


@dataclass
class _Report:
    status: str = "done"
    note: str | None = None
    decided: int = 0
    escalated: int = 0


def _digest(n: int) -> str:
    return f"{n:032x}"


def _warehouse(n: int) -> duckdb.DuckDBPyConnection:
    wh = duckdb.connect()
    wh.execute(_SETTINGS_SQL.read_text("utf-8"))
    for entity in ("incident", "change", "problem"):
        wh.execute(f"CREATE TABLE core.{entity} (record_id VARCHAR, opened_at TIMESTAMPTZ)")
    for i in range(1, n + 1):
        wh.execute("INSERT INTO enrich.text_redacted VALUES (?, 'incident', ?, ?)",
                   [f"inc_{i}", f"secret body of inc_{i}", _digest(i)])  # fmt: skip
        wh.execute(
            "INSERT INTO core.incident VALUES (?, ?)", [f"inc_{i}", _NOW - timedelta(hours=i)]
        )
    return wh


def _write(cache: DecisionCache, member: tuple[str, str], rows: dict[int, dict[str, Any]]) -> None:
    """Cache rows of one member through its writer: {record n: {question: distribution}}."""
    outputs = [
        DecisionOutput.model_validate({"record_id": f"inc_{n}", "content_hash": _digest(n),
            "decider": member[0], "decider_version": member[1],
            "answers": {qid: Answer(answer=max(d, key=d.__getitem__),
                probability=max(d.values()), distribution=d) for qid, d in answers.items()}})
        for n, answers in rows.items()
    ]  # fmt: skip
    with cache.writer(*member, questions=_QS, flush_rows=2_000) as writer:
        writer.add(outputs, samples=None)


def test_it03_07_deep_mode_three_members(ops_store: OpsStoreHandle, tmp_path: Path) -> None:
    """IT03-07 deep mode, 3 members: ensemble rows, agreement only for ensemble, items <= cap."""
    del ops_store
    paths = EnrichPaths(data_root=tmp_path, embedding_path="data/e", laya_current_file="data/c")
    cache, labels, calibration = (DecisionCache(paths, _QSV), LabelStore(paths, _QSV),
                                  CalibrationStore(paths))  # fmt: skip
    wh = _warehouse(5)
    # Stage 3 (Laya): inc_1..inc_4 below the 0.90 band on q_choice; inc_5 sure; q_bool sure.
    laya = {
        n: {"q_choice": _SPLIT["laya"] if n <= 2 else _AGREE, "q_bool": _TRUE} for n in (1, 2, 3, 4)
    }
    _write(cache, _LAYA, {**laya, 5: {"q_choice": _SURE, "q_bool": _TRUE}})
    frame: dict[str, Any] = {"qs": _QS, "cfg": _CFG, "deciders": DecidersSettings(),
        "cache": cache, "labels": labels, "calibration": calibration,
        "primaries": {"q_choice": "laya", "q_bool": "laya"}, "now": _NOW}  # fmt: skip
    resolve.resolve_frame(wh, versions=_VERSIONS, **frame)
    band = ensemble_stage.ensemble_band(wh, cfg=_CFG, qs=_QS)
    assert [(i.record_id, i.question_ids) for i in band] == [
        (f"inc_{n}", ("q_choice",)) for n in (1, 2, 3, 4)]  # fmt: skip
    # Stages 4 and 6 (members over the band): inc_1, inc_2 split, inc_3, inc_4 agree.
    for member in _MEMBERS[1:]:
        rows = {n: {"q_choice": _SPLIT[member[0]] if n <= 2 else _AGREE} for n in (1, 2, 3, 4)}
        _write(cache, member, rows)
    report = _Report()
    gold = {(d, "q_choice"): acc for d, acc in (("laya", 0.8), ("openjev", 0.85), ("llm", 0.8))}
    with capture_logs() as logs:
        version = ensemble_stage.run_ensemble_pool(wh, band=band, qs=_QS, cfg=_CFG, cache=cache,
            calibration=calibration, members=_MEMBERS, gold_accuracy=gold, build_id="b-1",
            report=report)  # fmt: skip
    assert report.decided == 4
    versions = {**_VERSIONS, "ensemble": version}
    resolve.run_resolve(wh, build_id="b-1", run_started_at=_NOW + timedelta(hours=1),
                        report=report, versions=versions, **frame)  # fmt: skip
    got = wh.execute(
        "SELECT record_id, question, decider, decider_version, agreement, escalated "
        "FROM enrich.decision ORDER BY record_id, question"
    ).fetchall()
    ensemble = {(r[0], r[1]): r[4] for r in got if r[2] == "ensemble"}
    assert set(ensemble) == {(f"inc_{n}", "q_choice") for n in (1, 2, 3, 4)}
    assert all(r[3] == version for r in got if r[2] == "ensemble")
    assert ensemble[("inc_1", "q_choice")] == pytest.approx(1 / 3)
    assert ensemble[("inc_3", "q_choice")] == 1.0
    assert all(r[4] is None for r in got if r[2] != "ensemble")  # agreement: ensemble only
    assert {r[2] for r in got if r[2] != "ensemble"} == {"laya"}
    assert not any(r[5] for r in got)  # pooled argmax equals Laya's here
    match = {"purpose": "ensemble_disagreement", "question_set_version": _QSV}
    items = list_review_items(kind="label_check", status="pending", payload_match=match)
    assert len(items) == 1  # two disagreements (inc_1, inc_2), cap 1
    assert items[0].payload["content_hash"] in {_digest(1), _digest(2)}
    assert "secret body" not in json.dumps([logs, [i.payload for i in items]], default=str)
