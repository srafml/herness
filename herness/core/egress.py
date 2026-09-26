"""Egress guard: the one decision point for off-network calls (impl 10 U10-50/51/56/107).

``EgressGuard.check`` runs design 10 §5.4 steps 1-7 and writes one egress line per decision
before anything could be sent; a refusal also writes an audit ``egress`` line and raises
``EgressBlocked``. Guarded clients and transports (U10-52..55) are added by T10-17.
"""

from __future__ import annotations

import hashlib
import math
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, Literal, NoReturn, get_args

import httpx

from herness.core import audit as _audit
from herness.core import config as _config
from herness.core import time as clock
from herness.core._egress_scan import host_reason, rescan
from herness.core.config import HernessConfig
from herness.core.egress_log import EgressLog
from herness.core.errors import EgressBlocked, FatalError, StoreBusy
from herness.core.ids import new_ulid
from herness.core.logging import get_logger
from herness.core.redact import Redactor, get_redactor
from herness.core.redact_patterns import EntityType

__all__ = ["BLOCKING_TYPES", "CHARS_PER_TOKEN", "LOOPBACK_HOSTS", "MAX_RESPONSE_BYTES"]
__all__ += ["MODEL_DOWNLOAD_HOSTS", "PAYLOAD_CLASSES_BY_PROFILE", "EgressGuard", "EgressTicket"]
__all__ += ["PayloadClass", "Purpose", "cloud_chat_allowed", "get_guard", "reset_guard"]

Purpose = Literal["reasoning_final", "reasoning", "bulk_classification", "model_download"]
PayloadClass = Literal["aggregated_evidence", "redacted_text", "none"]
PAYLOAD_CLASSES_BY_PROFILE: Final[dict[str, frozenset[PayloadClass]]] = {
    "local": frozenset(),
    "synth": frozenset(),
    "hybrid": frozenset({"aggregated_evidence"}),
    "premium": frozenset({"aggregated_evidence", "redacted_text"}),
}
MODEL_DOWNLOAD_HOSTS: Final = frozenset({"huggingface.co", "cdn-lfs.huggingface.co"})  # V-17
MAX_RESPONSE_BYTES: Final = 52_428_800
CHARS_PER_TOKEN: Final = 3.5
BLOCKING_TYPES: Final[frozenset[EntityType]] = frozenset({
    "EMAIL", "PHONE", "CARD", "NATIONAL_ID", "CREDENTIAL", "URL_TOKEN", "EMPLOYEE_ID",
    "USER_ID", "PERSON",
})  # fmt: skip  # ``IP`` is added when ``security.redaction.mask_ip`` (§5.4 step 6)
LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "::1", "localhost"})
_PURPOSES: Final = frozenset(get_args(Purpose))
_CLASSES: Final = frozenset(get_args(PayloadClass))
_log = get_logger("core.egress")


@dataclass(frozen=True)
class EgressTicket:
    """An allowed decision: its ``egress_id``, the logged ``tokens_in`` and the start time."""

    egress_id: str
    tokens_in: int
    started_monotonic: float


def cloud_chat_allowed(cfg: HernessConfig) -> bool:
    """True when purpose ``reasoning`` with ``aggregated_evidence`` passes steps 1-2 (U10-107)."""
    egress = cfg.security.egress
    if not egress.enabled or "reasoning" not in egress.purposes:
        return False
    if cfg.profile == "premium":
        return True
    return cfg.profile == "hybrid" and cfg.security.data_policy.chat_approved


class EgressGuard:
    """Decide off-network requests for one config; thread-safe (U10-51, U10-56)."""

    def __init__(
        self, cfg: HernessConfig, redactor_factory: Callable[[], Redactor], log: EgressLog
    ) -> None:
        self._cfg = cfg
        self._redactor_factory = redactor_factory
        self._redactor: Redactor | None = None
        self._redactor_lock = threading.Lock()
        self._log = log
        ip: frozenset[str] = frozenset({"IP"} if cfg.security.redaction.mask_ip else ())
        self._blocking: frozenset[str] = BLOCKING_TYPES | ip

    def _window_open(self) -> bool:
        """Is a U10-55 download window open on this thread?"""
        return False  # T10-17: wire U10-55 download_window (thread-local flag)

    def _policy(self, purpose: str, payload_class: str) -> str | None:
        """Steps 1-2: profile gate, purpose, payload class and the R-38 chat rule."""
        egress, profile = self._cfg.security.egress, self._cfg.profile
        if not egress.enabled:
            return "profile_forbids_egress"
        if purpose not in egress.purposes:
            return "purpose_not_allowed"
        allowed = PAYLOAD_CLASSES_BY_PROFILE.get(profile, frozenset())
        if payload_class not in allowed or payload_class == "none":
            return "payload_class_not_allowed"
        chat = profile == "hybrid" and purpose == "reasoning"
        if chat and not self._cfg.security.data_policy.chat_approved:
            return "chat_not_approved"
        return None

    def _steps(
        self, url: str, body: bytes, token_estimate: int | None, line: dict[str, Any]
    ) -> str | None:
        """Steps 1-5 without the day cap; fills ``line``, returns the first failing reason."""
        windowed = line["purpose"] == "model_download" and self._window_open()
        if not windowed and (reason := self._policy(line["purpose"], line["payload_class"])):
            return reason
        try:
            parsed = httpx.URL(url)
        except (httpx.InvalidURL, TypeError):
            return "url_invalid"
        line["destination"], line["path"] = parsed.host.lower() or None, parsed.path
        egress = self._cfg.security.egress
        hosts = frozenset(egress.destinations)
        if windowed:  # T10-24: add the registry hosts of the pinned deploy.*.image values
            hosts |= MODEL_DOWNLOAD_HOSTS
        if reason := host_reason(parsed, hosts):
            return reason
        if len(body) > egress.max_request_bytes:
            return "body_too_large"
        line["tokens_in"] = token_estimate or math.ceil(len(body) / CHARS_PER_TOKEN)
        return "tokens_per_request" if line["tokens_in"] > egress.max_tokens_per_request else None

    def _redactor_once(self) -> Redactor:
        with self._redactor_lock:  # fetched at the first re-scan only (U10-56)
            if self._redactor is None:
                self._redactor = self._redactor_factory()
            return self._redactor

    def _refuse(self, line: dict[str, Any], reason: str) -> NoReturn:
        """Audit the refusal and raise ``EgressBlocked`` (step 7, blocked path)."""
        egress_id, hits = line["egress_id"], line.get("scan_hits", {})
        _log.warning(
            "egress.call.blocked",
            egress_id=egress_id,
            reason=reason,
            purpose=line["purpose"],
            destination=line["destination"],
            scan_hits=hits,
        )
        # T08-05: herness_egress_calls_total{decision="blocked", purpose, reason} += 1
        try:
            _audit.audit("egress", "system", egress_id=egress_id, reason=reason)
        except FatalError:
            reason = "egress_log_failed"  # fail closed: the refusal stands, nothing is sent
        msg = f"egress blocked ({egress_id}): {reason}"
        raise EgressBlocked(msg, egress_id=egress_id, reason=reason)

    def _check_and_log(  # noqa: PLR0913 - spec signature (U10-51)
        self,
        url: str,
        body: bytes,
        purpose: Purpose,
        payload_class: PayloadClass,
        token_estimate: int | None,
        *,
        method: str,
        run_id: str | None,
        task_id: str | None,
    ) -> EgressTicket:
        """Steps 1-7: one egress line, then a ticket or ``EgressBlocked``; nothing is sent."""
        started = clock.monotonic()
        line: dict[str, Any] = {
            "egress_id": "egr_" + new_ulid(),
            "purpose": purpose if purpose in _PURPOSES else None,  # never echo a free string
            "payload_class": payload_class if payload_class in _CLASSES else None,
            "destination": None,
            "method": method,
            "path": None,
            "run_id": run_id,
            "task_id": task_id,
            "bytes_out": len(body),
            "tokens_in": None,
        }
        reason, scan = self._steps(url, body, token_estimate, line), None
        hits: dict[str, int] = {}
        if reason is None:  # step 6 outside the lock: the scan is the slow part
            scan, hits = rescan(body, self._redactor_once, self._blocking)
        tokens: int = line["tokens_in"] or 0
        try:
            with self._log.locked():  # the day cap and the append are one critical section
                cap = self._cfg.security.egress.max_tokens_per_day
                if reason is None and self._log.tokens_today(clock.now()) + tokens > cap:
                    reason, hits = "tokens_per_day", {}
                reason = reason or scan
                line |= {"decision": "blocked" if reason else "allowed", "reason": reason}
                line |= {"scan_hits": hits, "payload_sha256": None}
                if reason is None:  # a refused body gets no hash: a short one would be guessable
                    line["payload_sha256"] = hashlib.sha256(body).hexdigest()
                self._log.write(line)
        except (StoreBusy, OSError):
            self._refuse(line, "egress_log_failed")
        if reason is not None:
            self._refuse(line, reason)
        _log.info(
            "egress.call.allowed",
            egress_id=line["egress_id"],
            purpose=purpose,
            payload_class=payload_class,
            destination=line["destination"],
            bytes_out=len(body),
            tokens_in=tokens,
            run_id=run_id,
            task_id=task_id,
        )
        # T08-05: herness_egress_calls_total{decision="allowed", purpose, reason=""} += 1
        # T08-05: herness_egress_tokens_total{direction="in", destination} += tokens
        # T08-05: herness_egress_bytes_total{direction="out"} += len(body)
        return EgressTicket(line["egress_id"], tokens, started)

    def check(
        self,
        url: str,
        body: bytes,
        purpose: Purpose,
        payload_class: PayloadClass,
        token_estimate: int | None = None,
    ) -> None:
        """Decide one request and log the decision; ``EgressBlocked`` on refusal (U10-51)."""
        kw: dict[str, Any] = {"method": "POST", "run_id": None, "task_id": None}
        self._check_and_log(url, body, purpose, payload_class, token_estimate, **kw)


# --- U10-56 process-wide guard ---------------------------------------------------------------


class _State:
    guard: EgressGuard | None = None


_GUARD_LOCK: Final = threading.Lock()


def get_guard() -> EgressGuard:
    """The process-wide guard, built once from the current config (U10-56)."""
    with _GUARD_LOCK:
        if _State.guard is None:
            cfg = _config.get_config()
            log = EgressLog(cfg.paths.logs, _config.config_hash(cfg), cfg.profile)
            _State.guard = EgressGuard(cfg, get_redactor, log)
        return _State.guard


def reset_guard() -> None:
    """Drop the cached guard (config reset hook, U10-10)."""
    with _GUARD_LOCK:
        _State.guard = None


_config._RESET_HOOKS.append(reset_guard)
