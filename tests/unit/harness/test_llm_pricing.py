"""Tests for herness.harness.llm.pricing.cost_usd (U05-22)."""

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from herness.core.types.harness import Usage
from herness.harness.llm.pricing import cost_usd
from herness.harness.llm.settings import PricePerMTok

pytestmark = pytest.mark.unit

OPUS = PricePerMTok(
    input=Decimal("4.00"),
    output=Decimal("20.00"),
    cache_read=Decimal("0.20"),
    cache_write=Decimal("5.00"),
)
SONNET = PricePerMTok(
    input=Decimal("2.00"),
    output=Decimal("10.00"),
    cache_read=Decimal("0.20"),
    cache_write=Decimal("2.50"),
)
HAIKU = PricePerMTok(
    input=Decimal("1.00"),
    output=Decimal("5.00"),
    cache_read=Decimal("0.10"),
    cache_write=Decimal("1.25"),
)
USAGE = Usage(
    input_tokens=12_345,
    output_tokens=6_789,
    cache_read_tokens=100_000,
    cache_write_tokens=20_000,
    reasoning_tokens=5_000,
)
FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
_UNIT = Decimal("0.000001")


@pytest.mark.parametrize(
    ("prices", "full", "batch"),
    [
        (OPUS, "0.305160", "0.152580"),
        (SONNET, "0.162580", "0.081290"),
        (HAIKU, "0.081290", "0.040645"),
    ],
    ids=["opus", "sonnet", "haiku"],
)
def test_ut05_21_exact_cost_per_anthropic_row(prices: PricePerMTok, full: str, batch: str) -> None:
    """UT05-21 exact Decimal cost with cache tokens, with and without the batch discount."""
    assert cost_usd(USAGE, prices) == Decimal(full)
    assert cost_usd(USAGE, prices, batch=True) == Decimal(batch)
    assert cost_usd(USAGE, prices).as_tuple().exponent == -6


def test_ut05_21_reasoning_tokens_are_not_added_again() -> None:
    """UT05-21 thinking tokens sit inside output_tokens and are not charged twice."""
    plain = USAGE.model_copy(update={"reasoning_tokens": 0})
    assert cost_usd(USAGE, OPUS) == cost_usd(plain, OPUS)


def test_ut05_21_rounds_half_even_to_six_places() -> None:
    """UT05-21 the result is quantized to 6 places with ROUND_HALF_EVEN."""
    half = PricePerMTok(
        input=Decimal("0.5"), output=Decimal(0), cache_read=Decimal(0), cache_write=Decimal(0)
    )
    assert cost_usd(Usage(input_tokens=3), half) == Decimal("0.000002")
    assert cost_usd(Usage(input_tokens=5), half) == Decimal("0.000002")
    assert cost_usd(Usage(input_tokens=1), half) == Decimal("0.000000")
    assert cost_usd(Usage(), OPUS) == Decimal("0.000000")


_counts = st.integers(min_value=0, max_value=10_000_000)
_usages = st.builds(
    Usage,
    input_tokens=_counts,
    output_tokens=_counts,
    cache_read_tokens=_counts,
    cache_write_tokens=_counts,
    reasoning_tokens=_counts,
)
_int_price = st.integers(min_value=0, max_value=100).map(Decimal)
_int_prices = st.builds(
    PricePerMTok, input=_int_price, output=_int_price, cache_read=_int_price, cache_write=_int_price
)
_cent_price = st.decimals(min_value=0, max_value=100, places=2)
_prices = st.builds(
    PricePerMTok,
    input=_cent_price,
    output=_cent_price,
    cache_read=_cent_price,
    cache_write=_cent_price,
)


@given(usage=_usages, prices=_prices)
def test_pt05_03_non_negative_and_batch_halves(usage: Usage, prices: PricePerMTok) -> None:
    """PT05-03 cost is non-negative and the batch flag halves it (within one quantum)."""
    full = cost_usd(usage, prices)
    half = cost_usd(usage, prices, batch=True)
    assert full >= 0
    assert half >= 0
    assert abs(half * 2 - full) <= 2 * _UNIT


@given(usage=_usages, prices=_int_prices)
def test_pt05_03_batch_halves_exactly_with_whole_prices(usage: Usage, prices: PricePerMTok) -> None:
    """PT05-03 with whole-dollar prices the batch cost is the half of the full cost, rounded."""
    full = cost_usd(usage, prices)
    assert cost_usd(usage, prices, batch=True) == (full / 2).quantize(_UNIT)


@given(
    a=_usages,
    b=_usages,
    prices=_int_prices,
    field=st.sampled_from(FIELDS),
)
def test_pt05_03_linear_per_field(a: Usage, b: Usage, prices: PricePerMTok, field: str) -> None:
    """PT05-03 cost is linear in each usage field."""
    zero = Usage()
    only_a = zero.model_copy(update={field: getattr(a, field)})
    only_b = zero.model_copy(update={field: getattr(b, field)})
    both = zero.model_copy(update={field: getattr(a, field) + getattr(b, field)})
    assert cost_usd(both, prices) == cost_usd(only_a, prices) + cost_usd(only_b, prices)
    doubled = zero.model_copy(update={field: 2 * getattr(a, field)})
    assert cost_usd(doubled, prices) == 2 * cost_usd(only_a, prices)
