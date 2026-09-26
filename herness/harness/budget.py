"""Run-wide phase token and cost ledger (U06-25..U06-28, R-02, R-25; TH06-03, TH06-11).

`RunBudget` implements the spec 05 `BudgetLedger` protocol and is the only raiser of
`BudgetExceeded` for run budgets. Every counter sits behind one `threading.Lock`, so the
ledger is safe across threads and asyncio tasks (nothing awaits inside the lock).
"""

from __future__ import annotations

import math
import threading
from collections.abc import Mapping
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Literal

from pydantic import JsonValue

from herness.core.errors import BudgetExceeded, ConfigError, SchemaViolation

__all__ = ["RunBudget", "new_phase_budgets"]

PhaseName = Literal["analysis", "writer", "chat"]
_CENT = Decimal("0.01")
_INT_KEYS = ("tokens_in", "tokens_out", "calls")
_EXTRA_KEYS = ("tokens_cap", "tokens_used", "tokens_remaining", "cost_cap", "cost_remaining")


class RunBudget:
    """Token and cost ledger of one run phase (`analysis`, `writer` or `chat`)."""

    def __init__(
        self,
        name: PhaseName,
        tokens_cap: int,
        cost_cap: Decimal | None = None,
        *,
        cost_cap_raises: bool = True,
        run_id: str,
    ) -> None:
        if tokens_cap < 1 or (cost_cap is not None and cost_cap < 0):
            msg = "budget caps invalid: tokens_cap must be >= 1 and cost_cap >= 0"
            raise ConfigError(msg, run_id=run_id, phase=name)
        self.name: PhaseName = name
        self.run_id = run_id
        self.tokens_cap = tokens_cap
        self.cost_cap = cost_cap
        self.cost_cap_raises = cost_cap_raises
        self._lock = threading.Lock()
        self.tokens_in = self.tokens_out = self.calls = 0
        self.cost_used = Decimal(0)
        self._exhausted = self._cost_cap_reached = False

    @property
    def exhausted(self) -> bool:
        """True once a raising cap was reached; sticky except through `restore`."""
        return self._exhausted

    @property
    def cost_cap_reached(self) -> bool:
        """True once `cost_used >= cost_cap`; sticky except through `restore`."""
        return self._cost_cap_reached

    @property
    def tokens_used(self) -> int:
        """`tokens_in + tokens_out`."""
        return self.tokens_in + self.tokens_out

    def charge(self, tokens_in: int, tokens_out: int, cost_usd: Decimal) -> None:
        """Record one model call; raise `BudgetExceeded` when the phase is exhausted (U06-26)."""
        if tokens_in < 0 or tokens_out < 0 or cost_usd < 0:
            msg = "negative charge"
            raise ConfigError(msg, run_id=self.run_id, phase=self.name)
        with self._lock:
            self.tokens_in += tokens_in
            self.tokens_out += tokens_out
            self.cost_used += cost_usd
            self.calls += 1
            self._update_flags()
            exhausted = self._exhausted
        if exhausted:
            msg = f"run budget exhausted: run_id={self.run_id} phase={self.name}"
            raise BudgetExceeded(msg)

    def _update_flags(self) -> None:
        """Set the sticky flags from the counters; the caller holds the lock."""
        if self.tokens_used >= self.tokens_cap:
            self._exhausted = True
        if self.cost_cap is not None and self.cost_used >= self.cost_cap:
            self._cost_cap_reached = True
            if self.cost_cap_raises:
                self._exhausted = True

    def snapshot(self) -> dict[str, JsonValue]:
        """Serialize totals for `run.token_usage` (U06-27)."""
        with self._lock:
            used, cap = self.tokens_used, self.cost_cap
            left = None if cap is None else format(max(Decimal(0), cap - self.cost_used), "f")
            return {
                "name": self.name,
                "tokens_cap": self.tokens_cap,
                "tokens_in": self.tokens_in,
                "tokens_out": self.tokens_out,
                "tokens_used": used,
                "tokens_remaining": max(0, self.tokens_cap - used),
                "cost_cap": None if cap is None else format(cap, "f"),
                "cost_used": format(self.cost_used, "f"),
                "cost_remaining": left,
                "calls": self.calls,
                "exhausted": self._exhausted,
                "cost_cap_reached": self._cost_cap_reached,
            }

    def restore(self, snap: Mapping[str, object]) -> None:
        """Restore counters from a snapshot of the same phase; caps stay the current knobs.

        Both flags are recomputed against the current caps, so a resume with a raised cap
        continues and one with a lowered cap stops.
        """
        counts: list[int] = []
        for key in _INT_KEYS:
            value = snap.get(key)
            if type(value) is int and value >= 0:
                counts.append(value)
        cost = _parse_cost(snap.get("cost_used"))
        flags = (snap.get("exhausted"), snap.get("cost_cap_reached"))
        absent = any(k not in snap for k in _EXTRA_KEYS) or any(type(f) is not bool for f in flags)
        if snap.get("name") != self.name or len(counts) != len(_INT_KEYS) or cost is None or absent:
            msg = f"budget snapshot invalid: run_id={self.run_id}"
            raise SchemaViolation(msg)
        with self._lock:
            self.tokens_in, self.tokens_out, self.calls = counts
            self.cost_used = cost
            self._exhausted = self._cost_cap_reached = False
            self._update_flags()


def _parse_cost(value: object) -> Decimal | None:
    """A non-negative finite decimal string, else None."""
    try:
        cost = Decimal(value) if isinstance(value, str) else None
    except InvalidOperation:
        return None
    return cost if cost is not None and cost.is_finite() and cost >= 0 else None


def new_phase_budgets(
    *,
    run_id: str,
    run_tokens: int,
    writer_reserve: float,
    cost_cap: Decimal | None,
    cost_cap_raises: bool,
) -> tuple[RunBudget, RunBudget]:
    """Split the run budget into the `analysis` and `writer` ledgers (U06-28, design 06 §6.3)."""
    if not 0 < writer_reserve < 1:
        msg = "writer_reserve must be in (0, 1)"
        raise ConfigError(msg, run_id=run_id)
    share = Decimal(1) - Decimal(str(writer_reserve))
    a_tokens = math.floor(share * run_tokens)
    a_cost: Decimal | None = None
    w_cost: Decimal | None = None
    if cost_cap is not None:
        a_cost = (cost_cap * share).quantize(_CENT, rounding=ROUND_DOWN)
        w_cost = cost_cap - a_cost
    raises = cost_cap_raises
    analysis = RunBudget("analysis", a_tokens, a_cost, cost_cap_raises=raises, run_id=run_id)
    writer = RunBudget(
        "writer", run_tokens - a_tokens, w_cost, cost_cap_raises=raises, run_id=run_id
    )
    return analysis, writer
