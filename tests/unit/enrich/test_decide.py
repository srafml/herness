"""Tests for primary rules and resolution reference (U03-70 ... U03-74, T03-17).

`primary_decider_for` and `chain_after` take a keyword-only `deciders: DecidersSettings`
(see the T03-17 report for the deviation from the literal U03-70/U03-71 signatures, forced
by the R-76 settings split that moved `deciders.*.enabled` out of `DecisionsConfig`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from herness.core.types import Question
from herness.enrich.decide import (
    CachedAnswer,
    Resolution,
    chain_after,
    gate,
    primary_decider_for,
    resolve_pair,
)
from herness.enrich.settings import (
    DecidersSettings,
    DecisionsConfig,
    JevSettings,
    OpenJevSettings,
)

pytestmark = pytest.mark.unit

_T0 = datetime(2026, 1, 1, tzinfo=UTC)
_T1 = datetime(2026, 1, 2, tzinfo=UTC)


def _question(qid: str = "root_cause") -> Question:
    return Question(
        id=qid,
        type="choice",
        instructions="Most likely root cause of this incident, in detail.",
        options={"a": "Option A.", "b": "Option B."},
        threshold=0.7,
    )


def _cfg(
    *,
    primary: str = "laya",
    chain: tuple[str, ...] = ("openjev", "llm"),
    override: str | None = None,
    qid: str = "root_cause",
) -> DecisionsConfig:
    question: dict[str, object] = {
        "id": qid,
        "type": "choice",
        "instructions": "Most likely root cause of this incident, in detail.",
        "options": {"a": "Option A.", "b": "Option B."},
        "threshold": 0.7,
    }
    if override is not None:
        question["primary_decider"] = override
    return DecisionsConfig.model_validate(
        {
            "question_set_version": "qs-2026-10-01.1",
            "primary_decider": primary,
            "escalation_chain": chain,
            "questions": (question,),
            "change_link": {"use_decider": False},
        }
    )


def _deciders(*, openjev_enabled: bool = True, jev_enabled: bool = False) -> DecidersSettings:
    return DecidersSettings(
        openjev=OpenJevSettings(enabled=openjev_enabled), jev=JevSettings(enabled=jev_enabled)
    )


# --- UT03-68: primary_decider_for -------------------------------------------------------


@dataclass(frozen=True)
class _PrimaryCase:
    name: str
    primary: str
    chain: tuple[str, ...]
    override: str | None
    accepted: frozenset[str] | None
    openjev_on: bool
    jev_on: bool
    expected: str


_UT03_68_CASES = [
    _PrimaryCase(
        "laya_default_degraded_falls_to_openjev_teacher",
        "laya",
        ("openjev", "llm"),
        None,
        None,
        True,
        False,
        "openjev",
    ),
    _PrimaryCase(
        "laya_default_accepted_returns_laya",
        "laya",
        ("openjev", "llm"),
        None,
        frozenset({"root_cause"}),
        True,
        False,
        "laya",
    ),
    _PrimaryCase(
        "laya_default_accepted_set_excludes_question",
        "laya",
        ("openjev", "llm"),
        None,
        frozenset(),
        True,
        False,
        "openjev",
    ),
    _PrimaryCase(
        "override_openjev_disabled_falls_to_llm_teacher",
        "laya",
        ("openjev", "llm"),
        "openjev",
        None,
        False,
        False,
        "llm",
    ),
    _PrimaryCase(
        "override_jev_enabled_returns_jev",
        "laya",
        ("openjev", "llm"),
        "jev",
        None,
        True,
        True,
        "jev",
    ),
    _PrimaryCase(
        "override_jev_disabled_falls_to_openjev_teacher",
        "laya",
        ("openjev", "llm"),
        "jev",
        None,
        True,
        False,
        "openjev",
    ),
    _PrimaryCase(
        "override_llm_always_llm", "laya", ("openjev", "llm"), "llm", None, True, False, "llm"
    ),
    _PrimaryCase(
        "jev_in_chain_and_enabled_is_teacher", "laya", ("jev", "llm"), None, None, True, True, "jev"
    ),
    _PrimaryCase(
        "jev_in_chain_but_disabled_openjev_is_teacher",
        "laya",
        ("jev", "llm"),
        None,
        None,
        True,
        False,
        "openjev",
    ),
]


@pytest.mark.parametrize("case", _UT03_68_CASES, ids=[c.name for c in _UT03_68_CASES])
def test_ut03_68_primary_decider_for(case: _PrimaryCase) -> None:
    """UT03-68 combinations of accepted set, overrides, enabled flags; table-driven names."""
    cfg = _cfg(primary=case.primary, chain=case.chain, override=case.override)
    deciders = _deciders(openjev_enabled=case.openjev_on, jev_enabled=case.jev_on)
    got = primary_decider_for(_question(), cfg=cfg, laya_accepted=case.accepted, deciders=deciders)
    assert got == case.expected, case.name


# --- UT03-69: chain_after ----------------------------------------------------------------


def test_ut03_69_chain_after_jev_replaces_openjev_when_enabled() -> None:
    """UT03-69 with jev enabled, openjev is replaced by jev in the whole chain."""
    cfg = _cfg(primary="laya", chain=("openjev", "llm"))
    deciders = _deciders(openjev_enabled=True, jev_enabled=True)
    assert chain_after("laya", cfg=cfg, deciders=deciders) == ("jev", "llm")


def test_ut03_69_chain_after_primary_openjev_leaves_llm() -> None:
    """UT03-69 primary openjev: members after its position are just [llm]."""
    cfg = _cfg(primary="laya", chain=("openjev", "llm"))
    deciders = _deciders(openjev_enabled=True, jev_enabled=True)
    assert chain_after("openjev", cfg=cfg, deciders=deciders) == ("llm",)


def test_ut03_69_chain_after_primary_jev_against_openjev_authored_chain() -> None:
    """UT03-69 primary jev matches the openjev slot it replaced; result never contains it."""
    cfg = _cfg(primary="laya", chain=("openjev", "llm"))
    deciders = _deciders(openjev_enabled=True, jev_enabled=True)
    result = chain_after("jev", cfg=cfg, deciders=deciders)
    assert result == ("llm",)
    assert "jev" not in result


def test_ut03_69_chain_after_primary_jev_against_jev_authored_chain() -> None:
    """UT03-69 primary jev against a chain that already lists jev literally."""
    cfg = _cfg(primary="laya", chain=("jev", "llm"))
    deciders = _deciders(openjev_enabled=True, jev_enabled=True)
    result = chain_after("jev", cfg=cfg, deciders=deciders)
    assert result == ("llm",)
    assert "jev" not in result


def test_ut03_69_chain_after_primary_jev_against_both_listed_chain_openjev_first() -> None:
    """UT03-69 chain listing both openjev and jev: primary jev matches either slot."""
    cfg = _cfg(primary="laya", chain=("openjev", "jev", "llm"))
    deciders = _deciders(openjev_enabled=True, jev_enabled=True)
    result = chain_after("jev", cfg=cfg, deciders=deciders)
    assert result == ("llm",)
    assert "jev" not in result


def test_ut03_69_chain_after_primary_jev_against_both_listed_chain_jev_first() -> None:
    """UT03-69 chain listing jev then openjev: primary jev still never survives the slice."""
    cfg = _cfg(primary="laya", chain=("jev", "openjev", "llm"))
    deciders = _deciders(openjev_enabled=True, jev_enabled=True)
    result = chain_after("jev", cfg=cfg, deciders=deciders)
    assert result == ("llm",)
    assert "jev" not in result


def test_ut03_69_chain_after_removes_disabled_members() -> None:
    """UT03-69 a disabled openjev is removed from the chain."""
    cfg = _cfg(primary="laya", chain=("openjev", "llm"))
    deciders = _deciders(openjev_enabled=False, jev_enabled=False)
    assert chain_after("laya", cfg=cfg, deciders=deciders) == ("llm",)


def test_ut03_69_chain_after_dedupes_and_keeps_llm_last() -> None:
    """UT03-69 replacing openjev with an already-present jev dedupes; llm stays last."""
    cfg = _cfg(primary="laya", chain=("openjev", "jev", "llm"))
    deciders = _deciders(openjev_enabled=True, jev_enabled=True)
    assert chain_after("laya", cfg=cfg, deciders=deciders) == ("jev", "llm")


def test_ut03_69_chain_after_primary_at_end_is_empty() -> None:
    """UT03-69 a primary at the end of the chain leaves nothing after it."""
    cfg = _cfg(primary="laya", chain=("openjev", "llm"))
    deciders = _deciders(openjev_enabled=True, jev_enabled=False)
    assert chain_after("llm", cfg=cfg, deciders=deciders) == ()


# --- UT03-70: gate, Resolution -------------------------------------------------------------


def test_ut03_70_gate_at_threshold_is_true() -> None:
    """UT03-70 p = threshold: gate returns true."""
    assert gate(0.7, 0.7) is True


def test_ut03_70_gate_below_and_above_threshold() -> None:
    """UT03-70 gate compares p_calibrated >= threshold either side of the boundary."""
    assert gate(0.6999, 0.7) is False
    assert gate(0.9, 0.7) is True


def test_ut03_70_resolution_final_invariant() -> None:
    """UT03-70 Resolution status == 'final' iff answer/probability/decider fields are set."""
    final = Resolution(
        status="final",
        answer="a",
        probability=0.9,
        decider="openjev",
        decider_version="v1",
        escalated=False,
        decided_at=_T0,
        agreement=None,
        review_status="none",
    )
    queued = Resolution(
        status="queue",
        answer=None,
        probability=None,
        decider=None,
        decider_version=None,
        escalated=False,
        decided_at=None,
        agreement=None,
        review_status="none",
    )
    assert final.status == "final"
    assert queued.status == "queue"
    assert queued.answer is None


# --- UT03-71: resolve_pair -----------------------------------------------------------------


def test_ut03_71_primary_pass() -> None:
    """UT03-71 primary row gates true: final, not escalated."""
    rows = {"openjev": CachedAnswer("a", 0.9, _T0, "ov1")}
    res = resolve_pair(
        human=None,
        rows=rows,
        primary="openjev",
        chain=("jev", "llm"),
        threshold=0.7,
        in_scope=True,
        pending_review=False,
    )
    assert res == Resolution(
        status="final",
        answer="a",
        probability=0.9,
        decider="openjev",
        decider_version="ov1",
        escalated=False,
        decided_at=_T0,
        agreement=None,
        review_status="none",
    )


def test_ut03_71_primary_fail_with_chain_row() -> None:
    """UT03-71 primary row gates false; first chain member with a row is final, escalated."""
    rows = {
        "openjev": CachedAnswer("a", 0.5, _T0, "ov1"),
        "jev": CachedAnswer("b", 0.6, _T0, "jv1"),
    }
    res = resolve_pair(
        human=None,
        rows=rows,
        primary="openjev",
        chain=("jev", "llm"),
        threshold=0.7,
        in_scope=True,
        pending_review=False,
    )
    assert res == Resolution(
        status="final",
        answer="b",
        probability=0.6,
        decider="jev",
        decider_version="jv1",
        escalated=True,
        decided_at=_T0,
        agreement=None,
        review_status="none",
    )


def test_ut03_71_none_in_scope_queues() -> None:
    """UT03-71 no machine result, in scope: queue."""
    res = resolve_pair(
        human=None,
        rows={},
        primary="openjev",
        chain=("jev", "llm"),
        threshold=0.7,
        in_scope=True,
        pending_review=False,
    )
    assert res.status == "queue"
    assert res.answer is None


def test_ut03_71_none_out_of_scope_and_no_primary_row() -> None:
    """UT03-71 no machine result, out of scope, no primary row: out_of_scope."""
    res = resolve_pair(
        human=None,
        rows={},
        primary="openjev",
        chain=("jev", "llm"),
        threshold=0.7,
        in_scope=False,
        pending_review=False,
    )
    assert res.status == "out_of_scope"
    assert res.answer is None


def test_ut03_71_out_of_scope_but_primary_row_present_still_queues() -> None:
    """UT03-71 below-threshold primary row present, out of scope: still queue, not out_of_scope."""
    rows = {"openjev": CachedAnswer("a", 0.3, _T0, "ov1")}
    res = resolve_pair(
        human=None,
        rows=rows,
        primary="openjev",
        chain=(),
        threshold=0.7,
        in_scope=False,
        pending_review=False,
    )
    assert res.status == "queue"


def test_ut03_71_pending_review() -> None:
    """UT03-71 a final result with an open label_check: review_status pending."""
    rows = {"openjev": CachedAnswer("a", 0.9, _T0, "ov1")}
    res = resolve_pair(
        human=None,
        rows=rows,
        primary="openjev",
        chain=("jev", "llm"),
        threshold=0.7,
        in_scope=True,
        pending_review=True,
    )
    assert res.status == "final"
    assert res.review_status == "pending"


def test_ut03_71_human_confirms_machine_result() -> None:
    """UT03-71 human answer matches m: m is returned with review_status confirmed."""
    rows = {"openjev": CachedAnswer("a", 0.9, _T0, "ov1")}
    res = resolve_pair(
        human=("a", _T1),
        rows=rows,
        primary="openjev",
        chain=("jev", "llm"),
        threshold=0.7,
        in_scope=True,
        pending_review=False,
    )
    assert res == Resolution(
        status="final",
        answer="a",
        probability=0.9,
        decider="openjev",
        decider_version="ov1",
        escalated=False,
        decided_at=_T0,
        agreement=None,
        review_status="confirmed",
    )


def test_ut03_71_human_corrects_machine_result() -> None:
    """UT03-71 human answer differs from m: human wins, review_status corrected."""
    rows = {"openjev": CachedAnswer("a", 0.9, _T0, "ov1")}
    res = resolve_pair(
        human=("b", _T1),
        rows=rows,
        primary="openjev",
        chain=("jev", "llm"),
        threshold=0.7,
        in_scope=True,
        pending_review=False,
    )
    assert res == Resolution(
        status="final",
        answer="b",
        probability=1.0,
        decider="human",
        decider_version="human",
        escalated=False,
        decided_at=_T1,
        agreement=None,
        review_status="corrected",
    )


def test_ut03_71_human_label_with_no_machine_result_is_confirmed() -> None:
    """UT03-71 a fresh human label with no machine result: confirmed, not corrected."""
    res = resolve_pair(
        human=("c", _T1),
        rows={},
        primary="openjev",
        chain=(),
        threshold=0.7,
        in_scope=True,
        pending_review=False,
    )
    assert res == Resolution(
        status="final",
        answer="c",
        probability=1.0,
        decider="human",
        decider_version="human",
        escalated=False,
        decided_at=_T1,
        agreement=None,
        review_status="confirmed",
    )


def test_ut03_71_ensemble_row_wins_and_escalates_without_laya() -> None:
    """UT03-71 an ensemble row takes precedence; escalated true with no laya row."""
    rows = {"ensemble": CachedAnswer("x", 0.8, _T0, "ens1", agreement=0.9)}
    res = resolve_pair(
        human=None,
        rows=rows,
        primary="openjev",
        chain=("jev", "llm"),
        threshold=0.7,
        in_scope=True,
        pending_review=False,
    )
    assert res == Resolution(
        status="final",
        answer="x",
        probability=0.8,
        decider="ensemble",
        decider_version="ens1",
        escalated=True,
        decided_at=_T0,
        agreement=0.9,
        review_status="none",
    )


def test_ut03_71_ensemble_matches_laya_is_not_escalated() -> None:
    """UT03-71 ensemble agrees with the laya row: not escalated."""
    rows = {
        "ensemble": CachedAnswer("x", 0.8, _T0, "ens1", agreement=0.9),
        "laya": CachedAnswer("x", 0.85, _T0, "laya1"),
    }
    res = resolve_pair(
        human=None,
        rows=rows,
        primary="openjev",
        chain=("jev", "llm"),
        threshold=0.7,
        in_scope=True,
        pending_review=False,
    )
    assert res.escalated is False


def test_ut03_71_ensemble_disagrees_with_laya_is_escalated() -> None:
    """UT03-71 ensemble disagrees with the laya row: escalated."""
    rows = {
        "ensemble": CachedAnswer("x", 0.8, _T0, "ens1", agreement=0.9),
        "laya": CachedAnswer("y", 0.85, _T0, "laya1"),
    }
    res = resolve_pair(
        human=None,
        rows=rows,
        primary="openjev",
        chain=("jev", "llm"),
        threshold=0.7,
        in_scope=True,
        pending_review=False,
    )
    assert res.escalated is True


# --- PT03-08: property test ------------------------------------------------------------

_NAMES = ("laya", "openjev", "jev", "llm", "ensemble")
_ANSWERS = ("a", "b", "c")


@st.composite
def _rows_strategy(draw: st.DrawFn) -> dict[str, CachedAnswer]:
    names = draw(st.sets(st.sampled_from(_NAMES), max_size=len(_NAMES)))
    rows: dict[str, CachedAnswer] = {}
    for name in names:
        answer = draw(st.sampled_from(_ANSWERS))
        p_cal = draw(st.floats(min_value=0.0, max_value=1.0, allow_nan=False))
        agreement = (
            draw(st.floats(min_value=0.0, max_value=1.0, allow_nan=False))
            if name == "ensemble"
            else None
        )
        rows[name] = CachedAnswer(
            answer=answer, p_cal=p_cal, decided_at=_T0, version=f"{name}-v1", agreement=agreement
        )
    return rows


_human_strategy = st.none() | st.tuples(st.sampled_from(_ANSWERS), st.just(_T1))


@given(
    primary=st.sampled_from(["laya", "openjev", "jev", "llm"]),
    rows=_rows_strategy(),
    human=_human_strategy,
    threshold=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    in_scope=st.booleans(),
    pending_review=st.booleans(),
)
def test_pt03_08_final_iff_fields_set_and_escalated_iff_not_primary(
    primary: str,
    rows: dict[str, CachedAnswer],
    human: tuple[str, datetime] | None,
    threshold: float,
    in_scope: bool,
    pending_review: bool,
) -> None:
    """PT03-08 final iff fields set; escalated iff decider != primary (ensemble rule aside).

    `human` is drawn too: `final ⇔ fields set` holds unconditionally, including on the
    human branch. Only `escalated ⇔ decider != primary` is genuinely in tension with a
    human correction (U03-74 step 2 hard-codes `escalated=False, decider="human"`
    regardless of `primary`), so that half of the property is skipped when `decider ==
    "human"`.
    """
    chain = tuple(name for name in ("openjev", "jev", "llm") if name != primary)
    res = resolve_pair(
        human=human,
        rows=rows,
        primary=primary,
        chain=chain,
        threshold=threshold,
        in_scope=in_scope,
        pending_review=pending_review,
    )
    fields_set = (
        res.answer is not None
        and res.probability is not None
        and res.decider is not None
        and res.decider_version is not None
        and res.decided_at is not None
    )
    assert (res.status == "final") == fields_set
    if res.status == "final" and res.decider != "human":
        if res.decider == "ensemble":
            laya = rows.get("laya")
            expected_escalated = laya is None or rows["ensemble"].answer != laya.answer
        else:
            expected_escalated = res.decider != primary
        assert res.escalated == expected_escalated
