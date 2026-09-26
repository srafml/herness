"""Tests for herness.eval.grading numeric, entity and rubric grading (U11-56, U11-57, U11-59).

Rule grading and `count_unsupported` (U11-58, U11-60) live in `test_grading_rules.py`.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from herness.core.errors import EgressBlocked, ModelUnavailable
from herness.core.types.harness.evidence import NumberRef
from herness.core.types.swarm.drafts import ChatAnswer, Paragraph, RecommendationItem
from herness.eval import grading as g
from herness.eval.golden import (
    EntitiesExpected,
    NumericExpected,
    ReferenceResult,
    RubricExpected,
    SuiteError,
    Tolerance,
)

pytestmark = pytest.mark.unit

_QID = "q_" + "0" * 16
_FND = "fnd_" + "0" * 26


def _ref(nid: str, value: float | int | str, unit: str = "count", **kw: object) -> NumberRef:
    fields: dict[str, object] = {
        "id": nid, "value": value, "unit": unit, "query_id": _QID, "column": "value",
        "row_key": None,
    }  # fmt: skip
    fields.update(kw)
    return NumberRef.model_validate(fields)


def _reference(rows: list[tuple[object, ...]], columns: list[str] | None = None) -> ReferenceResult:
    return ReferenceResult(query_id="ref_q", columns=columns or ["value"], rows=rows)


def _numeric(unit: str = "count", **tol: float) -> NumericExpected:
    return NumericExpected.model_validate(
        {"reference_sql": "SELECT 1", "unit": unit, "tolerance": Tolerance(**tol) if tol else None}
    )


def _entities(check: str) -> EntitiesExpected:
    return EntitiesExpected(reference_sql="SELECT 1", check=check)


def _ids(*ids: str) -> ReferenceResult:
    return _reference([(i, n) for n, i in enumerate(ids)], ["entity_id", "score"])


def test_ut11_78_relative_tolerance_passes() -> None:
    """UT11-78 carrier with [[n1]] value 8.08, reference 8.0, rel 0.01 passes."""
    carrier = g.NumberCarrier(text="MTTR is [[n1]] hours", numbers=[_ref("n1", 8.08, "hours")])
    result = g.grade_numeric(_numeric("hours", rel=0.01), _reference([(8.0,)]), [carrier])
    assert result.status == "passed"
    assert result.check == "numeric"
    assert result.detail["query_id"] == "ref_q"
    assert result.detail["reference"] == "8.0"
    assert result.detail["candidates"] == [{"id": "n1", "value": "8.08"}]
    far = g.NumberCarrier(text="[[n1]]", numbers=[_ref("n1", 8.09, "hours")])
    assert g.grade_numeric(_numeric("hours", rel=0.01), _reference([(8.0,)]), [far]).status == (
        "failed"
    )


def test_ut11_78_zero_reference_and_value_column() -> None:
    """UT11-78 r == 0 needs |v| <= 1e-9; the `value` column wins over others."""
    zero = _reference([(0,)])
    tiny = g.NumberCarrier(text="[[n1]]", numbers=[_ref("n1", 0)])
    some = g.NumberCarrier(text="[[n1]]", numbers=[_ref("n1", 1)])
    assert g.grade_numeric(_numeric(rel=0.5), zero, [tiny]).status == "passed"
    assert g.grade_numeric(_numeric(rel=0.5), zero, [some]).status == "failed"
    ref = _reference([("x", 5)], ["name", "value"])
    five = g.NumberCarrier(text="[[n1]]", numbers=[_ref("n1", 5)])
    assert g.grade_numeric(_numeric(abs=0), ref, [five]).status == "passed"
    only = _reference([(5,)], ["n_incidents"])
    assert g.grade_numeric(_numeric(abs=0), only, [five]).status == "passed"


def test_ut11_78_reference_shape_errors() -> None:
    """UT11-78 reference needs exactly one row and one value column, numeric."""
    carrier = g.NumberCarrier(text="[[n1]]", numbers=[_ref("n1", 1)])
    for bad in (
        _reference([]),
        _reference([(1,), (2,)]),
        _reference([(1, 2)], ["a", "b"]),
        _reference([(None,)]),
        _reference([("abc",)]),
        _reference([(True,)]),
    ):
        with pytest.raises(SuiteError):
            g.grade_numeric(_numeric(abs=0), bad, [carrier])


def test_ut11_79_exact_and_unit_mismatch() -> None:
    """UT11-79 abs 0 on 12 vs 12 passes; unit hours vs expected count fails."""
    ref = _reference([(12,)])
    count = g.NumberCarrier(text="There were [[n1]] incidents", numbers=[_ref("n1", 12)])
    assert g.grade_numeric(_numeric("count", abs=0), ref, [count]).status == "passed"
    hours = g.NumberCarrier(text="[[n1]]", numbers=[_ref("n1", 12, "hours")])
    result = g.grade_numeric(_numeric("count", abs=0), ref, [hours])
    assert result.status == "failed"
    assert result.detail["candidates"] == []
    # no tolerance given → exact
    assert g.grade_numeric(_numeric("count"), ref, [count]).status == "passed"


def test_ut11_80_marker_absent_fails_usd_passes() -> None:
    """UT11-80 NumberRef without its marker fails; USD "1250000.00" vs Decimal passes."""
    ref = _reference([(12,)])
    absent = g.NumberCarrier(text="no marker here", numbers=[_ref("n1", 12)])
    assert g.grade_numeric(_numeric(abs=0), ref, [absent]).status == "failed"
    usd_ref = _reference([(Decimal("1250000"),)])
    usd = g.NumberCarrier(text="cost [[n3]]", numbers=[_ref("n3", "1250000.00", "usd")])
    other = g.NumberCarrier(text="[[n1]]", numbers=[_ref("n1", 3)])
    assert g.grade_numeric(_numeric("usd", abs=0), usd_ref, [other, usd]).status == "passed"


def test_ut11_80_carriers_from_pipeline_types() -> None:
    """UT11-80 carriers come from ChatAnswer, Paragraph and RecommendationItem."""
    n1 = _ref("n1", 12)
    answer = ChatAnswer(text="[[n1]] incidents", numbers=[n1], query_ids=[_QID])
    para = Paragraph(text="para [[n1]]", numbers=[n1], finding_ids=[_FND])
    rec = RecommendationItem.model_validate(
        {"kind": "fund", "target_type": "candidate", "target_id": "c1", "headline": "Fund it",
         "summary": "saves [[n1]]", "numbers": [n1], "finding_ids": [_FND],
         "query_ids": [_QID], "rank": 1}
    )  # fmt: skip
    carriers = g.carriers_from([answer, para, rec])
    assert [c.text for c in carriers] == [
        "[[n1]] incidents",
        "para [[n1]]",
        "Fund it\nsaves [[n1]]",
    ]
    assert all(c.numbers == [n1] for c in carriers)


def test_ut11_81_rank1() -> None:
    """UT11-81 ranked [a,b,c], ref [a,x] → rank1 passed."""
    assert g.grade_entities(_entities("rank1"), _ids("a", "x"), ["a", "b", "c"]).status == "passed"
    assert g.grade_entities(_entities("rank1"), _ids("x", "a"), ["a"]).status == "failed"
    assert g.grade_entities(_entities("rank1"), _ids("a"), []).status == "failed"
    with pytest.raises(SuiteError):
        g.grade_entities(_entities("rank1"), _reference([], ["entity_id"]), ["a"])


def test_ut11_82_topk_contains() -> None:
    """UT11-82 ranked [a..e], ref [a,b,x,y,e]: topk 5:3 passed, 5:4 failed."""
    ref, ranked = _ids("a", "b", "x", "y", "e"), ["a", "b", "c", "d", "e"]
    passed = g.grade_entities(_entities("topk_contains:5:3"), ref, ranked)
    assert passed.status == "passed"
    assert passed.check == "entities:topk_contains:5:3"
    assert g.grade_entities(_entities("topk_contains:5:4"), ref, ranked).status == "failed"


def test_ut11_83_set_equals() -> None:
    """UT11-83 ranked [b,a,z], ref [a,b] → set_equals passed."""
    assert g.grade_entities(_entities("set_equals"), _ids("a", "b"), ["b", "a", "z"]).status == (
        "passed"
    )
    assert g.grade_entities(_entities("set_equals"), _ids("a", "b"), ["b", "z", "a"]).status == (
        "failed"
    )


def test_ut11_84_kendall_tau() -> None:
    """UT11-84 one adjacent swap in 5 gives tau 0.8; a missing item counts at the end."""
    ref = ["a", "b", "c", "d", "e"]
    ok, tau = g.kendall_tau_check(["a", "b", "c", "e", "d"], ref, 0.8)
    assert ok
    assert tau == pytest.approx(0.8)
    # "c" missing → position 4: [0, 1, 4, 2, 3] has 2 discordant pairs of 10 → tau 0.6
    ok, tau = g.kendall_tau_check(["a", "b", "d", "e"], ref, 0.8)
    assert tau == pytest.approx(0.6)
    assert not ok
    result = g.grade_entities(_entities("kendall_tau>=0.8"), _ids(*ref), ["a", "b", "c", "e", "d"])
    assert result.status == "passed"
    assert result.detail["tau"] == pytest.approx(0.8)


def test_ut11_84_kendall_tau_degenerate() -> None:
    """UT11-84 n < 2 is a SuiteError; NaN tau (all missing → ties) fails."""
    with pytest.raises(SuiteError):
        g.kendall_tau_check(["a"], ["a"], 0.5)
    ok, tau = g.kendall_tau_check([], ["a", "b"], 0.5)
    assert not ok
    assert math.isnan(tau)
    result = g.grade_entities(_entities("kendall_tau>=0.5"), _ids("a", "b"), [])
    assert result.status == "failed"
    assert result.detail["tau"] is None


def test_ut11_85_chat_entity_ids_longest_first() -> None:
    """UT11-85 "Platform Ops L2" wins over "Platform Ops"; order of first occurrence."""
    index = {"platform ops": "t_ops", "platform ops l2": "t_l2", "payments": "s_pay"}
    text = "PLATFORM OPS L2 beats Payments; then platform ops, and payments again. Platform Opsx"
    assert g.chat_entity_ids(text, index) == ["t_l2", "s_pay", "t_ops"]
    assert g.chat_entity_ids("platform ops then Platform Ops L2", index) == ["t_ops", "t_l2"]
    assert g.chat_entity_ids("nothing", index) == []
    assert g.chat_entity_ids("x", {"": "empty"}) == []


@given(st.lists(st.uuids().map(str), min_size=2, max_size=30, unique=True))
def test_pt11_06_identity_and_reversal(ids: list[str]) -> None:
    """PT11-06 identical order gives tau 1, reversed order gives -1."""
    assert g.kendall_tau_check(ids, ids, 1.0) == (True, pytest.approx(1.0))
    ok, tau = g.kendall_tau_check(list(reversed(ids)), ids, 0.0)
    assert not ok
    assert tau == pytest.approx(-1.0)


@dataclass
class _Score:
    scores: Mapping[str, int]


class _Judge:
    def __init__(self, scores: Mapping[str, int] | None = None) -> None:
        self._scores = scores
        self.calls: list[tuple[str, tuple[str, ...], float, str]] = []

    def score(
        self, question_id: str, criteria: Sequence[str], min_score: float, final_text: str
    ) -> _Score:
        self.calls.append((question_id, tuple(criteria), min_score, final_text))
        if self._scores is None:
            msg = "judge down"
            raise ModelUnavailable(msg)
        return _Score(self._scores)


def test_ut11_91_rubric_skipped_reasons() -> None:
    """UT11-91 judge None and judge raising ModelUnavailable both skip, reasons differ."""
    exp = RubricExpected(criteria=["clarity"], min_score=3)
    none = g.grade_rubric(exp, "G01", "text", None)
    down = g.grade_rubric(exp, "G01", "text", _Judge())
    assert (none.status, down.status) == ("skipped", "skipped")
    assert none.detail["reason"] == "no_judge"
    assert down.detail["reason"] == "judge_unavailable"
    assert none.check == "rubric"


def test_ut11_91_rubric_skipped_on_egress_blocked() -> None:
    """UT11-91 a judge refused by the egress guard skips with its own reason."""

    class _Blocked:
        def score(self, *_args: object) -> _Score:
            msg = "egress refused"
            raise EgressBlocked(msg, reason="profile_local")

    exp = RubricExpected(criteria=["clarity"], min_score=3)
    result = g.grade_rubric(exp, "G01", "text", _Blocked())
    assert (result.status, result.detail) == ("skipped", {"reason": "judge_egress_blocked"})


def test_ut11_92_rubric_mean() -> None:
    """UT11-92 scores 3 and 4 with min 3.5 pass; lower mean or no scores fail."""
    exp = RubricExpected(criteria=["clarity", "actionability"], min_score=3.5)
    judge = _Judge({"clarity": 3, "actionability": 4})
    result = g.grade_rubric(exp, "G07", "final", judge)
    assert result.status == "passed"
    assert result.detail["mean"] == pytest.approx(3.5)
    assert judge.calls == [("G07", ("clarity", "actionability"), 3.5, "final")]
    assert g.grade_rubric(exp, "G07", "f", _Judge({"clarity": 3, "actionability": 3})).status == (
        "failed"
    )
    assert g.grade_rubric(exp, "G07", "f", _Judge({})).status == "failed"
