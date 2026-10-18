"""Nightly delta scoping of the resolve stage (UT03-143 ... UT03-148, T03-36 carry-over w50-s11).

Each test runs real `run_resolve` calls on file-backed warehouses (`wh-<build>.duckdb`) over a
real decision cache, label store and ops store. The reference of every delta night is the same
night resolved in full (`delta=None`) on a copy of the build: `enrich.decision` must be equal.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pytest
from tests.support.ops_store import OpsStoreHandle

from herness.core.types import Question, QuestionSet
from herness.enrich import resolve
from herness.enrich._resolve_delta import DELTA_STAGES, STATE_NAME, ResolveDelta
from herness.enrich.cache import CACHE_SCHEMA, DecisionCache, write_part
from herness.enrich.calibrate import CalibrationResult, CalibrationStore
from herness.enrich.labels import HUMAN_SCHEMA, LabelStore
from herness.enrich.layout import EnrichPaths
from herness.enrich.pipeline import StageReport
from herness.enrich.questions import question_fingerprint
from herness.enrich.settings import DecidersSettings, DecisionsConfig
from herness.store.ops import create_review_item

pytestmark = pytest.mark.unit

_SETTINGS_SQL = Path(__file__).resolve().parents[3] / "herness/model/sql/000_settings.sql"
_QSV = "qs-2026-10-01.1"
_NOW = datetime(2026, 9, 26, 2, tzinfo=UTC)
_DECIDED = _NOW - timedelta(hours=1)
_LAYA_V = "laya-20260901-1"
_QB = Question.model_validate(
    {"id": "q_bool", "type": "bool", "threshold": 0.7, "applies_to": ("incident", "change"),
     "instructions": "Classify this ticket with care, please."}
)  # fmt: skip
_QC = Question.model_validate(
    {"id": "q_choice", "type": "choice", "threshold": 0.6, "applies_to": ("incident",),
     "options": {"a": "option a", "b": "option b"},
     "instructions": "Pick the best option for this ticket."}
)  # fmt: skip
_QS = QuestionSet(version=_QSV, questions=(_QB, _QC))
_FP = {q.id: question_fingerprint(q) for q in _QS.questions}
_CFG = DecisionsConfig.model_validate(
    {"question_set_version": _QSV, "questions": (), "change_link": {"use_decider": False},
     "spot_check": {"nightly_rate": 0.0}}
)  # fmt: skip
_PRIMARIES = {"q_bool": "laya", "q_choice": "laya"}
_VERSIONS = {"laya": _LAYA_V, "openjev": "v1", "llm": "v1"}
_DECISION_SQL = "SELECT * FROM enrich.decision ORDER BY record_id, question"


@dataclass
class _Night:
    wh: duckdb.DuckDBPyConnection
    delta: ResolveDelta
    report: StageReport
    path: Path
    rows: list[tuple[Any, ...]] = field(default_factory=list)

    @property
    def scope(self) -> set[str]:
        """Record ids the frame resolved (the last `enrich_scope`)."""
        return {r[0] for r in self.wh.execute("SELECT record_id FROM enrich_scope").fetchall()}


@dataclass
class _World:
    tmp: Path
    paths: EnrichPaths
    cache: DecisionCache
    labels: LabelStore
    calibration: CalibrationStore
    versions: dict[str, str] = field(default_factory=lambda: dict(_VERSIONS))
    records: dict[str, list[tuple[str, str, str]]] = field(default_factory=dict)
    open_builds: dict[str, duckdb.DuckDBPyConnection] = field(default_factory=dict)

    def part(self, decider: str, version: str, rows: list[tuple[str, str, str, float]]) -> None:
        """Cache rows (hash, question, answer, p of the answer) of one partition."""
        table = pa.Table.from_pylist(
            [{"content_hash": ch, "question": q, "question_fingerprint": _FP[q], "answer": a,
              "probability": p, "distribution": _dist(q, a, p), "backend_confidence": None,
              "samples": None, "decided_at": _DECIDED} for ch, q, a, p in rows],
            schema=CACHE_SCHEMA,
        )  # fmt: skip
        write_part(self.paths.cache_partition(_QSV, decider, version), table)

    def build(
        self, build_id: str, records: list[tuple[str, str, str]]
    ) -> duckdb.DuckDBPyConnection:
        """A writable build file with the given (record, entity, hash) text rows."""
        self.records[build_id] = records
        wh = self.open_builds[build_id] = duckdb.connect(str(self.tmp / f"wh-{build_id}.duckdb"))
        wh.execute(_SETTINGS_SQL.read_text("utf-8"))
        for entity in ("incident", "change", "problem"):
            wh.execute(f"CREATE TABLE core.{entity} (record_id VARCHAR, opened_at TIMESTAMPTZ)")
        for rid, entity, ch in records:
            wh.execute("INSERT INTO enrich.text_redacted VALUES (?, ?, ?, ?)",
                       [rid, entity, f"body of {rid}", ch])  # fmt: skip
            wh.execute(f"INSERT INTO core.{entity} VALUES (?, ?)",  # noqa: S608
                       [rid, _NOW - timedelta(days=5)])  # fmt: skip
        return wh

    def resolve(
        self, wh: duckdb.DuckDBPyConnection, build_id: str, prev: str | None, **kw: Any
    ) -> _Night:
        """`run_resolve` as the pipeline calls it, then the state commit."""
        delta = kw.pop("delta", None) or ResolveDelta.for_call(
            None if prev is None else self.tmp / f"wh-{prev}.duckdb",
            "standard", DELTA_STAGES, fresh=True)  # fmt: skip
        report = StageReport()
        if prev in self.open_builds:  # the previous build file is closed by the time of a night
            self.open_builds.pop(prev).close()
        resolve.run_resolve(
            wh, qs=_QS, cfg=_CFG, build_id=build_id, run_started_at=_NOW - timedelta(hours=2),
            report=report, deciders=DecidersSettings(), cache=self.cache, labels=self.labels,
            calibration=self.calibration, primaries=_PRIMARIES, versions=self.versions,
            now=_NOW, delta=delta, **kw)  # fmt: skip
        rows = wh.execute(_DECISION_SQL).fetchall()
        delta.commit(wh, self.cache, build_id)
        return _Night(wh, delta, report, self.tmp / f"wh-{build_id}.duckdb", rows)

    def full_reference(self, night: _Night, build_id: str) -> _Night:
        """The same night resolved in full (`delta=None`) on a build of its own."""
        wh = self.build(f"{build_id}-full", self.records[build_id])
        report = StageReport()
        resolve.run_resolve(
            wh, qs=_QS, cfg=_CFG, build_id=build_id, run_started_at=_NOW - timedelta(hours=2),
            report=report, deciders=DecidersSettings(), cache=self.cache, labels=self.labels,
            calibration=self.calibration, primaries=_PRIMARIES, versions=self.versions,
            now=_NOW)  # fmt: skip
        return _Night(wh, ResolveDelta(), report, self.tmp, wh.execute(_DECISION_SQL).fetchall())


def _dist(question: str, answer: str, p: float) -> list[tuple[str, float]]:
    if question == "q_bool":
        return [(answer, p), ("false" if answer == "true" else "true", 1 - p)]
    return [(answer, p), ("b" if answer == "a" else "a", 1 - p)]


@pytest.fixture
def world(ops_store: OpsStoreHandle, tmp_path: Path) -> _World:
    paths = EnrichPaths(data_root=tmp_path, embedding_path="data/e", laya_current_file="data/c")
    return _World(tmp_path, paths, DecisionCache(paths, _QSV), LabelStore(paths, _QSV),
                  CalibrationStore(paths))  # fmt: skip


_NIGHT1 = [("i1", "incident", "h1"), ("i2", "incident", "h2"), ("i3", "incident", "h3"),
           ("c1", "change", "h4")]  # fmt: skip


def _prime(w: _World) -> _Night:
    """Night 1 of every test: h1/h4 confident, h2 below the gate (queued), h3 confident."""
    w.part("laya", _LAYA_V, [
        ("h1", "q_bool", "true", 0.9), ("h1", "q_choice", "a", 0.9),
        ("h2", "q_bool", "true", 0.6), ("h2", "q_choice", "a", 0.9),
        ("h3", "q_bool", "false", 0.9), ("h3", "q_choice", "b", 0.9),
        ("h4", "q_bool", "true", 0.95),
    ])  # fmt: skip
    return w.resolve(w.build("n1", _NIGHT1), "n1", None)


def _same(delta: _Night, full: _Night) -> None:
    assert delta.rows == full.rows
    assert (delta.report.decided, delta.report.escalated) == (
        full.report.decided, full.report.escalated)  # fmt: skip


def test_ut03_143_first_build_resolves_everything_and_stores_state(world: _World) -> None:
    """UT03-143 no previous warehouse: every text row is resolved; the state file is written."""
    night = _prime(world)
    assert night.scope == {"i1", "i2", "i3", "c1"}
    assert night.delta.active is False
    assert night.delta.reason == "not_eligible"
    assert len(night.rows) == 6  # i2/q_bool is queued
    state = json.loads((world.cache.directory / STATE_NAME).read_text("utf-8"))
    assert state["build_id"] == "n1"
    assert state["rows"] == 6
    assert len(state["parts"]) == 1


def test_ut03_144_new_and_changed_records_only(world: _World) -> None:
    """UT03-144 a new and a changed record are resolved; untouched ones are carried over, and
    `enrich.decision`, the counters and the coverage equal a full resolve of the same night."""
    _prime(world)
    world.part("laya", _LAYA_V, [
        ("h5", "q_bool", "true", 0.9), ("h5", "q_choice", "a", 0.9),
        ("h6", "q_bool", "false", 0.9), ("h6", "q_choice", "b", 0.9),
    ])  # fmt: skip
    records = [("i1", "incident", "h1"), ("i2", "incident", "h2"), ("i3", "incident", "h6"),
               ("c1", "change", "h4"), ("i9", "incident", "h5")]  # fmt: skip
    night = world.resolve(world.build("n2", records), "n2", "n1")
    assert night.delta.active is True
    assert night.scope == {"i2", "i3", "i9"}  # i2 stays queued; i3 changed; i9 new
    _same(night, world.full_reference(night, "n2"))
    assert {r[0] for r in night.rows} == {"i1", "i3", "c1", "i9"} | {"i2"}  # i2: q_choice


def test_ut03_145_cache_row_written_tonight_re_resolves_an_untouched_record(
    world: _World,
) -> None:
    """UT03-145 a cache row for an untouched hash (the teacher answering a queued pair) makes
    that record resolve again; the carried rows of the others stay."""
    _prime(world)
    world.part("openjev", "v1", [("h2", "q_bool", "false", 0.8)])
    night = world.resolve(world.build("n2", _NIGHT1), "n2", "n1")
    assert night.delta.active is True
    assert night.scope == {"i2"}
    got = {(r[0], r[1]): r[5] for r in night.rows}  # decider
    assert got[("i2", "q_bool")] == "openjev"
    assert got[("i1", "q_bool")] == "laya"
    _same(night, world.full_reference(night, "n2"))


def _recalibrate(w: _World) -> None:
    cal = CalibrationResult(2.0, 0.1, 0.1, 0.8, 200, False)
    w.calibration.save("laya", _LAYA_V, _QSV, {"q_bool": cal})


def _touch_part(w: _World) -> None:
    part = w.cache.part_files()[0]
    stat = part.stat()
    os.utime(part, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))


@pytest.mark.parametrize("reason", ["inputs_changed", "cache_changed", "prev_mismatch"])
def test_ut03_146_changed_inputs_resolve_in_full(world: _World, reason: str) -> None:
    """UT03-146 a changed calibration, a changed cache part or another previous build resolve
    every row, and the result still equals the full resolve."""
    _prime(world)
    delta = None
    if reason == "inputs_changed":
        _recalibrate(world)
    elif reason == "cache_changed":
        _touch_part(world)
    else:
        delta = ResolveDelta.for_call(
            world.tmp / "wh-x.duckdb", "standard", DELTA_STAGES, fresh=True
        )
    night = world.resolve(world.build("n2", _NIGHT1), "n2", "n1", delta=delta)
    assert night.delta.active is False
    assert night.delta.reason == reason
    assert night.scope == {"i1", "i2", "i3", "c1"}
    _same(night, world.full_reference(night, "n2"))


@pytest.mark.parametrize("kind", ["deep", "resumed", "subset"])
def test_ut03_146_ineligible_calls_resolve_in_full(world: _World, kind: str) -> None:
    """UT03-146 a deep run, a resumed run and a stage subset are not delta nights."""
    _prime(world)
    prev = world.tmp / "wh-n1.duckdb"
    delta = {
        "deep": ResolveDelta.for_call(prev, "deep", DELTA_STAGES, fresh=True),
        "resumed": ResolveDelta.for_call(prev, "standard", DELTA_STAGES, fresh=False),
        "subset": ResolveDelta.for_call(prev, "standard", frozenset({"resolve"}), fresh=True),
    }[kind]
    night = world.resolve(world.build("n2", _NIGHT1), "n2", "n1", delta=delta)
    assert night.delta.reason == "not_eligible"
    assert night.scope == {"i1", "i2", "i3", "c1"}
    _same(night, world.full_reference(night, "n2"))


def test_ut03_147_human_labels_and_pending_reviews_are_resolved_again(world: _World) -> None:
    """UT03-147 a human label (tonight, or in the previous review_status) and a pending
    `label_check` on an untouched hash resolve that hash again."""
    _prime(world)
    label = pa.Table.from_pylist([{
        "content_hash": "h1", "record_id": "i1", "question": "q_bool",
        "question_fingerprint": _FP["q_bool"], "answer": "false", "labeled_by": "u" * 32,
        "labeled_at": _NOW - timedelta(days=1), "item_id": "rev_1",
    }], schema=HUMAN_SCHEMA)  # fmt: skip
    world.labels.append("human", label)
    payload = {"purpose": "gold", "question_set_version": _QSV, "question": "q_choice",
               "content_hash": "h3"}  # fmt: skip
    create_review_item("label_check", payload, now=_NOW)
    night = world.resolve(world.build("n2", _NIGHT1), "n2", "n1")
    assert night.delta.active is True
    assert night.scope == {"i1", "i2", "i3"}  # c1 (h4) untouched
    got = {(r[0], r[1]): (r[2], r[11]) for r in night.rows}
    assert got[("i1", "q_bool")] == ("false", "corrected")
    assert got[("i3", "q_choice")][1] == "pending"
    _same(night, world.full_reference(night, "n2"))
    third = world.resolve(world.build("n3", _NIGHT1), "n3", "n2")  # the flagged rows again
    assert third.scope == {"i1", "i2", "i3"}  # i2 is still queued
    _same(third, world.full_reference(third, "n3"))


def test_ut03_148_removed_records_and_the_chain_of_nights(world: _World) -> None:
    """UT03-148 a record gone from the build drops its decisions; the state of a delta night
    serves the next one (three nights in a row stay equal to their full resolves)."""
    _prime(world)
    n2 = world.resolve(world.build("n2", _NIGHT1[:2] + _NIGHT1[3:]), "n2", "n1")
    assert n2.delta.active is True
    assert n2.scope == {"i2"}
    assert {r[0] for r in n2.rows} == {"i1", "i2", "c1"}
    _same(n2, world.full_reference(n2, "n2"))
    n3 = world.resolve(world.build("n3", _NIGHT1), "n3", "n2")
    assert n3.delta.active is True, n3.delta.reason
    assert n3.scope == {"i2", "i3"}  # i3 is back (no previous row); i2 is still queued
    _same(n3, world.full_reference(n3, "n3"))
    n4 = world.resolve(world.build("n4", _NIGHT1), "n4", "n3")
    assert n4.scope == {"i2"}
    _same(n4, world.full_reference(n4, "n4"))
