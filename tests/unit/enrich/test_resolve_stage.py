"""Tests for spot-check selection and the ``decision_wide`` DDL (U03-81, U03-82, T03-20).

UT03-77 builds ``enrich_resolved`` by hand (the precondition of `select_spot_checks`) and
recomputes the expected picks with :mod:`hashlib`, independently of DuckDB's ``sha256``.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest

from herness.core.errors import SchemaViolation
from herness.core.types import Question, QuestionSet
from herness.enrich import resolve
from herness.enrich.questions import question_fingerprint
from herness.enrich.settings import DecisionsConfig

pytestmark = pytest.mark.unit

_SETTINGS_SQL = Path(__file__).resolve().parents[3] / "herness/model/sql/000_settings.sql"
_QSV = "qs-2026-10-01.1"
_BUILD = "b-20260926-0001"
_SINCE = datetime(2026, 9, 25, tzinfo=UTC)
_PAYLOAD_KEYS = {
    "record_id", "content_hash", "question", "question_fingerprint", "question_set_version",
    "answer", "probability", "decider", "decider_version", "purpose", "text_ref",
}  # fmt: skip


def _question(qid: str, **kw: Any) -> Question:
    base = {"id": qid, "type": "bool", "instructions": "Classify this ticket with care, please."}
    return Question.model_validate({**base, "threshold": 0.7, **kw})


_QS = QuestionSet(
    version=_QSV,
    questions=(
        _question("q_bool"),
        _question("q_other", threshold=0.8),
        _question("change_caused_pair", applies_to=("change",)),
    ),
)
_CFG = DecisionsConfig.model_validate(
    {"question_set_version": _QSV, "questions": (), "change_link": {"use_decider": False}}
)


def _resolved(rows: list[tuple[str, str, str, float, str, datetime]]) -> duckdb.DuckDBPyConnection:
    """A connection holding ``enrich_resolved`` rows (record, hash, question, p, decider, at)."""
    wh = duckdb.connect()
    wh.execute(
        "CREATE TEMP TABLE enrich_resolved (record_id VARCHAR, entity VARCHAR, "
        "content_hash VARCHAR, question VARCHAR, status VARCHAR, answer VARCHAR, "
        "probability DOUBLE, decider VARCHAR, decider_version VARCHAR, escalated BOOLEAN, "
        "decided_at TIMESTAMPTZ, review_status VARCHAR)"
    )
    wh.executemany(
        "INSERT INTO enrich_resolved VALUES (?, 'incident', ?, ?, 'final', 'true', ?, ?, 'v1', "
        "false, ?, 'none')",
        [list(r) for r in rows],
    )
    return wh


def _key(content_hash: str, question: str = "q_bool", build_id: str = _BUILD) -> str:
    return hashlib.sha256(f"{build_id}|{content_hash}|{question}".encode()).hexdigest()


def _world(n_rows: int, band_every: int) -> list[tuple[str, str, str, float, str, datetime]]:
    """``n_rows`` new laya rows of q_bool; every ``band_every``-th is in [0.7, 0.8]."""
    at = _SINCE + timedelta(hours=1)
    rows = [
        (f"inc_{i}", f"h{i}", "q_bool", 0.75 if i % band_every == 0 else 0.95, "laya", at)
        for i in range(n_rows)
    ]
    old = _SINCE - timedelta(days=1)
    rows += [(f"old_{i}", f"o{i}", "q_bool", 0.75, "laya", old) for i in range(50)]
    rows += [(f"hum_{i}", f"u{i}", "q_bool", 1.0, "human", at) for i in range(50)]
    return rows


def _expected(rows: list[tuple[str, str, str, float, str, datetime]], n: int) -> list[str]:
    """Reference picks: n//2 uniform by hash, then band, then uniform fill."""
    pool = sorted(
        (r for r in rows if r[4] != "human" and r[5] >= _SINCE and r[2] == "q_bool"),
        key=lambda r: _key(r[1]),
    )
    chosen = [r[1] for r in pool[: n // 2]]
    band = [r[1] for r in pool if 0.7 <= r[3] <= 0.8 and r[1] not in chosen]
    chosen += band[: n - n // 2]
    chosen += [r[1] for r in pool if r[1] not in chosen][: n - len(chosen)]
    return chosen


def _select(wh: duckdb.DuckDBPyConnection, **kw: Any) -> list[dict[str, object]]:
    args: dict[str, Any] = {
        "qs": _QS, "cfg": _CFG, "build_id": _BUILD, "since": _SINCE,
        "open_counts": {"q_bool": 290},
    }  # fmt: skip
    return resolve.select_spot_checks(wh, **{**args, **kw})


# --- UT03-77 -----------------------------------------------------------------------------------


def test_ut03_77_capped_by_open_cap_half_uniform_half_band() -> None:
    """UT03-77 10,000 new rows, open count 290: 10 picks, 5 uniform + 5 band, deterministic."""
    rows = _world(10_000, band_every=50)  # 200 band rows: uniform picks rarely hit the band
    wh = _resolved(rows)
    got = _select(wh)
    assert len(got) == 10
    hashes = [str(p["content_hash"]) for p in got]
    assert hashes == _expected(rows, 10)
    in_band = [h for h in hashes if int(h[1:]) % 50 == 0]
    by_hash = sorted(
        (r for r in rows if r[4] == "laya" and r[5] >= _SINCE), key=lambda r: _key(r[1])
    )
    uniform = [r[1] for r in by_hash[:5]]
    assert hashes[:5] == uniform
    assert set(hashes[5:]) <= set(in_band)
    assert len(set(hashes[5:])) == 5
    assert _select(wh) == got  # deterministic across runs
    assert _select(_resolved(rows)) == got
    assert [str(p["content_hash"]) for p in _select(wh, build_id="b-other")] != hashes


def test_ut03_77_payload_shape_carries_no_text() -> None:
    """UT03-77 payload per design 03 §4.6: purpose spot_check, text_ref, no text (TH03-03)."""
    wh = _resolved(_world(2_000, band_every=10))
    got = _select(wh, open_counts={})
    assert len(got) == 2  # floor(0.001 x 2000)
    fp = question_fingerprint(_QS.questions[0])
    for payload in got:
        assert set(payload) == _PAYLOAD_KEYS
        assert payload["purpose"] == "spot_check"
        assert payload["text_ref"] == "enrich.text_redacted"
        assert payload["question_set_version"] == _QSV
        assert payload["question_fingerprint"] == fp
        assert payload["decider"] == "laya"
        assert payload["decider_version"] == "v1"
        assert payload["answer"] == "true"
        assert isinstance(payload["probability"], float)
        assert str(payload["record_id"]).startswith("inc_")


def test_ut03_77_band_shortfall_filled_from_uniform_pool() -> None:
    """UT03-77 a band with too few rows: the rest come from the uniform pool, no repeats."""
    rows = _world(10_000, band_every=5_000)  # band rows h0 and h5000 only
    got = [str(p["content_hash"]) for p in _select(_resolved(rows), open_counts={})]
    assert len(got) == 10  # min(50, 10, 300)
    assert len(set(got)) == 10
    assert got == _expected(rows, 10)
    assert {"h0", "h5000"} <= set(got)


def test_ut03_77_caps() -> None:
    """UT03-77 nightly max, the open cap and the 0.1 % rate bound n; odd n favours the band."""
    rows = _world(100_000, band_every=10)
    wh = _resolved(rows)
    assert len(_select(wh, open_counts={})) == 50  # nightly_max_per_question
    assert _select(wh, open_counts={"q_bool": 300}) == []
    assert _select(wh, open_counts={"q_bool": 400}) == []
    small = _resolved(_world(1_500, band_every=10))
    one = _select(small, open_counts={})
    assert len(one) == 1  # floor(1.5): 0 uniform + 1 band
    assert one[0]["probability"] == pytest.approx(0.75)


def test_ut03_77_duplicate_content_counted_once_per_pick() -> None:
    """UT03-77 records sharing a content hash are one candidate (lowest record id)."""
    at = _SINCE + timedelta(hours=1)
    rows = [(f"inc_{i}", "same", "q_bool", 0.95, "laya", at) for i in range(3_000)]
    got = _select(_resolved(rows), open_counts={})
    assert [(p["record_id"], p["content_hash"]) for p in got] == [("inc_0", "same")]


def test_ut03_77_per_question_in_set_order() -> None:
    """UT03-77 each question is sampled on its own, in question-set order."""
    at = _SINCE + timedelta(hours=1)
    rows = [(f"a{i}", f"a{i}", "q_other", 0.85, "openjev", at) for i in range(1_000)]
    rows += [(f"b{i}", f"b{i}", "q_bool", 0.95, "laya", at) for i in range(2_000)]
    got = _select(_resolved(rows), open_counts={})
    assert [p["question"] for p in got] == ["q_bool", "q_bool", "q_other"]


def test_ut03_77_missing_frame_is_schema_violation() -> None:
    """UT03-77 without ``enrich_resolved`` the selection raises SchemaViolation."""
    with pytest.raises(SchemaViolation, match=r"^select_spot_checks: "):
        _select(duckdb.connect())


# --- UT03-78 -----------------------------------------------------------------------------------

_TWO = QuestionSet(
    version=_QSV,
    questions=(
        _question("change_caused_pair", applies_to=("change",)),
        _question("q_bool"),
        _question("q_choice", type="choice", options={"a": "Option A.", "b": "Option B."}),
    ),
)
_EXPECTED_DDL = (
    "CREATE OR REPLACE VIEW enrich.decision_wide AS SELECT record_id, "
    "MAX(answer) FILTER (WHERE question = 'q_bool') AS \"q_bool\", "
    "MAX(probability) FILTER (WHERE question = 'q_bool') AS \"q_bool_p\", "
    "MAX(answer) FILTER (WHERE question = 'q_choice') AS \"q_choice\", "
    "MAX(probability) FILTER (WHERE question = 'q_choice') AS \"q_choice_p\" "
    "FROM enrich.decision GROUP BY record_id"
)


def test_ut03_78_decision_wide_ddl_runs_on_duckdb() -> None:
    """UT03-78 QS of 2 questions: expected DDL; it runs on DuckDB; wide row values."""
    ddl = resolve.decision_wide_sql(_TWO)
    assert ddl == _EXPECTED_DDL
    wh = duckdb.connect()
    wh.execute(_SETTINGS_SQL.read_text("utf-8"))
    wh.executemany(
        "INSERT INTO enrich.decision (record_id, question, answer, probability) VALUES (?,?,?,?)",
        [
            ["inc_1", "q_bool", "true", 0.9],
            ["inc_1", "q_choice", "b", 0.6],
            ["inc_2", "q_bool", "false", 1.0],
        ],
    )
    wh.execute(ddl)
    wh.execute(ddl)  # CREATE OR REPLACE: idempotent
    got = wh.execute("SELECT * FROM enrich.decision_wide ORDER BY record_id").fetchall()
    cols = [d[0] for d in wh.description or ()]
    assert cols == ["record_id", "q_bool", "q_bool_p", "q_choice", "q_choice_p"]
    assert got == [("inc_1", "true", 0.9, "b", 0.6), ("inc_2", "false", 1.0, None, None)]
