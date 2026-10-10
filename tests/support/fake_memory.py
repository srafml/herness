"""`FakeMemoryStore`: a recording stand-in for `herness.harness.memory.MemoryStore` (impl 09 §11).

Implements the four calls the shared user actions make (`propose`, `approve`, `reject`,
`decide`) with the impl 07 signatures. Every call is appended to `calls` as
`(method, args, kwargs)`; `raises[method]` makes that method raise instead.
"""

from __future__ import annotations

import datetime
from typing import Any, Literal

from herness.core.types.memory import MemoryProposal
from herness.harness.memory.types import ProposeResult

__all__ = ["FakeMemoryStore"]

PENDING_MEMORY_ID = "mem_01HZX3K5V7Q8R9S0T1V2W3X4Y9"
PENDING_REVIEW_ID = "rev_01HZX3K5V7Q8R9S0T1V2W3X4Y9"


class FakeMemoryStore:
    """Records `propose`, `approve`, `reject` and `decide`; never activates anything itself."""

    def __init__(self, order: list[tuple[Any, ...]] | None = None) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.raises: dict[str, BaseException] = {}
        self._order = order  # optional shared event list, to check call order against audits

    def _record(self, method: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((method, args, kwargs))
        if self._order is not None:
            self._order.append(("memory", method, *args))
        if method in self.raises:
            raise self.raises[method]

    def names(self) -> list[str]:
        """The method names called, in order."""
        return [name for name, _, _ in self.calls]

    def propose(self, item: MemoryProposal, run_ctx: object | None = None) -> ProposeResult:
        self._record("propose", item, run_ctx=run_ctx)
        return ProposeResult(
            memory_id=PENDING_MEMORY_ID,
            status="pending_approval",
            review_item_id=PENDING_REVIEW_ID,
            merged_into=None,
            flags=[],
        )

    def approve(
        self,
        memory_id: str,
        user_ref: str,
        note: str | None = None,
        confidence: float | None = None,
    ) -> None:
        self._record("approve", memory_id, user_ref, note)
        del confidence

    def reject(self, memory_id: str, user_ref: str, note: str) -> None:
        self._record("reject", memory_id, user_ref, note)

    def decide(
        self,
        rec_id: str,
        decision: Literal["accepted", "rejected", "deferred"],
        reason: str,
        user_ref: str,
        effective_at: datetime.datetime | None = None,
    ) -> None:
        self._record("decide", rec_id, decision, reason, user_ref, effective_at)
