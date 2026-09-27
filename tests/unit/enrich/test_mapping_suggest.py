"""Tests for herness.enrich.mapping_suggest (U03-111 ... U03-114; T03-27).

The core tables are created by hand with only the columns the module reads, next to the real
``000_settings.sql``. Review items go to the real migrated ops store (T11-40).
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

import duckdb
import numpy as np
import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st
from rapidfuzz import fuzz
from structlog.testing import capture_logs
from tests.support.fake_clock import FakeClock
from tests.support.ops_store import OpsStoreHandle

from herness.core.errors import SchemaViolation
from herness.core.types import Question, QuestionSet
from herness.enrich import mapping_suggest as ms
from herness.enrich.embed import Encoder
from herness.enrich.settings import DecisionsConfig, MappingWeights
from herness.enrich.text import normalize_text
from herness.store.ops import create_review_item, decide_review_item, list_review_items
from herness.store.ops import shared as ops_shared

pytestmark = pytest.mark.unit

SETTINGS_SQL = Path(__file__).resolve().parents[3] / "herness/model/sql/000_settings.sql"
QSV = "qs-2026-09-01"
USER = "ab" * 16
T0 = datetime.datetime(2026, 9, 1, 12, tzinfo=datetime.UTC)
ABBR = {"pmt": "payment", "auth": "authentication"}
DIM = 1024


@dataclass
class _Report:
    status: Literal["done", "skipped", "degraded", "failed"] = "done"
    rows: int = 0


class _StubRedactor:
    """Records every input; wraps each text as ``<R:...>``; fails texts containing ``FAIL``."""

    def __init__(self) -> None:
        self.inputs: list[str | None] = []

    def redact_batch(self, texts: Sequence[str | None]) -> list[str | None]:
        self.inputs.extend(texts)
        return [None if t is None or "FAIL" in t else f"<R:{t}>" for t in texts]


class _RecordingEncoder:
    """Stands in for `Encoder`: records texts; every text maps to the same unit vector."""

    def __init__(self) -> None:
        self.texts: list[str] = []

    def encode(self, texts: Sequence[str], *, batch_size: int) -> np.ndarray:
        self.texts.extend(texts)
        rows = np.zeros((len(texts), DIM), dtype=np.float32)
        rows[:, 0] = 1.0
        return rows


def _enc(encoder: _RecordingEncoder) -> Encoder:
    return cast("Encoder", encoder)


@dataclass
class _Metrics:
    calls: list[tuple[str, float, dict[str, Any]]] = field(default_factory=list)

    def __call__(self, name: str, value: float = 1.0, **kw: Any) -> None:
        self.calls.append((name, value, kw))


@pytest.fixture(autouse=True)
def _no_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    """`decide_review_item` audits under the caller's config; these tests need no audit trail."""
    monkeypatch.setattr(ops_shared, "audit", lambda *_a, **_k: None)


@pytest.fixture
def metrics(monkeypatch: pytest.MonkeyPatch) -> _Metrics:
    rec = _Metrics()
    monkeypatch.setattr(ms, "record_counter", rec)
    return rec


def _wh() -> duckdb.DuckDBPyConnection:
    wh = duckdb.connect()
    wh.execute(SETTINGS_SQL.read_text("utf-8"))
    wh.execute(
        "CREATE TABLE core.work_item (record_id VARCHAR, project VARCHAR, component VARCHAR, "
        "service_id VARCHAR, summary VARCHAR, created_at TIMESTAMPTZ)"
    )
    wh.execute("CREATE TABLE core.team (team_id VARCHAR, name VARCHAR, active BOOLEAN)")
    wh.execute("CREATE TABLE core.service (service_id VARCHAR, name VARCHAR)")
    wh.execute("CREATE TABLE core.service_map (service_id VARCHAR, team_id VARCHAR)")
    wh.execute(
        "CREATE TABLE core.incident (record_id VARCHAR, team_id VARCHAR, service_id VARCHAR, "
        "opened_at TIMESTAMPTZ)"
    )
    return wh


def _cfg(**mapping: object) -> DecisionsConfig:
    return DecisionsConfig.model_validate(
        {
            "question_set_version": QSV,
            "questions": (),
            "change_link": {"use_decider": False},
            "mapping_suggest": mapping,
        }
    )


def _unit(*weights: tuple[int, float]) -> np.ndarray:
    v = np.zeros(DIM, dtype=np.float32)
    for i, w in weights:
        v[i] = w
    return v / np.linalg.norm(v)


def _vectors(
    subjects: Sequence[ms.Subject],
    services: Sequence[ms.Service],
    subject_vecs: np.ndarray | None = None,
    service_vecs: np.ndarray | None = None,
) -> ms.MappingVectors:
    same = _unit((0, 1.0))
    return ms.MappingVectors(
        tuple(subjects),
        tuple(services),
        subject_vecs if subject_vecs is not None else np.stack([same] * len(subjects)),
        service_vecs if service_vecs is not None else np.stack([same] * len(services)),
    )


def _team(team_id: str, name: str) -> ms.Subject:
    return ms.Subject("team", None, None, team_id, name)


def _jira(project: str, component: str, n: int = 1) -> ms.Subject:
    return ms.Subject("jira_component", project, component, None, f"{project} {component}", n)


def _pending() -> list[dict[str, object]]:
    items = list_review_items(kind="mapping_suggestion", status="pending", limit=1000)
    return [dict(it.payload) for it in items]


# --- UT03-106 / PT03-14: norm_name --------------------------------------------------------------


def test_ut03_106_norm_name_expands_and_strips() -> None:
    """UT03-106 "PMT-Auth Svc" normalizes to "payment authentication svc"."""
    assert ms.norm_name("PMT-Auth Svc", abbreviations=ABBR) == "payment authentication svc"


def test_ut03_106_norm_name_whole_tokens_and_unicode_punct() -> None:
    """UT03-106 only whole tokens expand; Unicode P* chars become spaces; runs collapse."""
    assert ms.norm_name("  pmtx «Auth»—API  ", abbreviations=ABBR) == "pmtx authentication api"
    assert ms.norm_name("", abbreviations=ABBR) == ""
    assert ms.norm_name("Svc", abbreviations={"svc": "Service-Desk"}) == "service desk"


_KEY = st.from_regex(r"[a-z0-9]{1,16}", fullmatch=True)


@settings(max_examples=300, deadline=None)
@given(
    s=st.text(max_size=60),
    abbreviations=st.dictionaries(_KEY, st.text(min_size=1, max_size=30), max_size=5),
)
def test_pt03_14_norm_name_idempotent(s: str, abbreviations: dict[str, str]) -> None:
    """PT03-14 normalization is idempotent when no expansion token is an abbreviation key."""
    for value in abbreviations.values():
        assume(
            not any(tok in abbreviations for tok in ms.norm_name(value, abbreviations={}).split())
        )
    once = ms.norm_name(s, abbreviations=abbreviations)
    assert ms.norm_name(once, abbreviations=abbreviations) == once


# --- UT03-107: mapping_scores -------------------------------------------------------------------


def test_ut03_107_mapping_scores_known_matrices() -> None:
    """UT03-107 2 subjects x 3 services with known vectors: expected matrices; the Jira row
    moves the co-occurrence weight to semantic (and ignores its co-occurrence entries)."""
    subjects = ["PMT api", "Auth Team"]
    services = ["payment api", "authentication", "search"]
    subject_vecs = np.stack([_unit((0, 1.0)), _unit((1, 0.6), (2, 0.8))])
    service_vecs = np.stack([_unit((0, 1.0)), _unit((1, 1.0)), _unit((2, -1.0))])
    cooc = np.array([[0.9, 0.9, 0.9], [0.25, 0.5, 0.25]], dtype=np.float32)
    w = MappingWeights()

    got = ms.mapping_scores(
        subject_names=subjects,
        subject_types=["jira_component", "team"],
        service_names=services,
        subject_vecs=subject_vecs,
        service_vecs=service_vecs,
        cooccurrence=cooc,
        weights=w,
        abbreviations=ABBR,
    )

    fuzzy = np.array(
        [
            [
                fuzz.token_set_ratio(ms.norm_name(a, abbreviations=ABBR),
                                     ms.norm_name(b, abbreviations=ABBR)) / 100
                for b in services
            ]
            for a in subjects
        ]
    )  # fmt: skip
    semantic = np.array([[1.0, 0.0, 0.0], [0.0, 0.6, 0.0]])
    jira = w.fuzzy * fuzzy[0] + (w.semantic + w.cooccurrence) * semantic[0]
    team = w.fuzzy * fuzzy[1] + w.semantic * semantic[1] + w.cooccurrence * cooc[1]
    for m in (got.fuzzy, got.semantic, got.cooccurrence, got.score):
        assert m.shape == (2, 3)
        assert m.dtype == np.float32
    assert fuzzy[0, 0] == 1.0
    assert fuzzy[1, 1] == 1.0
    np.testing.assert_allclose(got.fuzzy, fuzzy, atol=1e-6)
    np.testing.assert_allclose(got.semantic, semantic, atol=1e-6)
    np.testing.assert_allclose(got.cooccurrence, cooc, atol=1e-6)
    np.testing.assert_allclose(got.score, np.stack([jira, team]), atol=1e-5)
    assert float(got.score.min()) >= 0.0
    assert float(got.score.max()) <= 1.0


def test_ut03_107_mapping_scores_empty_shapes() -> None:
    """UT03-107 no subjects or no services: (s, v) zero matrices."""
    got = ms.mapping_scores(
        subject_names=["a"],
        subject_types=["team"],
        service_names=[],
        subject_vecs=np.zeros((1, DIM), dtype=np.float32),
        service_vecs=np.zeros((0, DIM), dtype=np.float32),
        cooccurrence=np.zeros((1, 0)),
        weights=MappingWeights(),
        abbreviations={},
    )
    assert got.score.shape == (1, 0)
    assert got.fuzzy.dtype == np.float32


# --- UT03-108: prepare_mapping_vectors ----------------------------------------------------------


def _fill(wh: duckdb.DuckDBPyConnection) -> None:
    for i in range(22):  # 22 PAY/api summaries: only the 20 most recent are used
        wh.execute(
            "INSERT INTO core.work_item VALUES (?, 'PAY', 'api', NULL, ?, ?)",
            [f"w{i:02d}", f"Call John {i:02d}", T0 + datetime.timedelta(hours=i)],
        )
    wh.execute("INSERT INTO core.work_item VALUES ('x1', 'WEB', 'ui', NULL, 'FAIL here', ?)", [T0])
    wh.execute("INSERT INTO core.work_item VALUES ('x2', 'WEB', 'ui', NULL, NULL, ?)", [T0])
    wh.execute("INSERT INTO core.work_item VALUES ('x3', 'OPS', 'db', 's1', 'mapped', ?)", [T0])
    wh.execute("INSERT INTO core.work_item VALUES ('x4', 'OPS', NULL, NULL, 'no comp', ?)", [T0])
    wh.execute(
        "INSERT INTO core.team VALUES ('t1', 'Alice''s Team', true), ('t2', 'Mapped', true), "
        "('t3', 'Old', false), ('t4', NULL, NULL)"
    )
    wh.execute("INSERT INTO core.service_map VALUES ('s1', 't2')")
    wh.execute("INSERT INTO core.service VALUES ('s1', 'Payments''; DROP TABLE core.team; --'), "
               "('s2', NULL)")  # fmt: skip
    for i in range(3):
        rid = f"inc{i}"
        wh.execute(
            "INSERT INTO core.incident VALUES (?, 't1', 's1', ?)",
            [rid, T0 + datetime.timedelta(hours=i)],
        )
        wh.execute(
            "INSERT INTO enrich.text_redacted VALUES (?, 'incident', ?, 'h')", [rid, f"{i}" * 250]
        )


def test_ut03_108_prepare_redacts_every_encoder_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT03-108 a stub redactor recording inputs: every encoder text is built from redacted
    names and summaries or from `text_redacted` (first 200 chars, 20 most recent)."""
    redactor = _StubRedactor()
    monkeypatch.setattr(ms, "get_redactor", lambda: redactor)
    wh = _wh()
    _fill(wh)
    q = Question.model_validate(
        {
            "id": "q_service",
            "type": "choice",
            "options_source": "core.service",
            "options": {"s1": "Payments desk", "s2": "Search"},
            "threshold": 0.7,
            "instructions": "Pick the affected service.",
        }
    )
    static = Question.model_validate(
        {
            "id": "q_static",
            "type": "choice",
            "options": {"a": "Option A", "b": "Option B"},
            "threshold": 0.7,
            "instructions": "Pick a static option.",
        }
    )
    encoder = _RecordingEncoder()

    vec = ms.prepare_mapping_vectors(
        wh,
        encoder=_enc(encoder),
        qs=QuestionSet(version=QSV, questions=(q, static)),
    )

    pay = "; ".join(f"<R:Call John {i:02d}>" for i in range(21, 1, -1))
    snippets = "; ".join(f"{i}" * 200 for i in (2, 1, 0))
    expected = [
        f"<R:PAY api>: {pay}",
        "<R:WEB ui>: ",  # the failed summary is dropped (fail closed)
        f"<R:Alice's Team>: {snippets}",
        "<R:>: ",
        f"<R:Payments'; DROP TABLE core.team; -->: {snippets}",
        "<R:>: ",
        "s1: <R:Payments desk>",
        "s2: <R:Search>",
    ]
    assert sorted(encoder.texts) == sorted(normalize_text(t) for t in expected)
    assert "Call John 00" not in " ".join(encoder.texts)
    assert all(t is not None for t in redactor.inputs)
    assert [s.name for s in vec.subjects] == ["PAY api", "WEB ui", "Alice's Team", ""]
    assert [(s.subject_type, s.work_items) for s in vec.subjects] == [
        ("jira_component", 22), ("jira_component", 2), ("team", 0), ("team", 0),
    ]  # fmt: skip
    assert [s.service_id for s in vec.services] == ["s1", "s2"]
    assert vec.subject_vecs.shape == (4, DIM)
    assert vec.service_vecs.shape == (2, DIM)
    assert set(vec.option_vecs) == {"q_service"}
    assert set(vec.option_vecs["q_service"]) == {"s1", "s2"}
    assert wh.execute("SELECT count(*) FROM core.team").fetchone() == (4,)


def test_ut03_108_prepare_caps_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT03-108 subjects, services and options beyond the limits are cut with a warning."""
    monkeypatch.setattr(ms, "get_redactor", _StubRedactor)
    monkeypatch.setattr(ms, "MAX_SUBJECTS", 2)
    monkeypatch.setattr(ms, "MAX_SERVICES", 1)
    monkeypatch.setattr(ms, "MAX_OPTIONS", 1)
    wh = _wh()
    _fill(wh)
    q = Question.model_validate(
        {
            "id": "q_team",
            "type": "choice",
            "options_source": "core.team",
            "options": {"t1": "Team one", "t2": "Team two"},
            "threshold": 0.7,
            "instructions": "Pick the owning team.",
        }
    )
    with capture_logs() as logs:
        vec = ms.prepare_mapping_vectors(
            wh,
            encoder=_enc(_RecordingEncoder()),
            qs=QuestionSet(version=QSV, questions=(q,)),
        )
    assert [s.name for s in vec.subjects] == ["PAY api", "WEB ui"]
    assert [s.service_id for s in vec.services] == ["s1"]
    assert vec.option_vecs == {"q_team": {"t1": vec.option_vecs["q_team"]["t1"]}}
    capped = sorted(e["what"] for e in logs if e["event"] == "enrich.suggest.capped")
    assert capped == ["options", "services", "subjects"]


def test_ut03_108_prepare_empty_warehouse(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT03-108 nothing to map: no redaction call, empty vectors."""
    monkeypatch.setattr(ms, "get_redactor", lambda: pytest.fail("no text to redact"))
    vec = ms.prepare_mapping_vectors(
        _wh(),
        encoder=_enc(_RecordingEncoder()),
        qs=QuestionSet(version=QSV, questions=()),
    )
    assert vec.subjects == ()
    assert vec.services == ()
    assert vec.subject_vecs.shape == (0, DIM)


def test_ut03_108_prepare_schema_error() -> None:
    """UT03-108 a missing core table raises SchemaViolation naming only the error class."""
    wh = duckdb.connect()
    with pytest.raises(SchemaViolation) as err:
        ms.prepare_mapping_vectors(
            wh,
            encoder=_enc(_RecordingEncoder()),
            qs=QuestionSet(version=QSV, questions=()),
        )
    assert "CatalogException" in str(err.value)
    assert "work_item" not in str(err.value)


# --- UT03-109 / UT03-110: run_suggest_stage -----------------------------------------------------


def test_ut03_109_rejected_not_reemitted_others_pending(
    ops_store: OpsStoreHandle, metrics: _Metrics, fake_clock: FakeClock
) -> None:
    """UT03-109 a rejected item for (team, s1): not re-emitted; the others emitted pending."""
    rejected = {
        "subject_type": "team", "jira_project": None, "jira_component": None, "team_id": "t1",
        "service_id": "s1", "score": 0.9,
    }  # fmt: skip
    decide_review_item(
        create_review_item("mapping_suggestion", rejected, now=T0), "rejected",
        decided_by=USER, now=T0,
    )  # fmt: skip
    wh = _wh()
    for i, svc in enumerate(["s1", "s1", "s1", None]):
        wh.execute("INSERT INTO core.incident VALUES (?, 't1', ?, ?)", [f"i{i}", svc, T0])
    vectors = _vectors(
        [_team("t1", "Payments"), _jira("PAY", "gateway", 7)],
        [ms.Service("s1", "Payments"), ms.Service("s2", "Payments Gateway"),
         ms.Service("s3", "zz qq")],
    )  # fmt: skip
    report = _Report()

    with capture_logs() as logs:
        ms.run_suggest_stage(wh, vectors=vectors, cfg=_cfg(), report=report)

    pending = _pending()
    keys = sorted((p["subject_type"], p["team_id"] or p["jira_component"], p["service_id"])
                  for p in pending)  # fmt: skip
    assert keys == [
        ("jira_component", "gateway", "s1"), ("jira_component", "gateway", "s2"),
        ("jira_component", "gateway", "s3"), ("team", "t1", "s2"),
    ]  # fmt: skip
    team_s2 = next(p for p in pending if p["team_id"] == "t1")
    assert team_s2["evidence_counts"] == {
        "work_items": 0, "team_incidents": 4, "team_incidents_on_service": 0,
    }  # fmt: skip
    assert team_s2["cooccurrence"] == 0.0
    assert team_s2["algorithm_version"] == "map-v1"
    assert team_s2["jira_project"] is None
    assert set(team_s2) == {
        "subject_type", "jira_project", "jira_component", "team_id", "service_id", "score",
        "fuzzy", "semantic", "cooccurrence", "evidence_counts", "algorithm_version",
    }  # fmt: skip
    jira_s2 = next(p for p in pending if p["jira_component"] and p["service_id"] == "s2")
    assert jira_s2["evidence_counts"] == {
        "work_items": 7, "team_incidents": 0, "team_incidents_on_service": 0,
    }  # fmt: skip
    assert jira_s2["team_id"] is None
    assert jira_s2["semantic"] == 1.0
    rejected_items = list_review_items(kind="mapping_suggestion", status="rejected")
    assert len(rejected_items) == 1
    assert list_review_items(kind="mapping_suggestion", status="approved") == []
    assert report.rows == 4
    assert metrics.calls == [
        ("herness_enrich_mapping_suggestions_total", 4, {"component": "enrich"})
    ]
    emitted = [e for e in logs if e["event"] == "enrich.suggest.emitted"]
    assert emitted[0]["created"] == 4
    assert emitted[0]["suppressed"] == 1

    ms.run_suggest_stage(wh, vectors=vectors, cfg=_cfg(), report=report)  # idempotent
    assert len(_pending()) == 4
    assert report.rows == 4


def test_ut03_109_cooccurrence_and_top_n_order(
    ops_store: OpsStoreHandle, metrics: _Metrics
) -> None:
    """UT03-109 team co-occurrence is the share of the team's incidents; ties rank by
    service_id and only `top_n` per subject are emitted."""
    wh = _wh()
    for i, svc in enumerate(["s4", "s4", "s9", None]):
        wh.execute("INSERT INTO core.incident VALUES (?, 't1', ?, ?)", [f"i{i}", svc, T0])
    services = [ms.Service(s, "Billing") for s in ("s5", "s3", "s4", "s1")]
    vectors = _vectors([_team("t1", "Billing")], services)

    ms.run_suggest_stage(wh, vectors=vectors, cfg=_cfg(top_n=2), report=_Report())

    got = {p["service_id"]: p for p in _pending()}
    assert set(got) == {"s4", "s1"}  # s4 wins on co-occurrence, s1 < s3 < s5 on the tie
    assert got["s4"]["cooccurrence"] == 0.5
    assert got["s4"]["evidence_counts"] == {
        "work_items": 0, "team_incidents": 4, "team_incidents_on_service": 2,
    }  # fmt: skip
    assert got["s4"]["score"] == pytest.approx(0.35 + 0.45 + 0.2 * 0.5, abs=1e-5)


def test_ut03_110_scores_below_threshold_emit_nothing(
    ops_store: OpsStoreHandle, metrics: _Metrics
) -> None:
    """UT03-110 scores below 0.60: nothing emitted."""
    vectors = _vectors(
        [_team("t1", "Payments"), _jira("WEB", "frontend")],
        [ms.Service("s1", "Zebra Quux")],
        subject_vecs=np.stack([_unit((1, 1.0)), _unit((2, 1.0))]),
        service_vecs=np.stack([_unit((3, 1.0))]),
    )
    report = _Report()
    ms.run_suggest_stage(_wh(), vectors=vectors, cfg=_cfg(), report=report)
    assert list_review_items(kind="mapping_suggestion") == []
    assert report.rows == 0
    assert metrics.calls == [
        ("herness_enrich_mapping_suggestions_total", 0, {"component": "enrich"})
    ]


def test_ut03_110_skipped_without_vectors(metrics: _Metrics) -> None:
    """UT03-110 the embed stage was skipped (no vectors): stage skipped, nothing emitted."""
    report = _Report()
    with capture_logs() as logs:
        ms.run_suggest_stage(_wh(), vectors=None, cfg=_cfg(), report=report)
    assert report.status == "skipped"
    assert metrics.calls == []
    assert [e["event"] for e in logs] == ["enrich.suggest.skipped"]


def test_ut03_110_cooccurrence_schema_error(metrics: _Metrics) -> None:
    """UT03-110 a missing core.incident raises SchemaViolation before any write."""
    wh = duckdb.connect()
    vectors = _vectors([_team("t1", "x")], [ms.Service("s1", "x")])
    with pytest.raises(SchemaViolation):
        ms.run_suggest_stage(wh, vectors=vectors, cfg=_cfg(), report=_Report())
    assert metrics.calls == []
