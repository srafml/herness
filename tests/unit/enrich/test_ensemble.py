"""Tests for herness.enrich.deciders.ensemble (U03-65 ... U03-67; T03-16)."""

from __future__ import annotations

import errno
import math
from pathlib import Path

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.support.fake_clock import FakeClock

from herness.core.errors import ConfigError, StoreBusy
from herness.core.types import Answer, DecisionInput, DecisionOutput, Question, QuestionSet
from herness.enrich.cache import DecisionCache
from herness.enrich.calibrate import CalibrationResult, CalibrationStore
from herness.enrich.deciders.ensemble import EnsembleDecider, ensemble_version, pool_log_linear
from herness.enrich.layout import EnrichPaths

pytestmark = pytest.mark.unit

QSV = "qs-2026-09-01"
LAYA = ("laya", "laya-20260901-1")
OPENJEV = ("openjev", "openjev-0.4.0/openjev-latest")
LLM = ("llm", "local/qwen")
MEMBERS = (LAYA, OPENJEV, LLM)
H1, H2, H3 = "1" * 32, "2" * 32, "3" * 32
EPS = 1e-6


def _expected_pool(dists: list[list[float]], weights: list[float]) -> np.ndarray:
    """Reference log-linear pool written out term by term (design 03 §5.9)."""
    total = sum(weights)
    log_q = [
        sum(w / total * math.log(d[k] + EPS) for d, w in zip(dists, weights, strict=True))
        for k in range(len(dists[0]))
    ]
    top = max(log_q)
    exps = [math.exp(v - top) for v in log_q]
    return np.array([e / sum(exps) for e in exps])


# --- U03-65 pool_log_linear -------------------------------------------------------------


def test_ut03_63_pool_two_agree_one_disagrees_one_missing() -> None:
    """UT03-63 pooled vector to 1e-9, weights renormalized over present members, agreement 2/3."""
    dists = {
        "laya": np.array([0.7, 0.2, 0.1]),
        "openjev": np.array([0.6, 0.3, 0.1]),
        "llm": np.array([0.1, 0.8, 0.1]),
    }
    weights = {"laya": 0.9, "openjev": 0.8, "llm": 0.6, "jev": 0.95}  # jev is missing
    pooled, agreement = pool_log_linear(dists, weights)
    expected = _expected_pool([[0.7, 0.2, 0.1], [0.6, 0.3, 0.1], [0.1, 0.8, 0.1]], [0.9, 0.8, 0.6])
    np.testing.assert_allclose(pooled, expected, rtol=0, atol=1e-9)
    assert abs(pooled.sum() - 1.0) < 1e-12
    assert agreement == pytest.approx(2 / 3)
    # Renormalization: scaling every present weight leaves the pool unchanged.
    scaled, _ = pool_log_linear(dists, {k: v * 7 for k, v in weights.items()})
    np.testing.assert_allclose(scaled, pooled, rtol=0, atol=1e-12)


def test_ut03_63_all_zero_weights_are_equal_weights() -> None:
    """UT03-63 all-zero (or absent) weights pool with equal weights."""
    dists = {"a": np.array([0.9, 0.1]), "b": np.array([0.2, 0.8])}
    pooled, agreement = pool_log_linear(dists, {"a": 0.0, "b": 0.0})
    expected = _expected_pool([[0.9, 0.1], [0.2, 0.8]], [1.0, 1.0])
    np.testing.assert_allclose(pooled, expected, rtol=0, atol=1e-9)
    absent, _ = pool_log_linear(dists, {})
    np.testing.assert_allclose(absent, pooled, rtol=0, atol=1e-12)
    assert agreement == pytest.approx(0.5)


def test_ut03_63_tie_goes_to_lowest_index() -> None:
    """UT03-63 a tied pooled vector takes the lowest index as argmax for agreement."""
    dists = {"a": np.array([0.5, 0.5]), "b": np.array([0.5, 0.5])}
    pooled, agreement = pool_log_linear(dists, {"a": 1.0, "b": 1.0})
    np.testing.assert_allclose(pooled, [0.5, 0.5], atol=1e-12)
    assert agreement == 1.0


@pytest.mark.parametrize(
    ("dists", "weights"),
    [
        ({}, {}),
        ({"a": np.array([0.5, 0.5]), "b": np.array([0.2, 0.3, 0.5])}, {}),
        ({"a": np.array([[0.5, 0.5]])}, {}),
        ({"a": np.array([])}, {}),
        ({"a": np.array([0.5, 0.5])}, {"a": -1.0}),
        ({"a": np.array([0.5, 0.5])}, {"a": math.nan}),
    ],
)
def test_ut03_63_invalid_input_is_config_error(
    dists: dict[str, np.ndarray], weights: dict[str, float]
) -> None:
    """UT03-63 no member, unequal or non-vector lengths and bad weights raise ConfigError."""
    with pytest.raises(ConfigError):
        pool_log_linear(dists, weights)


_VECTOR = st.lists(st.floats(0.0, 1.0, allow_nan=False), min_size=2, max_size=6)


@settings(max_examples=150, deadline=None)
@given(
    k=st.integers(2, 6),
    raw=st.lists(_VECTOR, min_size=1, max_size=5),
    weights=st.lists(st.floats(0.0, 1.0, allow_nan=False), min_size=5, max_size=5),
)
def test_pt03_07_pooled_sums_to_one(k: int, raw: list[list[float]], weights: list[float]) -> None:
    """PT03-07 the pooled output sums to 1 and agreement lies in [0, 1]."""
    dists = {f"m{i}": np.resize(np.array(v), k) for i, v in enumerate(raw)}
    pooled, agreement = pool_log_linear(dists, {f"m{i}": w for i, w in enumerate(weights)})
    assert pooled.shape == (k,)
    assert abs(pooled.sum() - 1.0) < 1e-9
    assert np.all(pooled >= 0)
    assert 0.0 <= agreement <= 1.0


@settings(max_examples=150, deadline=None)
@given(
    k=st.integers(2, 6),
    top=st.integers(0, 5),
    members=st.integers(1, 5),
    rest=st.lists(st.floats(0.0, 0.2, allow_nan=False), min_size=6, max_size=6),
    weights=st.lists(st.floats(0.0, 1.0, allow_nan=False), min_size=5, max_size=5),
)
def test_pt03_07_unanimous_members_keep_argmax(
    k: int, top: int, members: int, rest: list[float], weights: list[float]
) -> None:
    """PT03-07 unanimous members give that argmax with agreement 1."""
    top %= k
    dists = {}
    for i in range(members):
        vec = np.array(rest[:k]) * (i + 1) / members
        vec[top] = 0.5 + 0.1 * i / members
        dists[f"m{i}"] = vec / vec.sum()
    pooled, agreement = pool_log_linear(dists, {f"m{i}": w for i, w in enumerate(weights)})
    assert int(np.argmax(pooled)) == top
    assert agreement == 1.0


# --- U03-66 ensemble_version ------------------------------------------------------------


def test_ut03_64_version_changes_with_weight_and_is_stable() -> None:
    """UT03-64 same members with a changed weight give a different version; equal inputs agree."""
    weights = {("laya", "q_a"): 0.9, ("openjev", "q_a"): 0.8}
    first = ensemble_version(MEMBERS, weights)
    assert len(first) == 12
    assert all(c in "0123456789abcdef" for c in first)
    assert ensemble_version(MEMBERS, dict(weights)) == first
    assert ensemble_version(tuple(reversed(MEMBERS)), weights) == first  # members sorted
    changed = {**weights, ("openjev", "q_a"): 0.81}
    assert ensemble_version(MEMBERS, changed) != first
    below_precision = {**weights, ("openjev", "q_a"): 0.8000000001}
    assert ensemble_version(MEMBERS, below_precision) == first  # format(w, ".6f")


def test_ut03_64_version_is_pinned_for_a_fixed_input() -> None:
    """UT03-64 the hex of a fixed configuration is pinned (canonical JSON, sha256[:12])."""
    weights = {("laya", "q_a"): 0.9, ("openjev", "q_a"): 0.8}
    assert ensemble_version(MEMBERS, weights) == "87865117b6d8"


def test_ut03_64_version_changes_with_member_version() -> None:
    """UT03-64 a changed member version changes the ensemble version."""
    weights = {("laya", "q_a"): 0.9}
    bumped = (("laya", "laya-20260902-1"), OPENJEV, LLM)
    assert ensemble_version(MEMBERS, weights) != ensemble_version(bumped, weights)


# --- U03-67 EnsembleDecider ------------------------------------------------------------


def _question(qid: str, qtype: str = "bool", options: dict[str, str] | None = None) -> Question:
    return Question.model_validate(
        {
            "id": qid,
            "type": qtype,
            "instructions": "Classify it.",
            "options": options,
            "threshold": 0.7,
            "fingerprint": (qid[-1] * 16),
        }
    )


Q_BOOL = _question("q_a")
Q_CHOICE = _question("q_c", "choice", {"db": "Database", "net": "Network", "app": "App"})
Q_EMPTY = _question("q_b")
QS = QuestionSet(version=QSV, questions=(Q_BOOL, Q_CHOICE, Q_EMPTY))


@pytest.fixture
def paths(tmp_path: Path) -> EnrichPaths:
    return EnrichPaths(data_root=tmp_path, embedding_path="data/e", laya_current_file="data/c")


def _answer(dist: dict[str, float]) -> Answer:
    top = max(dist, key=lambda k: dist[k])
    return Answer(answer=top, probability=dist[top], distribution=dist)


def _write(
    cache: DecisionCache,
    member: tuple[str, str],
    rows: dict[str, dict[str, dict[str, float]]],
    qs: QuestionSet = QS,
) -> None:
    with cache.writer(member[0], member[1], questions=qs, flush_rows=100) as writer:
        writer.add(
            [
                DecisionOutput(
                    record_id="r-" + h[:4],
                    content_hash=h,
                    decider=member[0],  # type: ignore[arg-type]
                    decider_version=member[1],
                    answers={qid: _answer(d) for qid, d in answers.items()},
                )
                for h, answers in rows.items()
            ],
            samples=None,
        )


def _item(content_hash: str, question_ids: tuple[str, ...] | None = None) -> DecisionInput:
    return DecisionInput(
        record_id="r-" + content_hash[:4],
        entity="incident",
        content_hash=content_hash,
        text="text",
        question_ids=question_ids,
    )


def _calibrated_bool(p_true: float, t: float) -> np.ndarray:
    p = min(max(p_true, 1e-9), 1 - 1e-9)
    c = 1.0 / (1.0 + math.exp(-(math.log(p) - math.log1p(-p)) / t))
    return np.array([c, 1.0 - c])


def _seed(paths: EnrichPaths) -> tuple[DecisionCache, CalibrationStore]:
    cache = DecisionCache(paths, QSV)
    _write(
        cache,
        LAYA,
        {
            H1: {"q_a": {"true": 0.8, "false": 0.2}, "q_c": {"db": 0.6, "net": 0.3, "app": 0.1}},
            H2: {"q_a": {"true": 0.3, "false": 0.7}},
        },
    )
    _write(
        cache,
        OPENJEV,
        {
            H1: {"q_a": {"true": 0.9, "false": 0.1}, "q_c": {"db": 0.2, "net": 0.7, "app": 0.1}},
            H2: {"q_a": {"true": 0.4, "false": 0.6}},
        },
    )
    _write(cache, LLM, {H1: {"q_a": {"true": 0.25, "false": 0.75}}, H2: {}})
    # Rows the ensemble must ignore: a non-member version, and a stale fingerprint (the
    # fingerprint of another question of the set, so only the per-question check drops it).
    _write(cache, ("llm", "local/other"), {H2: {"q_a": {"true": 0.99, "false": 0.01}}})
    stale_q = Q_BOOL.model_copy(update={"fingerprint": "c" * 16})
    stale = QuestionSet(version=QSV, questions=(stale_q,))
    _write(cache, LLM, {H2: {"q_a": {"true": 0.99, "false": 0.01}}}, qs=stale)
    calibration = CalibrationStore(paths)
    result = CalibrationResult(
        temperature=2.0, ece=0.01, ece_raw=0.02, accuracy=0.9, n=200, uncalibrated=False
    )
    calibration.save(OPENJEV[0], OPENJEV[1], QSV, {"q_a": result})
    return cache, calibration


WEIGHTS = {("laya", "q_a"): 0.9, ("openjev", "q_a"): 0.8, ("llm", "q_a"): 0.6}


def test_ut03_65_pooled_answers_from_three_members(paths: EnrichPaths) -> None:
    """UT03-65 cache rows of 3 members for 2 items: pooled answers, confidence = agreement."""
    cache, calibration = _seed(paths)
    decider = EnsembleDecider(cache, calibration, members=MEMBERS, weights=WEIGHTS, qsv=QSV)
    assert decider.name == "ensemble"
    assert decider.version == ensemble_version(MEMBERS, WEIGHTS)
    decider.health()  # no-op: must not raise

    out = decider.decide([_item(H1), _item(H2), _item(H3)], QS)

    assert [o.content_hash for o in out] == [H1, H2, H3]
    assert all(o.decider == "ensemble" and o.decider_version == decider.version for o in out)
    # H1 q_a: laya and llm raw, openjev calibrated with T = 2; weights 0.9/0.8/0.6.
    expected = _expected_pool(
        [[0.8, 0.2], list(_calibrated_bool(0.9, 2.0)), [0.25, 0.75]], [0.9, 0.8, 0.6]
    )
    a = out[0].answers["q_a"]
    assert a.distribution["true"] == pytest.approx(expected[0], abs=1e-9)
    assert a.answer == "true"
    assert a.probability == pytest.approx(expected[0], abs=1e-9)
    assert a.backend_confidence == pytest.approx(2 / 3)  # llm disagrees
    # H1 q_c: no weights for q_c -> equal weights; label order = options order.
    expected_c = _expected_pool([[0.6, 0.3, 0.1], [0.2, 0.7, 0.1]], [1.0, 1.0])
    c = out[0].answers["q_c"]
    assert list(c.distribution) == ["db", "net", "app"]
    assert [c.distribution[k] for k in ("db", "net", "app")] == pytest.approx(
        list(expected_c), abs=1e-9
    )
    assert c.answer == "net"
    assert c.backend_confidence == pytest.approx(0.5)
    # H2 q_a: only laya and openjev rows count (llm rows are another version / stale).
    expected_2 = _expected_pool([[0.3, 0.7], list(_calibrated_bool(0.4, 2.0))], [0.9, 0.8])
    b = out[1].answers["q_a"]
    assert b.distribution["true"] == pytest.approx(expected_2[0], abs=1e-9)
    assert b.answer == "false"
    assert b.backend_confidence == 1.0
    # Questions without member rows are absent; items without rows have no answers.
    assert set(out[0].answers) == {"q_a", "q_c"}
    assert set(out[1].answers) == {"q_a"}
    assert out[2].answers == {}
    assert out[2].error is None


def test_ut03_65_asked_questions_only_and_deterministic(paths: EnrichPaths) -> None:
    """UT03-65 an item's question_ids limit the answers; equal inputs give equal outputs."""
    cache, calibration = _seed(paths)
    decider = EnsembleDecider(cache, calibration, members=MEMBERS, weights=WEIGHTS, qsv=QSV)
    first = decider.decide([_item(H1, ("q_c",))], QS)
    assert set(first[0].answers) == {"q_c"}
    again = EnsembleDecider(cache, calibration, members=MEMBERS, weights=WEIGHTS, qsv=QSV)
    assert again.decide([_item(H1, ("q_c",))], QS) == first


def test_ut03_65_empty_cache_and_no_members(paths: EnrichPaths) -> None:
    """UT03-65 no cache part, an empty batch or no members give outputs without answers."""
    cache, calibration = DecisionCache(paths, QSV), CalibrationStore(paths)
    decider = EnsembleDecider(cache, calibration, members=MEMBERS, weights={}, qsv=QSV)
    assert [o.answers for o in decider.decide([_item(H1)], QS)] == [{}]
    assert decider.decide([], QS) == []
    seeded, calibration = _seed(paths)
    empty = EnsembleDecider(seeded, calibration, members=(), weights={}, qsv=QSV)
    assert [o.answers for o in empty.decide([_item(H1)], QS)] == [{}]


def test_ut03_65_latest_member_row_wins(paths: EnrichPaths, fake_clock: FakeClock) -> None:
    """UT03-65 of two rows for one member, item and question, the later decided_at is pooled."""
    cache, calibration = DecisionCache(paths, QSV), CalibrationStore(paths)
    _write(cache, LAYA, {H1: {"q_a": {"true": 0.9, "false": 0.1}}})
    fake_clock.advance(60)
    _write(cache, LAYA, {H1: {"q_a": {"true": 0.2, "false": 0.8}}})
    decider = EnsembleDecider(cache, calibration, members=(LAYA,), weights={}, qsv=QSV)
    answer = decider.decide([_item(H1, ("q_a",))], QS)[0].answers["q_a"]
    assert answer.answer == "false"
    assert answer.distribution["true"] == pytest.approx(_expected_pool([[0.2, 0.8]], [1])[0])


def test_ut03_65_equal_time_rows_pick_independent_of_write_order(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    """UT03-65 rows with one decided_at resolve the same whichever part was written first."""
    del fake_clock  # frozen: both rows carry the same decided_at
    rows = [{H1: {"q_a": {"true": 0.9, "false": 0.1}}}, {H1: {"q_a": {"true": 0.2, "false": 0.8}}}]
    results = []
    for name, order in (("a", rows), ("b", rows[::-1])):
        paths = EnrichPaths(
            data_root=tmp_path / name, embedding_path="data/e", laya_current_file="data/c"
        )
        cache = DecisionCache(paths, QSV)
        for part in order:
            _write(cache, LAYA, part)
        decider = EnsembleDecider(
            cache, CalibrationStore(paths), members=(LAYA,), weights={}, qsv=QSV
        )
        results.append(decider.decide([_item(H1, ("q_a",))], QS)[0].answers)
    assert results[0] == results[1]


def test_ut03_65_duplicate_member_decider_is_config_error(paths: EnrichPaths) -> None:
    """UT03-65 two versions of one decider cannot both be members."""
    cache, calibration = DecisionCache(paths, QSV), CalibrationStore(paths)
    with pytest.raises(ConfigError, match="distinct"):
        EnsembleDecider(cache, calibration, members=(LLM, ("llm", "x")), weights={}, qsv=QSV)


def test_ut03_65_cache_read_lock_is_store_busy(
    paths: EnrichPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-65 an OS lock while reading the cache surfaces as StoreBusy (IO errors only)."""
    cache, calibration = _seed(paths)

    def locked() -> None:
        raise OSError(errno.EACCES, "locked")

    monkeypatch.setattr(cache, "dataset", locked)
    decider = EnsembleDecider(cache, calibration, members=MEMBERS, weights=WEIGHTS, qsv=QSV)
    with pytest.raises(StoreBusy):
        decider.decide([_item(H1)], QS)


def test_ut03_65_calibration_lock_is_store_busy(
    paths: EnrichPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-65 an OS lock while reading a calibration file surfaces as StoreBusy."""
    cache, calibration = _seed(paths)

    def locked(*args: object) -> None:
        raise OSError(errno.EBUSY, "locked")

    monkeypatch.setattr(calibration, "temperature", locked)
    decider = EnsembleDecider(cache, calibration, members=MEMBERS, weights=WEIGHTS, qsv=QSV)
    with pytest.raises(StoreBusy):
        decider.decide([_item(H1)], QS)


def test_ut03_65_row_of_member_name_with_other_member_version_dropped(
    paths: EnrichPaths,
) -> None:
    """UT03-65 a row with a member's decider name but a version not its own is not pooled."""
    cache, calibration = DecisionCache(paths, QSV), CalibrationStore(paths)
    _write(cache, LAYA, {H1: {"q_a": {"true": 0.8, "false": 0.2}}})
    # decider "llm" with laya's version: passes both isin filters, fails the pairing check.
    _write(cache, ("llm", LAYA[1]), {H1: {"q_a": {"true": 0.01, "false": 0.99}}})
    decider = EnsembleDecider(cache, calibration, members=(LAYA, LLM), weights={}, qsv=QSV)
    answer = decider.decide([_item(H1, ("q_a",))], QS)[0].answers["q_a"]
    assert answer.distribution["true"] == pytest.approx(_expected_pool([[0.8, 0.2]], [1])[0])
    assert answer.backend_confidence == 1.0


def _calibrated_choice(p: list[float], t: float) -> list[float]:
    z = [math.log(v + 1e-9) / t for v in p]
    e = [math.exp(v - max(z)) for v in z]
    return [v / sum(e) for v in e]


def test_ut03_65_choice_question_uses_member_temperature(paths: EnrichPaths) -> None:
    """UT03-65 a choice question with T = 0.5 for openjev pools the calibrated vector."""
    cache, calibration = _seed(paths)
    result = CalibrationResult(
        temperature=0.5, ece=0.01, ece_raw=0.02, accuracy=0.9, n=200, uncalibrated=False
    )
    calibration.save(OPENJEV[0], OPENJEV[1], QSV, {"q_c": result})
    decider = EnsembleDecider(cache, calibration, members=MEMBERS, weights=WEIGHTS, qsv=QSV)
    c = decider.decide([_item(H1, ("q_c",))], QS)[0].answers["q_c"]
    openjev = _calibrated_choice([0.2, 0.7, 0.1], 0.5)
    expected = _expected_pool([[0.6, 0.3, 0.1], openjev], [1.0, 1.0])  # no q_c weights
    assert [c.distribution[k] for k in ("db", "net", "app")] == pytest.approx(
        list(expected), abs=1e-9
    )
    assert c.answer == "net"
    assert c.probability == pytest.approx(expected[1], abs=1e-9)
