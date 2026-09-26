"""Security tests for the guarded and loopback clients (impl 10 card T10-17).

ST10-07, ST10-09, ST10-12 (streaming-body half, moved here from T10-16), ST10-32, ST10-35,
ST10-40, ST10-41 and the host cases of ST10-54. No real network: ``respx`` answers at the
pool layer and ``record_connects`` refuses and records every ``socket.connect``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
import structlog
from pydantic import SecretStr
from tests.support.egress_harness import API, audit_fields, egress_lines, load, make_guard
from tests.support.egress_servers import record_connects
from tests.support.fake_keyring import MemoryKeyring

from herness.core import config as c
from herness.core import egress as eg
from herness.core.errors import EgressBlocked

pytestmark = pytest.mark.unit

EVIDENCE = json.dumps({"metric": "mttr_hours", "value": 12.5}).encode()
MIB = 1_048_576


@pytest.fixture(autouse=True)
def _isolate(fake_keyring: MemoryKeyring) -> Iterator[None]:
    yield
    c.reset_config()


@pytest.fixture
def connects(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    return record_connects(monkeypatch)


def _blocked(fn: Any, *args: Any, **kw: Any) -> EgressBlocked:
    with pytest.raises(EgressBlocked) as info:
        fn(*args, **kw)
    return info.value


def test_st10_07_local_profile_client_never_connects(
    tmp_path: Path, connects: list[object]
) -> None:
    """ST10-07 profile local: get_guard().http_client to the API is blocked; zero connects."""
    load(tmp_path, "local")
    try:
        with eg.get_guard().http_client("reasoning_final", "aggregated_evidence") as client:
            exc = _blocked(client.post, API, content=EVIDENCE)
    finally:
        eg.reset_guard()
    assert exc.reason == "profile_forbids_egress"
    assert connects == []


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("https://api.anthropic.com.evil.com/v1/messages", "host_not_allowed"),
        ("https://api.anthropic.com@evil.com/v1/messages", "userinfo_present"),
        ("https://[2606:4700::1]/v1/messages", "ip_literal"),
        ("http://api.anthropic.com/v1/messages", "scheme_not_https"),
    ],
)
def test_st10_09_look_alike_hosts_are_blocked(
    tmp_path: Path, connects: list[object], url: str, reason: str
) -> None:
    """ST10-09 look-alike, user-info, IPv6 literal and http:// are refused before any socket."""
    guard = make_guard(load(tmp_path, "hybrid"))
    with guard.http_client("reasoning_final", "aggregated_evidence") as client:
        assert _blocked(client.post, url, content=EVIDENCE).reason == reason
    assert connects == []
    assert [ln["reason"] for ln in egress_lines(guard._cfg.paths.logs)] == [reason]


def test_st10_09_case_folded_host_is_allowed(tmp_path: Path) -> None:
    """ST10-09 https://API.ANTHROPIC.COM:443/ is the valid host, case-folded."""
    guard = make_guard(load(tmp_path, "hybrid"))
    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://api.anthropic.com/v1/messages").respond(200, json={})
        with guard.http_client("reasoning_final", "aggregated_evidence") as client:
            response = client.post("https://API.ANTHROPIC.COM:443/v1/messages", content=EVIDENCE)
    assert response.status_code == 200
    allowed, completed = egress_lines(guard._cfg.paths.logs)
    assert (allowed["destination"], completed["destination"]) == ("api.anthropic.com",) * 2


def test_st10_12_streaming_body_is_blocked(tmp_path: Path, connects: list[object]) -> None:
    """ST10-12 a streaming request body cannot be scanned: blocked streaming_body, audited."""
    guard = make_guard(load(tmp_path, "hybrid"))

    def chunks() -> Iterator[bytes]:
        yield EVIDENCE

    with guard.http_client("reasoning_final", "aggregated_evidence") as client:
        exc = _blocked(client.post, API, content=chunks())
    assert exc.reason == "streaming_body"
    (line,) = egress_lines(guard._cfg.paths.logs)
    assert (line["decision"], line["reason"], line["payload_sha256"]) == (
        "blocked",
        "streaming_body",
        None,
    )
    assert audit_fields(guard._cfg.paths.logs) == [
        {"egress_id": exc.egress_id, "reason": "streaming_body"}
    ]
    assert connects == []


def test_st10_12_async_streaming_body_is_blocked(tmp_path: Path, connects: list[object]) -> None:
    """ST10-12 the async client refuses an async streaming body the same way."""
    guard = make_guard(load(tmp_path, "hybrid"))

    async def chunks() -> AsyncIterator[bytes]:
        yield EVIDENCE

    async def send() -> None:
        async with guard.async_http_client("reasoning_final", "aggregated_evidence") as client:
            await client.post(API, content=chunks())

    assert _blocked(asyncio.run, send()).reason == "streaming_body"
    assert connects == []


def test_st10_12_steps_1_to_3_come_before_the_streaming_check(tmp_path: Path) -> None:
    """ST10-12 a streaming body to a foreign host reports the host reason first."""
    guard = make_guard(load(tmp_path, "hybrid"))
    with guard.http_client("reasoning_final", "aggregated_evidence") as client:
        exc = _blocked(client.post, "https://evil.com/x", content=iter([b"x"]))
    assert exc.reason == "host_not_allowed"


def test_st10_32_redirect_is_not_followed(tmp_path: Path) -> None:
    """ST10-32 an allowed host answering 302 to https://evil.com: not followed, evil untouched."""
    guard = make_guard(load(tmp_path, "hybrid"))
    with respx.mock(assert_all_called=False) as mock:
        mock.post(API).respond(302, headers={"Location": "https://evil.com/steal"})
        evil = mock.route(host="evil.com").respond(200)
        with guard.http_client("reasoning_final", "aggregated_evidence") as client:
            assert client.post(API, content=EVIDENCE).status_code == 302
            forced = _blocked(client.post, API, content=EVIDENCE, follow_redirects=True)
    assert forced.reason == "host_not_allowed"  # each hop is checked again
    assert evil.call_count == 0


def test_st10_35_model_download_client_and_worker_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST10-35 http_client("model_download", "none") and a window inside a worker: blocked."""
    guard = make_guard(load(tmp_path, "hybrid"))
    for factory in (guard.http_client, guard.async_http_client):
        _blocked(factory, "model_download", "none")
    monkeypatch.setenv("HERNESS_WORKER", "1")
    window = guard.download_window(allow_download=True, actor="system")
    assert "inside jobs" in str(_blocked(window.__enter__))
    assert audit_fields(guard._cfg.paths.logs, "admin_action") == []


def _order_guard(tmp_path: Path) -> tuple[eg.EgressGuard, list[str]]:
    guard = make_guard(load(tmp_path, "hybrid"))
    order: list[str] = []
    real_write = guard._log.write

    def write(line: dict[str, Any]) -> None:
        real_write(line)
        order.append(line["decision"])

    guard._log.write = write  # type: ignore[method-assign]
    return guard, order


def test_st10_40_allowed_line_precedes_the_inner_transport(tmp_path: Path) -> None:
    """ST10-40 the allowed line is written before the inner transport; completed after close."""
    guard, order = _order_guard(tmp_path)

    def handler(_request: httpx.Request) -> httpx.Response:
        order.append("inner")
        return httpx.Response(200, content=iter([b'{"usage": {"input_tokens": 2}}']))

    transport = eg.GuardedTransport(
        httpx.MockTransport(handler),
        guard=guard,
        purpose="reasoning_final",
        payload_class="aggregated_evidence",
        run_id=None,
        task_id=None,
    )
    response = transport.handle_request(httpx.Request("POST", API, content=EVIDENCE))
    assert order == ["allowed", "inner"]
    response.read()
    response.close()
    response.close()
    assert order == ["allowed", "inner", "completed"]  # exactly one completed line
    transport.close()


def test_st10_40_async_order(tmp_path: Path) -> None:
    """ST10-40 async transport: allowed, inner, then completed after aclose."""
    guard, order = _order_guard(tmp_path)

    def handler(_request: httpx.Request) -> httpx.Response:
        order.append("inner")
        return httpx.Response(200, content=_achunks([b"ok"]))

    transport = eg.AsyncGuardedTransport(
        httpx.MockTransport(handler),
        guard=guard,
        purpose="reasoning_final",
        payload_class="aggregated_evidence",
        run_id="run_9",
        task_id=None,
    )

    async def run() -> None:
        request = httpx.Request("POST", API, content=EVIDENCE)
        response = await transport.handle_async_request(request)
        assert order == ["allowed", "inner"]
        await response.aread()
        await response.aclose()
        await transport.aclose()

    asyncio.run(run())
    assert order == ["allowed", "inner", "completed"]


async def _achunks(parts: list[bytes]) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


def test_st10_40_already_read_response_completes_at_once(tmp_path: Path) -> None:
    """ST10-40 an inner transport returning a read body: completed right after the send."""
    guard, order = _order_guard(tmp_path)

    def handler(_request: httpx.Request) -> httpx.Response:
        order.append("inner")
        return httpx.Response(200, json={"usage": {"input_tokens": 2, "output_tokens": 1}})

    for cls in (eg.GuardedTransport, eg.AsyncGuardedTransport):
        order.clear()
        transport = cls(
            httpx.MockTransport(handler),
            guard=guard,
            purpose="reasoning_final",
            payload_class="aggregated_evidence",
            run_id=None,
            task_id=None,
        )
        request = httpx.Request("POST", API, content=EVIDENCE)
        if isinstance(transport, eg.GuardedTransport):
            transport.handle_request(request)
        else:
            asyncio.run(transport.handle_async_request(request))
        assert order == ["allowed", "inner", "completed"]
    completed = egress_lines(guard._cfg.paths.logs)[-1]
    assert (completed["tokens_in"], completed["tokens_out"]) == (2, 1)


def _huge() -> Iterator[bytes]:
    block = b"\0" * MIB
    for _ in range(60):
        yield block


async def _ahuge() -> AsyncIterator[bytes]:
    for block in _huge():
        yield block


def test_st10_41_sixty_mib_response_is_cut(tmp_path: Path) -> None:
    """ST10-41 a response streaming 60 MiB -> EgressBlocked("response_too_large")."""
    guard = make_guard(load(tmp_path, "hybrid"))
    with respx.mock() as mock:
        mock.post(API).mock(return_value=httpx.Response(200, content=_huge()))
        with guard.http_client("reasoning_final", "aggregated_evidence") as client:
            exc = _blocked(client.post, API, content=EVIDENCE)
    assert exc.reason == "response_too_large"
    assert str(exc) == f"egress blocked ({exc.egress_id}): response_too_large"
    _allowed, completed = egress_lines(guard._cfg.paths.logs)
    assert (completed["egress_id"], completed["reason"]) == (exc.egress_id, "response_too_large")
    assert eg.MAX_RESPONSE_BYTES < completed["bytes_in"] <= eg.MAX_RESPONSE_BYTES + MIB


def test_st10_41_async_sixty_mib_response_is_cut(tmp_path: Path) -> None:
    """ST10-41 the async client cuts a 60 MiB response the same way."""
    guard = make_guard(load(tmp_path, "hybrid"))

    async def send() -> None:
        async with guard.async_http_client("reasoning_final", "aggregated_evidence") as client:
            await client.post(API, content=EVIDENCE)

    with respx.mock() as mock:
        mock.post(API).mock(return_value=httpx.Response(200, content=_ahuge()))
        assert _blocked(asyncio.run, send()).reason == "response_too_large"
    assert egress_lines(guard._cfg.paths.logs)[-1]["reason"] == "response_too_large"


@pytest.mark.parametrize(
    "base_url",
    ["http://10.0.0.5:8000", "http://127.0.0.1.evil.com:8000", "http://u:p@127.0.0.1:8000"],
)
@pytest.mark.parametrize("factory", [eg.loopback_http_client, eg.aloopback_http_client])
def test_st10_54_non_loopback_base_url(connects: list[object], factory: Any, base_url: str) -> None:
    """ST10-54 non-loopback base_url (both variants): not_loopback before any socket."""
    bearer = SecretStr("-".join(("st10", "54", "bearer")))
    with structlog.testing.capture_logs() as logs:
        exc = _blocked(factory, base_url, timeout_s=5.0, bearer=bearer)
    assert exc.reason == "not_loopback"
    assert connects == []
    assert [(entry["event"], entry["log_level"]) for entry in logs] == [
        ("egress.loopback.blocked", "warning")
    ]
    assert set(logs[0]) == {"event", "log_level", "component", "host"}  # host only
    assert "u:p" not in repr(logs)
    assert bearer.get_secret_value() not in repr(logs) + str(exc)


def test_st10_54_absolute_url_off_loopback(connects: list[object]) -> None:
    """ST10-54 a loopback client sending an absolute http://example.org/ URL: not_loopback."""
    bearer = SecretStr("-".join(("st10", "54", "abs")))
    with (
        structlog.testing.capture_logs() as logs,
        eg.loopback_http_client("http://127.0.0.1:8000", timeout_s=5.0, bearer=bearer) as client,
    ):
        exc = _blocked(client.get, "http://example.org/")
    assert exc.reason == "not_loopback"
    assert [entry["host"] for entry in logs] == ["example.org"]
    assert bearer.get_secret_value() not in repr(logs)
    assert connects == []


def test_st10_54_async_absolute_url_off_loopback(connects: list[object]) -> None:
    """ST10-54 async variant: an absolute non-loopback URL is refused per request."""

    async def send() -> None:
        async with eg.aloopback_http_client("http://[::1]:8000", timeout_s=5.0) as client:
            await client.get("https://" + ":".join(("user", "pw")) + "@127.0.0.1/x")

    assert _blocked(asyncio.run, send()).reason == "not_loopback"  # user info refused too
    assert connects == []
