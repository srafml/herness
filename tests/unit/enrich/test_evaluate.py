"""Tests for herness.enrich.evaluate (U03-127 ... U03-129; T03-30)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
import pytest
from tests.unit.enrich import _evaluate_fixtures as fx

from herness.enrich import evaluate
from herness.enrich.calibrate import CalibrationStore, apply_temperature, cross_fit
from herness.enrich.evaluate import (
    QuestionMetrics,
    evaluate_candidate,
    macro_metric,
    question_metrics,
)
from herness.enrich.labels import GOLD_SCHEMA, gold_digest
from herness.enrich.questions import load_question_set

pytestmark = pytest.mark.unit

_LAYA_KEYS = {
    "accuracy", "macro_f1", "mae", "within_one", "ece", "temperature",
    "coverage_at_threshold", "accuracy_at_threshold",
}  # fmt: skip
_QUESTION_KEYS = {
    "type", "question_fingerprint", "n_gold", "laya", "teacher", "system", "criteria",
    "passed", "accepted_proposed",
}  # fmt: skip
_TOP_KEYS = {
    "version", "question_set_version", "gold_path", "gold_sha256", "evaluated_at",
    "questions", "macro_metric",
}  # fmt: skip


# UT03-123 -----------------------------------------------------------------------------------


def test_ut03_123_choice_metrics_exact() -> None:
    """UT03-123 choice: accuracy, macro-F1, coverage and accuracy at threshold are exact."""
    probs = np.array([[0.8, 0.1, 0.1], [0.2, 0.75, 0.05], [0.6, 0.3, 0.1], [0.1, 0.1, 0.8]])
    m = question_metrics(
        probs, np.array([0, 1, 1, 2]), np.array([0, 1, 0, 1]), qtype="choice", threshold=0.7
    )
    assert m.accuracy == pytest.approx(0.75)
    assert m.macro_f1 == pytest.approx((2 / 3 + 2 / 3 + 1) / 3)
    assert (m.mae, m.within_one) == (None, None)
    assert m.coverage_at_threshold == pytest.approx(0.75)
    assert m.accuracy_at_threshold == pytest.approx(1.0)
    assert (m.n, m.uncalibrated, m.temperature) == (4, True, 1.0)


def test_ut03_123_bool_metrics_exact() -> None:
    """UT03-123 bool: accuracy and coverage exact; macro-F1, MAE and within-one are None."""
    probs = np.array([[0.9, 0.1], [0.3, 0.7], [0.6, 0.4], [0.2, 0.8]])
    m = question_metrics(
        probs, np.array([0, 1, 1, 0]), np.array([0, 0, 1, 1]), qtype="bool", threshold=0.75
    )
    assert m.accuracy == pytest.approx(0.5)
    assert (m.macro_f1, m.mae, m.within_one) == (None, None, None)
    assert m.coverage_at_threshold == pytest.approx(0.5)
    assert m.accuracy_at_threshold == pytest.approx(0.5)


def test_ut03_123_score_metrics_exact() -> None:
    """UT03-123 score: MAE and within-one exact, macro-F1 None, no covered row -> None."""
    probs = np.array(
        [[0.7, 0.1, 0.1, 0.1], [0.1, 0.1, 0.1, 0.7], [0.1, 0.1, 0.7, 0.1], [0.1, 0.7, 0.1, 0.1]]
    )
    m = question_metrics(
        probs, np.array([0, 1, 2, 3]), np.array([0, 1, 0, 1]), qtype="score", threshold=0.9
    )
    assert m.accuracy == pytest.approx(0.5)
    assert m.mae == pytest.approx(1.0)
    assert m.within_one == pytest.approx(0.5)
    assert m.macro_f1 is None
    assert m.coverage_at_threshold == 0.0
    assert m.accuracy_at_threshold is None


def test_ut03_123_calibrated_metrics_use_cross_fit() -> None:
    """UT03-123 with >= 100 rows: T and ECE come from cross_fit; coverage uses P'."""
    rng = np.random.default_rng(7)
    z = rng.normal(0.0, 1.0, size=(300, 3))
    probs = np.exp(3 * z) / np.exp(3 * z).sum(axis=1, keepdims=True)
    labels = np.array([rng.choice(3, p=np.exp(r) / np.exp(r).sum()) for r in z])
    folds = np.arange(300) % 2
    m = question_metrics(probs, labels, folds, qtype="choice", threshold=0.7)
    cal = cross_fit(probs, labels, folds, "choice")
    assert not m.uncalibrated
    assert (m.temperature, m.ece) == (cal.temperature, cal.ece)
    calibrated = apply_temperature(probs, cal.temperature, "choice")
    assert m.coverage_at_threshold == pytest.approx(float(np.mean(calibrated.max(axis=1) >= 0.7)))


def test_ut03_123_empty_input_is_uncalibrated() -> None:
    """UT03-123 no rows: zero accuracy and coverage, uncalibrated."""
    empty = np.zeros((0, 4))
    m = question_metrics(empty, np.zeros(0), np.zeros(0), qtype="score", threshold=0.6)
    assert (m.n, m.accuracy, m.coverage_at_threshold, m.uncalibrated) == (0, 0.0, 0.0, True)
    assert (m.mae, m.within_one, m.accuracy_at_threshold, m.macro_f1) == (None, None, None, None)
    c = question_metrics(np.zeros((0, 3)), np.zeros(0), np.zeros(0), qtype="choice", threshold=0.6)
    assert c.macro_f1 is None


# UT03-124 -----------------------------------------------------------------------------------


def _m(accuracy: float, within_one: float | None = None) -> QuestionMetrics:
    return QuestionMetrics(
        accuracy=accuracy, macro_f1=None, mae=None, within_one=within_one, ece=0.0,
        temperature=1.0, coverage_at_threshold=1.0, accuracy_at_threshold=None, n=10,
        uncalibrated=True,
    )  # fmt: skip


def test_ut03_124_macro_metric_mean_of_scoring_questions() -> None:
    """UT03-124 3 questions, one non-scoring: the mean of the 2 primary metrics."""
    raw = fx.decisions().model_dump(by_alias=True)
    raw["questions"][1]["scoring_use"] = False  # change_caused
    qs = load_question_set(type(fx.decisions()).model_validate(raw))
    metrics = {
        "root_cause": _m(0.8),
        "change_caused": _m(0.1),
        "business_impact": _m(0.2, within_one=0.9),
        "unknown_q": _m(0.0),
    }
    assert macro_metric(metrics, qs) == pytest.approx(0.85)
    assert macro_metric({}, qs) == 0.0
    assert macro_metric({"business_impact": _m(0.2)}, qs) == 0.0  # score without within-one


# UT03-125 -----------------------------------------------------------------------------------


@pytest.fixture
def gauges(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, float, dict[str, str]]]:
    seen: list[tuple[str, float, dict[str, str]]] = []

    def record(name: str, value: float, *, component: str, labels: dict[str, str]) -> None:
        assert component == "enrich"
        seen.append((name, value, labels))

    monkeypatch.setattr(evaluate, "record_gauge", record)
    return seen


def _run(f: fx.Fixture, **kwargs: object) -> dict[str, object]:
    args: dict[str, object] = {
        "version": fx.VERSION, "qs": f.qs, "cfg": f.cfg, "store": f.store, "cache": f.cache,
        "calibration": f.calibration, "teacher": fx.TEACHER, "paths": f.paths, "now": fx.NOW,
    }  # fmt: skip
    args.update(kwargs)
    return evaluate_candidate(**args)  # type: ignore[arg-type]


def test_ut03_125_eval_json_keys_equal_design(
    tmp_path: Path, gauges: list[tuple[str, float, dict[str, str]]]
) -> None:
    """UT03-125 eval.json keys equal design 03 §4.4; file equals the returned document."""
    f = fx.build(tmp_path)
    doc = _run(f)
    on_disk = json.loads((f.paths.laya_dir(fx.VERSION) / "eval.json").read_text("utf-8"))
    assert on_disk == doc
    assert set(doc) == _TOP_KEYS
    assert doc["version"] == fx.VERSION
    assert doc["question_set_version"] == fx.QSV
    assert doc["gold_path"] == f"data/labels/{fx.QSV}/gold/"
    assert doc["gold_sha256"] == gold_digest(f.frozen_gold)
    assert doc["evaluated_at"] == "2026-10-04T03:12:00.000000Z"
    questions = doc["questions"]
    assert isinstance(questions, dict)
    assert set(questions) == {"root_cause", "change_caused"}  # business_impact not frozen
    for qid, entry in questions.items():
        assert set(entry) == _QUESTION_KEYS
        assert set(entry["laya"]) == _LAYA_KEYS
        assert set(entry["teacher"]) == {"decider", "accuracy", "ece"}
        assert set(entry["system"]) == {"accuracy"}
        assert set(entry["passed"]) == set(entry["criteria"])
        assert entry["question_fingerprint"] == f.qs.get(qid).fingerprint
    assert questions["root_cause"]["n_gold"] == fx.N_CHOICE
    assert questions["root_cause"]["type"] == "choice"
    assert questions["root_cause"]["teacher"]["decider"] == "openjev"
    assert {g[2]["decider"] for g in gauges} == {"laya", "openjev"}
    assert all(g[0] == "herness_enrich_gold_metric_ratio" for g in gauges)


def test_ut03_125_metrics_passed_flags_and_proposal(tmp_path: Path) -> None:
    """UT03-125 values, passed flags, system accuracy; uncalibrated never proposed."""
    f = fx.build(tmp_path)
    doc = _run(f)
    rc = doc["questions"]["root_cause"]  # type: ignore[index]
    assert rc["laya"]["accuracy"] == pytest.approx(0.99)
    assert rc["teacher"]["accuracy"] == pytest.approx(0.9)  # latest duplicate row wins
    assert rc["system"]["accuracy"] == pytest.approx(0.99)
    assert rc["criteria"] == {
        "min_accuracy": 0.8, "min_macro_f1": 0.6, "max_ece": 0.05, "min_coverage": 0.7,
        "max_gap_to_teacher": 0.02,
    }  # fmt: skip
    assert all(rc["passed"].values())
    assert rc["accepted_proposed"] is True
    cc = doc["questions"]["change_caused"]  # type: ignore[index]
    assert cc["laya"]["accuracy"] == pytest.approx(1.0)
    assert cc["laya"]["coverage_at_threshold"] == pytest.approx(57 / 60)
    assert cc["system"]["accuracy"] == pytest.approx(57 / 60)  # low-p rows escalate to a miss
    assert cc["laya"]["macro_f1"] is None
    assert all(cc["passed"].values())
    assert cc["accepted_proposed"] is False  # 60 gold rows: uncalibrated (TH03-17)
    assert doc["macro_metric"] == pytest.approx((0.99 + 1.0) / 2)


def test_ut03_125_calibration_files_written(tmp_path: Path) -> None:
    """UT03-125 calibration.json holds the candidate's T; the teacher's file is updated."""
    f = fx.build(tmp_path)
    _run(f)
    fresh = CalibrationStore(f.paths)
    laya = fresh.load("laya", fx.VERSION, fx.QSV)
    assert set(laya) == {"root_cause", "change_caused"}
    assert laya["change_caused"].uncalibrated
    assert not laya["root_cause"].uncalibrated
    assert (f.paths.laya_dir(fx.VERSION) / "calibration.json").is_file()
    teacher = fresh.load(*fx.TEACHER, fx.QSV)
    assert teacher["root_cause"].accuracy == pytest.approx(0.9)


def test_ut03_125_failed_criterion_and_blocked(tmp_path: Path) -> None:
    """UT03-125 a failed bound sets passed false; blocked questions are absent."""
    f = fx.build(tmp_path)
    doc = _run(f, cfg=fx.decisions(min_accuracy=0.99, max_gap_to_teacher=0.0, max_mae=0.1))
    rc = doc["questions"]["root_cause"]  # type: ignore[index]
    assert rc["passed"]["min_accuracy"] is True
    assert rc["passed"]["max_mae"] is False  # a missing metric fails its criterion
    assert rc["accepted_proposed"] is False
    doc = _run(f, blocked=frozenset({"root_cause"}))
    assert set(doc["questions"]) == {"change_caused"}  # type: ignore[arg-type]


def test_ut03_125_gap_to_teacher_fails(tmp_path: Path) -> None:
    """UT03-125 max_gap_to_teacher: teacher.accuracy - laya.accuracy <= bound, else false."""
    f = fx.build(tmp_path)
    perfect = ("llm", "local/perfect")
    fx.write_teacher(f, perfect, fx.perfect_row)
    doc = _run(f, teacher=perfect, cfg=fx.decisions(max_gap_to_teacher=0.0))
    rc = doc["questions"]["root_cause"]  # type: ignore[index]
    assert rc["teacher"]["accuracy"] == pytest.approx(1.0)
    assert rc["passed"]["max_gap_to_teacher"] is False
    assert rc["passed"]["min_accuracy"] is True
    assert rc["accepted_proposed"] is False
    doc = _run(f, teacher=perfect, cfg=fx.decisions(max_gap_to_teacher=0.02))
    assert doc["questions"]["root_cause"]["passed"]["max_gap_to_teacher"] is True  # type: ignore[index]


def test_ut03_125_no_frozen_gold_empty_questions(tmp_path: Path) -> None:
    """UT03-125 no frozen gold: empty questions, macro 0.0, nothing accepted."""
    f = fx.build(tmp_path)
    for marker in (f.paths.labels_dir(fx.QSV, "gold") / "_frozen").glob("*.json"):
        marker.unlink()
    doc = _run(f)
    assert doc["questions"] == {}
    assert doc["macro_metric"] == 0.0
    assert doc["gold_sha256"] == gold_digest(GOLD_SCHEMA.empty_table())
    assert (f.paths.laya_dir(fx.VERSION) / "eval.json").is_file()


def test_ut03_125_missing_teacher_rows(tmp_path: Path) -> None:
    """UT03-125 a teacher with no cache rows: null accuracy and ECE, gap criterion fails."""
    f = fx.build(tmp_path)
    doc = _run(f, teacher=("llm", "local/qwen3"))
    rc = doc["questions"]["root_cause"]  # type: ignore[index]
    assert rc["teacher"] == {"decider": "llm", "accuracy": None, "ece": None}
    assert rc["passed"]["max_gap_to_teacher"] is False
    assert rc["system"]["accuracy"] == pytest.approx(0.99)
    assert not CalibrationStore(f.paths).load("llm", "local/qwen3", fx.QSV)


def test_ut03_125_no_cache_rows_never_proposed(tmp_path: Path) -> None:
    """UT03-125 no cache at all: questions present with n = 0 metrics, never proposed."""
    f = fx.build(tmp_path)
    shutil.rmtree(f.paths.cache_dir(fx.QSV))
    doc = _run(f)
    for entry in doc["questions"].values():  # type: ignore[attr-defined]
        assert entry["laya"]["accuracy"] == 0.0
        assert entry["system"]["accuracy"] == 0.0
        assert entry["accepted_proposed"] is False
    assert CalibrationStore(f.paths).load("laya", fx.VERSION, fx.QSV)["root_cause"].uncalibrated
