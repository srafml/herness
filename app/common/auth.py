"""Dashboard identity: OS user or trusted proxy header, the session actor, display names.

Implements impl 09 U09-54 - U09-56 (design 09 §9.1 - §9.2; spec 10 §9.2; R-50). The identity
header is trusted only when exposure is on, the app is bound to loopback, a trusted proxy is set
and the peer address equals it; everything else fails closed (TH09-04, TH09-05). The OS user
comes from the process credentials, never from the environment (``herness.reports._os_user``;
TH09-27).
"""

from __future__ import annotations

import ipaddress
import re
import types
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

import streamlit as st

from herness.core.audit import audit
from herness.core.logging import get_logger
from herness.reports._os_user import os_user as _process_user
from herness.reports.rules import Actor, load_user_ref_key, role_for, user_ref_for

if TYPE_CHECKING:
    from herness.core.settings import RolesConfig, UiConfig

__all__ = [
    "Identity", "Roster", "build_roster", "current_actor", "display_name", "resolve_identity",
]  # fmt: skip

_log = get_logger("app.auth")
_LOOPBACK: Final = frozenset({"127.0.0.1", "::1", "localhost"})
_HEADER_VALUE_RE: Final = re.compile(r"[A-Za-z0-9._@\-]+")
_MAX_HEADER: Final = 256
_ANONYMOUS: Final = "anonymous"
_ACTOR_KEY: Final = "herness_actor"
_AUDITED_KEY: Final = "herness_actor_audited"  # the username whose session was audited
_fallback_logged: list[bool] = []

type Roster = types.MappingProxyType[str, str]
"""Immutable ``user_ref -> username`` for every configured admin and reviewer (U09-56)."""


@dataclass(frozen=True)
class Identity:
    """Who the user is: ``username`` None means nobody (``source`` ``none``)."""

    username: str | None
    source: Literal["os", "header", "none"]
    reason: str | None


def _ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    text = value.strip().lower()
    try:
        return ipaddress.ip_address("127.0.0.1" if text == "localhost" else text)
    except ValueError:
        return None


def _untrusted(ui: UiConfig, remote_ip: str | None) -> str | None:
    """The first failed exposure condition of U09-54 step 2 (a) - (d), else None."""
    proxy = ui.expose.trusted_proxy
    if ui.bind.strip().lower() not in _LOOPBACK:
        return "bind_not_loopback"
    if proxy is None:
        return "no_trusted_proxy"
    if remote_ip is None:
        return "remote_ip_unknown"
    peer, trusted = _ip(remote_ip), _ip(proxy)
    if peer is None or trusted is None or peer != trusted:
        return "untrusted_peer"
    return None


def resolve_identity(
    headers: Mapping[str, str], remote_ip: str | None, ui: UiConfig, os_user: str
) -> Identity:
    """Decide who the user is (U09-54); the header counts only when every trust check holds."""
    wanted = ui.expose.identity_header.lower()
    value = next((v for k, v in headers.items() if k.lower() == wanted), None)
    if not ui.expose.enabled:
        reason = None if value is None else "header_ignored_expose_disabled"
        return Identity(os_user, "os", reason)
    if (failed := _untrusted(ui, remote_ip)) is not None:
        return Identity(None, "none", failed)
    if value is None or not value.strip():
        return Identity(None, "none", "header_missing")
    value = value.strip()
    if len(value) > _MAX_HEADER or _HEADER_VALUE_RE.fullmatch(value) is None:
        return Identity(None, "none", "header_invalid")
    return Identity(value, "header", None)


def build_roster(roles: RolesConfig, key: bytes) -> Roster:
    """``user_ref -> username`` for every name in ``admins`` and ``reviewers`` (U09-56)."""
    names = [*roles.admins, *roles.reviewers]
    return types.MappingProxyType({user_ref_for(name, key): name for name in names})


def display_name(user_ref: str, roster: Roster) -> str:
    """Roster name, else ``user:<first 8 chars>``; ``anonymous`` stays ``anonymous``."""
    if user_ref == _ANONYMOUS:
        return _ANONYMOUS
    return roster.get(user_ref) or f"user:{user_ref[:8]}"


def _warn_fallback() -> None:
    if not _fallback_logged:
        _fallback_logged.append(True)
        _log.warning("app.identity.fallback", source="getpass")  # no user name: personal data


def current_actor() -> Actor:
    """The ``Actor`` of this browser session; one ``auth`` audit line per session (U09-55)."""
    from app.common.bootstrap import get_services  # noqa: PLC0415 - cycle: bootstrap imports us

    services = get_services()
    ui = services.cfg.security.ui
    remote_ip = getattr(st.context, "ip_address", None)  # V09-01: absent -> None
    identity = resolve_identity(
        dict(st.context.headers), remote_ip, ui, _process_user(on_fallback=_warn_fallback)
    )
    cached = st.session_state.get(_ACTOR_KEY)
    if isinstance(cached, Actor) and st.session_state.get(_AUDITED_KEY) == identity.username:
        return cached
    if identity.reason is not None:
        _log.warning("app.identity.header_ignored", reason=identity.reason)  # never the value
    role = role_for(identity.username, ui.roles)
    user_ref = (
        _ANONYMOUS
        if identity.username is None
        else user_ref_for(identity.username, load_user_ref_key())
    )
    result = "denied" if role == "denied" else "allowed"
    audit("auth", user_ref, user_ref=user_ref, role=role, result=result)  # failure propagates
    _log.info("app.auth.resolved", user_ref=user_ref, role=role, source=identity.source)
    actor = Actor(user_ref, role, "dashboard", display=display_name(user_ref, services.roster))
    st.session_state[_ACTOR_KEY] = actor
    st.session_state[_AUDITED_KEY] = identity.username
    return actor
