"""Tests for the guarded clients, transports, download window and loopback clients.

Impl 10 U10-52..55 and U10-59 (card T10-17): UT10-52 (client-config half and the int-only
token estimate), UT10-54, UT10-55 and UT10-74. No socket is opened: ``respx`` answers at the
connection-pool layer, under the guarded and loopback transports.
"""

from __future__ import annotations

import asyncio
import json
import ssl
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpcore
import httpx
import pytest
import respx
from pydantic import SecretStr
from tests.support.egress_harness import API, audit_fields, egress_lines, load, make_guard
from tests.support.fake_keyring import MemoryKeyring

from herness.core import _egress_transport as et
from herness.core import config as c
from herness.core import egress as eg
from herness.core import egress_clients as ec
from herness.core.errors import ConfigError, EgressBlocked

pytestmark = pytest.mark.unit

EVIDENCE = json.dumps({"metric": "mttr_hours", "service": "svc-7", "value": 12.5}).encode()
HF = "https://huggingface.co/Qwen/x/resolve/main/model.safetensors"


@pytest.fixture(autouse=True)
def _isolate(fake_keyring: MemoryKeyring) -> Iterator[None]:
    yield
    c.reset_config()


def _hybrid(tmp_path: Path, **network: Any) -> eg.EgressGuard:
    cfg = load(tmp_path, "hybrid")
    if network:
        net = cfg.security.network.model_copy(update=network)
        cfg = cfg.model_copy(update={"security": cfg.security.model_copy(update={"network": net})})
    return make_guard(cfg)


def _reason(fn: Any, *args: Any, **kw: Any) -> str | None:
    with pytest.raises(EgressBlocked) as info:
        fn(*args, **kw)
    return info.value.reason


# --- UT10-52 client configuration (U10-52, U10-53) -------------------------------------------


def test_ut10_52_sync_client_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT10-52 http_client: GuardedTransport, no redirects, no env, timeout, TLS, no retries."""
    where: list[int] = []
    real_where = ec.certifi.where
    monkeypatch.setattr(ec.certifi, "where", lambda: where.append(1) or real_where())
    client = _hybrid(tmp_path).http_client("reasoning_final", "aggregated_evidence", timeout=30.0)
    assert client.follow_redirects is False
    assert client.trust_env is False
    assert client.timeout == httpx.Timeout(30.0, connect=10.0)
    transport = client._transport
    assert isinstance(transport, eg.GuardedTransport)
    inner = transport._inner
    assert isinstance(inner, httpx.HTTPTransport)  # noqa: TID251 - type check, no client
    pool = inner._pool
    assert isinstance(pool, httpcore.ConnectionPool)
    assert not isinstance(pool, httpcore.HTTPProxy)  # no proxy configured, none from the env
    assert pool._retries == 0
    ctx = pool._ssl_context
    assert ctx is not None
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2
    assert (ctx.verify_mode, ctx.check_hostname) == (ssl.CERT_REQUIRED, True)
    assert where == [1]  # the CA set is certifi's
    client.close()


def test_ut10_52_async_client_configuration(tmp_path: Path) -> None:
    """UT10-52 async_http_client: AsyncGuardedTransport over AsyncHTTPTransport, same settings."""
    client = _hybrid(tmp_path).async_http_client("reasoning_final", "aggregated_evidence")
    assert (client.follow_redirects, client.trust_env) == (False, False)
    assert client.timeout == httpx.Timeout(120.0, connect=10.0)
    transport = client._transport
    assert isinstance(transport, eg.AsyncGuardedTransport)
    inner = transport._inner
    assert isinstance(inner, httpx.AsyncHTTPTransport)  # noqa: TID251 - type check, no client
    assert inner._pool._retries == 0
    ctx = inner._pool._ssl_context
    assert ctx is not None
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2
    asyncio.run(client.aclose())


def test_ut10_52_proxy_comes_from_config(tmp_path: Path) -> None:
    """UT10-52 security.network.http_proxy is the proxy of both clients."""
    guard = _hybrid(tmp_path, http_proxy="http://proxy.corp.example:3128")
    sync_pool = guard.http_client("reasoning_final", "aggregated_evidence")._transport._inner._pool  # type: ignore[attr-defined]
    async_client = guard.async_http_client("reasoning_final", "aggregated_evidence")
    async_pool = async_client._transport._inner._pool  # type: ignore[attr-defined]
    assert isinstance(sync_pool, httpcore.HTTPProxy)
    assert isinstance(async_pool, httpcore.AsyncHTTPProxy)
    for pool in (sync_pool, async_pool):
        assert pool._proxy_url.host == b"proxy.corp.example"
        assert pool._proxy_url.port == 3128


@pytest.mark.parametrize("timeout", [0.0, -1.0, 3_600.5, float("nan")])
def test_ut10_52_timeout_bounds(tmp_path: Path, timeout: float) -> None:
    """UT10-52 a timeout outside (0, 3600] is a ConfigError for both clients."""
    guard = _hybrid(tmp_path)
    for factory in (guard.http_client, guard.async_http_client):
        with pytest.raises(ConfigError):
            factory("reasoning_final", "aggregated_evidence", timeout=timeout)
    client = guard.http_client("reasoning_final", "aggregated_evidence", timeout=3_600.0)
    assert client.timeout.read == 3_600.0


def test_ut10_52_model_download_is_never_a_client(tmp_path: Path) -> None:
    """UT10-52 purpose model_download -> EgressBlocked("model_download only inside deploy pull")."""
    guard = _hybrid(tmp_path)
    for factory in (guard.http_client, guard.async_http_client):
        with pytest.raises(EgressBlocked, match="model_download only inside deploy pull"):
            factory("model_download", "none")


@pytest.mark.parametrize("estimate", [True, 2.5, 1e9])
def test_ut10_52_non_int_estimate_uses_body_length(tmp_path: Path, estimate: object) -> None:
    """UT10-52 a bool or float token_estimate is ignored: the body length decides (T10-16 N2)."""
    guard = _hybrid(tmp_path)
    guard.check(API, b"x" * 35, "reasoning_final", "aggregated_evidence", estimate)  # type: ignore[arg-type]
    (line,) = egress_lines(guard._cfg.paths.logs)
    assert line["tokens_in"] == 10


# --- UT10-54 guarded transport lines (U10-54, U10-57) ---------------------------------------


def _completed_pair(logs: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    allowed, completed = egress_lines(logs)
    assert (allowed["decision"], completed["decision"]) == ("allowed", "completed")
    assert allowed["egress_id"] == completed["egress_id"]
    return allowed, completed


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        ({"input_tokens": 11, "output_tokens": 7}, (11, 7)),
        ({"prompt_tokens": 13, "completion_tokens": 5}, (13, 5)),
    ],
)
def test_ut10_54_allowed_then_completed_with_provider_tokens(
    tmp_path: Path, usage: dict[str, int], expected: tuple[int, int]
) -> None:
    """UT10-54 respx 200 JSON usage: allowed then completed, one egress_id, provider tokens."""
    guard = _hybrid(tmp_path)
    body = {"content": [{"type": "text", "text": "fine"}], "usage": usage}
    with respx.mock(assert_all_called=True) as mock:
        mock.post(API).respond(200, json=body)
        client = guard.http_client(
            "reasoning_final", "aggregated_evidence", run_id="run_1", task_id="tsk_1"
        )
        with client:
            response = client.post(API, content=EVIDENCE)
    assert response.json() == body
    allowed, completed = _completed_pair(guard._cfg.paths.logs)
    assert (completed["tokens_in"], completed["tokens_out"]) == expected
    assert completed["status_code"] == 200
    assert completed["bytes_in"] == len(response.content)
    assert isinstance(completed["latency_ms"], int)
    assert completed["latency_ms"] >= 0
    assert completed["reason"] is None
    for key in ("purpose", "payload_class", "destination", "method", "path", "run_id", "task_id"):
        assert completed[key] == allowed[key], key
    assert (completed["run_id"], completed["task_id"]) == ("run_1", "tsk_1")


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="plain text"),
        httpx.Response(200, json={"usage": {"input_tokens": True, "output_tokens": -1}}),
        httpx.Response(200, json={"usage": "none"}),
        httpx.Response(200, content=b"{not json", headers={"content-type": "application/json"}),
    ],
)
def test_ut10_54_without_provider_usage_the_estimate_stands(
    tmp_path: Path, response: httpx.Response
) -> None:
    """UT10-54 no usable usage block: tokens_in is the estimate, tokens_out null."""
    guard = _hybrid(tmp_path)
    with respx.mock() as mock:
        mock.post(API).mock(return_value=response)
        with guard.http_client("reasoning_final", "aggregated_evidence") as client:
            client.post(API, content=EVIDENCE)
    allowed, completed = _completed_pair(guard._cfg.paths.logs)
    assert completed["tokens_in"] == allowed["tokens_in"]
    assert completed["tokens_out"] is None


def test_ut10_54_large_json_is_not_parsed(tmp_path: Path) -> None:
    """UT10-54 a JSON body over 10 MiB is counted but not read for usage."""
    guard = _hybrid(tmp_path)
    pad = "x" * et.USAGE_MAX_BYTES
    payload = json.dumps({"pad": pad, "usage": {"input_tokens": 1, "output_tokens": 1}})
    with respx.mock() as mock:
        mock.post(API).respond(200, content=payload, headers={"content-type": "application/json"})
        with guard.http_client("reasoning_final", "aggregated_evidence") as client:
            client.post(API, content=EVIDENCE)
    allowed, completed = _completed_pair(guard._cfg.paths.logs)
    assert completed["bytes_in"] == len(payload)
    assert (completed["tokens_in"], completed["tokens_out"]) == (allowed["tokens_in"], None)


def test_ut10_54_inner_failure_writes_completed_and_reraises(tmp_path: Path) -> None:
    """UT10-54 an inner exception: completed with status_code null and the class, re-raised."""
    guard = _hybrid(tmp_path)
    with respx.mock() as mock:
        mock.post(API).mock(side_effect=httpx.ConnectError("refused"))
        with (
            guard.http_client("reasoning_final", "aggregated_evidence") as client,
            pytest.raises(httpx.ConnectError),
        ):
            client.post(API, content=EVIDENCE)
    _allowed, completed = _completed_pair(guard._cfg.paths.logs)
    assert (completed["status_code"], completed["reason"]) == (None, "ConnectError")
    assert completed["bytes_in"] == 0


def test_ut10_54_async_client_lines(tmp_path: Path) -> None:
    """UT10-54 the async client writes the same allowed and completed pair."""
    guard = _hybrid(tmp_path)

    async def send() -> None:
        async with guard.async_http_client("reasoning_final", "aggregated_evidence") as client:
            await client.post(API, content=EVIDENCE)

    with respx.mock() as mock:
        mock.post(API).respond(200, json={"usage": {"input_tokens": 3, "output_tokens": 4}})
        asyncio.run(send())
    _allowed, completed = _completed_pair(guard._cfg.paths.logs)
    assert (completed["tokens_in"], completed["tokens_out"]) == (3, 4)


def test_ut10_54_async_inner_failure(tmp_path: Path) -> None:
    """UT10-54 async inner exception: completed with the class, re-raised."""
    guard = _hybrid(tmp_path)

    async def send() -> None:
        async with guard.async_http_client("reasoning_final", "aggregated_evidence") as client:
            await client.post(API, content=EVIDENCE)

    with respx.mock() as mock:
        mock.post(API).mock(side_effect=httpx.ReadTimeout("slow"))
        with pytest.raises(httpx.ReadTimeout):
            asyncio.run(send())
    _allowed, completed = _completed_pair(guard._cfg.paths.logs)
    assert completed["reason"] == "ReadTimeout"


def test_ut10_54_completed_log_failure_is_reported_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-54 a failing completed-line write does not mask the response."""
    guard = _hybrid(tmp_path)
    real_write = guard._log.write

    def write(line: dict[str, Any]) -> None:
        if line["decision"] == "completed":
            msg = "disk full"
            raise OSError(msg)
        real_write(line)

    monkeypatch.setattr(guard._log, "write", write)
    with respx.mock() as mock:
        mock.post(API).respond(200, json={})
        with guard.http_client("reasoning_final", "aggregated_evidence") as client:
            assert client.post(API, content=EVIDENCE).status_code == 200
    assert [ln["decision"] for ln in egress_lines(guard._cfg.paths.logs)] == ["allowed"]


# --- UT10-55 download window -----------------------------------------------------------------


def test_ut10_55_window_preconditions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT10-55 allow_download false, HERNESS_WORKER=1, profile synth: EgressBlocked x3."""
    guard = _hybrid(tmp_path)
    with (
        pytest.raises(EgressBlocked, match="deploy pull requires --allow-download"),
        guard.download_window(allow_download=False, actor="system"),
    ):
        pass
    monkeypatch.setenv("HERNESS_WORKER", "1")
    with (
        pytest.raises(EgressBlocked, match="model_download is refused inside jobs"),
        guard.download_window(allow_download=True, actor="system"),
    ):
        pass
    monkeypatch.delenv("HERNESS_WORKER")
    synth = make_guard(guard._cfg.model_copy(update={"profile": "synth"}))
    with (
        pytest.raises(EgressBlocked, match="profile synth cannot download"),
        synth.download_window(allow_download=True, actor="system"),
    ):
        pass
    assert audit_fields(guard._cfg.paths.logs, "admin_action") == []


def test_ut10_55_valid_window_allows_download_and_audits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-55 a valid window admits huggingface.co model_download and audits deploy_pull."""
    monkeypatch.setenv("HERNESS_WORKER", "0")
    guard = _hybrid(tmp_path)
    assert _reason(guard.check, HF, b"", "model_download", "none") == "purpose_not_allowed"
    with guard.download_window(allow_download=True, actor="system"):
        guard.check(HF, b"", "model_download", "none")
        seen: list[bool] = []
        other = threading.Thread(target=lambda: seen.append(guard._window_open()))
        other.start()
        other.join()
        assert seen == [False]  # the flag is local to the opening thread
    assert _reason(guard.check, HF, b"", "model_download", "none") == "purpose_not_allowed"
    assert audit_fields(guard._cfg.paths.logs, "admin_action") == [
        {"action": "deploy_pull", "target": "download_window"}
    ]
    decisions = [(ln["decision"], ln["reason"]) for ln in egress_lines(guard._cfg.paths.logs)]
    assert decisions == [
        ("blocked", "purpose_not_allowed"),
        ("allowed", None),
        ("blocked", "purpose_not_allowed"),
    ]


def test_ut10_55_flag_cleared_on_exception(tmp_path: Path) -> None:
    """UT10-55 the window flag is cleared on exit, including on exception."""
    guard = _hybrid(tmp_path)
    seen: list[bool] = []

    def fail_inside() -> None:
        with guard.download_window(allow_download=True, actor="system"):
            seen.append(guard._window_open())
            raise RuntimeError

    with pytest.raises(RuntimeError):
        fail_inside()
    assert seen == [True]
    assert not guard._window_open()


# --- UT10-74 loopback clients (U10-59) -------------------------------------------------------


def test_ut10_74_loopback_clients_are_built_and_reach_the_transport() -> None:
    """UT10-74 both variants: no redirects, no env, loopback transport; /health reaches it."""
    base = "http://127.0.0.1:8000"
    sync_client = eg.loopback_http_client(base, timeout_s=5.0)
    async_client = eg.aloopback_http_client(base, timeout_s=5.0)
    for client in (sync_client, async_client):
        assert (client.follow_redirects, client.trust_env) == (False, False)
        assert client.timeout == httpx.Timeout(5.0, connect=5.0)
        assert str(client.base_url).rstrip("/") == base
    assert isinstance(sync_client._transport, ec.LoopbackOnlyTransport)
    assert isinstance(async_client._transport, ec.AsyncLoopbackOnlyTransport)
    assert sync_client._transport._inner._pool._retries == 0  # type: ignore[attr-defined]

    async def health() -> int:
        async with async_client:
            return (await async_client.get("/health")).status_code

    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(f"{base}/health").respond(200, text="ok")
        with sync_client:
            assert sync_client.get("/health").status_code == 200
        assert asyncio.run(health()) == 200
    assert route.call_count == 2


def test_ut10_74_short_timeout_caps_connect() -> None:
    """UT10-74 timeout=Timeout(timeout_s, connect=min(timeout_s, 5.0))."""
    assert eg.loopback_http_client("http://localhost:1", timeout_s=60.0).timeout == httpx.Timeout(
        60.0, connect=5.0
    )
    assert eg.loopback_http_client("https://[::1]:1", timeout_s=2.0).timeout == httpx.Timeout(2.0)


@pytest.mark.parametrize("factory", [eg.loopback_http_client, eg.aloopback_http_client])
def test_ut10_74_non_loopback_and_bad_timeout(
    factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-74 10.0.0.5: not_loopback before any client exists; timeout_s=0: ConfigError."""
    built: list[int] = []
    for cls in (httpx.Client, httpx.AsyncClient):  # noqa: TID251 - spies, nothing is built
        monkeypatch.setattr(cls, "__init__", lambda *_a, **_k: built.append(1))
    with pytest.raises(EgressBlocked, match=r"loopback client used for 10\.0\.0\.5") as info:
        factory("http://10.0.0.5:8000", timeout_s=5.0)
    assert info.value.reason == "not_loopback"
    for base in ("ftp://127.0.0.1:21", "not a url", "http://[bad", ""):
        assert _reason(factory, base, timeout_s=5.0) == "not_loopback"
    with pytest.raises(ConfigError):
        factory("http://127.0.0.1:8000", timeout_s=0)
    assert built == []


def test_ut10_74_bearer_header_is_set() -> None:
    """UT10-74 bearer sets the default Authorization header."""
    value = "-".join(("loop", "bearer", "value"))
    client = eg.loopback_http_client(
        "http://127.0.0.1:8000", timeout_s=5.0, bearer=SecretStr(value)
    )
    assert client.headers["Authorization"] == f"Bearer {value}"
    assert "Authorization" not in eg.loopback_http_client("http://127.0.0.1:1", timeout_s=1).headers
