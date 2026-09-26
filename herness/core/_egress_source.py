"""Non-constructing helpers of the source client (impl 10 U10-110; card T10-33).

Size-forced private sibling of ``herness.core.egress_clients``, which builds
``source_http_client`` and ``SourceHostTransport`` from these pieces and re-exports both
(ST10-25 exemption list stays ``egress.py`` + ``egress_clients.py``: this module builds no
``httpx2`` client or pool transport, only allowlists, precondition checks and refusal reasons).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from herness.core._egress_streams import LOOPBACK_HOSTS
from herness.core.config_sources import SDK_SOURCE_KINDS
from herness.core.errors import ConfigError, EgressBlocked
from herness.core.logging import get_logger

if TYPE_CHECKING:
    import httpx2

    from herness.core.config import HernessConfig

__all__ = ["MAX_SOURCE_RESPONSE_BYTES", "MAX_SOURCE_TIMEOUT_S", "blocked", "body_error"]
__all__ += ["check_int", "check_timeout_s", "check_verify", "enabled_source"]
__all__ += ["process_allowlist", "source_allowlist", "source_refusal"]

MAX_SOURCE_TIMEOUT_S = 600.0
MAX_SOURCE_RESPONSE_BYTES = 1_073_741_824  # 1 GiB (U10-110)
_log = get_logger("core.egress")


def check_timeout_s(value: object) -> None:
    """``ConfigError`` unless ``0 < value <= 600`` (a bool or non-number fails too)."""
    ok = (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and 0 < value <= MAX_SOURCE_TIMEOUT_S
    )
    if not ok:
        msg = "timeout_s must be greater than 0 and at most 600 seconds"
        raise ConfigError(msg)


def check_int(value: object, low: int, high: int, name: str) -> None:
    """``ConfigError`` unless ``value`` is an ``int`` (not ``bool``) in ``[low, high]``."""
    ok = isinstance(value, int) and not isinstance(value, bool) and low <= value <= high
    if not ok:
        msg = f"{name} must be an integer between {low} and {high}"
        raise ConfigError(msg)


def check_verify(verify: object, source: str) -> Path | None:
    """``True`` -> ``None`` (certifi); an existing file -> itself; else ``ConfigError``.

    U10-110's Preconditions row is unconditional: any value that is not ``True`` or an
    existing CA bundle file, including ``False`` and a ``Path`` that does not exist, raises
    the same verbatim message (fix round 1, review Important #1).
    """
    if verify is True:
        return None
    if isinstance(verify, Path) and verify.is_file():
        return verify
    msg = f"TLS verification cannot be disabled for source {source}"
    raise ConfigError(msg)


def enabled_source(cfg: HernessConfig, source: str) -> object:
    """The named section of ``cfg.sources.sources``; ``ConfigError`` unknown or disabled."""
    holder = getattr(getattr(cfg, "sources", None), "sources", None)
    fields = type(holder).model_fields if holder is not None else {}
    section = getattr(holder, source, None) if source in fields else None
    if section is None or not getattr(section, "enabled", False):
        msg = f"unknown or disabled source {source}"
        raise ConfigError(msg)
    return section


def source_allowlist(source: str, section: object, base_host: str) -> frozenset[str]:
    """SDK sources: their ``hosts``; other sources: exactly the ``base_url`` host (R-06 ruling).

    ``hosts`` of a non-SDK source only widens the socket guard, never the client itself
    (impl 01 U01-15/§7): a ``servicenow`` client with ``hosts: [sso.example.com]`` still
    refuses a request to ``sso.example.com``.
    """
    if source in SDK_SOURCE_KINDS:
        return frozenset(str(h).lower() for h in getattr(section, "hosts", ()) or ())
    return frozenset({base_host}) if base_host else frozenset()


def source_refusal(
    url: httpx2.URL, source_hosts: frozenset[str], allowed_hosts: frozenset[str]
) -> str | None:
    """The refusal reason for one request URL against both allowlists, else ``None``."""
    host = (url.host or "").lower()
    if url.userinfo:
        return "userinfo_present"
    loopback = host in LOOPBACK_HOSTS
    if url.scheme != "https" and not (loopback and url.scheme == "http"):
        return "scheme_not_https"
    if host not in source_hosts or host not in allowed_hosts:
        return "host_not_allowed"
    return None


def blocked(source: str, host: str, reason: str) -> EgressBlocked:
    """Log ``egress.source.blocked`` and build the refusal (the caller raises it)."""
    _log.warning("egress.source.blocked", source=source, host=host, reason=reason)
    # T08-05: herness_socket_blocked_total{event="source_client"} += 1
    msg = f"source client refused: {reason}"
    return EgressBlocked(msg, reason=reason)


def body_error(reason: str) -> EgressBlocked:
    """``open_body``'s error callback: the exact wording U10-110 gives ``response_too_large``."""
    if reason == "response_too_large":
        return EgressBlocked("source response too large", reason=reason)
    return EgressBlocked(f"source client refused: {reason}", reason=reason)


def process_allowlist(cfg: HernessConfig) -> frozenset[str]:
    """The installed ``SocketPolicy`` allowlist, else the U10-58 allowlist of ``cfg``."""
    # egress_socket imports egress_clients, which imports this module: a top-level import here
    # would cycle back, so it is deferred to call time (both modules are complete by then).
    from herness.core import egress_socket as _sock  # noqa: PLC0415

    policy = _sock._POLICY
    return policy.allowed_hosts if policy is not None else _sock._allowlist(cfg)[0]
