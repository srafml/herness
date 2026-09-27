"""Shared part of the two `JobContext` implementations (private sibling of `context`, T08-20).

Row shortcuts, the first-wins stop reason, `load_state`, the `gpu_scope` restore rule, note
cleaning, the 4 MiB checkpoint cap and the U08-85 default GPU wait. Kept apart from
`herness.core.jobs.context` for the impl 08 §2 line budget of that module.
"""

from __future__ import annotations

import copy
import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import TYPE_CHECKING, Final

from herness.core.config import get_config
from herness.core.errors import SchemaViolation
from herness.core.ids import canonical_json
from herness.core.logging import get_logger
from herness.core.redact import redact_text

if TYPE_CHECKING:
    from pydantic import JsonValue

    from herness.core.jobs.ports import JobRow, StopReason
    from herness.core.resilience.settings import GpuSettings
    from herness.core.types import GpuClass, JobKind, ServiceName

__all__ = [
    "NO_GPU_SLOT",
    "STATE_MAX_BYTES",
    "ContextBase",
    "clean_note",
    "default_wait_s",
    "start_s",
    "state_json",
]

STATE_MAX_BYTES: Final = 4_194_304  # job checkpoint cap, 4 MiB (TH08-10)
NOTE_CHARS: Final = 200
WAIT_MARGIN_S: Final = 60.0  # the two "+ 60" terms of the U08-85 default wait
NO_GPU_SLOT: Final = "GPU control requires the GPU slot"

_log = get_logger("jobs")


def _gpu() -> GpuSettings:
    return get_config().resilience.resilience.gpu


def default_wait_s(start: float) -> float:
    """U08-85 default wait: stop + 60 + start + warm-up + 60 seconds."""
    g = _gpu()
    return g.stop_timeout_s + WAIT_MARGIN_S + start + g.warmup_timeout_s + WAIT_MARGIN_S


def start_s(*, cls: GpuClass | None = None, service: ServiceName | None = None) -> float:
    """Largest `start_timeout_s` of the services of `cls`, or of service `service` (else 0)."""
    starts = (
        s.start_timeout_s
        for c, spec in _gpu().classes.items()
        for n, s in spec.services.items()
        if c == cls or n == service
    )
    return max(starts, default=0.0)


def clean_note(note: str | None) -> str | None:
    """Redact, then cut to 200 chars; redaction failure drops the note (fail closed)."""
    if note is None:
        return None
    redacted = redact_text(note)
    return None if redacted is None else redacted[:NOTE_CHARS]


def state_json(state: dict[str, JsonValue]) -> bytes:
    """Canonical JSON bytes of a checkpoint; over 4 MiB → `SchemaViolation`."""
    data = canonical_json(state).encode("utf-8")
    if len(data) > STATE_MAX_BYTES:
        msg = "job state exceeds 4 MiB"
        raise SchemaViolation(msg)
    return data


@contextmanager
def _gpu_scope(
    require: Callable[[GpuClass], GpuClass | None], cls: GpuClass, job_id: str
) -> Iterator[None]:
    """Switch to `cls`, then restore the previous class on normal or exceptional exit."""
    previous = require(cls)
    restore = previous if previous is not None and previous != cls else None
    try:
        yield
    except BaseException:
        if restore is not None:
            try:
                require(restore)
            except Exception as exc:  # noqa: BLE001 - the original exception must propagate
                _log.error(
                    "jobs.gpu.restore_failed",
                    job_id=job_id,
                    error_type=type(exc).__name__,
                    **{"class": restore},
                )
        raise
    if restore is not None:
        require(restore)


class ContextBase:
    """Row shortcuts, first-wins stop reason and `load_state`, shared by both contexts."""

    def __init__(self, row: JobRow) -> None:
        self._row = row
        self._lock = threading.Lock()
        self._stop_reason: StopReason | None = None
        self._stop_event = threading.Event()

    @property
    def job(self) -> JobRow:
        return self._row

    @property
    def job_id(self) -> str:
        return self._row.job_id

    @property
    def kind(self) -> JobKind:
        return self._row.kind

    @property
    def attempt(self) -> int:
        return self._row.attempts

    @property
    def stop_reason(self) -> StopReason | None:
        return self._stop_reason

    def should_yield(self) -> bool:
        """True once a stop was requested (safe from any thread)."""
        return self._stop_reason is not None

    def _set_stop(self, reason: StopReason) -> None:
        with self._lock:
            if self._stop_reason is None:
                self._stop_reason = reason
        self._stop_event.set()

    def load_state(self) -> dict[str, JsonValue]:
        """A copy of `row.result["state"]`; empty on the first attempt."""
        state = (self._row.result or {}).get("state", {})
        return copy.deepcopy(state) if isinstance(state, dict) else {}

    def _require(self, cls: GpuClass, timeout_s: float | None) -> GpuClass | None:
        raise NotImplementedError  # pragma: no cover - both subclasses override it

    def require_gpu_class(self, cls: GpuClass, *, timeout_s: float | None = None) -> None:
        """Blocking swap to `cls`."""
        self._require(cls, timeout_s)

    def gpu_scope(self, cls: GpuClass) -> AbstractContextManager[None]:
        """Context manager: switch to `cls`, restore the previous class on exit."""
        return _gpu_scope(lambda c: self._require(c, None), cls, self.job_id)
