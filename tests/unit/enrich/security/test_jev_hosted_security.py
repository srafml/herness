"""ST03-03 (TH03-02): hosted Jev forced under the `local` profile is refused by the guard
before any socket opens (T03-13).

The guard is the real `get_guard()` built from the `local` config of `jev_env` (egress
disabled). `ScriptedNet` sits below `GuardedTransport` and `socket` connects are recorded,
so a request that got past the guard would show up in either. The key is synthetic (R-67).
"""

from __future__ import annotations

import socket
import traceback
from typing import Any

import httpx2
import pytest
from pydantic import SecretStr
from structlog.testing import capture_logs
from tests.support.egress_harness import audit_fields, egress_lines
from tests.unit.enrich._openjev_support import QS, install, item, jev_env, ok

from herness.core import config as c
from herness.core.errors import AuthError, EgressBlocked, ModelUnavailable
from herness.core.resilience import DeciderChain, ProcessState
from herness.core.types import GpuClass, ServiceName
from herness.enrich.deciders.jev_hosted import JevHostedDecider
from herness.enrich.settings import JevSettings

pytestmark = pytest.mark.unit

__all__ = ["jev_env"]  # the fixture is used by name

_SYNTHETIC = "synthetic-jev-key"
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


class _Gpu:
    """GPU state stub: nothing loaded (only `jev` could run)."""

    def loaded_class(self) -> GpuClass:
        return "none"

    def service_healthy(self, name: ServiceName) -> bool:
        del name
        return False


@pytest.fixture
def sockets(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Record (and refuse) every off-host socket connect; loopback connects pass, since
    asyncio's self-pipe on Windows is a loopback socket pair."""
    seen: list[object] = []
    real_connect = socket.socket.connect

    def off_host(address: object) -> bool:
        host = address[0] if isinstance(address, tuple) else address
        return host not in _LOOPBACK

    def connect(self: socket.socket, address: Any) -> None:
        if off_host(address):
            seen.append(address)
            msg = "socket opened in ST03-03"
            raise AssertionError(msg)
        real_connect(self, address)

    def create_connection(address: object, *_a: Any, **_k: Any) -> socket.socket:
        seen.append(address)
        msg = "socket opened in ST03-03"
        raise AssertionError(msg)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "create_connection", create_connection)
    return seen


def _decider() -> JevHostedDecider:
    settings = JevSettings(enabled=True)
    return JevHostedDecider(settings, api_key=SecretStr(_SYNTHETIC), samples=None)


def test_st03_03_local_profile_decide_blocked_no_socket(
    jev_env: ProcessState, monkeypatch: pytest.MonkeyPatch, sockets: list[object]
) -> None:
    """ST03-03 local profile, jev called directly: EgressBlocked(profile_forbids_egress) from
    the real guard; nothing reaches the pool; no socket; blocked egress line, no key in it."""
    del jev_env
    assert c.get_config().profile == "local"
    net = install(monkeypatch, ok)
    with pytest.raises(EgressBlocked) as info:
        _decider().decide([item(1)], QS)
    assert info.value.reason == "profile_forbids_egress"
    assert info.value.egress_id is not None
    assert net.requests == []
    assert sockets == []
    logs = c.get_config().paths.logs
    lines = egress_lines(logs)
    assert [ln["decision"] for ln in lines] == ["blocked"]
    assert (lines[0]["purpose"], lines[0]["payload_class"]) == (
        "bulk_classification",
        "redacted_text",
    )
    assert lines[0]["payload_sha256"] is None
    assert [f["reason"] for f in audit_fields(logs)] == ["profile_forbids_egress"]
    assert _SYNTHETIC not in repr(lines) + str(info.value) + repr(vars(info.value))


def test_st03_03_jev_forced_in_chain_blocked_no_socket(
    jev_env: ProcessState, monkeypatch: pytest.MonkeyPatch, sockets: list[object]
) -> None:
    """ST03-03 local profile with jev forced into the DeciderChain (availability gate
    bypassed): EgressBlocked propagates from the chain, never retried, no socket opened."""
    del jev_env
    monkeypatch.setattr(DeciderChain, "_available", lambda *_a, **_k: True)
    net = install(monkeypatch, ok)
    decider = _decider()
    calls: list[str] = []

    def resolve(name: str) -> JevHostedDecider:
        calls.append(name)
        return decider

    chain = DeciderChain(["jev"], gpu=_Gpu(), resolve=resolve)
    with pytest.raises(EgressBlocked) as info:
        chain.decide([item(1), item(2)], QS)
    assert info.value.reason == "profile_forbids_egress"
    assert calls == ["jev"]
    assert net.requests == []
    assert sockets == []


def test_st03_03_local_profile_health_is_model_unavailable(
    jev_env: ProcessState, monkeypatch: pytest.MonkeyPatch, sockets: list[object]
) -> None:
    """ST03-03 local profile: health is refused by the guard -> ModelUnavailable, no socket."""
    del jev_env
    net = install(monkeypatch, ok)
    with pytest.raises(ModelUnavailable, match=r"^jev health: EgressBlocked$"):
        _decider().health()
    assert net.requests == []
    assert sockets == []


@pytest.mark.parametrize(("status", "error"), [(401, AuthError), (503, ModelUnavailable)])
def test_st03_16_jev_key_absent_from_errors_and_logs(
    jev_env: ProcessState, status: int, error: type[Exception]
) -> None:
    """ST03-16 (TH03-14) hosted Jev: a reply echoing the key yields an error and log events
    that never contain it; the key is sent only in the Authorization header."""
    del jev_env
    seen: list[httpx2.Request] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(status, json={"detail": f"bad key {_SYNTHETIC}"})

    decider = JevHostedDecider(
        JevSettings(),
        api_key=SecretStr(_SYNTHETIC),
        samples=None,
        client_factory=lambda: httpx2.Client(transport=httpx2.MockTransport(handle)),
    )
    with capture_logs() as logs, pytest.raises(error) as info:
        decider.decide([item(1)], QS)
    text = "".join(traceback.format_exception(info.value)) + repr(vars(info.value))
    assert _SYNTHETIC not in text + repr(logs)
    assert {r.headers["authorization"] for r in seen} == {f"Bearer {_SYNTHETIC}"}
    assert all(_SYNTHETIC.encode() not in r.content for r in seen)
