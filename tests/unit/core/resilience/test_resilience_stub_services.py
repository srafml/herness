"""Tests for the HERNESS_STUB_SERVICES format shared with the stub fixture (T08-25; DD11-04)."""

from __future__ import annotations

import httpx2
import pytest
from tests.support.fault_env import FaultEnv
from tests.support.stub_http import StubHTTPServer, StubRequest, StubResponse

from herness.core import egress
from herness.core.resilience import ProcessState, faults

pytestmark = pytest.mark.unit


class _Stub(StubHTTPServer):
    def respond(self, request: StubRequest) -> StubResponse:
        return StubResponse.json(200, {"path": request.path})


def _refused(server: StubHTTPServer) -> bool:
    try:
        with egress.loopback_http_client(server.base_url, timeout_s=2) as client:
            client.get("/")
    except httpx2.TransportError:
        return True
    return False


@pytest.mark.usefixtures("stub_services")
def test_ut08_113_stub_fixture_url_resolves_and_kill_service_reaches_it(
    fault_env: FaultEnv,
    reset_process_state: ProcessState,
) -> None:
    """UT08-113 a `stub_services` URL resolves via `faults._stub_url`; kill_service reaches it."""
    hook: list[str] = []
    reset_process_state.kill_service_hook = hook.append
    fault_env([{"point": "gpu.after_start", "action": "kill_service:openjev"}])
    assert faults._stub_url("openjev") is None  # fixture started empty: no leakage
    with _Stub(service_name="openjev") as server, _Stub(service_name="other") as other:
        assert faults._stub_url("openjev") == server.base_url
        assert faults._stub_url("other") == other.base_url
        faults.fault_point("gpu.after_start")
        assert _refused(server)
        assert not _refused(other)
    assert hook == []  # the stub route handled it: the compose hook was not used
