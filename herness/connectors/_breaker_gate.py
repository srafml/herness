"""Pre-flight breaker check of the sync paths (impl 01 U01-39 step 1, T01-27).

Private sibling of `runner.py`, `backfill.py` and `reconcile.py`, which import `guard` from here.
The check raises `CircuitOpen` while the source breaker is open and its cooldown is not over,
so a skipped source makes no request. It never claims the half-open probe (U08-24 step 4): the
request-level guard of `retry_page` is the only claimer, so after the cooldown exactly one real
request goes out and its outcome closes or re-opens the breaker.
"""

from __future__ import annotations

from herness.core import time as clock
from herness.core.errors import CircuitOpen
from herness.core.resilience import breaker


def guard(key: str) -> None:
    """Raise `CircuitOpen` while ``key``'s breaker is open before its probe is due; else return."""
    retry_at = breaker(key).retry_at()  # None unless open
    if retry_at is not None and clock.now() < retry_at:
        msg = f"circuit open: {key}"
        raise CircuitOpen(msg, key=key, retry_at=retry_at)
