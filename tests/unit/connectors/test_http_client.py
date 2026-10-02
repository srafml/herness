"""Tests for herness.connectors.http.http_client and SourceHttp.check_next_url
(impl 01 U01-58, U01-62; T01-14; TH01-01, TH01-02, TH01-15).

The fake ``source_http_client`` records its arguments; the host-guard, redirect and TLS rows
use the real egress factory (``MockNet`` below it, or a real local TLS server).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
from pydantic import ValidationError
from tests.support.egress_mock import MockNet
from tests.support.egress_servers import self_signed, tls_server
from tests.support.fake_keyring import MemoryKeyring
from tests.support.ops_store import OpsStoreHandle
from tests.unit.connectors._http_data import (
    BASE,
    SyntheticBearer,
    bind_resilience,
    load_sources,
    mock_client,
    reply,
)
from tests.unit.connectors._settings_data import adapter, servicenow

import herness.connectors.base as base_module
from herness.connectors import http
from herness.connectors.http import (
    MAX_RESPONSE_BYTES,
    ForeignHostError,
    SourceHttp,
    http_client,
)
from herness.connectors.settings import ServiceNowSettings
from herness.connectors.settings_entities import MonitoringAdapterSettings
from herness.core import config as c
from herness.core import egress
from herness.core.errors import ConfigError, EgressBlocked, SchemaViolation, SourceUnavailable
from herness.core.resilience import ProcessState

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolate(
    fake_keyring: MemoryKeyring, ops_store: OpsStoreHandle, reset_process_state: ProcessState
) -> Iterator[None]:
    bind_resilience()
    yield
    c.reset_config()


@pytest.fixture
def factory_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
    """Replace the egress source factory with a recorder; loopback_http_client must not run."""
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def fake_source(*args: Any, **kwargs: Any) -> httpx2.Client:
        calls.append((args, kwargs))
        return mock_client(lambda _r: reply(), base_url=args[1])

    def no_loopback(*_a: Any, **_k: Any) -> httpx2.Client:
        msg = "loopback_http_client is for local model servers only"
        raise AssertionError(msg)

    monkeypatch.setattr(egress, "source_http_client", fake_source)
    monkeypatch.setattr(egress, "loopback_http_client", no_loopback)
    return calls


# --- UT01-63: arguments handed to the egress factory ----------------------------------


def test_ut01_63_servicenow_https_arguments(
    factory_calls: list[tuple[tuple[Any, ...], dict[str, Any]]],
) -> None:
    """UT01-63 an https source: connector name, base_url, verify True, timeout, pool size
    min(2 x max_concurrency, 64) and max_response_bytes reach source_http_client."""
    settings = ServiceNowSettings.model_validate(servicenow(base_url=BASE, timeout_s=30))
    with http_client(settings, source="servicenow", max_concurrency=3) as client:
        assert isinstance(client, httpx2.Client)
    assert factory_calls == [
        (
            ("servicenow", BASE),
            {
                "timeout_s": 30.0,
                "verify": True,
                "max_connections": 6,
                "max_response_bytes": MAX_RESPONSE_BYTES,
            },
        )
    ]


def test_ut01_63_monitoring_loopback_arguments(
    factory_calls: list[tuple[tuple[Any, ...], dict[str, Any]]], tmp_path: Path
) -> None:
    """UT01-63 stream key `monitoring:prometheus` gives the connector name `monitoring`; a
    loopback http base_url uses the same factory; a CA bundle path is passed as a Path; the
    pool size is capped at 64."""
    bundle = tmp_path / "ca.pem"
    bundle.write_text("synthetic bundle\n", encoding="utf-8")
    raw = adapter("prometheus", base_url="http://127.0.0.1:9090", verify=str(bundle))
    settings = MonitoringAdapterSettings.model_validate(raw)
    http_client(settings, source="monitoring:prometheus", max_concurrency=40).close()
    (args, kwargs) = factory_calls[0]
    assert args == ("monitoring", "http://127.0.0.1:9090")
    assert kwargs["verify"] == bundle
    assert isinstance(kwargs["verify"], Path)
    assert kwargs["max_connections"] == 64
    assert kwargs["max_response_bytes"] == MAX_RESPONSE_BYTES
    assert kwargs["timeout_s"] == settings.timeout_s


def test_ut01_63_missing_base_url_is_config_error(
    factory_calls: list[tuple[tuple[Any, ...], dict[str, Any]]],
) -> None:
    """UT01-63 a source without base_url is refused before the factory is called."""
    settings = ServiceNowSettings.model_validate(servicenow()).model_copy(update={"base_url": None})
    with pytest.raises(ConfigError, match="base_url"):
        http_client(settings, source="servicenow", max_concurrency=1)
    assert factory_calls == []


def test_ut01_63_base_reexports_http_client() -> None:
    """UT01-63 `herness.connectors.base.http_client` is the same function (module map)."""
    assert base_module.http_client is http.http_client


# --- UT01-63: the real egress-built client refuses other hosts and redirects ----------


@pytest.fixture
def net(monkeypatch: pytest.MonkeyPatch) -> MockNet:
    return MockNet().install(monkeypatch)


def _source(tmp_path: Path) -> SourceHttp:
    settings = load_sources(tmp_path)
    client = http_client(settings, source="servicenow", max_concurrency=1)
    return SourceHttp(client, breaker_key="servicenow", auth=SyntheticBearer())


def test_ut01_63_request_to_other_host_is_egress_blocked(tmp_path: Path, net: MockNet) -> None:
    """UT01-63 a request to a host other than base_url's is refused with EgressBlocked
    before any request leaves (not retried: it is fatal)."""
    evil = net.route("evil.example", json={})
    with pytest.raises(EgressBlocked):
        _source(tmp_path).get_json("https://evil.example/api/now/table/incident")
    assert evil.call_count == 0


def test_ut01_63_redirect_is_not_followed(tmp_path: Path, net: MockNet) -> None:
    """UT01-63 a 302 to another host is not followed: SchemaViolation("unexpected
    redirect"), and the other host never sees a request."""
    net.route("sn.example", status=302, headers={"Location": "https://evil.example/x"})
    evil = net.route("evil.example", json={})
    with pytest.raises(SchemaViolation, match="unexpected redirect"):
        _source(tmp_path).get_json("/api/now/table/incident")
    assert evil.call_count == 0


def test_ut01_63_check_next_url_to_other_host(tmp_path: Path, net: MockNet) -> None:
    """UT01-63 check_next_url to another host raises ForeignHostError carrying the host."""
    del net
    with pytest.raises(ForeignHostError) as info:
        _source(tmp_path).check_next_url("https://evil.example/api/now/table/incident?p=2")
    assert info.value.host == "evil.example"


@pytest.mark.parametrize(
    "url",
    [
        "/api/now/table/incident?p=2",  # relative
        "http://sn.example/api/x",  # not https
        "https://user@sn.example/api/x",  # user info
        "https://sn.example.evil.example/api/x",  # suffix trick
        "https://sn.example:8443/api/x",  # same host, other port
        "not a url at all\x00",
    ],
)
def test_ut01_63_check_next_url_refusals(url: str) -> None:
    """UT01-63 a next URL that is not absolute https on the base_url host is refused."""
    source = SourceHttp(mock_client(lambda _r: reply()), breaker_key="servicenow", auth=None)
    with pytest.raises(ForeignHostError):
        source.check_next_url(url)


def test_ut01_63_check_next_url_accepts_own_host() -> None:
    """UT01-63 an absolute https URL on the base_url host (any case) is returned as given;
    on a loopback http base_url, http next links on that host are accepted too."""
    source = SourceHttp(mock_client(lambda _r: reply()), breaker_key="servicenow", auth=None)
    url = "https://SN.example/api/now/table/incident?sysparm_offset=100"
    assert source.check_next_url(url) == url
    local = SourceHttp(
        mock_client(lambda _r: reply(), base_url="http://127.0.0.1:9090"),
        breaker_key="monitoring:prometheus",
        auth=None,
    )
    assert (
        local.check_next_url("http://127.0.0.1:9090/api/v1/x") == "http://127.0.0.1:9090/api/v1/x"
    )


# --- ST01-01: TLS verification cannot be turned off -----------------------------------


@pytest.mark.parametrize("value", [False, "false", "/nonexistent/ca.pem"])
def test_st01_01_verify_false_rejected_by_config(value: object) -> None:
    """ST01-01 `verify: false` (or anything but an existing CA bundle) is rejected."""
    with pytest.raises(ValidationError, match="TLS verification cannot be disabled"):
        ServiceNowSettings.model_validate(servicenow(verify=value))


def test_st01_01_self_signed_server_is_source_unavailable(tmp_path: Path) -> None:
    """ST01-01 a real local TLS server with a self-signed certificate: the connection fails
    certificate verification and surfaces as SourceUnavailable (classified TLS error)."""
    cert, key = self_signed(tmp_path)
    with tls_server(cert, key) as port:
        settings = load_sources(tmp_path / "cfg", f"https://127.0.0.1:{port}")
        client = http_client(settings, source="servicenow", max_concurrency=1)
        with client, pytest.raises(SourceUnavailable) as info:
            SourceHttp(client, breaker_key="servicenow", auth=None).get_json("/api/x")
    assert isinstance(info.value.__cause__, httpx2.ConnectError)


def test_st01_01_ca_bundle_path_trusts_the_server(tmp_path: Path) -> None:
    """ST01-01 control: with `verify: <ca bundle>` naming that certificate the handshake
    succeeds (the stub answers `ok`, which is not JSON: SchemaViolation, not a TLS error)."""
    cert, key = self_signed(tmp_path)
    with tls_server(cert, key) as port:
        settings = load_sources(tmp_path / "cfg", f"https://127.0.0.1:{port}", verify=cert)
        with (
            http_client(settings, source="servicenow", max_concurrency=1) as client,
            pytest.raises(SchemaViolation, match="malformed JSON"),
        ):
            SourceHttp(client, breaker_key="servicenow", auth=None).get_json("/api/x")
