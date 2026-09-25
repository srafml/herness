"""Tests for herness.core.types.harness.evidence (U05-10, U05-11, U05-12)."""

from datetime import UTC, datetime
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError
from tests.support.harness_fakes import FakeOps

from herness.core.ids import query_id
from herness.core.types import (
    Evidence,
    ItemResult,
    NumberCheck,
    NumberRef,
    UncitedSpan,
    VerifiableItem,
    VerificationResult,
)

pytestmark = pytest.mark.unit

BUILD = "20260101-000000-ABCDEF"
QID = "q_0123456789abcdef"
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def _ref(**overrides: Any) -> NumberRef:
    fields: dict[str, Any] = {
        "id": "n1",
        "value": 42,
        "unit": "count",
        "query_id": QID,
        "column": "n",
        "row_key": None,
    }
    fields.update(overrides)
    return NumberRef(**fields)


def test_ut05_06_valid_refs_pass() -> None:
    """UT05-06 usd with a decimal string and other units with numbers build."""
    assert _ref(unit="usd", value="1250000.00", format="usd").value == "1250000.00"
    assert _ref(unit="usd", value="-3").value == "-3"
    assert _ref(unit="pct", value=12.5, row_key={"team": "a", "open": True}).value == 12.5
    assert type(_ref(value=7).value) is int
    with pytest.raises(ValidationError):
        _ref().value = 1  # type: ignore[misc]


@pytest.mark.parametrize(
    "overrides",
    [
        {"unit": "usd", "value": 1250000},
        {"unit": "usd", "value": "1,250,000"},
        {"unit": "usd", "value": "12."},
        {"unit": "count", "value": "42"},
        {"id": "x1"},
        {"id": "n"},
        {"query_id": "q_0123"},
        {"query_id": "Q_0123456789ABCDEF"},
        {"value": True},
        {"value": float("nan")},
        {"value": float("inf")},
        {"column": ""},
        {"row_key": {f"k{i}": i for i in range(17)}},
        {"row_key": {"k": float("inf")}},
        {"format": "eur"},
        {"extra": 1},
    ],
)
def test_ut05_06_invalid_refs_raise(overrides: dict[str, Any]) -> None:
    """UT05-06 usd with a number, non-usd with a string, bad ids and bool values raise."""
    with pytest.raises(ValidationError):
        _ref(**overrides)


_ROW_VALUE = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=8),
)
_REFS = st.builds(
    NumberRef,
    id=st.from_regex(r"\An[0-9]{1,6}\Z"),
    value=st.one_of(st.integers(), st.floats(allow_nan=False, allow_infinity=False)),
    unit=st.sampled_from(["count", "pct", "ratio", "hours", "score", "rank", "other"]),
    query_id=st.from_regex(r"\Aq_[0-9a-f]{16}\Z"),
    column=st.text(min_size=1, max_size=128),
    row_key=st.one_of(st.none(), st.dictionaries(st.text(max_size=8), _ROW_VALUE, max_size=16)),
    format=st.one_of(st.none(), st.sampled_from(["int", "pct1", "ratio2", "prob2"])),
) | st.builds(
    NumberRef,
    id=st.from_regex(r"\An[0-9]{1,6}\Z"),
    value=st.from_regex(r"\A-?[0-9]{1,12}(\.[0-9]{1,6})?\Z"),
    unit=st.just("usd"),
    query_id=st.from_regex(r"\Aq_[0-9a-f]{16}\Z"),
    column=st.text(min_size=1, max_size=128),
    row_key=st.none(),
    format=st.sampled_from([None, "usd", "usd_compact"]),
)


@given(_REFS)
def test_pt05_01_number_ref_json_round_trip(ref: NumberRef) -> None:
    """PT05-01 any valid NumberRef round-trips through JSON unchanged."""
    again = NumberRef.model_validate_json(ref.model_dump_json())
    assert again == ref
    assert type(again.value) is type(ref.value)


def _evidence(**overrides: Any) -> Evidence:
    sql = "select count(*) as n from core.ticket where team = $team"
    params: dict[str, Any] = {"team": "a"}
    fields: dict[str, Any] = {
        "query_id": query_id(sql, params, BUILD),
        "run_id": "run_1",
        "build_id": BUILD,
        "sql": sql,
        "params": params,
        "result_hash": "a" * 64,
        "row_count": 1,
        "result_sample": [{"n": 3}],
        "executed_at": NOW,
        "duration_ms": 12,
    }
    fields.update(overrides)
    return Evidence(**fields)


def test_ut05_47_evidence_query_id_and_limits() -> None:
    """UT05-47 Evidence recomputes query_id, needs aware UTC and bounds its sample."""
    ev = _evidence()
    assert Evidence.model_validate_json(ev.model_dump_json()) == ev
    ops = FakeOps()
    assert ops.record_evidence(ev) is True
    assert ops.record_evidence(ev.model_copy(update={"run_id": "run_2"})) is False
    assert ops.get_evidence(ev.query_id) == ev
    bad: list[dict[str, Any]] = [
        {"sql": "select 2"},
        {"params": {"team": "b"}},
        {"build_id": "not-a-build"},
        {"params": {"x": float("nan")}},
        {"executed_at": datetime(2026, 9, 25)},  # noqa: DTZ001 - naive on purpose
        {"result_sample": [{"n": i} for i in range(51)]},
        {"result_hash": "abc"},
        {"row_count": -1},
        {"duration_ms": -1},
        {"sql": "x" * 20_001},
    ]
    for overrides in bad:
        with pytest.raises(ValidationError):
            _evidence(**overrides)


def _check(result: str = "match", number_id: str = "n1") -> NumberCheck:
    return NumberCheck(
        number_id=number_id,
        query_id=QID,
        column="n",
        row_key=None,
        claimed=3,
        actual=3 if result == "match" else None,
        result=result,  # type: ignore[arg-type]
    )


def _item(passed: bool, checks: list[NumberCheck], **lists: Any) -> ItemResult:
    fields: dict[str, Any] = {
        "uncited": [],
        "unknown_markers": [],
        "bad_refs": [],
        "unverified_findings": [],
    }
    fields.update(lists)
    return ItemResult(where="finding:fnd_1", passed=passed, checks=checks, **fields)


def test_ut05_07_consistent_results_pass() -> None:
    """UT05-07 consistent passed flags and counts build and round-trip JSON."""
    good = _item(True, [_check(), _check(number_id="n2")])
    bad = _item(False, [_check("mismatch")], uncited=[UncitedSpan(text="42", start=0, end=2)])
    assert good.claim_support is None
    result = VerificationResult(
        build_id=BUILD,
        passed=False,
        items=[good, bad],
        n_numbers=3,
        n_failed=1,
        verified_at=NOW,
        duration_ms=5,
    )
    assert VerificationResult.model_validate_json(result.model_dump_json()) == result
    empty = VerificationResult(
        build_id=BUILD,
        passed=True,
        items=[],
        n_numbers=0,
        n_failed=0,
        verified_at=NOW,
        duration_ms=0,
    )
    assert empty.passed is True


@pytest.mark.parametrize(
    ("passed", "checks", "lists"),
    [
        (True, [_check("mismatch")], {}),
        (False, [_check()], {}),
        (True, [], {"uncited": [UncitedSpan(text="7", start=1, end=2)]}),
        (True, [], {"unknown_markers": ["n9"]}),
        (True, [], {"bad_refs": ["expected_usd_ref"]}),
        (True, [], {"unverified_findings": ["fnd_1"]}),
    ],
)
def test_ut05_07_inconsistent_item_raises(
    passed: bool, checks: list[NumberCheck], lists: dict[str, Any]
) -> None:
    """UT05-07 ItemResult.passed must match its checks and lists."""
    with pytest.raises(ValidationError):
        _item(passed, checks, **lists)


@pytest.mark.parametrize(
    ("passed", "n_numbers", "n_failed", "verified_at"),
    [
        (True, 2, 1, NOW),
        (False, 3, 1, NOW),
        (False, 2, 0, NOW),
        (False, 2, 1, datetime(2026, 9, 25)),  # noqa: DTZ001 - naive on purpose
    ],
)
def test_ut05_07_inconsistent_result_raises(
    passed: bool, n_numbers: int, n_failed: int, verified_at: datetime
) -> None:
    """UT05-07 VerificationResult passed, n_numbers, n_failed and verified_at are checked."""
    items = [_item(True, [_check()]), _item(False, [_check("row_not_found")])]
    with pytest.raises(ValidationError):
        VerificationResult(
            build_id=BUILD,
            passed=passed,
            items=items,
            n_numbers=n_numbers,
            n_failed=n_failed,
            verified_at=verified_at,
            duration_ms=1,
        )


def test_ut05_07_verifiable_item_limits() -> None:
    """UT05-07 VerifiableItem bounds where, text, numbers, finding_ids and refs."""
    item = VerifiableItem(where="sections[0].paragraphs[1]", text="[[n1]] open", numbers=[_ref()])
    assert (item.finding_ids, item.refs) == ([], {})
    bad: list[dict[str, Any]] = [
        {"where": "w" * 201},
        {"text": "t" * 20_001},
        {"numbers": [_ref()] * 201},
        {"finding_ids": ["fnd_1"] * 201},
        {"refs": {f"r{i}": "n1" for i in range(51)}},
    ]
    for overrides in bad:
        fields: dict[str, Any] = {"where": "w", "text": "t", "numbers": []}
        fields.update(overrides)
        with pytest.raises(ValidationError):
            VerifiableItem(**fields)
