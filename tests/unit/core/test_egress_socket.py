"""Tests for herness.core.egress_socket (impl 10 U10-58; card T10-18).

UT10-56 exercises ``SocketPolicy`` directly; PT10-07 is the hypothesis property that any
hostname outside the allowlist is always blocked. Installation, host derivation and the
threat-model scenarios live in ``tests/security/test_st10_socket.py``; the subprocess and
benchmark rows live in ``tests/integration/test_security_socket.py`` and ``tests/bench``.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING, Any, ClassVar, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.unit.connectors._settings_data import mongodb, servicenow

from herness.connectors.settings import MongoSettings, ServiceNowSettings, SourcesSection
from herness.core import egress_socket as es
from herness.core.egress_clients import LOOPBACK_HOSTS
from herness.core.errors import EgressBlocked
from herness.core.settings import SecurityConfig

if TYPE_CHECKING:
    from herness.core.config import HernessConfig

pytestmark = pytest.mark.unit

_ALLOWED = "allowed.example.com"
_RESOLVED_IP = "93.184.216.34"


def _fake_getaddrinfo(*_args: Any, **_kwargs: Any) -> list[tuple[Any, ...]]:
    return [(2, 1, 6, "", (_RESOLVED_IP, 443))]


@dataclass
class _FakeSourcesFile:
    sources: SourcesSection


@dataclass
class _FakeConfig:
    """Duck-typed stand-in for HernessConfig: attribute access only, as U10-58 reads it."""

    profile: str
    security: SecurityConfig
    sources: _FakeSourcesFile


def _full_cfg(profile: str = "local", **security: Any) -> HernessConfig:
    """A duck-typed stand-in cast to ``HernessConfig``: U10-58 reads it by attribute only."""
    section = SourcesSection(
        servicenow=ServiceNowSettings.model_validate(servicenow(hosts=["sso.example.com"])),
        mongodb=MongoSettings.model_validate(mongodb()),
    )
    sec = SecurityConfig.model_validate(security) if security else SecurityConfig()
    return cast("HernessConfig", _FakeConfig(profile, sec, _FakeSourcesFile(section)))


def test_ut10_56_loopback_is_always_allowed() -> None:
    """UT10-56 loopback names and addresses pass both checks with an empty allowlist."""
    policy = es.SocketPolicy(frozenset(), None)
    policy.check_getaddrinfo("localhost", 80)
    policy.check_getaddrinfo(None, 80)
    policy.check_getaddrinfo("", 80)
    policy.check_getaddrinfo("127.0.0.1", 80)
    policy.check_connect(("127.0.0.1", 80))
    policy.check_connect(("::1", 443))
    policy.check_sendto(("localhost", 80))


def test_ut10_56_allowed_host_resolves_and_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT10-56 an allowlisted host resolves once; the resolved IP is then allowed to connect."""
    calls: list[Any] = []

    def counting(*args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        calls.append(args)
        return _fake_getaddrinfo(*args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", counting)
    policy = es.SocketPolicy(frozenset({_ALLOWED}), None)
    policy.check_getaddrinfo(_ALLOWED, 443)
    policy.check_getaddrinfo(_ALLOWED, 443)  # fresh: no second resolution
    assert len(calls) == 1
    policy.check_connect((_RESOLVED_IP, 443))
    # the resolved IP is also allowed back through check_getaddrinfo (not itself an allowed host)
    policy.check_getaddrinfo(_RESOLVED_IP, 443)


def test_ut10_56_check_address_ignores_malformed_addresses() -> None:
    """UT10-56 an empty tuple or a non-tuple, non-string address is never checked."""
    policy = es.SocketPolicy(frozenset(), None)
    policy.check_connect(())
    policy.check_connect(12345)
    policy.check_sendto(())


def test_ut10_56_fresh_cache_entry_expires() -> None:
    """UT10-56 an expired resolution-cache entry is dropped and reported as not fresh."""
    es._RESOLVED["expired.example.com"] = monotonic() - 1.0
    assert es._fresh("expired.example.com") is False
    assert "expired.example.com" not in es._RESOLVED


def test_ut10_56_hook_dispatches_to_the_active_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT10-56 _hook maps each socket event to the matching check, and skips when re-entrant."""
    calls: list[tuple[str, Any]] = []

    class _Spy:
        def check_getaddrinfo(self, host: Any, port: Any) -> None:
            calls.append(("getaddrinfo", (host, port)))

        def check_connect(self, address: Any) -> None:
            calls.append(("connect", address))

        def check_sendto(self, address: Any) -> None:
            calls.append(("sendto", address))

    monkeypatch.setattr(es, "_POLICY", _Spy())
    es._hook("socket.getaddrinfo", ("h", 80, 0, 0, 0))
    es._hook("socket.connect", (None, ("1.2.3.4", 80)))
    es._hook("socket.sendto", (None, ("1.2.3.4", 53)))
    es._hook("socket.gethostbyname", (None,))  # an unmapped event is ignored
    assert calls == [
        ("getaddrinfo", ("h", 80)),
        ("connect", ("1.2.3.4", 80)),
        ("sendto", ("1.2.3.4", 53)),
    ]
    es._REENTRANT.active = True
    try:
        es._hook("socket.getaddrinfo", ("other", 80, 0, 0, 0))
    finally:
        es._REENTRANT.active = False
    assert len(calls) == 3  # re-entrant: no dispatch


def test_ut10_56_hook_is_a_noop_without_an_active_policy() -> None:
    """UT10-56 _hook does nothing while _POLICY is None (the reset state)."""
    assert es._POLICY is None
    es._hook("socket.connect", (None, ("203.0.113.9", 443)))  # would block if it dispatched


def test_ut10_56_source_hosts_from_a_full_config_object() -> None:
    """UT10-56 _source_hosts unions hosts and non-SDK base_url hosts from a full config."""
    hosts = es._source_hosts(_full_cfg())
    assert hosts == frozenset({"sso.example.com", "acme.service-now.com", "db0.example.com"})


def test_ut10_56_source_hosts_defensive_fallbacks() -> None:
    """UT10-56 _source_hosts is empty without a sources section; skips a hostless base_url."""
    no_sources = cast("HernessConfig", _FakeConfig("local", SecurityConfig(), cast(Any, None)))
    assert es._source_hosts(no_sources) == frozenset()

    class _FakeSource:
        enabled = True
        hosts: tuple[str, ...] = ()
        base_url = "not-a-url-with-no-host"

    class _FakeSection:
        model_fields: ClassVar[dict[str, Any]] = {"weird": None}
        weird = _FakeSource()

    section = cast(SourcesSection, _FakeSection())
    files = _FakeSourcesFile(section)
    weird = cast("HernessConfig", _FakeConfig("local", SecurityConfig(), files))
    assert es._source_hosts(weird) == frozenset()


def test_ut10_56_allowlist_unions_egress_extra_hosts_and_proxy() -> None:
    """UT10-56 _allowlist unions loopback, sources, egress destinations, extra hosts and proxy."""
    cfg = _full_cfg(
        egress={
            "enabled": True,
            "destinations": ["api.anthropic.com"],
            "purposes": ["reasoning_final"],
        },
        network={
            "extra_allowed_hosts": ["extra.example.com"],
            "http_proxy": "http://proxy.corp.example:3128",
        },
    )
    allowed, proxy = es._allowlist(cfg)
    assert proxy == "proxy.corp.example"
    expected = {
        "api.anthropic.com",
        "extra.example.com",
        "proxy.corp.example",
        "sso.example.com",
        "acme.service-now.com",
        "db0.example.com",
        *LOOPBACK_HOSTS,
    }
    assert allowed == frozenset(expected)


def test_ut10_56_allowlist_disabled_egress_excludes_destinations() -> None:
    """UT10-56 egress destinations are excluded from the allowlist while egress is disabled."""
    cfg = _full_cfg(egress={"enabled": False, "destinations": ["api.anthropic.com"]})
    allowed, proxy = es._allowlist(cfg)
    assert proxy is None
    assert "api.anthropic.com" not in allowed


def test_ut10_56_synth_profile_allows_loopback_only() -> None:
    """UT10-56 profile synth collapses the allowlist to loopback, ignoring everything else."""
    cfg = _full_cfg("synth", network={"extra_allowed_hosts": ["extra.example.com"]})
    allowed, proxy = es._allowlist(cfg)
    assert allowed == LOOPBACK_HOSTS
    assert proxy is None


def test_ut10_56_unix_socket_address_is_allowed() -> None:
    """UT10-56 an AF_UNIX address (str or bytes) is never checked against the allowlist."""
    policy = es.SocketPolicy(frozenset(), None)
    policy.check_connect("herness.sock")  # AF_UNIX addresses are opaque strings or bytes
    policy.check_sendto(b"herness.sock")


def test_ut10_56_other_host_is_blocked() -> None:
    """UT10-56 a host outside the allowlist raises EgressBlocked from both checks."""
    policy = es.SocketPolicy(frozenset({_ALLOWED}), None)
    with pytest.raises(EgressBlocked) as info:
        policy.check_getaddrinfo("other.example.com", 443)
    assert str(info.value) == "socket blocked: other.example.com"
    with pytest.raises(EgressBlocked) as info:
        policy.check_connect(("203.0.113.9", 443))
    assert str(info.value) == "socket blocked: 203.0.113.9"
    with pytest.raises(EgressBlocked):
        policy.check_sendto(("other.example.com", 53))


def test_ut10_56_egress_blocked_reason_is_masked() -> None:
    """UT10-56 the raised EgressBlocked carries no egress_id and a generic invalid reason."""
    policy = es.SocketPolicy(frozenset(), None)
    with pytest.raises(EgressBlocked) as info:
        policy.check_getaddrinfo("evil.example.com", 443)
    assert info.value.egress_id is None


# --- PT10-07 property: any non-allowlisted hostname is always blocked -----------------------


@given(
    label=st.text(
        alphabet=st.characters(whitelist_categories=("Ll", "Nd")), min_size=1, max_size=20
    )
)
def test_pt10_07_random_hostnames_are_always_blocked(label: str) -> None:
    """PT10-07 a random hostname never equal to the one allowed host is always blocked."""
    policy = es.SocketPolicy(frozenset({_ALLOWED}), None)
    host = f"{label}.invalid-test-domain.example"
    with pytest.raises(EgressBlocked):
        policy.check_getaddrinfo(host, 443)
