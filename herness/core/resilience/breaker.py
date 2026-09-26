"""Circuit breakers and probes (U08-23..U08-27; design 08 §3.1, §5.3; TH08-05).

State lives in `source_health`, cached per process for `BREAKER_CACHE_S`; transitions go
through `health_apply`, the probe is claimed by one conditional `UPDATE`, and events and
metrics are recorded after the write. The module is callable (`classify` precedent).
"""

from __future__ import annotations

import dataclasses
import re
import sys
import threading
import types
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Final, Literal

from herness.core import time as clock
from herness.core.config import get_config
from herness.core.errors import (
    AuthError,
    CircuitOpen,
    ConfigError,
    HernessError,
    ModelUnavailable,
    SourceUnavailable,
)
from herness.core.logging import get_logger
from herness.core.redact import redact_text
from herness.core.resilience._state import process_state, require_ops_backend
from herness.core.resilience.events import record_event
from herness.core.resilience.metrics import record_counter
from herness.core.resilience.ports import HealthRow
from herness.core.resilience.settings import BreakerSettings
from herness.core.types import BreakerState

type BreakerEvent = Literal["failure", "success", "force_open", "probe_claimed"]
type TransitionKind = Literal["breaker_open", "breaker_half_open", "breaker_close"]
type ProbePrefix = Literal["source", "model", "decider"]

BREAKER_CACHE_S: Final = 5
HALF_OPEN_STALE_S: Final = 600
PROBE_TIMEOUT_S: Final = 30
ERROR_MAX_CHARS: Final = 500
_LABEL_MAX_CHARS: Final = 64  # metric label values are at most 64 chars (U08-19)
_KEY_RE: Final = re.compile(
    r"(?:model|decider|monitoring):[A-Za-z0-9_.\-]{1,64}|[a-z][a-z0-9_]{0,63}"
)
_PREFIXES: Final = frozenset({"source", "model", "decider"})
_TO_STATE: Final[dict[TransitionKind, BreakerState]] = {
    "breaker_open": "open",
    "breaker_half_open": "half_open",
    "breaker_close": "closed",
}
_TRANSITIONS_METRIC: Final = "herness_resilience_breaker_transitions_total"
_CACHE_TTL: Final = timedelta(seconds=BREAKER_CACHE_S)
_log: Final = get_logger("resilience")
_STALE: Final = timedelta(seconds=HALF_OPEN_STALE_S)

type _Result = tuple[HealthRow, list[TransitionKind]]


def _closed_row(key: str, now: datetime) -> HealthRow:
    """The row a key without a `source_health` row reads as: closed, 0 failures, 0 trips."""
    return HealthRow(key, "closed", 0, 0, None, None, now)


def _to_open(row: HealthRow, now: datetime, failures: int) -> _Result:
    opened = dataclasses.replace(row, state="open", failures=failures, opened_at=now)
    return dataclasses.replace(opened, trips=row.trips + 1), ["breaker_open"]


def _on_failure(row: HealthRow, now: datetime, settings: BreakerSettings) -> _Result:
    if row.state == "half_open":
        return _to_open(row, now, row.failures)
    if row.state == "closed" and row.failures + 1 >= settings.failure_threshold:
        return _to_open(row, now, row.failures + 1)
    if row.state == "closed":
        return dataclasses.replace(row, failures=row.failures + 1), []
    return row, []  # open: an in-flight call finishing while open changes nothing


def _on_success(row: HealthRow) -> _Result:
    if row.state == "half_open":
        closed = dataclasses.replace(row, state="closed", failures=0, trips=0, opened_at=None)
        return closed, ["breaker_close"]
    if row.state == "closed":
        return dataclasses.replace(row, failures=0), []
    return row, []


def breaker_transition(
    row: HealthRow | None,
    event: BreakerEvent,
    now: datetime,
    settings: BreakerSettings,
    *,
    key: str,
    error: str | None = None,
) -> _Result:
    """Apply one event to a row (design 08 §5.3); returns the new row and the kinds to emit."""
    base = row if row is not None else _closed_row(key, now)
    last_error = error if event in {"failure", "force_open"} else base.last_error
    base = dataclasses.replace(base, source=key, last_error=last_error, updated_at=now)
    if event == "failure":
        return _on_failure(base, now, settings)
    if event == "success":
        return _on_success(base)
    if event == "force_open":
        return (base, []) if base.state == "open" else _to_open(base, now, base.failures)
    if base.state == "open":  # probe_claimed
        return dataclasses.replace(base, state="half_open"), ["breaker_half_open"]
    return base, []


def probe_due(row: HealthRow, settings: BreakerSettings) -> datetime:
    """`opened_at + min(cooldown_s * 2 ** (trips - 1), cooldown_max_s)` seconds."""
    cooldown = min(settings.cooldown_s * 2 ** (max(row.trips, 1) - 1), settings.cooldown_max_s)
    opened = row.opened_at if row.opened_at is not None else row.updated_at
    return opened + timedelta(seconds=cooldown)


def _family(key: str) -> BreakerSettings:
    """`R.breakers.model` / `.decider` by key prefix; everything else is a source."""
    breakers = get_config().resilience.resilience.breakers
    if key.startswith("model:"):
        return breakers.model
    if key.startswith("decider:"):
        return breakers.decider
    return breakers.source


def _error_text(err: HernessError) -> str:
    """Redacted `str(err)`, at most 500 chars; the class name when redaction fails."""
    try:
        text = redact_text(str(err))
    except HernessError:
        text = None
    return (text if text is not None else type(err).__name__)[:ERROR_MAX_CHARS]


class CircuitBreaker:
    """Per-key breaker over `source_health`; obtain it with `breaker(key)` (U08-24)."""

    def __init__(self, key: str) -> None:
        self.key = key
        self._lock = threading.Lock()
        self._cached: tuple[HealthRow, datetime] | None = None  # row and its read time

    def _cache(self, row: HealthRow, now: datetime) -> None:
        with self._lock:
            self._cached = (row, now)

    def _entry(self, now: datetime) -> tuple[HealthRow, datetime]:
        """The cached (row, read time) while younger than 5 s, else a fresh `health_get`."""
        with self._lock:
            cached = self._cached
        if cached is not None and timedelta(0) <= now - cached[1] < _CACHE_TTL:
            return cached
        return self._refresh(now), now

    def _refresh(self, now: datetime) -> HealthRow:
        row = require_ops_backend().health_get(self.key) or _closed_row(self.key, now)
        self._cache(row, now)
        return row

    def state(self) -> BreakerState:
        """The (cached) breaker state."""
        return self._entry(clock.now())[0].state

    def retry_at(self) -> datetime | None:
        """The probe due time while open, else None."""
        row = self._entry(clock.now())[0]
        return probe_due(row, _family(self.key)) if row.state == "open" else None

    def allow(self) -> bool:
        """True when a call may go ahead: closed, or this caller won the probe claim."""
        now = clock.now()
        row, read_at = self._entry(now)
        due = self._claimable(row, now)
        if due is not None and row.state == "open" and read_at < now:
            row = self._refresh(now)  # another process may have probed and re-opened it
            due = self._claimable(row, now)
        if row.state == "closed":
            return True
        return due is not None and self._claim(row, now, due)

    def _claimable(self, row: HealthRow, now: datetime) -> datetime | None:
        """The probe due time when ``row`` lets a caller claim the probe at ``now``."""
        if row.state == "closed" or (row.state == "half_open" and row.updated_at >= now - _STALE):
            return None  # closed, or another caller's probe is in flight
        due = probe_due(row, _family(self.key))
        return None if row.state == "open" and now < due else due

    def _claim(self, row: HealthRow, now: datetime, due: datetime) -> bool:
        """Claim the half-open probe across processes (U08-24 step 4)."""
        if not require_ops_backend().health_claim_probe(self.key, now, due, now - _STALE):
            self._refresh(now)
            return False
        claimed = dataclasses.replace(row, state="half_open", updated_at=now)
        self._cache(claimed, now)
        self._emit(["breaker_half_open"], claimed, "probe")
        return True

    def record_success(self) -> None:
        """Record a successful call; no I/O while the cache is closed with no failures."""
        row = self._entry(clock.now())[0]
        if row.state == "closed" and row.failures == 0:
            return
        self._apply("success", None, None)

    def record_failure(self, err: HernessError) -> None:
        """Count a `SourceUnavailable` or `ModelUnavailable`; other classes are ignored."""
        if not isinstance(err, SourceUnavailable | ModelUnavailable):
            return
        self._apply("failure", _error_text(err), type(err).__name__)

    def force_open(self, err: HernessError) -> None:
        """Open the breaker at once (reason `auth` for an `AuthError`)."""
        reason = "auth" if isinstance(err, AuthError) else type(err).__name__
        self._apply("force_open", _error_text(err), reason)

    def _apply(self, event: BreakerEvent, error: str | None, reason: str | None) -> None:
        """Write one transition through `health_apply`, then cache it and emit it."""
        now = clock.now()
        settings = _family(self.key)
        kinds: list[TransitionKind] = []

        def fn(before: HealthRow | None) -> HealthRow:
            after, emitted = breaker_transition(
                before, event, now, settings, key=self.key, error=error
            )
            kinds[:] = emitted  # a retried write keeps only the last attempt's kinds
            return after

        _, after = require_ops_backend().health_apply(self.key, fn, now)
        if after is None:  # pragma: no cover - fn always returns a row
            return
        self._cache(after, now)
        self._emit(kinds, after, reason)

    def _emit(self, kinds: list[TransitionKind], row: HealthRow, reason: str | None) -> None:
        """One `resilience_event` and one transitions counter increment per kind."""
        due = probe_due(row, _family(self.key)) if row.state == "open" else None
        fields = {"key": self.key, "failures": row.failures, "trips": row.trips, "reason": reason}
        detail = {k: v for k, v in (fields | {"probe_due": due}).items() if v is not None}
        for kind in kinds:
            record_event(kind, component="resilience", target=self.key, detail=detail)
            labels = {"key": self.key[:_LABEL_MAX_CHARS], "to_state": _TO_STATE[kind]}
            record_counter(_TRANSITIONS_METRIC, component="resilience", labels=labels)


def breaker(key: str) -> CircuitBreaker:
    """The process-wide breaker of ``key``; an invalid key raises `ConfigError`."""
    if _KEY_RE.fullmatch(key) is None:
        msg = "invalid breaker key"
        raise ConfigError(msg)
    state = process_state()
    with state.lock:
        instance = state.breakers.get(key)
        if instance is None:
            instance = state.breakers[key] = CircuitBreaker(key)
    return instance


def guard(key: str) -> None:
    """Raise `CircuitOpen` unless ``breaker(key).allow()``."""
    b = breaker(key)
    if b.allow():
        return
    retry_at = b.retry_at() or clock.now() + timedelta(seconds=_family(key).cooldown_s)
    msg = f"circuit open: {key}"
    raise CircuitOpen(msg, key=key, retry_at=retry_at)


def register_probe(prefix: ProbePrefix, fn: Callable[[str], None]) -> None:
    """Register the probe function of one key family (`source`, `model`, `decider`)."""
    if prefix not in _PREFIXES:
        msg = "unknown probe prefix"
        raise ConfigError(msg)
    state = process_state()
    with state.lock:
        state.probes[prefix] = fn


def _split(key: str) -> tuple[ProbePrefix, str]:
    """(`model` | `decider` | `source`, the name passed to the probe function)."""
    if key.startswith("model:"):
        return "model", key.removeprefix("model:")
    if key.startswith("decider:"):
        return "decider", key.removeprefix("decider:")
    return "source", key


def _network_ok(prefix: ProbePrefix, name: str) -> bool:
    """False for a `model:` key whose client is off-network without egress or unknown."""
    if prefix != "model":
        return True
    chains = process_state().chains
    if chains is None:
        return False
    try:
        off_network = chains.config(name).off_network
    except Exception as exc:  # noqa: BLE001 - an unknown client is never probed
        _log.debug("resilience.probe.client_unknown", error_type=type(exc).__name__)
        return False
    return not off_network or get_config().security.egress.enabled


# T08-07: replace with call_with_timeout
def _call_with_timeout[T](fn: Callable[[], T], timeout_s: float) -> T:
    """Run ``fn`` on a daemon thread; still running after ``timeout_s`` → `ModelUnavailable`."""
    box: dict[str, object] = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller's thread
            box["error"] = exc

    thread = threading.Thread(target=run, name="herness-timeout", daemon=True)
    thread.start()
    thread.join(timeout_s)
    if thread.is_alive():
        msg = f"call timed out after {timeout_s}s"
        raise ModelUnavailable(msg)
    error = box.get("error")
    if isinstance(error, BaseException):
        raise error
    return box["value"]  # type: ignore[return-value]


def _run_probe(b: CircuitBreaker, fn: Callable[[str], None], name: str) -> None:
    """Run one claimed probe and record its outcome; a failure always re-opens."""
    try:
        _call_with_timeout(lambda: fn(name), PROBE_TIMEOUT_S)
    except (SourceUnavailable, ModelUnavailable) as err:
        b.record_failure(err)
    except Exception as exc:  # noqa: BLE001 - a probe crash is a probe failure
        b.record_failure(ModelUnavailable(f"probe failed: {type(exc).__name__}"))
    else:
        b.record_success()


def run_due_probes(now: datetime) -> int:
    """Run the registered probe of every open breaker whose probe is due; return the count."""
    state, ops, count = process_state(), require_ops_backend(), 0
    for row in ops.health_list(["open"]):
        if _KEY_RE.fullmatch(row.source) is None:
            _log.debug("resilience.probe.invalid_key")  # the key text stays out of the log
            continue
        prefix, name = _split(row.source)
        with state.lock:
            fn = state.probes.get(prefix)
        fresh = None if fn is None else ops.health_get(row.source)  # earlier probes of this
        if fn is None or fresh is None or fresh.state != "open":  # tick may take 30 s each
            continue
        due = probe_due(fresh, _family(row.source))
        if now < due or not _network_ok(prefix, name):
            continue
        b = breaker(row.source)
        if not b._claim(fresh, now, due):
            continue
        _run_probe(b, fn, name)
        count += 1
    return count


class _CallableModule(types.ModuleType):
    """The package attribute `breaker` is this module (U08-25's function shares the name)."""

    def __call__(self, key: str) -> CircuitBreaker:
        return breaker(key)


sys.modules[__name__].__class__ = _CallableModule
