"""Tests for herness.enrich.link_changes (U03-107 ... U03-110; T03-26).

The core DDL is not in the tree yet: ``core.incident`` and ``core.change`` are created by hand
with only the columns the link stage reads, next to the real ``000_settings.sql``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import duckdb
import pyarrow.parquet as pq
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.support.fake_clock import FakeClock

from herness.core.errors import ConfigError, SchemaViolation
from herness.core.types import Answer, DecisionOutput, QuestionSet
from herness.enrich.cache import DecisionCache
from herness.enrich.calibrate import CalibrationResult, CalibrationStore
from herness.enrich.layout import EnrichPaths
from herness.enrich.link_changes import (
    heuristic_link_score,
    link_candidates,
    pair_inputs,
    run_link_stage,
)
from herness.enrich.questions import load_question_set
from herness.enrich.settings import DecisionsConfig
from herness.enrich.text import content_hash, pair_text

pytestmark = pytest.mark.unit

SETTINGS_SQL = Path(__file__).resolve().parents[3] / "herness/model/sql/000_settings.sql"
T0 = datetime(2026, 9, 1, 12, tzinfo=UTC)
QSV = "qs-2026-09-01"
OJ: tuple[Literal["openjev"], str] = ("openjev", "openjev-0.4.0/qwen:7b")
PAIR_Q = {
    "id": "change_caused_pair",
    "type": "bool",
    "applies_to": ["incident"],
    "instructions": "The change described caused the incident described.",
    "threshold": 0.70,
    "scoring_use": False,
}
DEFAULTS = {"tau_h": 12.0, "ci_weight": 1.0, "service_weight": 0.6}


def cfg_with(**change_link: Any) -> DecisionsConfig:
    return DecisionsConfig.model_validate(
        {"question_set_version": QSV, "questions": [PAIR_Q], "change_link": change_link}
    )


@dataclass(frozen=True)
class Inc:
    record_id: str
    opened_at: datetime | None = T0
    ci_id: str | None = None
    service_id: str | None = None
    caused_by: str | None = None
    text: str | None = "incident text"


@dataclass(frozen=True)
class Chg:
    record_id: str
    ci_id: str | None = None
    service_id: str | None = None
    actual_end: datetime | None = None
    actual_start: datetime | None = None
    planned_end: datetime | None = None
    outcome: str | None = None
    type: str | None = None
    text: str | None = "change text"


@dataclass
class Report:
    rows: int = 0
    decided: int = 0
    extra: dict[str, int] = field(default_factory=dict)


def make_warehouse(incidents: Sequence[Inc], changes: Sequence[Chg]) -> duckdb.DuckDBPyConnection:
    """In-memory warehouse: 000_settings.sql, minimal core tables, hand-written text rows."""
    wh = duckdb.connect()
    wh.execute(SETTINGS_SQL.read_text("utf-8"))
    wh.execute(
        "CREATE TABLE core.incident (record_id VARCHAR, opened_at TIMESTAMPTZ, ci_id VARCHAR, "
        "service_id VARCHAR, caused_by_change_id VARCHAR)"
    )
    wh.execute(
        "CREATE TABLE core.change (record_id VARCHAR, type VARCHAR, planned_end TIMESTAMPTZ, "
        "actual_start TIMESTAMPTZ, actual_end TIMESTAMPTZ, ci_id VARCHAR, service_id VARCHAR, "
        "outcome VARCHAR)"
    )
    for i in incidents:
        wh.execute(
            "INSERT INTO core.incident VALUES (?, ?, ?, ?, ?)",
            [i.record_id, i.opened_at, i.ci_id, i.service_id, i.caused_by],
        )
        if i.text is not None:
            wh.execute(
                "INSERT INTO enrich.text_redacted VALUES (?, 'incident', ?, ?)",
                [i.record_id, i.text, content_hash(i.text)],
            )
    for c in changes:
        wh.execute(
            "INSERT INTO core.change VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                c.record_id,
                c.type,
                c.planned_end,
                c.actual_start,
                c.actual_end,
                c.ci_id,
                c.service_id,
                c.outcome,
            ],
        )
        if c.text is not None:
            wh.execute(
                "INSERT INTO enrich.text_redacted VALUES (?, 'change', ?, ?)",
                [c.record_id, c.text, content_hash(c.text)],
            )
    return wh


def cand(wh: duckdb.DuckDBPyConnection) -> dict[tuple[str, str], tuple[str, float]]:
    rows = wh.execute("SELECT incident_id, change_id, method, score FROM link_cand").fetchall()
    return {(i, c): (m, s) for i, c, m, s in rows}


def links(wh: duckdb.DuckDBPyConnection) -> list[tuple[str, str, str, float]]:
    """Rows of ``enrich.incident_change_link``, scores rounded to 9 places."""
    rows = wh.execute(
        "SELECT incident_id, change_id, method, score FROM enrich.incident_change_link "
        "ORDER BY incident_id, change_id"
    ).fetchall()
    return [(i, c, m, round(s, 9)) for i, c, m, s in rows]


def h(delta_h: float, *, ci: bool, outcome: str | None = None, kind: str | None = None) -> float:
    return heuristic_link_score(
        same_ci=ci,
        same_service=not ci,
        delta_h=delta_h,
        outcome=outcome,
        change_type=kind,
        **DEFAULTS,
    )


@pytest.fixture
def paths(tmp_path: Path) -> EnrichPaths:
    return EnrichPaths(data_root=tmp_path, embedding_path="data/e", laya_current_file="data/c")


# --- UT03-102 / PT03-13 ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("same_ci", "same_service", "delta_h", "outcome", "change_type", "expected"),
    [
        (True, False, 0.0, None, None, 1.0),  # same CI at t_c
        (False, True, 0.0, None, None, 0.6),  # service only
        (True, True, 0.0, None, "normal", 1.0),  # CI wins over service
        (True, False, 12.0, None, None, math.exp(-1)),
        (True, False, 72.0, None, None, math.exp(-6)),  # window edge (before_h)
        (False, True, -1.0, None, None, 0.6),  # incident before t_c: delta floored at 0
        (False, True, 0.0, "backed_out", None, 0.75),  # outcome boost
        (False, True, 0.0, "unsuccessful", None, 0.75),
        (False, True, 0.0, "successful_with_issues", None, 0.75),
        (False, True, 0.0, "successful", None, 0.6),
        (False, True, 0.0, None, "emergency", 0.66),
        (False, True, 0.0, "backed_out", "emergency", 0.825),  # both boosts
        (False, True, 6.0, "backed_out", "emergency", 0.825 * math.exp(-0.5)),
        (True, False, 0.0, "backed_out", "emergency", 1.0),  # capped at 1
    ],
)
def test_ut03_102_heuristic_link_score_table(
    *,
    same_ci: bool,
    same_service: bool,
    delta_h: float,
    outcome: str | None,
    change_type: str | None,
    expected: float,
) -> None:
    """UT03-102 window edges, CI vs service, outcome boost, emergency, cap at 1."""
    score = heuristic_link_score(
        same_ci=same_ci,
        same_service=same_service,
        delta_h=delta_h,
        outcome=outcome,
        change_type=change_type,
        **DEFAULTS,
    )
    assert score == pytest.approx(expected)


def test_ut03_102_neither_flag_is_config_error() -> None:
    """UT03-102 a pair matching neither CI nor service is rejected."""
    with pytest.raises(ConfigError):
        heuristic_link_score(
            same_ci=False,
            same_service=False,
            delta_h=0.0,
            outcome=None,
            change_type=None,
            **DEFAULTS,
        )


_FLAGS = st.sampled_from([(True, False), (False, True), (True, True)])
_OUTCOMES = st.sampled_from([None, "successful", "unsuccessful", "backed_out", "canceled"])
_TYPES = st.sampled_from([None, "standard", "normal", "emergency"])


@settings(max_examples=300, deadline=None)
@given(
    flags=_FLAGS,
    d1=st.floats(-200.0, 700.0),
    d2=st.floats(-200.0, 700.0),
    outcome=_OUTCOMES,
    change_type=_TYPES,
    tau_h=st.floats(1.0, 100.0),
    ci_weight=st.floats(0.01, 1.0),
    service_weight=st.floats(0.01, 1.0),
)
def test_pt03_13_score_bounded_and_non_increasing(  # noqa: PLR0913 - hypothesis strategies
    *,
    flags: tuple[bool, bool],
    d1: float,
    d2: float,
    outcome: str | None,
    change_type: str | None,
    tau_h: float,
    ci_weight: float,
    service_weight: float,
) -> None:
    """PT03-13 score in (0, 1], non-increasing in delta_h."""
    lo, hi = sorted((d1, d2))

    def score(delta: float) -> float:
        return heuristic_link_score(
            same_ci=flags[0],
            same_service=flags[1],
            delta_h=delta,
            outcome=outcome,
            change_type=change_type,
            tau_h=tau_h,
            ci_weight=ci_weight,
            service_weight=service_weight,
        )

    s_lo, s_hi = score(lo), score(hi)
    assert 0.0 < s_hi <= 1.0
    assert 0.0 < s_lo <= 1.0
    assert s_lo >= s_hi


# --- UT03-103 ----------------------------------------------------------------------------------

_RULE_CHANGES = (
    Chg("c_ci_1h", ci_id="ci1", actual_end=T0 - timedelta(hours=1)),
    Chg("c_svc_3h", ci_id="ci9", service_id="svc1", actual_end=T0 - timedelta(hours=3)),
    Chg(
        "c_emg",
        service_id="svc1",
        actual_end=T0 - timedelta(hours=6),
        outcome="backed_out",
        type="emergency",
    ),
    Chg("c_edge_before", ci_id="ci1", actual_end=T0 - timedelta(hours=72)),
    Chg("c_past_before", ci_id="ci1", actual_end=T0 - timedelta(hours=72, seconds=1)),
    Chg("c_edge_after", ci_id="ci1", actual_end=T0 + timedelta(hours=1)),
    Chg("c_past_after", ci_id="ci1", actual_end=T0 + timedelta(hours=1, seconds=1)),
    Chg(
        "c_running",
        ci_id="ci1",
        actual_start=T0 - timedelta(hours=100),
        actual_end=T0 + timedelta(hours=50),
    ),
    Chg("c_start_only", ci_id="ci1", actual_start=T0 - timedelta(hours=2)),
    Chg("c_planned", service_id="svc1", planned_end=T0 - timedelta(hours=2)),
    Chg("c_no_time", ci_id="ci1"),
    Chg("c_other", ci_id="ci2", service_id="svc2", actual_end=T0),
    Chg("c_null_keys", actual_end=T0),
)
_RULE_EXPECTED = {
    "c_ci_1h": h(1, ci=True),
    "c_svc_3h": h(3, ci=False),
    "c_emg": h(6, ci=False, outcome="backed_out", kind="emergency"),
    "c_edge_before": h(72, ci=True),
    "c_edge_after": h(-1, ci=True),
    "c_running": h(-50, ci=True),
    "c_start_only": h(2, ci=True),
    "c_planned": h(2, ci=False),
}


def test_ut03_103_window_rules_match_heuristic() -> None:
    """UT03-103 each window rule yields rows equal to U03-107 on each pair."""
    wh = make_warehouse([Inc("i1", ci_id="ci1", service_id="svc1")], _RULE_CHANGES)
    link_candidates(wh, cfg=cfg_with(min_score=0.0, top_n=20))
    got = cand(wh)
    assert set(got) == {("i1", change) for change in _RULE_EXPECTED}
    for change, expected in _RULE_EXPECTED.items():
        method, score = got["i1", change]
        assert method == "time_ci_window"
        assert score == pytest.approx(expected, rel=1e-12)


def test_ut03_103_top3_min_score_and_source_override() -> None:
    """UT03-103 top 3 by score then change_id; min_score; a source-field link overrides."""
    changes = [
        Chg("c_src", ci_id="ci1", actual_end=T0 - timedelta(hours=2)),  # also a window match
        Chg("c_a", ci_id="ci1", actual_end=T0 - timedelta(hours=1)),
        Chg("c_c", service_id="svc1", actual_end=T0 - timedelta(hours=1)),  # tie with c_b
        Chg("c_b", service_id="svc1", actual_end=T0 - timedelta(hours=1)),
        Chg("c_d", service_id="svc1", actual_end=T0 - timedelta(hours=4)),
        Chg("c_low", service_id="svc1", actual_end=T0 - timedelta(hours=24)),  # 0.08 < 0.30
        Chg("c_src2"),  # source field only, no window
    ]
    incidents = [
        Inc("i1", ci_id="ci1", service_id="svc1", caused_by="c_src"),
        Inc("i2", caused_by="c_src2"),
        Inc("i3", caused_by="c_missing"),  # does not resolve to a change
    ]
    wh = make_warehouse(incidents, changes)
    link_candidates(wh, cfg=cfg_with())
    got = cand(wh)
    assert got["i1", "c_src"] == ("source_field", 1.0)
    assert got["i2", "c_src2"] == ("source_field", 1.0)
    window = sorted(c for (i, c), (m, _) in got.items() if i == "i1" and m == "time_ci_window")
    assert window == ["c_a", "c_b", "c_c"]  # c_b before c_c on the tie; c_d is fourth
    assert not any(i == "i3" for i, _ in got)
    assert len(got) == 5


def test_ut03_103_missing_table_is_schema_violation() -> None:
    """UT03-103 a missing core table raises SchemaViolation."""
    wh = duckdb.connect()
    with pytest.raises(SchemaViolation, match="link stage candidates"):
        link_candidates(wh, cfg=cfg_with())


# --- UT03-104 ----------------------------------------------------------------------------------


def _band_warehouse() -> duckdb.DuckDBPyConnection:
    """Five band pairs (service-only, 0.6 * exp(-dh/12)), one without text, one out of band."""
    incidents = [
        Inc("i_old", opened_at=T0 - timedelta(days=5), service_id="s"),
        Inc("i_new", opened_at=T0, service_id="s", text="x" * 8_000),
        Inc("i_b", opened_at=T0 - timedelta(days=1), service_id="s"),
        Inc("i_a", opened_at=T0 - timedelta(days=1), service_id="s"),
        Inc("i_notext", opened_at=T0 + timedelta(days=3), service_id="s", text=None),
        Inc("i_ci", opened_at=T0 + timedelta(days=9), ci_id="k"),
    ]
    changes = [
        Chg("c_old", service_id="s", actual_end=T0 - timedelta(days=5, hours=1)),
        Chg("c_new2", service_id="s", actual_end=T0 - timedelta(hours=2), text="y" * 8_000),
        Chg("c_new1", service_id="s", actual_end=T0 - timedelta(hours=1)),
        Chg("c_day", service_id="s", actual_end=T0 - timedelta(days=1, hours=1)),
        Chg("c_nt", service_id="s", actual_end=T0 + timedelta(days=3)),
        Chg("c_ci", ci_id="k", actual_end=T0 + timedelta(days=9)),  # score 1.0: above band
    ]
    return make_warehouse(incidents, changes)


def test_ut03_104_pair_inputs_capped_ordered_index_written(paths: EnrichPaths) -> None:
    """UT03-104 band pairs above the cap: capped, ordered, pair index written."""
    wh = _band_warehouse()
    cfg = cfg_with(decider_max_pairs=4)
    qs = load_question_set(cfg)
    link_candidates(wh, cfg=cfg)
    inputs = pair_inputs(wh, cfg=cfg, qs=qs, paths=paths, build_id="b-1")
    ids = [item.record_id for item in inputs]
    assert ids == ["i_new|c_new1", "i_new|c_new2", "i_a|c_day", "i_b|c_day"]
    for item in inputs:
        assert item.entity == "incident"
        assert item.question_ids == ("change_caused_pair",)
        assert item.content_hash == content_hash(item.text)
        assert len(item.text) <= 12_000
    assert inputs[2].text == pair_text("incident text", "change text")
    assert inputs[1].text.startswith("INCIDENT:\n" + "x" * 10)
    index = pq.read_table(paths.pairs_dir() / "part-b-1.parquet")
    assert index.column_names == ["incident_id", "change_id", "content_hash"]
    assert index.to_pylist()[0] == {
        "incident_id": "i_new",
        "change_id": "c_new1",
        "content_hash": inputs[0].content_hash,
    }
    assert index.num_rows == 4
    # the same build overwrites its part
    pair_inputs(wh, cfg=cfg_with(decider_max_pairs=1), qs=qs, paths=paths, build_id="b-1")
    assert pq.read_table(paths.pairs_dir() / "part-b-1.parquet").num_rows == 1
    assert [p.name for p in paths.pairs_dir().iterdir()] == ["part-b-1.parquet"]


def test_ut03_104_band_excludes_skips_and_guards(paths: EnrichPaths) -> None:
    """UT03-104 out-of-band and textless pairs skipped; use_decider off; bad inputs rejected."""
    wh = _band_warehouse()
    cfg = cfg_with()
    qs = load_question_set(cfg)
    link_candidates(wh, cfg=cfg)
    ids = {item.record_id for item in pair_inputs(wh, cfg=cfg, qs=qs, paths=paths, build_id="b")}
    assert ids == {"i_new|c_new1", "i_new|c_new2", "i_a|c_day", "i_b|c_day", "i_old|c_old"}
    off = DecisionsConfig.model_validate(
        {"question_set_version": QSV, "questions": [], "change_link": {"use_decider": False}}
    )
    assert pair_inputs(wh, cfg=off, qs=qs, paths=paths, build_id="b2") == []
    assert not (paths.pairs_dir() / "part-b2.parquet").exists()
    with pytest.raises(ConfigError):
        pair_inputs(
            wh, cfg=cfg, qs=QuestionSet(version=QSV, questions=()), paths=paths, build_id="b"
        )
    for bad in ("../x", "a/b", "", "x."):
        with pytest.raises(ConfigError, match="build_id"):
            pair_inputs(wh, cfg=cfg, qs=qs, paths=paths, build_id=bad)


def test_ut03_104_without_link_cand_is_schema_violation(paths: EnrichPaths) -> None:
    """UT03-104 pair_inputs before link_candidates raises SchemaViolation."""
    wh = _band_warehouse()
    cfg = cfg_with()
    with pytest.raises(SchemaViolation, match="band pairs"):
        pair_inputs(wh, cfg=cfg, qs=load_question_set(cfg), paths=paths, build_id="b")


# --- UT03-105 ----------------------------------------------------------------------------------


def _answer_outputs(inputs: Sequence[Any], p_true: dict[str, float], version: str) -> list[Any]:
    outs = []
    for item in inputs:
        if item.record_id not in p_true:
            continue
        p = p_true[item.record_id]
        answer = "true" if p >= 0.5 else "false"
        outs.append(
            DecisionOutput(
                record_id=item.record_id,
                content_hash=item.content_hash,
                decider=OJ[0],
                decider_version=version,
                answers={
                    "change_caused_pair": Answer(
                        answer=answer,
                        probability=max(p, 1 - p),
                        distribution={"true": p, "false": 1 - p},
                    )
                },
            )
        )
    return outs


def _write_answers(
    cache: DecisionCache, qs: QuestionSet, outputs: list[Any], version: str = OJ[1]
) -> None:
    with cache.writer(OJ[0], version, questions=qs, flush_rows=100) as writer:
        writer.add(outputs, samples=None)


def _calibrated(p: float, t: float) -> float:
    return 1.0 / (1.0 + math.exp(-math.log(p / (1 - p)) / t))


def test_ut03_105_decider_blend_and_top_n(paths: EnrichPaths) -> None:
    """UT03-105 cached pair answers: score = 0.5*h + 0.5*p', method decider; <= 3 per incident."""
    changes = [Chg("c_src")] + [
        Chg(f"c_{k}", service_id="s", actual_end=T0 - timedelta(hours=k), text=f"change {k}")
        for k in range(1, 6)
    ]
    wh = make_warehouse([Inc("i1", service_id="s", caused_by="c_src")], changes)
    cfg = cfg_with()
    qs = load_question_set(cfg)
    link_candidates(wh, cfg=cfg)  # window: c_1, c_2, c_3 (top 3); source: c_src
    inputs = pair_inputs(wh, cfg=cfg, qs=qs, paths=paths, build_id="b")
    cache = DecisionCache(paths, QSV)
    _write_answers(cache, qs, _answer_outputs(inputs, {"i1|c_2": 0.9, "i1|c_3": 0.95}, OJ[1]))
    calibration = CalibrationStore(paths)
    calibration.save(
        *OJ,
        QSV,
        {"change_caused_pair": CalibrationResult(2.0, 0.05, 0.1, 0.9, 200, uncalibrated=False)},
    )
    report = Report()
    run_link_stage(
        wh, cfg=cfg, qs=qs, cache=cache, calibration=calibration, pair_decider=OJ, report=report
    )
    rows = {c: (m, s) for _, c, m, s in links(wh)}
    assert len(rows) == 3  # top_n including the source-field row, which ranks first
    assert rows["c_src"] == ("source_field", 1.0)
    for change, p in (("c_2", 0.9), ("c_3", 0.95)):
        method, score = rows[change]
        assert method == "decider"
        expected = 0.5 * h(int(change[-1]), ci=False) + 0.5 * _calibrated(p, 2.0)
        assert score == pytest.approx(expected, rel=1e-9)
    assert "c_1" not in rows  # 0.554 heuristic, below both blended scores
    assert (report.rows, report.decided) == (3, 2)
    assert wh.execute("SELECT count(*) FROM enrich.decision").fetchone() == (0,)


def test_ut03_105_no_answer_other_version_or_none_keeps_heuristic(paths: EnrichPaths) -> None:
    """UT03-105 unanswered band pairs, other versions and pair_decider None keep the heuristic."""
    wh = make_warehouse([Inc("i1", service_id="s")], [Chg("c_1", service_id="s", actual_end=T0)])
    cfg = cfg_with()
    qs = load_question_set(cfg)
    link_candidates(wh, cfg=cfg)
    inputs = pair_inputs(wh, cfg=cfg, qs=qs, paths=paths, build_id="b")
    cache = DecisionCache(paths, QSV)
    calibration = CalibrationStore(paths)
    run_link_stage(
        wh, cfg=cfg, qs=qs, cache=cache, calibration=calibration, pair_decider=OJ, report=Report()
    )  # empty cache
    other = "openjev-0.5.0/qwen:7b"
    _write_answers(cache, qs, _answer_outputs(inputs, {"i1|c_1": 0.9}, other), version=other)
    run_link_stage(
        wh, cfg=cfg, qs=qs, cache=cache, calibration=calibration, pair_decider=OJ, report=Report()
    )
    run_link_stage(
        wh, cfg=cfg, qs=qs, cache=cache, calibration=calibration, pair_decider=None, report=Report()
    )
    assert links(wh) == [("i1", "c_1", "time_ci_window", 0.6)] * 3


def test_ut03_105_latest_answer_uncalibrated_and_decider_off(
    paths: EnrichPaths, fake_clock: FakeClock
) -> None:
    """UT03-105 the latest cached answer counts with T = 1; use_decider off ignores the cache."""
    wh = make_warehouse([Inc("i1", service_id="s")], [Chg("c_1", service_id="s", actual_end=T0)])
    cfg = cfg_with()
    qs = load_question_set(cfg)
    link_candidates(wh, cfg=cfg)
    inputs = pair_inputs(wh, cfg=cfg, qs=qs, paths=paths, build_id="b")
    cache = DecisionCache(paths, QSV)
    _write_answers(cache, qs, _answer_outputs(inputs, {"i1|c_1": 0.2}, OJ[1]))
    fake_clock.advance(60)
    _write_answers(cache, qs, _answer_outputs(inputs, {"i1|c_1": 0.8}, OJ[1]))
    report = Report()
    run_link_stage(
        wh,
        cfg=cfg,
        qs=qs,
        cache=cache,
        calibration=CalibrationStore(paths),
        pair_decider=OJ,
        report=report,
    )
    assert links(wh) == [("i1", "c_1", "decider", round(0.5 * 0.6 + 0.5 * 0.8, 9))]
    off = cfg_with(use_decider=False)
    wh.execute("DELETE FROM enrich.incident_change_link")
    run_link_stage(
        wh,
        cfg=off,
        qs=qs,
        cache=cache,
        calibration=CalibrationStore(paths),
        pair_decider=OJ,
        report=report,
    )
    assert links(wh) == [("i1", "c_1", "time_ci_window", 0.6)]


def test_ut03_105_without_link_cand_is_schema_violation(paths: EnrichPaths) -> None:
    """UT03-105 the link stage without link_cand raises SchemaViolation and unregisters."""
    wh = make_warehouse([], [])
    cfg = cfg_with()
    with pytest.raises(SchemaViolation, match="link stage insert"):
        run_link_stage(
            wh,
            cfg=cfg,
            qs=load_question_set(cfg),
            cache=DecisionCache(paths, QSV),
            calibration=CalibrationStore(paths),
            pair_decider=None,
            report=Report(),
        )
    with pytest.raises(duckdb.CatalogException):
        wh.execute("SELECT * FROM _link_decided")
