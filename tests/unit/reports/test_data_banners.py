"""Unit tests for ``derive_banners`` and ``fill_rationale`` (impl 09 U09-16, U09-15).

Covers UT09-24 (banners) and the rationale rows of UT09-79.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from herness.reports._data import ReportBanner, derive_banners, fill_rationale

pytestmark = pytest.mark.unit

_ORDER = (
    "unconfirmed_weights", "dq_warnings", "findings_only", "partial_run", "hybrid_fallback",
    "off_network_profile", "budget_exhausted",
)  # fmt: skip
_FINDINGS_ONLY = (
    "The report writer did not finish. This report lists verified findings only, without"
    " narrative or recommendations."
)


def _derive(draft_banners: tuple[str, ...] = (), **kw: Any) -> list[ReportBanner]:
    args: dict[str, Any] = {
        "unconfirmed_keys": (), "dq_failed": False, "run_status": "done", "dead_tasks": 0,
        "profile": "local",
    } | kw  # fmt: skip
    return derive_banners(draft_banners, **args)


def _codes(banners: list[ReportBanner]) -> list[str]:
    return [b.code for b in banners]


def test_ut09_24_no_condition_no_banner() -> None:
    """UT09-24 a clean local or synth run with no draft banners gets no banner."""
    assert _derive() == []
    assert _derive(profile="synth") == []


@pytest.mark.parametrize(
    ("kw", "draft", "code", "text"),
    [
        ({"unconfirmed_keys": ["cost.a"]}, (), "unconfirmed_weights",
         "Dollar weights are placeholders and not yet confirmed."),
        ({"dq_failed": True}, (), "dq_warnings",
         "Some data quality checks failed. See Data quality and caveats."),
        ({"draft_mode": "findings_only"}, (), "findings_only", _FINDINGS_ONLY),
        ({"run_status": "partial"}, (), "partial_run",
         "This run is partial: some tasks did not finish."),
        ({"dead_tasks": 2}, (), "partial_run", "This run is partial: some tasks did not finish."),
        ({}, ("hybrid_fallback",), "hybrid_fallback",
         "An off-network step fell back to a local model."),
        ({"profile": "cloud"}, (), "off_network_profile",
         "Parts of this run used the off-network profile `cloud` under the approved data policy."),
        ({}, ("budget_exhausted",), "budget_exhausted",
         "The run budget ran out; some analysis was cut short."),
        ({}, ("dq_warnings",), "dq_warnings",
         "Some data quality checks failed. See Data quality and caveats."),
    ],
)  # fmt: skip
def test_ut09_24_each_condition_alone(
    kw: dict[str, Any], draft: tuple[str, ...], code: str, text: str
) -> None:
    """UT09-24 each condition alone gives exactly its banner with the exact text."""
    banners = _derive(draft, **kw)
    assert _codes(banners) == [code]
    assert banners[0].text == text


def test_ut09_24_unconfirmed_details_are_the_keys() -> None:
    """UT09-24 the unconfirmed_weights banner lists the keys as details; others have none."""
    banners = _derive(unconfirmed_keys=["cost.a", "cost.b"], dq_failed=True)
    assert banners[0] == ReportBanner(
        "unconfirmed_weights",
        "Dollar weights are placeholders and not yet confirmed.",
        ("cost.a", "cost.b"),
    )
    assert banners[1].details == ()


def test_ut09_24_all_combined_in_table_order_each_once() -> None:
    """UT09-24 every condition at once, draft duplicates of derived codes: table order, once."""
    draft = ("budget_exhausted", "hybrid_fallback", "dq_warnings", "partial_run")
    banners = _derive(
        draft, unconfirmed_keys=["w"], dq_failed=True, run_status="partial", dead_tasks=1,
        profile="cloud", draft_mode="findings_only",
    )  # fmt: skip
    assert _codes(banners) == list(_ORDER)
    assert banners[2].text == _FINDINGS_ONLY


def test_ut09_24_synth_profile_is_not_off_network() -> None:
    """UT09-24 profile synth (and local) never gives the off_network_profile banner."""
    for profile in ("synth", "local"):
        codes = _codes(_derive(("hybrid_fallback",), dq_failed=True, profile=profile))
        assert "off_network_profile" not in codes
        assert codes == ["dq_warnings", "hybrid_fallback"]


def test_ut09_24_full_mode_has_no_findings_only_banner() -> None:
    """UT09-24 draft_mode defaults to "full": no findings_only banner."""
    assert "findings_only" not in _codes(_derive(run_status="partial"))


def test_ut09_79_fill_rationale_placeholders() -> None:
    """UT09-79 placeholders filled (numbers with the plain format), unknown names kept."""
    template = "{entity_name} cuts {metric_label} from {current_value} to {target_value}: {nope}"
    params = {
        "entity_name": "Payments", "metric_label": "MTTR", "current_value": 12.5,
        "target_value": Decimal("8"), "unused": 1,
    }  # fmt: skip
    assert fill_rationale(template, params) == "Payments cuts MTTR from 12.5 to 8: {nope}"


def test_ut09_79_fill_rationale_non_matching_braces_untouched() -> None:
    """UT09-79 braces outside the placeholder pattern stay; None, bool and list values."""
    template = "{Upper} {1x} {} {delta_usd} {flag} {missing_val}"
    params = {"Upper": 1, "1x": 2, "delta_usd": None, "flag": True, "missing_val": [1]}
    assert fill_rationale(template, params) == "{Upper} {1x} {} — True [1]"
