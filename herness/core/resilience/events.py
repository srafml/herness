"""`record_event`: the single writer of `resilience_event` rows, their log line and trace
event (impl 08 U08-18; design 08 §2.7, §4.2, §4.4).

TH08-02: detail keys are allowlisted per kind, values are scalars or short lists of scalars,
and every string has known secret values masked and passes `redact_text` before it is cut
to 200 chars.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from datetime import datetime
from typing import Final, Literal, NamedTuple

from structlog.stdlib import BoundLogger

from herness.core import time as clock
from herness.core.errors import ConfigError, HernessError
from herness.core.ids import new_ulid
from herness.core.logging import get_logger
from herness.core.redact import redact_text
from herness.core.resilience._state import require_ops_backend
from herness.core.resilience.ports import EventRow, JsonScalar, TracerLike
from herness.core.secrets import scrub_secrets

STRING_MAX_CHARS: Final = 200
LIST_MAX_ITEMS: Final = 20
_REDACT_WINDOW: Final = 4000  # redaction runs on this prefix only: bounds its cost

type _Detail = dict[str, JsonScalar | list[JsonScalar]]


class _Spec(NamedTuple):
    fields: frozenset[str]
    level: int
    log_event: str
    trace_type: str | None = None


_RETRY = frozenset({"target", "attempt", "error_type", "wait_s", "policy", "breaker_key"})
_BREAKER = frozenset({"key", "failures", "trips", "reason", "probe_due"})
_JOB_END = frozenset({"kind", "attempt", "duration_s", "stop_reason", "partial"})
_SERVICE = frozenset({"service", "duration_s", "reason"})
_SCHEDULE = frozenset({"schedule", "fire_at", "step"})
_CHAIN = frozenset({"schedule", "fire_at", "step", "reason"})
_W, _I, _E = logging.WARNING, logging.INFO, logging.ERROR

# DETAIL_FIELDS, log level, §8.1 log event and trace type of each of the 21 kinds (U08-18).
_SPECS: Final[dict[str, _Spec]] = {
    "retry": _Spec(_RETRY | {"retry_after_s"}, _W, "resilience.call.retry_scheduled", "retry"),
    "fallback": _Spec(
        frozenset({"from_profile", "to_profile", "reason"}),
        _W,
        "resilience.chain.fallback_used",
        "fallback",
    ),
    "repair": _Spec(
        frozenset({"model_profile", "repair_no", "error_paths"}),
        _W,
        "resilience.output.repaired",
        "repair",
    ),
    "guard_stop": _Spec(
        frozenset({"cause", "step"}), _W, "resilience.loop.guard_stopped", "guard_stop"
    ),
    "breaker_open": _Spec(_BREAKER, _W, "resilience.breaker.opened"),
    "breaker_half_open": _Spec(_BREAKER, _I, "resilience.breaker.half_opened"),
    "breaker_close": _Spec(_BREAKER, _I, "resilience.breaker.closed"),
    "job_done": _Spec(_JOB_END, _I, "jobs.job.done"),
    "job_yield": _Spec(_JOB_END, _I, "jobs.job.yielded"),
    "job_failed": _Spec(frozenset({"kind", "attempt", "error_type"}), _E, "jobs.job.failed"),
    "lease_expired": _Spec(frozenset({"kind", "attempt", "outcome"}), _W, "jobs.lease.expired"),
    "gpu_swap": _Spec(frozenset({"from", "to", "duration_s", "reason"}), _I, "jobs.gpu.swapped"),
    "gpu_swap_failed": _Spec(
        frozenset({"from", "to", "step", "error_type"}), _E, "jobs.gpu.swap_failed"
    ),
    "service_restart": _Spec(_SERVICE, _W, "jobs.service.restarted"),
    "service_start": _Spec(_SERVICE, _I, "jobs.service.started"),
    "service_stop": _Spec(_SERVICE, _I, "jobs.service.stopped"),
    "schedule_fired": _Spec(_SCHEDULE, _I, "jobs.schedule.fired"),
    "schedule_missed": _Spec(_SCHEDULE, _W, "jobs.schedule.missed"),
    "chain_broken": _Spec(_CHAIN, _W, "jobs.chain.broken"),
    "chain_skipped": _Spec(_CHAIN, _I, "jobs.chain.skipped"),
    "rekey_planned": _Spec(frozenset({"fire_at", "key_id"}), _I, "jobs.rekey.planned"),
}

EVENT_KINDS: Final[frozenset[str]] = frozenset(_SPECS)
DETAIL_FIELDS: Final[dict[str, frozenset[str]]] = {k: s.fields for k, s in _SPECS.items()}

_logs: Final = {"resilience": get_logger("resilience"), "jobs": get_logger("jobs")}
_DROPPED: Final = object()  # a value that may not be stored


def _clean_str(text: str) -> str | object:
    """Mask known secret values (the log scrubber, U10-32), redact the first 4 000 chars,
    then keep the first 200; a failing scrub or redaction drops the value (fail closed).

    For a text longer than the window, the last 200 redacted chars are dropped before the
    cut: a value split at the window edge escapes detection, and when redaction shrinks the
    window (a long token becomes a short placeholder) that tail can move into the kept 200."""
    scrubbed = scrub_secrets(None, "record_event", {"value": text}).get("value")
    if not isinstance(scrubbed, str):
        return _DROPPED
    try:
        clean = redact_text(scrubbed[:_REDACT_WINDOW])
    except HernessError:
        return _DROPPED
    if clean is None:
        return _DROPPED
    if len(scrubbed) > _REDACT_WINDOW:
        clean = clean[: max(len(clean) - STRING_MAX_CHARS, 0)]
    return clean[:STRING_MAX_CHARS]


def _clean_scalar(value: object) -> JsonScalar | object:
    if isinstance(value, datetime):
        return clock.format_utc(value)
    if isinstance(value, str):
        return _clean_str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return _DROPPED
    if value is None or isinstance(value, bool | int | float):
        return value
    return _DROPPED  # dicts, objects and anything else JSON cannot hold as a scalar


def _clean_value(value: object) -> JsonScalar | list[JsonScalar] | object:
    if not isinstance(value, list | tuple):
        return _clean_scalar(value)
    items = [_clean_scalar(item) for item in value[:LIST_MAX_ITEMS]]
    if any(item is _DROPPED for item in items):
        return _DROPPED  # a list holding a nested value is dropped whole
    return items


def _filter_detail(kind: str, detail: Mapping[str, object], log: BoundLogger) -> _Detail:
    allowed = DETAIL_FIELDS[kind]
    out: _Detail = {}
    dropped = 0
    for key, value in detail.items():
        clean = _clean_value(value) if key in allowed else _DROPPED
        if clean is _DROPPED:
            dropped += 1
            continue
        out[key] = clean  # type: ignore[assignment]  # _DROPPED excluded above
    if dropped:  # key names and values stay out of the log (TH08-02)
        log.debug("resilience.event.detail_dropped", kind=kind, dropped=dropped)
    return out


def record_event(  # noqa: PLR0913 - the U08-18 signature
    kind: str,
    *,
    component: Literal["resilience", "jobs"],
    target: str | None = None,
    run_id: str | None = None,
    job_id: str | None = None,
    task_id: str | None = None,
    detail: Mapping[str, object] | None = None,
    tracer: TracerLike | None = None,
) -> None:
    """Write one `resilience_event` row, log it and fan it out to ``tracer`` (U08-18).

    Raises ConfigError for an unknown ``kind`` or ``component`` or an unbound ops backend; a
    store failure is logged as `resilience.event.dropped` and never raised.
    """
    spec = _SPECS.get(kind)
    if spec is None or component not in _logs:
        msg = "unknown resilience event kind or component"
        raise ConfigError(msg)
    ops = require_ops_backend()
    log = _logs[component]
    clean = _filter_detail(kind, detail or {}, log)
    cut_target = None if target is None else target[:STRING_MAX_CHARS]
    ids = {"run_id": run_id, "job_id": job_id, "task_id": task_id}
    row = EventRow(
        "evt_" + new_ulid(), clock.now(), kind, component, cut_target, **ids, detail=clean
    )
    try:
        ops.insert_event(row)
    except HernessError as exc:
        log.warning("resilience.event.dropped", kind=kind, error_type=type(exc).__name__)
    log.log(spec.level, spec.log_event, kind=kind, target=cut_target, **ids, detail=clean)
    if tracer is not None and spec.trace_type is not None:
        tracer.emit(spec.trace_type, **clean)
