"""Tests for the deep-mode ensemble stage (U03-88, U03-89, T03-22).

UT03-83 builds ``enrich_resolved`` and ``enrich_laya_cal`` by hand (the preconditions of
`ensemble_band`). UT03-84 writes member rows straight into a tmp decision cache and runs
`run_ensemble_pool` against the real (migrated) ops store; the expected disagreement order is
recomputed with :mod:`hashlib`, independently of DuckDB's ``sha256``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import duckdb
import pyarrow as pa
import pytest
from structlog.testing import capture_logs
from tests.support.ops_store import OpsStoreHandle

from herness.core.errors import SchemaViolation
from herness.core.types import Question, QuestionSet
from herness.enrich import ensemble_stage as es
from herness.enrich.cache import CACHE_SCHEMA, DecisionCache, write_part
from herness.enrich.calibrate import CalibrationResult, CalibrationStore
from herness.enrich.deciders.ensemble import EnsembleDecider, ensemble_version
from herness.enrich.layout import EnrichPaths
from herness.enrich.questions import question_fingerprint
from herness.enrich.resolve import QueueItem
from herness.enrich.settings import DecisionsConfig
from herness.store.ops import create_review_item, decide_review_item, list_review_items
from herness.store.ops import shared as ops_shared

pytestmark = pytest.mark.unit

_SETTINGS_SQL = Path(__file__).resolve().parents[3] / "herness/model/sql/000_settings.sql"
_QSV = "qs-2026-10-01.1"
_BUILD = "b-20260927-0001"
_NOW = datetime(2026, 9, 27, 2, tzinfo=UTC)
_MEMBERS = (("laya", "laya-20260901-1"), ("openjev", "v1"), ("llm", "v1"))


def _question(qid: str, qtype: str, **kw: Any) -> Question:
    extra = {"options": {"a": "Option A.", "b": "Option B.", "c": "Option C."}}
    base = {"id": qid, "type": qtype, "instructions": "Classify this ticket with care, please."}
    return Question.model_validate(
        {**base, "threshold": 0.7, **(extra if qtype == "choice" else {}), **kw}
    )


_QS = QuestionSet(
    version=_QSV,
    questions=(
        _question("q_choice", "choice"),
        _question("q_bool", "bool"),
        _question("q_info", "bool", scoring_use=False),
        _question("change_caused_pair", "bool", scoring_use=False),
    ),
)
_FPS = {q.id: question_fingerprint(q) for q in _QS.questions}


def _cfg(**ensemble: float) -> DecisionsConfig:
    return DecisionsConfig.model_validate({"question_set_version": _QSV, "questions": (),
        "change_link": {"use_decider": False}, "ensemble": ensemble})  # fmt: skip


def _digest(n: int) -> str:
    return f"{n:032x}"


@dataclass
class _Report:
    status: str = "done"
    note: str | None = None
    decided: int = 0


# --- UT03-83 ensemble_band --------------------------------------------------------------------


def _band_wh(rows: list[tuple[str, str, float, bool, int]]) -> duckdb.DuckDBPyConnection:
    """(record, question, laya p_cal, scoring_use, opened hours ago) as the resolve tables."""
    wh = duckdb.connect()
    wh.execute(_SETTINGS_SQL.read_text("utf-8"))
    wh.execute("CREATE TEMP TABLE enrich_resolved (record_id VARCHAR, entity VARCHAR, "
               "content_hash VARCHAR, opened_at TIMESTAMPTZ, question VARCHAR, "
               "scoring_use BOOLEAN, status VARCHAR)")  # fmt: skip
    wh.execute("CREATE TEMP TABLE enrich_laya_cal (record_id VARCHAR, question VARCHAR, "
               "answer VARCHAR, p_cal DOUBLE)")  # fmt: skip
    for rid in sorted({r[0] for r in rows}):
        n = int(rid.split("_")[1])
        wh.execute("INSERT INTO enrich.text_redacted VALUES (?, 'incident', ?, ?)",
                   [rid, f"secret body of {rid}", _digest(n)])  # fmt: skip
    for rid, qid, p, scoring, hours in rows:
        n = int(rid.split("_")[1])
        opened = None if hours < 0 else _NOW - timedelta(hours=hours)
        wh.execute("INSERT INTO enrich_resolved VALUES (?, 'incident', ?, ?, ?, ?, 'final')",
                   [rid, _digest(n), opened, qid, scoring])  # fmt: skip
        wh.execute("INSERT INTO enrich_laya_cal VALUES (?, ?, 'a', ?)", [rid, qid, p])
    return wh


_AROUND = [
    ("inc_1", "q_choice", 0.89, True, 5),  # in band
    ("inc_1", "q_bool", 0.95, True, 5),
    ("inc_2", "q_choice", 0.90, True, 4),  # exactly the band: not below
    ("inc_2", "q_bool", 0.91, True, 4),
    ("inc_3", "q_info", 0.20, False, 3),  # low but not a scoring question
    ("inc_3", "q_bool", 0.899, True, 3),
    ("inc_4", "q_choice", 0.10, True, 1),  # newest: first
    ("inc_4", "q_bool", 0.50, True, 1),
    ("inc_5", "q_bool", 0.30, True, -1),  # no opened_at: last
]


def test_ut03_83_band_selects_scoring_rows_below_band() -> None:
    """UT03-83 Laya calibrated rows around 0.9: rows < 0.9 for scoring questions only."""
    band = es.ensemble_band(_band_wh(_AROUND), cfg=_cfg(), qs=_QS)
    assert [(i.record_id, i.question_ids) for i in band] == [
        ("inc_4", ("q_bool", "q_choice")),
        ("inc_3", ("q_bool",)),
        ("inc_1", ("q_choice",)),
        ("inc_5", ("q_bool",)),
    ]
    assert band[0] == QueueItem("inc_4", "incident", _digest(4), "secret body of inc_4",
                                ("q_bool", "q_choice"))  # fmt: skip


def test_ut03_83_band_caps_and_config_band() -> None:
    """UT03-83 at most ``ensemble.max_rows`` records; ``ensemble.band`` is the bound."""
    wh = _band_wh(_AROUND)
    assert [i.record_id for i in es.ensemble_band(wh, cfg=_cfg(max_rows=2), qs=_QS)] == [
        "inc_4", "inc_3"]  # fmt: skip
    assert es.ensemble_band(wh, cfg=_cfg(max_rows=0), qs=_QS) == []
    low = es.ensemble_band(wh, cfg=_cfg(band=0.5), qs=_QS)
    assert [(i.record_id, i.question_ids) for i in low] == [("inc_4", ("q_choice",)),
                                                            ("inc_5", ("q_bool",))]  # fmt: skip


def test_ut03_83_band_without_scoring_questions_is_empty() -> None:
    """UT03-83 a question set without scoring questions needs no query."""
    qs = QuestionSet(version=_QSV, questions=(_question("q_info", "bool", scoring_use=False),))
    assert es.ensemble_band(duckdb.connect(), cfg=_cfg(), qs=qs) == []


def test_ut03_83_band_missing_tables_is_schema_violation() -> None:
    """UT03-83 no ``enrich_resolved`` (stage out of order): SchemaViolation, no row values."""
    with pytest.raises(SchemaViolation, match=r"^ensemble_band: "):
        es.ensemble_band(duckdb.connect(), cfg=_cfg(), qs=_QS)


# --- UT03-84 run_ensemble_pool ----------------------------------------------------------------

# Choice distributions whose argmax differ per member: pooled agreement 1/3.
_SPLIT = {"laya": {"a": 0.6, "b": 0.2, "c": 0.2}, "openjev": {"a": 0.25, "b": 0.5, "c": 0.25},
          "llm": {"a": 0.3, "b": 0.3, "c": 0.4}}  # fmt: skip
_SAME = {"true": 0.8, "false": 0.2}


@pytest.fixture(autouse=True)
def _no_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(event: str, actor: str, **fields: object) -> None:
        return None

    monkeypatch.setattr(ops_shared, "audit", fake)


class _SpyCalibration(CalibrationStore):
    def __init__(self, paths: EnrichPaths) -> None:
        super().__init__(paths)
        self.calls: list[tuple[str, str]] = []

    def temperature(
        self, decider: str, decider_version: str, qsv: str, qid: str
    ) -> tuple[float, bool]:
        self.calls.append((decider, qid))
        return super().temperature(decider, decider_version, qsv, qid)


@dataclass
class _Env:
    paths: EnrichPaths
    cache: DecisionCache
    calibration: _SpyCalibration

    def member_rows(
        self, decider: str, version: str, hashes: list[str], split: dict[str, Any] = _SPLIT
    ) -> None:
        rows = [
            {"content_hash": ch, "question": qid, "question_fingerprint": _FPS[qid],
             "answer": max(dist, key=dist.__getitem__), "probability": max(dist.values()),
             "distribution": list(dist.items()), "backend_confidence": None, "samples": None,
             "decided_at": _NOW}
            for ch in hashes
            for qid, dist in (("q_choice", split[decider]), ("q_bool", _SAME))
        ]  # fmt: skip
        write_part(self.paths.cache_partition(_QSV, decider, version),
                   pa.Table.from_pylist(rows, schema=CACHE_SCHEMA))  # fmt: skip

    def rows(self, decider: str = "ensemble") -> list[dict[str, Any]]:
        dataset = self.cache.dataset()
        rows = [] if dataset is None else dataset.to_table().to_pylist()
        return [r for r in rows if r["decider"] == decider]


@pytest.fixture
def env(tmp_path: Path, ops_store: OpsStoreHandle) -> _Env:
    del ops_store
    paths = EnrichPaths(data_root=tmp_path, embedding_path="data/e", laya_current_file="data/c")
    return _Env(paths, DecisionCache(paths, _QSV), _SpyCalibration(paths))


def _band(n: int) -> list[QueueItem]:
    return [QueueItem(f"inc_{i}", "incident", _digest(i), f"secret body of inc_{i}",
                      ("q_bool", "q_choice")) for i in range(1, n + 1)]  # fmt: skip


def _weights(members: tuple[tuple[str, str], ...] = _MEMBERS) -> dict[tuple[str, str], float]:
    return {(d, q): 0.8 for d, _ in members for q in ("q_bool", "q_choice")}


def _pool(env: _Env, band: list[QueueItem], report: _Report, **kw: Any) -> str:
    args: dict[str, Any] = {
        "band": band, "qs": _QS, "cfg": _cfg(disagreement_review_cap=3), "cache": env.cache,
        "calibration": env.calibration, "members": _MEMBERS, "gold_accuracy": _weights(),
        "build_id": _BUILD, "report": report,
    }  # fmt: skip
    return es.run_ensemble_pool(duckdb.connect(), **{**args, **kw})


def _items(
    status: Literal["pending", "approved", "rejected"] = "pending",
) -> list[dict[str, object]]:
    match = {"purpose": "ensemble_disagreement", "question_set_version": _QSV}
    return [dict(i.payload) for i in list_review_items(kind="label_check", status=status,
                                                       payload_match=match)]  # fmt: skip


def _hash_order(hashes: list[str], qid: str = "q_choice") -> list[str]:
    return sorted(
        hashes, key=lambda ch: hashlib.sha256(f"{_BUILD}|{ch}|{qid}".encode()).hexdigest()
    )


def _seed(env: _Env, n: int, members: tuple[tuple[str, str], ...] = _MEMBERS) -> list[str]:
    hashes = [_digest(i) for i in range(1, n + 1)]
    for decider, version in members:
        env.member_rows(decider, version, hashes)
    return hashes


def test_ut03_84_agreement_one_third_caches_rows_and_caps_reviews(env: _Env) -> None:
    """UT03-84 members with agreement 1/3: ensemble rows cached; disagreement items capped."""
    hashes = _seed(env, 6)
    report = _Report()
    with capture_logs() as logs:
        version = _pool(env, _band(6), report)
    assert version == ensemble_version(_MEMBERS, _weights())
    rows = env.rows()
    assert {r["decider_version"] for r in rows} == {version}
    assert len(rows) == 12
    assert report.decided == 12
    choice = [r for r in rows if r["question"] == "q_choice"]
    assert all(r["backend_confidence"] == pytest.approx(1 / 3) for r in choice)
    assert all(r["backend_confidence"] == 1.0 for r in rows if r["question"] == "q_bool")
    items = _items()
    assert len(items) == 3  # capped at disagreement_review_cap; q_bool agrees
    assert [p["content_hash"] for p in items] == _hash_order(hashes)[:3]
    first = items[0]
    assert set(first) == {"record_id", "content_hash", "question", "question_fingerprint",
                          "question_set_version", "answer", "probability", "decider",
                          "decider_version", "purpose", "text_ref"}  # fmt: skip
    assert (first["decider"], first["decider_version"], first["question"]) == (
        "ensemble", version, "q_choice")  # fmt: skip
    assert first["question_fingerprint"] == _FPS["q_choice"]
    assert sorted(env.calibration.calls) == sorted(
        (d, q) for d, _ in _MEMBERS for q in ("q_bool", "q_choice"))  # fmt: skip
    events = {e["event"]: e for e in logs}
    created = events["enrich.spot_check.created"]
    assert (created["count"], created["purpose"]) == (3, "ensemble_disagreement")
    assert events["enrich.ensemble.pooled"]["reviews"] == 3
    assert "secret body" not in json.dumps([logs, items], default=str)  # TH03-03


def test_ut03_84_rerun_is_restartable_and_idempotent(env: _Env) -> None:
    """UT03-84 a rerun pools nothing again and creates no further items (per-night cap)."""
    _seed(env, 6)
    _pool(env, _band(6), _Report())
    again = _Report()
    _pool(env, _band(6), again)
    assert again.decided == 0
    assert len(env.rows()) == 12
    assert len(_items()) == 3


def test_ut03_84_interrupted_run_is_completed_by_rerun(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-84 a failure after the first chunk keeps it; the rerun pools the rest only."""
    _seed(env, 5)
    monkeypatch.setattr(es, "POOL_CHUNK", 2)
    real = EnsembleDecider.decide
    calls: list[int] = []

    def flaky(self: Any, items: Any, qs: QuestionSet) -> Any:
        calls.append(len(items))
        if len(calls) == 2:
            msg = "disk gone"
            raise OSError(msg)
        return real(self, items, qs)

    monkeypatch.setattr(EnsembleDecider, "decide", flaky)
    with pytest.raises(OSError, match="disk gone"):
        _pool(env, _band(5), _Report())
    assert len(env.rows()) == 4  # first chunk of 2 items flushed
    assert _items() == []
    report = _Report()
    _pool(env, _band(5), report)
    assert calls[2:] == [2, 1]  # only the 3 missing items, in chunks of 2
    assert report.decided == 6
    assert len(env.rows()) == 10
    assert len(_items()) == 3


def test_ut03_84_missing_member_drops_out(env: _Env) -> None:
    """UT03-84 a member without rows drops out: version and weights over the present ones."""
    present = _MEMBERS[:2]
    _seed(env, 2, present)
    with capture_logs() as logs:
        version = _pool(env, _band(2), _Report())
    assert version == ensemble_version(present, _weights(present))
    assert not [e for e in logs if e["event"] == "enrich.ensemble.weights_default"]
    choice = [r for r in env.rows() if r["question"] == "q_choice"]
    assert {r["backend_confidence"] for r in choice} == {0.5}
    assert len(_items()) == 2  # 1/2 < 2/3


def test_ut03_84_missing_accuracy_warns_and_pools_equal(env: _Env) -> None:
    """UT03-84 no gold accuracy for a present member: warning, equal weights for the question."""
    _seed(env, 1)
    weights = {k: v for k, v in _weights().items() if k != ("llm", "q_bool")}
    with capture_logs() as logs:
        _pool(env, _band(1), _Report(), gold_accuracy=weights)
    warned = [e for e in logs if e["event"] == "enrich.ensemble.weights_default"]
    assert [(e["question"], e["log_level"]) for e in warned] == [("q_bool", "warning")]
    assert len(env.rows()) == 2


def test_ut03_84_blocking_statuses_suppress_rereview(env: _Env) -> None:
    """UT03-84 a decided (rejected) disagreement is never re-asked; scope is the qsv."""
    _seed(env, 1)
    version = ensemble_version(_MEMBERS, _weights())
    payload = {"record_id": "inc_1", "content_hash": _digest(1), "question": "q_choice",
               "question_fingerprint": _FPS["q_choice"], "question_set_version": _QSV,
               "answer": "a", "probability": 0.4, "decider": "ensemble",
               "decider_version": version, "purpose": "ensemble_disagreement",
               "text_ref": "enrich.text_redacted"}  # fmt: skip
    item = create_review_item("label_check", payload, now=_NOW)
    decide_review_item(item, "rejected", decided_by="a" * 32, note=None, now=_NOW)
    other = {**payload, "question_set_version": "qs-other.1"}
    create_review_item("label_check", other, now=_NOW)  # another qsv: outside the scope
    _pool(env, _band(1), _Report())
    assert _items() == []
    assert len(_items("rejected")) == 1


def test_ut03_84_no_members_or_empty_band_is_skipped(env: _Env) -> None:
    """UT03-84 nothing to pool: skipped, no rows, no items, a version is still returned."""
    report = _Report()
    version = _pool(env, _band(3), report)
    assert (report.status, report.note, report.decided) == ("skipped", "no_work", 0)
    assert version == ensemble_version((), {})
    _seed(env, 1)
    empty = _Report()
    _pool(env, [], empty)
    assert (empty.status, empty.note) == ("skipped", "no_work")
    assert env.rows() == []
    assert _items() == []


def test_ut03_84_cache_read_error_is_mapped(env: _Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT03-84 an unreadable cache while finding members: StoreBusy/FatalError, not OSError."""
    from herness.core.errors import HernessError  # noqa: PLC0415

    _seed(env, 1)

    class _Broken:
        def to_table(self, *args: Any, **kwargs: Any) -> Any:
            raise OSError(13, "locked")

    monkeypatch.setattr(env.cache, "dataset", _Broken)
    with pytest.raises(HernessError, match="cannot read decision cache"):
        _pool(env, _band(1), _Report())


def test_ut03_84_disagreement_query_error_is_schema_violation(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-84 a DuckDB error selecting disagreements: SchemaViolation, views unregistered."""
    _seed(env, 1)
    monkeypatch.setattr(es, "_DISAGREE_SQL", "SELECT * FROM no_such_table")
    wh = duckdb.connect()
    with pytest.raises(SchemaViolation, match=r"^run_ensemble_pool: "):
        es.run_ensemble_pool(
            wh, band=_band(1), qs=_QS, cfg=_cfg(), cache=env.cache, calibration=env.calibration,
            members=_MEMBERS, gold_accuracy=_weights(), build_id=_BUILD, report=_Report(),
        )  # fmt: skip
    views = wh.execute("SELECT view_name FROM duckdb_views() WHERE NOT internal").fetchall()
    assert views == []


def test_ut03_84_calibration_temperature_is_applied(env: _Env) -> None:
    """UT03-84 member temperatures come from the CalibrationStore (a sharper openjev wins)."""
    _seed(env, 1)
    sharp = CalibrationResult(
        temperature=0.2, ece=0.01, ece_raw=0.02, accuracy=0.9, n=200, uncalibrated=False
    )
    env.calibration.save("openjev", "v1", _QSV, {"q_choice": sharp})
    _pool(env, _band(1), _Report())
    choice = next(r for r in env.rows() if r["question"] == "q_choice")
    assert choice["answer"] == "b"


# Exactly two of three argmaxes (laya, openjev: "a") equal the pooled argmax "a".
_TWO_OF_THREE = {"laya": {"a": 0.6, "b": 0.2, "c": 0.2},
                 "openjev": {"a": 0.5, "b": 0.25, "c": 0.25},
                 "llm": {"a": 0.2, "b": 0.6, "c": 0.2}}  # fmt: skip


def test_ut03_84_agreement_two_thirds_is_not_a_disagreement(env: _Env) -> None:
    """UT03-84 agreement exactly 2/3 is cached as such and creates no disagreement item."""
    hashes = [_digest(1), _digest(2)]
    for decider, version in _MEMBERS:
        env.member_rows(decider, version, hashes, split=_TWO_OF_THREE)
    _pool(env, _band(2), _Report())
    choice = [r for r in env.rows() if r["question"] == "q_choice"]
    assert len(choice) == 2
    assert all(r["answer"] == "a" for r in choice)
    assert all(r["backend_confidence"] == pytest.approx(2 / 3) for r in choice)
    assert _items() == []  # the rule is agreement < 2/3, not <=
