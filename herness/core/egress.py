"""Egress guard: the one decision point for off-network calls (impl 10 U10-50/51/56/107).

``EgressGuard.check`` runs design 10 §5.4 steps 1-7 and writes one egress line per decision
before anything could be sent; a refusal also writes an audit ``egress`` line and raises
``EgressBlocked``. Guarded clients (U10-52/53) send through ``GuardedTransport`` (U10-54),
which runs the check before the pool opens a socket and logs the completion; a download
window (U10-55) admits ``model_download``. Loopback clients live in ``egress_clients`` (U10-59).
"""

from __future__ import annotations

import hashlib
import math
import os
import ssl
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Final, Literal, NoReturn, get_args

import httpx

from herness.core import audit as _audit
from herness.core import config as _config
from herness.core import time as clock
from herness.core._egress_scan import host_reason, rescan
from herness.core._egress_transport import (
    MAX_RESPONSE_BYTES,
    AsyncGuardedTransport,
    GuardedTransport,
    TransportOpts,
)
from herness.core.config import HernessConfig
from herness.core.egress_clients import (
    LOOPBACK_HOSTS,
    aloopback_http_client,
    check_timeout,
    loopback_http_client,
    tls_context,
)
from herness.core.egress_log import LINE_KEYS, EgressLog
from herness.core.egress_socket import install_socket_guard
from herness.core.errors import ConfigError, EgressBlocked, FatalError, StoreBusy
from herness.core.ids import new_ulid
from herness.core.logging import get_logger
from herness.core.redact import Redactor, get_redactor
from herness.core.redact_patterns import EntityType

__all__ = ["BLOCKING_TYPES", "CHARS_PER_TOKEN", "LOOPBACK_HOSTS", "MAX_RESPONSE_BYTES"]
__all__ += ["MODEL_DOWNLOAD_HOSTS", "PAYLOAD_CLASSES_BY_PROFILE", "EgressGuard", "EgressTicket"]
__all__ += ["AsyncGuardedTransport", "GuardedTransport", "PayloadClass", "Purpose"]
__all__ += ["aloopback_http_client", "cloud_chat_allowed", "get_guard", "install_socket_guard"]
__all__ += ["loopback_http_client", "reset_guard"]

Purpose = Literal["reasoning_final", "reasoning", "bulk_classification", "model_download"]
PayloadClass = Literal["aggregated_evidence", "redacted_text", "none"]
PAYLOAD_CLASSES_BY_PROFILE: Final[dict[str, frozenset[PayloadClass]]] = {
    "local": frozenset(),
    "synth": frozenset(),
    "hybrid": frozenset({"aggregated_evidence"}),
    "premium": frozenset({"aggregated_evidence", "redacted_text"}),
}
MODEL_DOWNLOAD_HOSTS: Final = frozenset({"huggingface.co", "cdn-lfs.huggingface.co"})  # V-17
CHARS_PER_TOKEN: Final = 3.5
BLOCKING_TYPES: Final[frozenset[EntityType]] = frozenset({
    "EMAIL", "PHONE", "CARD", "NATIONAL_ID", "CREDENTIAL", "URL_TOKEN", "EMPLOYEE_ID",
    "USER_ID", "PERSON",
})  # fmt: skip  # ``IP`` is added when ``security.redaction.mask_ip`` (§5.4 step 6)
_PURPOSES: Final = frozenset(get_args(Purpose))
_CLASSES: Final = frozenset(get_args(PayloadClass))
_log = get_logger("core.egress")
type _Ids = tuple[str | None, str | None]
_WINDOW: Final[ContextVar[bool]] = ContextVar("egress_download_window", default=False)


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
        """Is a U10-55 download window open on this thread (or task)?"""
        return _WINDOW.get()

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
        self, url: str, body: bytes | None, token_estimate: int | None, line: dict[str, Any]
    ) -> str | None:
        """Steps 1-5 without the day cap; fills ``line``, returns the first failing reason."""
        windowed = line["purpose"] == "model_download" and self._window_open()
        if not windowed and (reason := self._policy(line["purpose"], line["payload_class"])):
            return reason
        try:  # an IDNA or surrogate error is a ValueError (UnicodeError) too
            parsed = httpx.URL(url)
            line["destination"], line["path"] = parsed.host.lower() or None, parsed.path
        except (httpx.InvalidURL, ValueError, TypeError):
            return "url_invalid"
        egress = self._cfg.security.egress
        hosts = frozenset(egress.destinations)
        if windowed:  # T10-24: add the registry hosts of the pinned deploy.*.image values
            hosts |= MODEL_DOWNLOAD_HOSTS
        if reason := host_reason(parsed, hosts):
            return reason
        if body is None:  # a streaming body cannot be scanned (U10-54 step 1)
            return "streaming_body"
        if len(body) > egress.max_request_bytes:
            return "body_too_large"
        est = token_estimate  # an int of at least 1 only: a bool, float or 0 never lowers the total
        usable = isinstance(est, int) and not isinstance(est, bool) and est >= 1
        line["tokens_in"] = token_estimate if usable else math.ceil(len(body) / CHARS_PER_TOKEN)
        return "tokens_per_request" if line["tokens_in"] > egress.max_tokens_per_request else None

    def _redactor_once(self) -> Redactor:
        with self._redactor_lock:  # fetched at the first re-scan only (U10-56)
            if self._redactor is None:
                self._redactor = self._redactor_factory()
            return self._redactor

    def _refuse(self, line: dict[str, Any], reason: str) -> NoReturn:
        """Audit the refusal and raise ``EgressBlocked`` (step 7, blocked path)."""
        egress_id, hits = line["egress_id"], line["scan_hits"] or {}
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
        body: bytes | None,
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
        line: dict[str, Any] = dict.fromkeys(LINE_KEYS) | {  # one shape: unknowns stay null
            "egress_id": "egr_" + new_ulid(),
            "purpose": purpose if purpose in _PURPOSES else None,  # never echo a free string
            "payload_class": payload_class if payload_class in _CLASSES else None,
            "method": method,
            "run_id": run_id,
            "task_id": task_id,
            "bytes_out": len(body or b""),
        }
        reason, scan = self._steps(url, body, token_estimate, line), None
        hits: dict[str, int] = {}
        if reason is None and body is not None:  # step 6 outside the lock: the slow part
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
                if reason is None and body is not None:  # a refused body gets no hash (guessable)
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
            bytes_out=line["bytes_out"],
            tokens_in=tokens,
            run_id=run_id,
            task_id=task_id,
        )
        # T08-05: herness_egress_calls_total{decision="allowed", purpose, reason=""} += 1
        # T08-05: herness_egress_tokens_total{direction="in", destination} += tokens
        # T08-05: herness_egress_bytes_total{direction="out"} += line["bytes_out"]
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

    def _client_parts(
        self, purpose: Purpose, payload_class: PayloadClass, timeout: float, ids: _Ids
    ) -> tuple[ssl.SSLContext, str | None, TransportOpts, httpx.Timeout]:
        """U10-52 preconditions, then the TLS context, proxy, transport options and timeout."""
        if purpose == "model_download":
            msg = "model_download only inside deploy pull"
            raise EgressBlocked(msg)
        check_timeout(timeout, "timeout")
        opts = TransportOpts(purpose, payload_class, *ids)
        proxy = self._cfg.security.network.http_proxy  # never from the environment
        return tls_context(), proxy, opts, httpx.Timeout(timeout, connect=10.0)

    def http_client(
        self,
        purpose: Purpose,
        payload_class: PayloadClass,
        *,
        run_id: str | None = None,
        task_id: str | None = None,
        timeout: float = 120.0,
    ) -> httpx.Client:
        """The only synchronous client for a non-local endpoint (U10-52)."""
        ctx, proxy, opts, limit = self._client_parts(
            purpose, payload_class, timeout, (run_id, task_id)
        )
        inner = httpx.HTTPTransport(verify=ctx, proxy=proxy, retries=0)
        transport = GuardedTransport(inner, guard=self, **opts.fields())
        return httpx.Client(
            transport=transport, follow_redirects=False, trust_env=False, timeout=limit
        )

    def async_http_client(
        self,
        purpose: Purpose,
        payload_class: PayloadClass,
        *,
        run_id: str | None = None,
        task_id: str | None = None,
        timeout: float = 120.0,
    ) -> httpx.AsyncClient:
        """Async twin of ``http_client`` (U10-53)."""
        ctx, proxy, opts, limit = self._client_parts(
            purpose, payload_class, timeout, (run_id, task_id)
        )
        inner = httpx.AsyncHTTPTransport(verify=ctx, proxy=proxy, retries=0)
        transport = AsyncGuardedTransport(inner, guard=self, **opts.fields())
        return httpx.AsyncClient(
            transport=transport, follow_redirects=False, trust_env=False, timeout=limit
        )

    @contextmanager
    def download_window(self, *, allow_download: bool, actor: str) -> Iterator[None]:
        """Admit ``model_download`` for ``deploy pull --allow-download`` (U10-55).

        The flag is a context variable: local to this thread (and asyncio task), and carried
        into ``asyncio.to_thread`` where the async transport runs the check.
        """
        refusal = None
        if not allow_download:
            refusal = "deploy pull requires --allow-download"
        elif self._cfg.profile == "synth":
            refusal = "profile synth cannot download"
        elif os.environ.get("HERNESS_WORKER") == "1":  # set by T08-21 in worker processes
            refusal = "model_download is refused inside jobs"
        if refusal is not None:
            raise EgressBlocked(refusal)
        token = _WINDOW.set(True)
        try:
            _log.info("egress.download_window.opened", actor=actor)
            _audit.audit("admin_action", actor, action="deploy_pull", target="download_window")
            yield
        finally:
            _WINDOW.reset(token)
            _log.info("egress.download_window.closed", actor=actor)

    def _admit(self, request: httpx.Request, opts: TransportOpts) -> EgressTicket:
        """U10-54 steps 1-2: decide ``request``; a streaming body is refused."""
        try:
            body: bytes | None = request.content
        except httpx.RequestNotRead:
            body = None
        kw: dict[str, Any] = {"method": request.method}
        kw |= {"run_id": opts.run_id, "task_id": opts.task_id}
        url = str(request.url)
        return self._check_and_log(url, body, opts.purpose, opts.payload_class, None, **kw)

    def _write_completed(self, line: dict[str, Any]) -> None:
        """Append a ``completed`` line; a failure is logged, never raised (the call happened)."""
        try:
            self._log.write(line)
        except (StoreBusy, OSError) as exc:
            _log.error(
                "egress.log.failed", egress_id=line["egress_id"], error_type=type(exc).__name__
            )


# --- U10-56 process-wide guard ---------------------------------------------------------------


class _State:
    guard: EgressGuard | None = None


_GUARD_LOCK: Final = threading.Lock()


def get_guard() -> EgressGuard:
    """The process-wide guard, built once from the current config (U10-56)."""
    with _GUARD_LOCK:
        if _State.guard is None:
            cfg = _config.get_config()
            try:
                cfg_hash: str | None = _config.config_hash(cfg)
            except ConfigError:  # as audit does: an unresolvable key id leaves the hash null
                cfg_hash = None
            log = EgressLog(cfg.paths.logs, cfg_hash, cfg.profile)
            _State.guard = EgressGuard(cfg, get_redactor, log)
        return _State.guard


def reset_guard() -> None:
    """Drop the cached guard (config reset hook, U10-10)."""
    with _GUARD_LOCK:
        _State.guard = None


_config._RESET_HOOKS.append(reset_guard)
