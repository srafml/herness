"""Tests for the resolve SQL, frame and escalation queue (U03-78 ... U03-80, T03-19).

PT03-10 checks ``resolve_decisions.sql`` against the reference `resolve_pair` (U03-74) on
random candidate sets; the calibrated probability of the reference comes from
`apply_temperature` (U03-42), an independent implementation of the SQL formula.
UT03-76 covers `escalation_queue`; `resolve_frame` is covered here under IT03-04's ID (its
spec test) with real stores, as the stage integration test belongs to a later card.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pyarrow as pa
import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st
from tests.support.ops_store import OpsStoreHandle

from herness.core.errors import ConfigError, SchemaViolation
from herness.core.types import Question, QuestionSet
from herness.enrich import resolve
from herness.enrich.cache import CACHE_SCHEMA, DecisionCache, write_part
from herness.enrich.calibrate import CalibrationResult, CalibrationStore, apply_temperature
from herness.enrich.decide import CachedAnswer, resolve_pair
from herness.enrich.labels import HUMAN_SCHEMA, LabelStore
from herness.enrich.layout import EnrichPaths
from herness.enrich.settings import DecidersSettings, DecisionsConfig
from herness.store.ops import create_review_item

pytestmark = pytest.mark.unit

_SETTINGS_SQL = Path(__file__).resolve().parents[3] / "herness/model/sql/000_settings.sql"
_T0 = datetime(2026, 9, 1, tzinfo=UTC)
_SINCE = _T0 - timedelta(days=90)
_QSV = "qs-2026-10-01.1"
_FULL_CACHE = pa.schema([*CACHE_SCHEMA, ("decider", pa.string()), ("decider_version", pa.string())])
_CALIB = pa.schema(
    [
        ("decider", pa.string()),
        ("decider_version", pa.string()),
        ("question", pa.string()),
        ("t", pa.float64()),
        ("uncalibrated", pa.bool_()),
    ]
)
_DECIDERS = ("laya", "openjev", "jev", "llm", "ensemble")
_LABELS = {"bool": ["true", "false"], "choice": ["a", "b", "c"], "score": ["0", "1", "2", "3"]}
_FP = "0123456789abcdef"
_OTHER_FP = "f" * 16  # a stale question fingerprint
_OUT = (
    "status, answer, probability, decider, decider_version, escalated, decided_at, agreement, "
    "review_status"
)


def _warehouse() -> duckdb.DuckDBPyConnection:
    wh = duckdb.connect()
    wh.execute(_SETTINGS_SQL.read_text("utf-8"))
    for entity in ("incident", "change", "problem"):
        wh.execute(f"CREATE TABLE core.{entity} (record_id VARCHAR, opened_at TIMESTAMPTZ)")
    return wh


def _add_record(
    wh: duckdb.DuckDBPyConnection, rid: str, content_hash: str, opened_at: datetime | None
) -> None:
    wh.execute(
        "INSERT INTO enrich.text_redacted VALUES (?, 'incident', ?, ?)",
        [rid, f"text of {rid}", content_hash],
    )
    wh.execute("INSERT INTO core.incident VALUES (?, ?)", [rid, opened_at])


# --- PT03-10 -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Q:
    qid: str
    qtype: str
    threshold: float
    primary: str
    chain: tuple[str, ...]
    scoring_use: bool


@st.composite
def _questions(draw: st.DrawFn) -> list[_Q]:
    out = []
    for i in range(draw(st.integers(1, 3))):
        primary = draw(st.sampled_from(["laya", "openjev", "jev", "llm"]))
        others = [d for d in ("openjev", "jev", "llm") if d != primary]
        chain = draw(
            st.permutations(others).flatmap(
                lambda p: st.integers(0, len(p)).map(lambda n, p=p: tuple(p[:n]))
            )
        )
        out.append(
            _Q(
                f"q_{i}",
                draw(st.sampled_from(["bool", "choice", "score"])),
                draw(st.floats(0.5, 0.999)),
                primary,
                chain,
                draw(st.booleans()),
            )
        )
    return out


def _dist(draw: st.DrawFn, qtype: str) -> dict[str, float]:
    if qtype == "bool":
        p = draw(st.floats(0.0, 1.0))
        return {"true": p, "false": 1.0 - p}
    return {k: draw(st.floats(0.0, 1.0)) for k in _LABELS[qtype]}


def _p_cal(dist: dict[str, float], answer: str, t: float, qtype: str) -> float:
    keys = _LABELS[qtype]
    cal = apply_temperature(np.array([[dist[k] for k in keys]]), t, qtype)
    return float(cal[0, keys.index(answer)])


@dataclass
class _World:
    records: list[tuple[str, str, datetime | None]]
    questions: list[_Q]
    cache: list[dict[str, Any]]
    calib: list[dict[str, Any]]
    human: list[dict[str, Any]]
    pending: list[dict[str, Any]]
    expected: dict[tuple[str, str], Any]


def _cache_row(ch: str, q: _Q, decider: str, version: str, **kw: Any) -> dict[str, Any]:
    return {
        "content_hash": ch,
        "question": q.qid,
        "question_fingerprint": kw.get("fp", _FP),
        "answer": kw["answer"],
        "probability": 0.5,
        "distribution": list(kw["dist"].items()),
        "backend_confidence": kw.get("conf"),
        "samples": None,
        "decided_at": kw["at"],
        "decider": decider,
        "decider_version": version,
    }


def _draw_pair(
    draw: st.DrawFn, w: _World, ch: str, q: _Q, temps: dict[tuple[str, str], float]
) -> tuple[dict[str, CachedAnswer], tuple[str, datetime] | None, bool]:
    rows: dict[str, CachedAnswer] = {}
    for decider in draw(st.lists(st.sampled_from(_DECIDERS), unique=True, max_size=4)):
        dist, answer = _dist(draw, q.qtype), draw(st.sampled_from(_LABELS[q.qtype]))
        at = _T0 + timedelta(minutes=draw(st.integers(0, 10_000)))
        conf = draw(st.floats(0.0, 1.0)) if decider == "ensemble" else None
        w.cache.append(_cache_row(ch, q, decider, "v1", answer=answer, dist=dist, at=at, conf=conf))
        stale = {"answer": answer, "dist": _dist(draw, q.qtype), "at": at - timedelta(days=1)}
        w.cache.append(_cache_row(ch, q, decider, "v1", **stale))  # older duplicate: ignored
        w.cache.append(_cache_row(ch, q, decider, "v0", answer=answer, dist=dist, at=at))
        w.cache.append(_cache_row(ch, q, decider, "v1", fp=_OTHER_FP, **stale))
        p_cal = _p_cal(dist, answer, temps.get((decider, q.qid), 1.0), q.qtype)
        if decider == q.primary:
            assume(abs(p_cal - q.threshold) > 1e-9)  # float noise must not flip the gate
        rows[decider] = CachedAnswer(answer, p_cal, at, "v1", conf)
    human = None
    if draw(st.booleans()):
        human = (draw(st.sampled_from(_LABELS[q.qtype])), _T0 + timedelta(days=30))
        base = {
            "content_hash": ch,
            "record_id": "r",
            "question": q.qid,
            "labeled_by": "u",
            "item_id": "i",
        }
        w.human.append(
            {**base, "question_fingerprint": _FP, "answer": human[0], "labeled_at": human[1]}
        )
        w.human.append(
            {
                **base,
                "question_fingerprint": _OTHER_FP,
                "answer": "zz",
                "labeled_at": human[1],
            }
        )
    pending = draw(st.booleans())
    if pending:
        w.pending.append({"content_hash": ch, "question": q.qid})
    return rows, human, pending


@st.composite
def _worlds(draw: st.DrawFn) -> _World:
    questions = draw(_questions())
    hashes = [f"{i:032x}" for i in range(draw(st.integers(1, 3)))]
    opened = st.one_of(st.none(), st.integers(-200, 10).map(lambda d: _T0 + timedelta(days=d)))
    records = [
        (f"inc_{i}", draw(st.sampled_from(hashes)), draw(opened))
        for i in range(draw(st.integers(1, 4)))
    ]
    temps = {
        (d, q.qid): draw(st.floats(0.05, 10.0))
        for d in _DECIDERS
        for q in questions
        if draw(st.booleans())
    }
    calib = [
        {"decider": d, "decider_version": "v1", "question": qid, "t": t, "uncalibrated": False}
        for (d, qid), t in temps.items()
    ]
    w = _World(records, questions, [], calib, [], [], {})
    per_pair = {(ch, q.qid): _draw_pair(draw, w, ch, q, temps) for ch in hashes for q in questions}
    for rid, ch, opened_at in records:
        for q in questions:
            rows, human, pending = per_pair[(ch, q.qid)]
            in_scope = q.primary == "laya" or (opened_at is not None and opened_at >= _SINCE)
            w.expected[(rid, q.qid)] = resolve_pair(
                human=human,
                rows=rows,
                primary=q.primary,
                chain=q.chain,
                threshold=q.threshold,
                in_scope=in_scope,
                pending_review=pending,
            )
    return w


def _qmeta(questions: list[_Q]) -> pa.Table:
    return pa.Table.from_pylist(
        [
            {
                "question": q.qid,
                "fingerprint": _FP,
                "qtype": q.qtype,
                "threshold": q.threshold,
                "scoring_use": q.scoring_use,
                "primary": q.primary,
                "chain": list(q.chain),
                "applies_to": ["incident"],
                "labels": _LABELS[q.qtype],
            }
            for q in questions
        ],
        schema=resolve._QMETA_SCHEMA,
    )


@settings(max_examples=200, deadline=None)
@given(_worlds())
def test_pt03_10_sql_equals_resolve_pair(w: _World) -> None:
    """PT03-10: on random candidate sets the SQL result equals resolve_pair row by row."""
    wh = _warehouse()
    for rid, ch, opened_at in w.records:
        _add_record(wh, rid, ch, opened_at)
    wh.register("cache_rows", pa.Table.from_pylist(w.cache, schema=_FULL_CACHE))
    wh.register("human_latest", pa.Table.from_pylist(w.human, schema=HUMAN_SCHEMA))
    wh.register("calib", pa.Table.from_pylist(w.calib, schema=_CALIB))
    wh.register("qmeta", _qmeta(w.questions))
    versions = [{"decider": d, "decider_version": "v1"} for d in _DECIDERS]
    wh.register("versions", pa.Table.from_pylist(versions, schema=resolve._VERSIONS_SCHEMA))
    wh.register("pending_items", pa.Table.from_pylist(w.pending, schema=resolve._PENDING_SCHEMA))
    resolve._run_resolve_sql(wh, bootstrap_since=_SINCE)
    got = wh.execute(f"SELECT record_id, question, {_OUT} FROM enrich_resolved").fetchall()  # noqa: S608
    assert len(got) == len(w.expected)
    for rid, qid, status, answer, prob, decider, version, esc, at, agr, review in got:
        exp = w.expected[(rid, qid)]
        assert (status, answer, decider, version, esc, at, review) == (
            exp.status,
            exp.answer,
            exp.decider,
            exp.decider_version,
            exp.escalated,
            exp.decided_at,
            exp.review_status,
        )
        assert prob == (None if exp.probability is None else pytest.approx(exp.probability))
        assert agr == (None if exp.agreement is None else pytest.approx(exp.agreement))


def test_pt03_10_duckdb_error_is_schema_violation() -> None:
    """PT03-10: a missing input relation raises SchemaViolation naming resolve_decisions."""
    with pytest.raises(SchemaViolation, match=r"^resolve_decisions: "):
        resolve._run_resolve_sql(_warehouse(), bootstrap_since=_SINCE)


# --- UT03-76 -----------------------------------------------------------------------------------

_RESOLVED = [  # record_id, content_hash, opened_at, question, scoring_use, status
    ("inc_1", "h1", _T0 + timedelta(days=5), "q_a", False, "queue"),
    ("inc_2", "h2", _T0 - timedelta(days=50), "q_b", False, "queue"),
    ("inc_2", "h2", _T0 - timedelta(days=50), "q_a", True, "queue"),
    ("inc_2", "h2", _T0 - timedelta(days=50), "q_c", True, "final"),
    ("inc_3", "h3", _T0, "q_b", True, "queue"),
    ("inc_4", "h4", _T0 + timedelta(days=9), "q_a", True, "final"),
    ("inc_5", "h5", None, "q_a", False, "queue"),
    ("inc_6", "h6", _T0 + timedelta(days=1), "q_a", False, "out_of_scope"),
]


@pytest.fixture
def resolved_wh() -> duckdb.DuckDBPyConnection:
    """A warehouse with an `enrich_resolved` fixture and one `enrich_cand` row (inc_1/q_a)."""
    wh = _warehouse()
    wh.execute(
        "CREATE TEMP TABLE enrich_resolved (record_id VARCHAR, entity VARCHAR, "
        "content_hash VARCHAR, opened_at TIMESTAMPTZ, question VARCHAR, scoring_use BOOLEAN, "
        "status VARCHAR)"
    )
    for rid, ch, opened_at, qid, scoring, status in _RESOLVED:
        wh.execute(
            "INSERT INTO enrich_resolved VALUES (?, 'incident', ?, ?, ?, ?, ?)",
            [rid, ch, opened_at, qid, scoring, status],
        )
    for rid, ch in sorted({(r[0], r[1]) for r in _RESOLVED}):
        wh.execute(
            "INSERT INTO enrich.text_redacted VALUES (?, 'incident', ?, ?)",
            [rid, f"text of {rid}", ch],
        )
    wh.execute(
        "CREATE TEMP TABLE enrich_cand AS SELECT 'h1' AS content_hash, 'q_a' AS question, "
        "'openjev' AS decider"
    )
    return wh


def test_ut03_76_queue_cap_two(resolved_wh: duckdb.DuckDBPyConnection) -> None:
    """UT03-76: scoring_use first, newest first, 2 records with all their queued questions."""
    got = resolve.escalation_queue(resolved_wh, max_records=2)
    assert got == [
        resolve.QueueItem("inc_3", "incident", "h3", "text of inc_3", ("q_b",)),
        resolve.QueueItem("inc_2", "incident", "h2", "text of inc_2", ("q_a", "q_b")),
    ]


def test_ut03_76_queue_full_order_nulls_last(resolved_wh: duckdb.DuckDBPyConnection) -> None:
    """UT03-76: without a binding cap, non-scoring records follow newest first, NULLs last."""
    got = resolve.escalation_queue(resolved_wh, max_records=100)
    assert [item.record_id for item in got] == ["inc_3", "inc_2", "inc_1", "inc_5"]
    assert resolve.escalation_queue(resolved_wh, max_records=0) == []


def test_ut03_76_exclude_deciders(resolved_wh: duckdb.DuckDBPyConnection) -> None:
    """UT03-76: a queued pair with a current row of an excluded decider is left out."""
    got = resolve.escalation_queue(
        resolved_wh, max_records=100, exclude_deciders=frozenset({"openjev"})
    )
    assert [item.record_id for item in got] == ["inc_3", "inc_2", "inc_5"]


def test_ut03_76_errors(resolved_wh: duckdb.DuckDBPyConnection) -> None:
    """UT03-76: a negative cap is a ConfigError; a missing frame is a SchemaViolation."""
    with pytest.raises(ConfigError, match="max_records"):
        resolve.escalation_queue(resolved_wh, max_records=-1)
    with pytest.raises(SchemaViolation, match=r"^escalation_queue: "):
        resolve.escalation_queue(_warehouse(), max_records=1)


# --- resolve_frame (U03-79; spec test IT03-04) --------------------------------------------------


def _question(qid: str, qtype: str, **kw: Any) -> Question:
    extra: dict[str, Any] = {}
    if qtype == "choice":
        extra["options"] = {"a": "Option A.", "b": "Option B."}
    if qtype == "score":
        extra["levels"] = ("None at all.", "Low level.", "Medium level.", "High level.")
    base = {"id": qid, "type": qtype, "instructions": "Classify this ticket with care, please."}
    return Question.model_validate({**base, "threshold": 0.7, **extra, **kw})


_QS = QuestionSet(
    version=_QSV,
    questions=(
        _question("q_bool", "bool"),
        _question("q_choice", "choice", scoring_use=False),
        _question("q_score", "score"),
        _question("change_caused_pair", "bool", applies_to=("change",)),
    ),
)
_CFG = DecisionsConfig.model_validate(
    {
        "question_set_version": _QSV,
        "escalation_chain": ("openjev", "llm"),
        "questions": (),
        "change_link": {"use_decider": False},
    }
)
_PRIMARIES = {
    "q_bool": "laya",
    "q_choice": "openjev",
    "q_score": "openjev",
    "change_caused_pair": "openjev",
}
_NOW = datetime(2026, 9, 26, tzinfo=UTC)
_LAYA_V = "laya-20260901-1"


@pytest.fixture
def frame(ops_store: OpsStoreHandle, tmp_path: Path) -> Callable[..., None]:
    """Stores holding one Laya row, one calibrated OpenJev row, a human label, a pending item."""
    paths = EnrichPaths(data_root=tmp_path, embedding_path="data/e", laya_current_file="data/c")
    cache, labels, calibration = (
        DecisionCache(paths, _QSV),
        LabelStore(paths, _QSV),
        CalibrationStore(paths),
    )
    fps = {
        row["question"]: row["fingerprint"]
        for row in resolve._qmeta_table(
            _QS, cfg=_CFG, deciders=DecidersSettings(), primaries=_PRIMARIES
        ).to_pylist()
    }

    def part(
        version: str, decider: str, rows: list[tuple[str, str, str, dict[str, float]]]
    ) -> None:
        table = pa.Table.from_pylist(
            [
                {
                    "content_hash": ch,
                    "question": qid,
                    "question_fingerprint": fps[qid],
                    "answer": ans,
                    "probability": max(dist.values()),
                    "distribution": list(dist.items()),
                    "backend_confidence": None,
                    "samples": None,
                    "decided_at": _NOW - timedelta(hours=1),
                }
                for ch, qid, ans, dist in rows
            ],
            schema=CACHE_SCHEMA,
        )
        write_part(paths.cache_partition(_QSV, decider, version), table)

    part(_LAYA_V, "laya", [("h1", "q_bool", "true", {"true": 0.9, "false": 0.1})])
    part(
        "v1",
        "openjev",
        [
            ("h1", "q_choice", "a", {"a": 0.7, "b": 0.3}),
            ("h2", "q_score", "2", {"0": 0.05, "1": 0.05, "2": 0.8, "3": 0.1}),
        ],
    )
    calibration.save(
        "openjev", "v1", _QSV, {"q_choice": CalibrationResult(0.5, 0.1, 0.1, 0.8, 200, False)}
    )
    labels.append(
        "human",
        pa.Table.from_pylist(
            [
                {
                    "content_hash": "h2",
                    "record_id": "inc_2",
                    "question": "q_bool",
                    "question_fingerprint": fps["q_bool"],
                    "answer": "false",
                    "labeled_by": "u" * 32,
                    "labeled_at": _NOW - timedelta(days=2),
                    "item_id": "rev_1",
                }
            ],
            schema=HUMAN_SCHEMA,
        ),
    )
    payload = {
        "purpose": "spot_check",
        "question_set_version": _QSV,
        "question": "q_bool",
        "content_hash": "h1",
    }
    create_review_item("label_check", payload, now=_NOW)
    other = {**payload, "question": "q_choice", "question_set_version": "qs-2026-01-01.1"}
    create_review_item("label_check", other, now=_NOW)  # another question set: ignored

    def run(wh: duckdb.DuckDBPyConnection) -> None:
        resolve.resolve_frame(
            wh,
            qs=_QS,
            cfg=_CFG,
            deciders=DecidersSettings(),
            cache=cache,
            labels=labels,
            calibration=calibration,
            primaries=_PRIMARIES,
            versions={"laya": _LAYA_V, "openjev": "v1", "llm": "v1"},
            now=_NOW,
        )

    return run


def test_it03_04_resolve_frame_builds_enrich_resolved(frame: Callable[..., None]) -> None:
    """IT03-04 (unit part): resolve_frame registers stores, resolves and unregisters views."""
    wh = _warehouse()
    _add_record(wh, "inc_1", "h1", _NOW - timedelta(days=10))
    _add_record(wh, "inc_2", "h2", _NOW - timedelta(days=400))
    frame(wh)
    got = {
        (r[0], r[1]): r[2:]
        for r in wh.execute(
            "SELECT record_id, question, status, answer, decider, escalated, review_status, "
            "probability FROM enrich_resolved"
        ).fetchall()
    }
    assert len(got) == 6  # 2 records x 3 non-pair questions
    assert got[("inc_1", "q_bool")][:5] == ("final", "true", "laya", False, "pending")
    assert got[("inc_2", "q_bool")][:5] == ("final", "false", "human", False, "confirmed")
    choice = got[("inc_1", "q_choice")]
    assert choice[:5] == ("final", "a", "openjev", False, "none")
    a, b = (0.7 + 1e-9) ** 2, (0.3 + 1e-9) ** 2
    expected = a / (a + b)
    assert choice[5] == pytest.approx(expected)  # T = 0.5 from the calibration file
    assert got[("inc_2", "q_score")][:3] == ("final", "2", "openjev")
    assert got[("inc_1", "q_score")][0] == "queue"  # in the bootstrap window
    assert got[("inc_2", "q_choice")][0] == "out_of_scope"  # opened before the window
    views = {r[0] for r in wh.execute("SELECT view_name FROM duckdb_views()").fetchall()}
    assert views.isdisjoint({"cache_rows", "human_latest", "calib", "qmeta", "versions"})


def test_it03_04_resolve_frame_error_unregisters(frame: Callable[..., None]) -> None:
    """IT03-04 (unit part): a SQL error is a SchemaViolation and leaves no view registered."""
    wh = _warehouse()
    wh.execute("DROP TABLE core.problem")
    with pytest.raises(SchemaViolation, match=r"^resolve_decisions: "):
        frame(wh)
    with pytest.raises(duckdb.CatalogException):
        wh.execute("SELECT * FROM qmeta")


def test_it03_04_qmeta_needs_every_primary() -> None:
    """IT03-04 (unit part): a non-pair question without a primary decider is a ConfigError."""
    with pytest.raises(ConfigError, match="no primary decider"):
        resolve._qmeta_table(_QS, cfg=_CFG, deciders=DecidersSettings(), primaries={})
    meta = resolve._qmeta_table(_QS, cfg=_CFG, deciders=DecidersSettings(), primaries=_PRIMARIES)
    rows = {r["question"]: r for r in meta.to_pylist()}
    assert set(rows) == {"q_bool", "q_choice", "q_score"}  # pair question excluded
    assert rows["q_bool"]["chain"] == ["openjev", "llm"]
    assert rows["q_choice"]["chain"] == ["llm"]
    assert rows["q_choice"]["labels"] == ["a", "b"]
    assert rows["q_score"]["labels"] == ["0", "1", "2", "3"]
