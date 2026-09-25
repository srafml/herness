"""In-memory fakes of the harness handles (impl 05 §11, created in T05-02).

`FakeOps` implements `OpsHandle`, `FakeLedger` implements `BudgetLedger`, `FakeVectors`
implements `VectorHandle` and `RecordingTracer` implements `TraceEmitter`. Each keeps what
it receives in public attributes so tests can assert on it.
"""

import threading
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from decimal import Decimal

from pydantic import JsonValue

from herness.core.errors import BudgetExceeded
from herness.core.types import Evidence, VectorHit


class FakeOps:
    """`OpsHandle` over dicts: first write of an evidence row or a use row wins."""

    def __init__(self, finding_statuses: Mapping[str, str] | None = None) -> None:
        self.evidence: dict[str, Evidence] = {}
        self.uses: list[tuple[str, str, str | None, datetime]] = []
        self.statuses: dict[str, str] = dict(finding_statuses or {})
        self._lock = threading.Lock()

    def record_evidence(self, ev: Evidence) -> bool:
        with self._lock:
            if ev.query_id in self.evidence:
                return False
            self.evidence[ev.query_id] = ev
            return True

    def record_evidence_use(
        self, query_id: str, run_id: str, task_id: str | None, used_at: datetime
    ) -> bool:
        with self._lock:
            if any(use[:3] == (query_id, run_id, task_id) for use in self.uses):
                return False
            self.uses.append((query_id, run_id, task_id, used_at))
            return True

    def get_evidence(self, query_id: str) -> Evidence | None:
        return self.evidence.get(query_id)

    def finding_statuses(self, finding_ids: Sequence[str]) -> dict[str, str]:
        return {fid: self.statuses[fid] for fid in finding_ids if fid in self.statuses}


class FakeLedger:
    """`BudgetLedger` that sums charges and raises `BudgetExceeded` past an optional cap."""

    def __init__(self, *, max_cost_usd: Decimal | None = None) -> None:
        self.max_cost_usd = max_cost_usd
        self.charges: list[tuple[int, int, Decimal]] = []
        self._lock = threading.Lock()

    def charge(self, tokens_in: int, tokens_out: int, cost_usd: Decimal) -> None:
        with self._lock:
            self.charges.append((tokens_in, tokens_out, cost_usd))
            if self.max_cost_usd is not None and self._cost() > self.max_cost_usd:
                msg = "run budget exceeded"
                raise BudgetExceeded(msg)

    def snapshot(self) -> dict[str, JsonValue]:
        with self._lock:
            return {
                "tokens_in": sum(c[0] for c in self.charges),
                "tokens_out": sum(c[1] for c in self.charges),
                "cost_usd": str(self._cost()),
            }

    def _cost(self) -> Decimal:
        return sum((c[2] for c in self.charges), Decimal(0))


class FakeVectors:
    """`VectorHandle` returning preset hits (first `k`) and recording every search."""

    def __init__(self, hits: Iterable[VectorHit] = ()) -> None:
        self.hits = list(hits)
        self.calls: list[tuple[list[float], int, str | None, str | None]] = []

    def search_tickets(
        self, vector: Sequence[float], k: int, *, entity: str | None, service_id: str | None
    ) -> list[VectorHit]:
        self.calls.append((list(vector), k, entity, service_id))
        return self.hits[:k]


class RecordingTracer:
    """`TraceEmitter` that records `(type, span_id, fields)` and numbers spans `sp_000001`…."""

    def __init__(self, run_id: str = "run_test", task_id: str | None = None) -> None:
        self._run_id = run_id
        self._task_id = task_id
        self.events: list[tuple[str, str, dict[str, object]]] = []
        self._lock = threading.Lock()

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def task_id(self) -> str | None:
        return self._task_id

    def emit(self, type: str, /, **fields: object) -> str:  # noqa: A002 - protocol name
        with self._lock:
            span_id = f"sp_{len(self.events) + 1:06d}"
            self.events.append((type, span_id, dict(fields)))
            return span_id
