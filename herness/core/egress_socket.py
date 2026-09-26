"""Process socket guard: a ``sys.addaudithook`` blocking non-allowlisted sockets (U10-58).

Defense in depth for design 10 Section 5.4 "Socket guard": once installed, every
``socket.getaddrinfo``, ``socket.connect`` and ``socket.sendto`` call is checked against the
allowlist built from the source ``hosts`` lists (R-06), the egress allowlist and loopback. A
refused host raises ``EgressBlocked`` from the audit hook, aborting the call before it opens.
``install_socket_guard`` runs first in ``herness.cli.main``, before any import opens a socket.

Module state ``_POLICY``/``_RESOLVED`` is ENG Section 2.3 exception X-1: one process-wide
guard and DNS cache, swapped and cleared by ``reset_socket_guard`` (a U10-10 reset hook).
"""

from __future__ import annotations

import ipaddress
import os
import socket
import sys
import threading
from time import monotonic
from typing import TYPE_CHECKING, Any, Final, cast
from urllib.parse import urlsplit

from herness.core import config as _config
from herness.core.config_sources import SDK_SOURCE_KINDS, BootstrapConfig
from herness.core.egress_clients import LOOPBACK_HOSTS
from herness.core.errors import EgressBlocked
from herness.core.logging import get_logger

if TYPE_CHECKING:
    from herness.core.config import HernessConfig

__all__ = ["SocketPolicy", "install_socket_guard", "reset_socket_guard"]

_log = get_logger("core.egress_socket")
_RESOLVED_TTL_S: Final = 300.0
_ENV_VARS: Final[dict[str, str]] = {
    "HF_HUB_OFFLINE": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "DO_NOT_TRACK": "1",
}

# --- module state (ENG Section 2.3 exception X-1) --------------------------------------------

_POLICY: SocketPolicy | None = None
_RESOLVED: dict[str, float] = {}  # lower-cased host or IP literal -> expiry (monotonic seconds)
_RESOLVED_LOCK: Final = threading.Lock()
_REENTRANT: Final = threading.local()
_STATE_LOCK: Final = threading.Lock()  # guards the policy swap and the one-time hook install
_hook_installed = False


def _loopback(host: str) -> bool:
    """``localhost``, a loopback literal (U10-50), or an IP whose network is loopback."""
    if host in LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _fresh(key: str) -> bool:
    """Is ``key`` a live entry of the resolution cache (300 s expiry)?"""
    with _RESOLVED_LOCK:
        expiry = _RESOLVED.get(key)
        if expiry is None:
            return False
        if expiry < monotonic():
            del _RESOLVED[key]
            return False
        return True


def _remember(keys: set[str]) -> None:
    expiry = monotonic() + _RESOLVED_TTL_S
    with _RESOLVED_LOCK:
        for key in keys:
            _RESOLVED[key] = expiry


def _resolve_and_cache(host: object, port: object, name: str) -> None:
    """Resolve ``host`` for real (re-entrancy flag set) and cache its addresses plus itself."""
    _REENTRANT.active = True
    try:
        infos = socket.getaddrinfo(cast(Any, host), cast(Any, port))
    finally:
        _REENTRANT.active = False
    _remember({name, *(str(info[4][0]).lower() for info in infos)})


def _blocked(host: str) -> None:
    _log.warning("egress.socket.blocked", host=host)
    msg = f"socket blocked: {host}"
    raise EgressBlocked(msg)


def _check_address(address: object) -> None:
    """``check_connect``/``check_sendto`` shared body: loopback, resolved, ``AF_UNIX``, else."""
    if isinstance(address, str | bytes):  # AF_UNIX path (str or bytes)
        return
    if not (isinstance(address, tuple) and address):
        return
    host = str(address[0]).lower()
    if _loopback(host) or _fresh(host):
        return
    _blocked(host)


class SocketPolicy:
    """One process socket policy: the allowlist and the configured proxy host (U10-58)."""

    def __init__(self, allowed_hosts: frozenset[str], proxy_host: str | None) -> None:
        self.allowed_hosts = allowed_hosts
        self.proxy_host = proxy_host

    def check_getaddrinfo(self, host: object, port: object) -> None:
        """Allow ``None``/``""``/loopback/allowlisted hosts and cached IP literals, else block."""
        if host is None or host in ("", "localhost"):
            return
        name = str(host).lower()
        if _loopback(name):
            return
        if name in self.allowed_hosts:
            if not _fresh(name):
                _resolve_and_cache(host, port, name)
            return
        if _fresh(name):  # an IP literal already resolved from an allowed host
            return
        _blocked(name)

    def check_connect(self, address: object) -> None:
        """Allow loopback, a resolved IP or an ``AF_UNIX`` address, else block."""
        _check_address(address)

    def check_sendto(self, address: object) -> None:
        """Same rule as ``check_connect`` for connectionless sends."""
        _check_address(address)


def _hook(event: str, args: tuple[Any, ...]) -> None:
    """The ``sys.addaudithook`` callback; a no-op once ``_POLICY`` is cleared or re-entrant."""
    policy = _POLICY
    if policy is None or getattr(_REENTRANT, "active", False):
        return
    if event == "socket.getaddrinfo":
        policy.check_getaddrinfo(args[0], args[1])
    elif event == "socket.connect":
        policy.check_connect(args[1])
    elif event == "socket.sendto":
        policy.check_sendto(args[1])


def _source_hosts(cfg: HernessConfig | BootstrapConfig) -> frozenset[str]:
    """Enabled source ``hosts`` plus non-SDK ``base_url`` hosts (U10-58 step 1, R-06)."""
    if isinstance(cfg, BootstrapConfig):
        return frozenset(h.lower() for h in cfg.source_hosts)
    sources = getattr(getattr(cfg, "sources", None), "sources", None)
    if sources is None:
        return frozenset()
    hosts: set[str] = set()
    for name in type(sources).model_fields:
        src = getattr(sources, name)
        if src is None or not getattr(src, "enabled", False):
            continue
        hosts.update(str(h).lower() for h in getattr(src, "hosts", ()) or ())
        if name in SDK_SOURCE_KINDS:
            continue
        base_url = getattr(src, "base_url", None)
        if base_url and (host := urlsplit(base_url).hostname):
            hosts.add(host.lower())
    return frozenset(hosts)


def _allowlist(cfg: HernessConfig | BootstrapConfig) -> tuple[frozenset[str], str | None]:
    """U10-58 step 1: loopback, source hosts, the egress allowlist and the proxy host."""
    if cfg.profile == "synth":
        return LOOPBACK_HOSTS, None
    security = cfg.security
    proxy_host = (
        urlsplit(security.network.http_proxy).hostname if security.network.http_proxy else None
    )
    hosts = set(LOOPBACK_HOSTS) | _source_hosts(cfg)
    if security.egress.enabled:
        hosts.update(h.lower() for h in security.egress.destinations)
    hosts.update(h.lower() for h in security.network.extra_allowed_hosts)
    if proxy_host:
        hosts.add(proxy_host.lower())
    return frozenset(hosts), proxy_host


def install_socket_guard(cfg: HernessConfig | BootstrapConfig) -> None:
    """Compute the allowlist, set the offline env vars and (re)install the audit hook (U10-58).

    Safe to call more than once: each call replaces the active policy; the audit hook itself
    is added to the process only on the first call (``sys.addaudithook`` cannot be removed).
    """
    allowed, proxy_host = _allowlist(cfg)
    os.environ.update(_ENV_VARS)
    policy = SocketPolicy(allowed, proxy_host)
    global _POLICY, _hook_installed  # noqa: PLW0603 - ENG X-1 module state
    with _STATE_LOCK:
        _POLICY = policy
        if not _hook_installed:
            sys.addaudithook(_hook)
            _hook_installed = True
    _log.info("egress.guard.installed", profile=cfg.profile, allowed_hosts_count=len(allowed))


def reset_socket_guard() -> None:
    """Deactivate the guard and clear the resolution cache; the audit hook stays (U10-10)."""
    global _POLICY  # noqa: PLW0603 - ENG X-1 module state
    with _STATE_LOCK:
        _POLICY = None
    with _RESOLVED_LOCK:
        _RESOLVED.clear()


_config._RESET_HOOKS.append(reset_socket_guard)
