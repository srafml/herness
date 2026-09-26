"""Cost accounting of one model call (impl 05 U05-22, design §5.1.4)."""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from herness.core.types import Usage
    from herness.harness.llm.settings import PricePerMTok

_PER_MTOK: Final = Decimal(1_000_000)
_BATCH_FACTOR: Final = Decimal("0.5")
_QUANTUM: Final = Decimal("0.000001")
_PRECISION: Final = 60


def cost_usd(usage: Usage, prices: PricePerMTok, /, *, batch: bool = False) -> Decimal:
    """Return the US-dollar cost of ``usage`` at ``prices``, 6 places, ``ROUND_HALF_EVEN``.

    Thinking tokens are part of ``output_tokens`` and are not added again; the Batch API
    halves the cost. The result is non-negative and linear in each usage field.
    """
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        total = (
            usage.input_tokens * prices.input
            + usage.output_tokens * prices.output
            + usage.cache_read_tokens * prices.cache_read
            + usage.cache_write_tokens * prices.cache_write
        ) / _PER_MTOK
        if batch:
            total *= _BATCH_FACTOR
        return total.quantize(_QUANTUM, rounding=ROUND_HALF_EVEN)
