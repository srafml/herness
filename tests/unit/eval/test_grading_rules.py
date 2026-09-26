"""Tests for herness.eval.grading rules and unsupported numbers (U11-58, U11-60)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from herness.core.errors import QueryError
from herness.core.types.harness.evidence import NumberRef
from herness.eval import grading as g
from herness.eval.golden import ClaimRule, MentionRule, RulesExpected, SignRule
from herness.reports.settings import ReportsSection

pytestmark = pytest.mark.unit

_Q1 = "q_" + "1" * 16
_Q2 = "q_" + "2" * 16
_ALLOWED = ReportsSection().compiled_numeral_patterns
_BUILD = "b_1"


def _ref(nid: str, value: float | int | str, unit: str = "count", **kw: object) -> NumberRef:
    fields: dict[str, object] = {
        "id": nid, "value": value, "unit": unit, "query_id": _Q1, "column": "value",
        "row_key": None,
    }  # fmt: skip
    fields.update(kw)
    return NumberRef.model_validate(fields)


def _statuses(results: Sequence[g.GradeResult]) -> list[str]:
    return [r.status for r in results]


def test_ut11_86_split_sentences_and_must_mention() -> None:
    """UT11-86 string must_mention matches case-insensitively."""
    assert g.split_sentences("One. Two!  Three?\nFour\n\n five ") == [
        "One.", "Two!", "Three?", "Four", "five",
    ]  # fmt: skip
    rules = RulesExpected(must_mention=["platform OPS", "absent"])
    results = g.grade_rules(rules, "The Platform Ops team.", [], [])
    assert _statuses(results) == ["passed", "failed"]
    assert results[0].check == "must_mention"
    assert results[0].detail == {"item": "platform OPS"}


def test_ut11_87_mention_rule_same_sentence() -> None:
    """UT11-87 entity and phrase in different sentences fail; same sentence passes."""
    rule = MentionRule(entity="Payments", with_any=["MTTR", "repeat"])
    rules = RulesExpected(must_mention=[rule])
    split = "Payments is slow. Its mttr rose."
    assert _statuses(g.grade_rules(rules, split, [], [])) == ["failed"]
    assert _statuses(g.grade_rules(rules, "payments MTTR rose.", [], [])) == ["passed"]


def test_ut11_88_claim_rule_checks_verified_claims() -> None:
    """UT11-88 a verified finding claim matching a ClaimRule fails the rule."""
    rule = ClaimRule(entity="Team Blue", pattern=r"\bdegrad\w*")
    rules = RulesExpected(must_not_claim=[rule])
    clean = "Team Blue is stable. Other teams degraded."
    assert _statuses(g.grade_rules(rules, clean, [], [])) == ["passed"]
    claims = ["Nothing here.", "Volume up. TEAM BLUE DEGRADED sharply."]
    result = g.grade_rules(rules, clean, claims, [])
    assert _statuses(result) == ["failed"]
    assert result[0].check == "must_not_claim"
    assert result[0].detail["where"] == "claim[1]"
    assert _statuses(g.grade_rules(rules, "team blue degrading.", [], [])) == ["failed"]


def test_ut11_89_max_number_refs() -> None:
    """UT11-89 max_number_refs 0 passes with no NumberRef and fails with one."""
    rules = RulesExpected(max_number_refs=0)
    assert _statuses(g.grade_rules(rules, "t", [], [])) == ["passed"]
    result = g.grade_rules(rules, "t", [], [_ref("n1", 1)])
    assert _statuses(result) == ["failed"]
    assert result[0].detail == {"count": 1, "max": 0}
    assert g.grade_rules(RulesExpected(), "t", [], []) == []


def test_ut11_90_number_signs() -> None:
    """UT11-90 NumberRef column trend_slope value 0.3 satisfies number_signs positive."""
    slope = _ref("n1", 0.3, "ratio", column="trend_slope")
    usd = _ref("n2", "-5.00", "usd", column="delta")
    rules = RulesExpected(
        number_signs=[
            SignRule(column="trend_slope", sign="positive"),
            SignRule(column="trend_slope", sign="negative"),
            SignRule(column="delta", sign="negative"),
            SignRule(column="absent", sign="positive"),
        ]
    )
    results = g.grade_rules(rules, "t", [], [slope, usd])
    assert _statuses(results) == ["passed", "failed", "passed", "failed"]
    assert results[0].check == "number_signs"


class _Env:
    """Evidence lookup and rerun fakes for count_unsupported."""

    def __init__(
        self,
        rows: Mapping[str, list[dict[str, object]]],
        *,
        known: set[str] | None = None,
        failing: set[str] | None = None,
    ) -> None:
        self.rows = rows
        self.known = set(rows) if known is None else known
        self.failing = failing or set()
        self.reruns: list[str] = []

    def evidence(self, query_id: str, build_id: str) -> bool:
        return build_id == _BUILD and query_id in self.known

    def rerun(self, query_id: str) -> Sequence[Mapping[str, object]]:
        self.reruns.append(query_id)
        if query_id in self.failing:
            msg = "rerun failed"
            raise QueryError(msg)
        return self.rows[query_id]

    def count(self, text: str, numbers: Sequence[NumberRef]) -> g.UnsupportedReport:
        return g.count_unsupported(
            text, numbers, _BUILD, evidence=self.evidence, rerun=self.rerun,
            allowed_patterns=_ALLOWED, float_rel_tol=0.005,
        )  # fmt: skip


def test_ut11_93_allowed_numerals_and_valid_ref() -> None:
    """UT11-93 years, ids, dates, quarters and a valid [[n1]] give unsupported 0."""
    env = _Env({_Q1: [{"value": 42}]})
    report = env.count("In Q3 2026 INC0012345 and 2026-09-24 [[n1]]", [_ref("n1", 42)])
    assert report.unsupported == 0
    assert report.total == 1
    assert report.rate == 0.0
    assert (report.stray_numerals, report.orphan_markers, report.stale_refs) == ([], [], [])


def test_ut11_94_stray_and_orphan() -> None:
    """UT11-94 "about 42 incidents [[n2]]" without n2: stray 42 and orphan n2, rate 2/2."""
    report = _Env({}).count("about 42 incidents [[n2]]", [])
    assert report.stray_numerals == ["42"]
    assert report.orphan_markers == ["n2"]
    assert (report.total, report.unsupported, report.rate) == (2, 2, 1.0)
    empty = _Env({}).count("no numbers", [])
    assert (empty.total, empty.unsupported, empty.rate) == (0, 0, 0.0)


def test_ut11_95_stale_refs() -> None:
    """UT11-95 a ref whose query id is missing and one whose rerun value changed are stale."""
    env = _Env({_Q1: [{"value": 10}], _Q2: [{"value": 11}]}, known={_Q2})
    missing = _ref("n1", 10)
    changed = _ref("n2", 10, query_id=_Q2)
    report = env.count("[[n1]] and [[n2]]", [missing, changed])
    assert report.stale_refs == ["n1", "n2"]
    assert (report.total, report.unsupported) == (2, 2)
    assert env.reruns == [_Q2]


def test_ut11_95_stale_rerun_failures_and_rows() -> None:
    """UT11-95 rerun failure, missing column, row_key selection and comparisons."""
    rows = [{"team": "a", "value": Decimal("1250000.00"), "n": 3, "f": 0.1234},
            {"team": "b", "value": Decimal("7.50"), "n": 4, "f": 2.0}]  # fmt: skip
    env = _Env({_Q1: rows, _Q2: []}, failing={_Q2})
    refs = [
        _ref("n1", "1250000.00", "usd", row_key={"team": "a"}),  # decimal match
        _ref("n2", 4, column="n", row_key={"team": "b"}),  # integer match
        _ref("n3", 0.123, "ratio", column="f", row_key={"team": "a"}),  # float by decimals
        _ref("n4", 5, column="n", row_key={"team": "a"}),  # integer mismatch
        _ref("n5", 1, column="missing", row_key={"team": "a"}),  # missing column
        _ref("n6", 1, column="n"),  # row_key None with two rows
        _ref("n7", 1, column="n", row_key={"team": "zz"}),  # row not found
        _ref("n8", 1, query_id=_Q2),  # rerun fails
    ]
    text = " ".join(f"[[{r.id}]]" for r in refs)
    report = env.count(text, refs)
    assert report.stale_refs == ["n4", "n5", "n6", "n7", "n8"]
    assert env.reruns == [_Q1, _Q2]  # one rerun per query id


_ALLOWED_TOKENS = ("2026", "1999", "Q3 2025", "INC0012345", "2026-09-24", "OPS-12", "CHG42")


@given(
    st.lists(
        st.one_of(
            st.sampled_from(_ALLOWED_TOKENS),
            st.integers(1, 9).map(lambda k: f"[[n{k}]]"),
            st.sampled_from(("incidents", "rose", "and", "the", ".")),
        ),
        max_size=25,
    ),
    st.integers(0, 10**6),
)
def test_pt11_07_allowed_text_has_no_unsupported(tokens: list[str], value: int) -> None:
    """PT11-07 allowed numerals plus valid markers with matching refs give unsupported 0."""
    text = " ".join(tokens)
    refs = [_ref(f"n{k}", value) for k in range(1, 10)]
    report = _Env({_Q1: [{"value": value}]}).count(text, refs)
    assert report.unsupported == 0
    assert report.rate == 0.0
