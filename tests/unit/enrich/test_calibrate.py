"""Tests for herness.enrich.calibrate (U03-42 ... U03-47; T03-10)."""

from __future__ import annotations

import json
import os
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from herness.core.errors import ConfigError
from herness.enrich.calibrate import (
    CalibrationResult,
    CalibrationStore,
    apply_temperature,
    cross_fit,
    ece,
    fit_temperature,
)
from herness.enrich.layout import EnrichPaths

pytestmark = pytest.mark.unit

_MODULE = "herness.enrich.calibrate"

QSV = "qs-2026-10-01.1"
LAYA_V = "laya-20260901-1"


def _softmax(z: np.ndarray) -> np.ndarray:
    e = np.exp(z - z.max(axis=1, keepdims=True))
    out: np.ndarray = e / e.sum(axis=1, keepdims=True)
    return out


def _synthetic(n: int, k: int, sharpen: float, seed: int) -> tuple[np.ndarray, np.ndarray]:
    """Labels drawn from softmax(z); predictions are softmax(sharpen * z)."""
    rng = np.random.default_rng(seed)
    z = rng.normal(0.0, 1.5, size=(n, k))
    true = _softmax(z)
    labels = np.array([rng.choice(k, p=row) for row in true], dtype=np.int64)
    return _softmax(sharpen * z), labels


def _paths(tmp_path: Path) -> EnrichPaths:
    return EnrichPaths(data_root=tmp_path, embedding_path="models/bge", laya_current_file="x")


def _result(t: float = 1.5, *, uncalibrated: bool = False) -> CalibrationResult:
    return CalibrationResult(
        temperature=t, ece=0.05, ece_raw=0.1, accuracy=0.8, n=200, uncalibrated=uncalibrated
    )


def _doc(**overrides: object) -> str:
    doc: dict[str, object] = {
        "decider": "llm",
        "decider_version": "m-1",
        "question_set_version": QSV,
        "fitted_at": "x",
        "questions": {},
    }
    doc.update(overrides)
    return json.dumps(doc)


def test_ut03_39_apply_temperature_choice_and_bool() -> None:
    """UT03-39 argmax unchanged, rows sum to 1 and T = 1 is the identity (choice and bool)."""
    choice = np.array([[0.7, 0.2, 0.1], [0.1, 0.3, 0.6], [0.25, 0.5, 0.25]])
    boolean = np.array([[0.9, 0.1], [0.2, 0.8], [0.6, 0.4]])
    cases = ((choice, "choice"), (choice, "score"), (boolean, "bool"))
    for probs, qtype in cases:
        for t in (1.0, 2.0, 0.5):
            out = apply_temperature(probs, t, qtype)  # type: ignore[arg-type]
            assert out.shape == probs.shape
            np.testing.assert_allclose(out.sum(axis=1), 1.0, atol=1e-9)
            assert (out.argmax(axis=1) == probs.argmax(axis=1)).all()
        identity = apply_temperature(probs, 1.0, qtype)  # type: ignore[arg-type]
        np.testing.assert_allclose(identity, probs, atol=1e-8)
    softened = apply_temperature(boolean, 2.0, "bool")
    assert softened[0, 0] == pytest.approx(1 / (1 + np.exp(-np.log(9) / 2)))


@pytest.mark.parametrize("t", [0.0, -1.0, float("nan"), float("inf")])
def test_ut03_39_apply_temperature_rejects_bad_t(t: float) -> None:
    """UT03-39 t <= 0 or non-finite raises ConfigError."""
    with pytest.raises(ConfigError):
        apply_temperature(np.array([[0.5, 0.5]]), t, "bool")


def test_ut03_39_apply_temperature_rejects_bad_shape() -> None:
    """UT03-39 a non-matrix input or a bool matrix without two columns raises ConfigError."""
    with pytest.raises(ConfigError):
        apply_temperature(np.array([0.5, 0.5]), 1.0, "choice")
    with pytest.raises(ConfigError):
        apply_temperature(np.array([[0.2, 0.3, 0.5]]), 1.0, "bool")


def test_ut03_40_fit_temperature_recovers_t() -> None:
    """UT03-40 predictions sharpened with T = 2 fit a temperature within 5 % of 2."""
    probs, labels = _synthetic(6000, 4, 2.0, seed=7)
    t = fit_temperature(probs, labels, "choice")
    assert abs(t - 2.0) / 2.0 < 0.05


def test_ut03_40_fit_temperature_bool_and_bounds() -> None:
    """UT03-40 bool fit recovers T; the result always lies in [0.05, 10]."""
    probs, labels = _synthetic(6000, 2, 2.0, seed=11)
    t = fit_temperature(probs, labels, "bool")
    assert abs(t - 2.0) / 2.0 < 0.05
    one = fit_temperature(np.array([[0.9, 0.1]]), np.array([0]), "bool")
    assert 0.05 <= one <= 10


def test_ut03_40_fit_temperature_failure_returns_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT03-40 an optimizer failure returns 1.0 and cross_fit marks the question uncalibrated."""

    class _Failed:
        success = False
        x = 3.0

    monkeypatch.setattr(f"{_MODULE}.optimize.minimize_scalar", lambda *a, **k: _Failed())
    probs, labels = _synthetic(200, 3, 2.0, seed=3)
    assert fit_temperature(probs, labels, "choice") == 1.0
    folds = np.arange(200) % 2
    result = cross_fit(probs, labels, folds, "choice")
    assert result.uncalibrated
    assert result.temperature == 1.0


def test_ut03_41_ece_hand_worked() -> None:
    """UT03-41 30 rows, 15 equal-mass bins of 2: ECE equals the hand-computed value."""
    confs = [Fraction(51, 100) + Fraction(15, 1000) * i for i in range(30)]
    correct = [1, 1, 0, 1, 0, 0, 1, 1, 1, 0, 0, 1, 1, 1, 0]
    correct += [1, 1, 1, 0, 0, 1, 1, 1, 1, 0, 1, 1, 1, 1, 1]
    expected = Fraction(0)
    for b in range(15):
        acc = Fraction(correct[2 * b] + correct[2 * b + 1], 2)
        conf = (confs[2 * b] + confs[2 * b + 1]) / 2
        expected += Fraction(2, 30) * abs(acc - conf)
    order = np.random.default_rng(5).permutation(30)  # input order must not matter
    probs = np.array([[float(confs[i]), 1 - float(confs[i])] for i in order])
    labels = np.array([0 if correct[i] else 1 for i in order])
    assert ece(probs, labels) == pytest.approx(float(expected), abs=1e-12)


def test_ut03_41_ece_small_n_and_range() -> None:
    """UT03-41 fewer rows than bins uses one row per bin; the value lies in [0, 1]."""
    probs = np.array([[1.0, 0.0], [0.0, 1.0], [0.5, 0.5]])
    assert ece(probs, np.array([1, 0, 0])) == pytest.approx((1 + 1 + 0.5) / 3)
    assert 0.0 <= ece(probs, np.array([0, 1, 0]), n_bins=2) <= 1.0


def test_ut03_42_cross_fit_uncalibrated_cases() -> None:
    """UT03-42 99 rows, and 200 rows with one empty fold: uncalibrated with T = 1."""
    probs, labels = _synthetic(200, 3, 2.0, seed=9)
    small = cross_fit(probs[:99], labels[:99], np.arange(99) % 2, "choice")
    empty = cross_fit(probs, labels, np.zeros(200, dtype=np.int64), "choice")
    for result in (small, empty):
        assert result.uncalibrated
        assert result.temperature == 1.0
        assert result.ece == result.ece_raw
    assert small.n == 99
    assert empty.n == 200
    assert empty.ece_raw == pytest.approx(ece(probs, labels))
    assert empty.accuracy == pytest.approx(float(np.mean(probs.argmax(1) == labels)))


def test_ut03_42_result_invariant() -> None:
    """UT03-42 an uncalibrated CalibrationResult must carry T = 1.0."""
    with pytest.raises(ConfigError):
        _result(2.0, uncalibrated=True)
    assert _result(1.0, uncalibrated=True).uncalibrated


def test_ut03_43_cross_fit_overconfident() -> None:
    """UT03-43 1,000 overconfident rows: T > 1, ece < ece_raw, accuracy correct."""
    probs, labels = _synthetic(1000, 3, 3.0, seed=13)
    folds = np.random.default_rng(1).integers(0, 2, size=1000)
    result = cross_fit(probs, labels, folds, "choice")
    assert not result.uncalibrated
    assert result.temperature > 1.0
    assert result.ece < result.ece_raw
    assert result.accuracy == pytest.approx(float(np.mean(probs.argmax(1) == labels)))
    assert result.n == 1000
    assert result.temperature == pytest.approx(fit_temperature(probs, labels, "choice"))


def test_ut03_44_store_round_trip(tmp_path: Path) -> None:
    """UT03-44 save then load round-trips; entries merge; the file matches the layout."""
    store = CalibrationStore(_paths(tmp_path))
    path = store.save("openjev", "0.4.0", QSV, {"q1": _result(1.5)})
    assert path == _paths(tmp_path).calibration_file("openjev", "0.4.0", QSV)
    store.save("openjev", "0.4.0", QSV, {"q2": _result(1.0, uncalibrated=True)})
    fresh = CalibrationStore(_paths(tmp_path))
    loaded = fresh.load("openjev", "0.4.0", QSV)
    assert loaded == {"q1": _result(1.5), "q2": _result(1.0, uncalibrated=True)}
    assert fresh.temperature("openjev", "0.4.0", QSV, "q1") == (1.5, False)
    assert fresh.temperature("openjev", "0.4.0", QSV, "q2") == (1.0, True)
    doc = json.loads(path.read_text(encoding="utf-8"))
    top = {"decider", "decider_version", "question_set_version", "fitted_at", "questions"}
    assert set(doc) == top
    assert doc["question_set_version"] == QSV
    entry = {"temperature", "ece", "ece_raw", "accuracy", "n", "uncalibrated"}
    assert set(doc["questions"]["q1"]) == entry
    table = fresh.as_table([("openjev", "0.4.0"), ("llm", "m-1")], QSV)
    assert table.column_names == ["decider", "decider_version", "question", "t", "uncalibrated"]
    assert table.to_pylist() == [
        {"decider": "openjev", "decider_version": "0.4.0", "question": "q1", "t": 1.5,
         "uncalibrated": False},
        {"decider": "openjev", "decider_version": "0.4.0", "question": "q2", "t": 1.0,
         "uncalibrated": True},
    ]  # fmt: skip


def test_ut03_44_store_missing_file(tmp_path: Path) -> None:
    """UT03-44 no file or no entry: temperature is (1.0, True)."""
    store = CalibrationStore(_paths(tmp_path))
    assert store.load("jev", "1.0", QSV) == {}
    assert store.temperature("jev", "1.0", QSV, "q1") == (1.0, True)
    store.save("jev", "1.0", QSV, {"q1": _result()})
    assert store.temperature("jev", "1.0", QSV, "other") == (1.0, True)


def test_ut03_44_laya_file_other_qsv_is_missing(tmp_path: Path) -> None:
    """UT03-44 a Laya file lives in the version dir; another qsv is treated as missing."""
    paths = _paths(tmp_path)
    store = CalibrationStore(paths)
    path = store.save("laya", LAYA_V, QSV, {"q1": _result(2.5)})
    assert path == paths.laya_dir(LAYA_V) / "calibration.json"
    assert store.temperature("laya", LAYA_V, QSV, "q1") == (2.5, False)
    assert store.load("laya", LAYA_V, "qs-2026-10-01.2") == {}
    assert store.temperature("laya", LAYA_V, "qs-2026-10-01.2", "q1") == (1.0, True)
    replaced = store.save("laya", LAYA_V, "qs-2026-10-01.2", {"q9": _result(3.0)})
    assert CalibrationStore(paths).load("laya", LAYA_V, "qs-2026-10-01.2") == {"q9": _result(3.0)}
    assert replaced == path


def test_ut03_44_store_memoizes_and_rereads(tmp_path: Path) -> None:
    """UT03-44 reads are memoized per path and mtime; an atomic replace is picked up."""
    paths = _paths(tmp_path)
    reader, writer = CalibrationStore(paths), CalibrationStore(paths)
    path = writer.save("llm", "m-1", QSV, {"q1": _result(1.5)})
    assert reader.load("llm", "m-1", QSV)["q1"].temperature == 1.5
    stat = path.stat()
    writer.save("llm", "m-1", QSV, {"q1": _result(2.0)})
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))
    assert reader.load("llm", "m-1", QSV)["q1"].temperature == 2.0


_BAD_ENTRY = {
    "temperature": 2.0,
    "ece": 0.1,
    "ece_raw": 0.1,
    "accuracy": 0.5,
    "n": 10,
    "uncalibrated": True,
}


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        json.dumps([1, 2]),
        json.dumps({"decider": "llm"}),
        _doc(extra=1),
        _doc(decider="jev"),
        _doc(question_set_version="qs-2026-10-01.9"),
        _doc(questions={"q1": _BAD_ENTRY}),
    ],
)
def test_ut03_44_store_malformed_file(tmp_path: Path, content: str) -> None:
    """UT03-44 a malformed calibration file raises ConfigError naming the file."""
    paths = _paths(tmp_path)
    path = paths.calibration_file("llm", "m-1", QSV)
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError, match=r"\.json"):
        CalibrationStore(paths).load("llm", "m-1", QSV)


def test_ut03_44_store_oversized_file(tmp_path: Path) -> None:
    """UT03-44 a calibration file above 1 MB raises ConfigError."""
    paths = _paths(tmp_path)
    path = paths.calibration_file("llm", "m-1", QSV)
    path.parent.mkdir(parents=True)
    path.write_bytes(b" " * (1_048_576 + 1))
    with pytest.raises(ConfigError, match="1 MB"):
        CalibrationStore(paths).load("llm", "m-1", QSV)


def test_ut03_44_store_write_failure_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-44 a failed atomic write leaves no temp file and no target file."""
    paths = _paths(tmp_path)

    def _boom(*_a: object) -> None:
        msg = "disk full"
        raise OSError(msg)

    monkeypatch.setattr(f"{_MODULE}.os.replace", _boom)
    with pytest.raises(OSError, match="disk full"):
        CalibrationStore(paths).save("llm", "m-1", QSV, {"q1": _result()})
    target = paths.calibration_file("llm", "m-1", QSV)
    assert list(target.parent.iterdir()) == []


_WEIGHTS = st.lists(st.integers(1, 1000), min_size=2, max_size=6)


@settings(max_examples=150, deadline=None)
@given(
    rows=st.lists(_WEIGHTS, min_size=1, max_size=8),
    t=st.floats(0.05, 10.0, allow_nan=False),
    boolean=st.booleans(),
)
def test_pt03_04_rows_sum_to_one_and_argmax_kept(
    rows: list[list[int]], t: float, boolean: bool
) -> None:
    """PT03-04 calibrated rows sum to 1 and keep their argmax for any T in [0.05, 10]."""
    width = 2 if boolean else min(len(r) for r in rows)
    weights = np.array([r[:width] for r in rows], dtype=np.float64)
    for row in weights:
        top = np.sort(row)
        assume(top[-1] != top[-2])  # a unique maximum
    probs = weights / weights.sum(axis=1, keepdims=True)
    out = apply_temperature(probs, t, "bool" if boolean else "choice")
    np.testing.assert_allclose(out.sum(axis=1), 1.0, atol=1e-9)
    assert (out.argmax(axis=1) == probs.argmax(axis=1)).all()
