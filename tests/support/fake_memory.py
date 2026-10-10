"""`FakeMemoryStore`: a recording stand-in for `herness.harness.memory.MemoryStore` (impl 09 §11).

Implements the four calls the shared user actions make (`propose`, `approve`, `reject`,
`decide`) with the impl 07 signatures. Every call is appended to `calls` as
`(method, args, kwargs)`; `raises[method]` makes that method raise instead.

It keeps item state like impl 07: `propose` stores a `pending_approval` item (nothing becomes
active without a reviewer), `approve` / `reject` move a pending item to `active` / `rejected`,
and deciding an item that is no longer pending raises impl 07's `PolicyViolation` with rule
`approve.not_pending` / `reject.not_pending`. Ids never proposed count as pending.
"""

from __future__ import annotations

import datetime
from typing import Any, Literal

from herness.core.errors import PolicyViolation
from herness.core.types.memory import MemoryProposal
from herness.harness.memory.types import ProposeResult

__all__ = ["FakeMemoryStore"]

PENDING_MEMORY_ID = "mem_01HZX3K5V7Q8R9S0T1V2W3X4Y9"
PENDING_REVIEW_ID = "rev_01HZX3K5V7Q8R9S0T1V2W3X4Y9"
_PENDING = "pending_approval"


class FakeMemoryStore:
    """Records `propose`, `approve`, `reject` and `decide`; holds each item's status."""

    def __init__(self, order: list[tuple[Any, ...]] | None = None) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.raises: dict[str, BaseException] = {}
        self.status: dict[str, str] = {}
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

    def _move(self, memory_id: str, to: str, rule: str) -> None:
        if self.status.get(memory_id, _PENDING) != _PENDING:
            raise PolicyViolation(rule, details={"rule": rule})
        self.status[memory_id] = to

    def propose(self, item: MemoryProposal, run_ctx: object | None = None) -> ProposeResult:
        self._record("propose", item, run_ctx=run_ctx)
        self.status[PENDING_MEMORY_ID] = _PENDING
        return ProposeResult(
            memory_id=PENDING_MEMORY_ID,
            status=_PENDING,
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
        self._move(memory_id, "active", "approve.not_pending")

    def reject(self, memory_id: str, user_ref: str, note: str) -> None:
        self._record("reject", memory_id, user_ref, note)
        self._move(memory_id, "rejected", "reject.not_pending")

    def decide(
        self,
        rec_id: str,
        decision: Literal["accepted", "rejected", "deferred"],
        reason: str,
        user_ref: str,
        effective_at: datetime.datetime | None = None,
    ) -> None:
        self._record("decide", rec_id, decision, reason, user_ref, effective_at)
