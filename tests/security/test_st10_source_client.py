"""Security tests for the source client (impl 10 ST10-58, ST10-59; card T10-33).

ST10-58: a source client refuses every out-of-allowlist request (absolute URL, response-
supplied next link, redirect, http scheme, user info, an SDK source whose base_url host is
not listed) before the inner transport is called; zero connects to any other host. ST10-59:
TLS verification cannot be disabled, a missing CA bundle is a ``ConfigError``, an untrusted
self-signed certificate fails verification, and ``SSL_CERT_FILE``/``HTTPS_PROXY`` are ignored
(``trust_env=False``).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx2
import pytest
from tests.support.config_tree import write_full_config
from tests.support.egress_mock import MockNet
from tests.support.egress_servers import record_connects, self_signed, tls_server
from tests.support.fake_keyring import MemoryKeyring

from herness.core import config as c
from herness.core import egress_clients as ec
from herness.core.errors import ConfigError, EgressBlocked

pytestmark = pytest.mark.unit

SOURCES_YAML = """\
version: 1
sources:
  servicenow:
    enabled: true
    base_url: https://corp.service-now.com
    hosts: [sso.example.com]
    auth: {method: basic, credentials: "secret:sn"}
    entities:
      incident:
        fields: [number]
  snowflake:
    enabled: true
    account: acme
    warehouse: WH
    role: ROLE
    hosts: [acme.snowflakecomputing.com]
    auth: {method: key_pair, credentials: "secret:sf"}
    entities:
      orders:
        table: db.schema.tbl
        key_field: id
        updated_field: updated_at
        columns: [id, updated_at]
"""


@pytest.fixture(autouse=True)
def _isolate(fake_keyring: MemoryKeyring) -> Iterator[None]:
    yield
    c.reset_config()


@pytest.fixture
def net(monkeypatch: pytest.MonkeyPatch) -> MockNet:
    return MockNet().install(monkeypatch)


@pytest.fixture
def connects(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    return record_connects(monkeypatch)


def _load(tmp_path: Path, sources_yaml: str = SOURCES_YAML) -> c.HernessConfig:
    cfg_dir = write_full_config(tmp_path)
    (cfg_dir / "sources.yaml").write_text(sources_yaml, encoding="utf-8")
    return c.init_config("hybrid", config_dir=cfg_dir, env={})


def _servicenow(tmp_path: Path, **kw: object) -> httpx2.Client:
    _load(tmp_path)
    return ec.source_http_client(
        "servicenow",
        "https://corp.service-now.com",
        timeout_s=30.0,
        **kw,  # type: ignore[arg-type]
    )


# --- ST10-58: TH10-51, every request stays inside both allowlists ----------------------------


def test_st10_58_evil_host_via_get_is_blocked(tmp_path: Path, connects: list[object]) -> None:
    """ST10-58 an absolute URL to an unrelated host is refused; zero connects."""
    with _servicenow(tmp_path) as client, pytest.raises(EgressBlocked) as info:
        client.get("https://evil.example.com/api")
    assert info.value.reason == "host_not_allowed"
    assert connects == []


def test_st10_58_response_supplied_next_link_is_blocked(
    tmp_path: Path, connects: list[object]
) -> None:
    """ST10-58 a look-alike subdomain next link is refused the same way; zero connects."""
    with _servicenow(tmp_path) as client, pytest.raises(EgressBlocked) as info:
        client.get("https://corp.service-now.com.evil.com/")
    assert info.value.reason == "host_not_allowed"
    assert connects == []


def test_st10_58_redirect_to_another_host_is_not_followed(tmp_path: Path, net: MockNet) -> None:
    """ST10-58 a 302 to another host is returned as-is; the other host is never reached."""
    net.route("corp.service-now.com", status=302, headers={"Location": "https://evil.com/steal"})
    evil = net.route("evil.com")
    with _servicenow(tmp_path) as client:
        response = client.get("/x")
    assert response.status_code == 302
    assert evil.call_count == 0


def test_st10_58_http_scheme_is_blocked(tmp_path: Path, connects: list[object]) -> None:
    """ST10-58 a plain-http base_url (non-loopback) is refused at construction."""
    _load(tmp_path)
    with pytest.raises(EgressBlocked) as info:
        ec.source_http_client("servicenow", "http://corp.service-now.com", timeout_s=30.0)
    assert info.value.reason == "scheme_not_https"
    assert connects == []


def test_st10_58_userinfo_is_blocked(tmp_path: Path, connects: list[object]) -> None:
    """ST10-58 user info in base_url is refused at construction."""
    _load(tmp_path)
    with pytest.raises(EgressBlocked) as info:
        ec.source_http_client("servicenow", "https://u:p@corp.service-now.com", timeout_s=30.0)
    assert info.value.reason == "userinfo_present"
    assert connects == []


def test_st10_58_snowflake_base_url_host_not_in_hosts_is_blocked(
    tmp_path: Path, connects: list[object]
) -> None:
    """ST10-58 an SDK client whose base_url host is not in its own ``hosts`` is refused."""
    _load(tmp_path)
    with pytest.raises(EgressBlocked) as info:
        ec.source_http_client(
            "snowflake", "https://notlisted.snowflakecomputing.com", timeout_s=30.0
        )
    assert info.value.reason == "host_not_allowed"
    assert connects == []


def test_st10_58_hosts_widens_only_the_socket_guard_never_the_client(
    tmp_path: Path, connects: list[object]
) -> None:
    """ST10-58/UT01-97 a servicenow ``hosts: [sso.example.com]`` still refuses that host."""
    with _servicenow(tmp_path) as client, pytest.raises(EgressBlocked) as info:
        client.get("https://sso.example.com/oauth/token")
    assert info.value.reason == "host_not_allowed"
    assert connects == []


# --- ST10-59: TH10-51, TH10-18, TLS cannot be weakened -----------------------------------------


def test_st10_59_verify_false_is_config_error(tmp_path: Path) -> None:
    """ST10-59 verify=False: ConfigError, TLS verification cannot be disabled."""
    _load(tmp_path)
    with pytest.raises(ConfigError, match="TLS verification cannot be disabled"):
        ec.source_http_client(
            "servicenow",
            "https://corp.service-now.com",
            timeout_s=30.0,
            verify=False,  # type: ignore[arg-type]
        )


def test_st10_59_missing_ca_bundle_is_config_error(tmp_path: Path) -> None:
    """ST10-59 a CA bundle path that does not exist is a ConfigError."""
    _load(tmp_path)
    with pytest.raises(ConfigError):
        ec.source_http_client(
            "servicenow",
            "https://corp.service-now.com",
            timeout_s=30.0,
            verify=tmp_path / "missing-ca.pem",
        )


def test_st10_59_self_signed_certificate_is_refused(tmp_path: Path) -> None:
    """ST10-59 a self-signed certificate not in the CA set: verification error, no body sent."""
    _load(tmp_path)
    cert, key = self_signed(tmp_path)
    with (
        tls_server(cert, key) as port,
        ec.source_http_client("servicenow", f"https://127.0.0.1:{port}/", timeout_s=5.0) as client,
        pytest.raises(httpx2.ConnectError, match="CERTIFICATE"),
    ):
        client.get("/")


def test_st10_59_env_ssl_cert_file_and_https_proxy_are_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST10-59 our own verify= path is used; SSL_CERT_FILE/HTTPS_PROXY are ignored."""
    _load(tmp_path)
    cert, key = self_signed(tmp_path)
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "attacker-ca.pem"))
    for var in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy"):
        monkeypatch.setenv(var, "http://127.0.0.1:9")  # nothing listens: using it would fail
    with (
        tls_server(cert, key) as port,
        ec.source_http_client(
            "servicenow", f"https://127.0.0.1:{port}/", timeout_s=5.0, verify=cert
        ) as client,
    ):
        response = client.get("/")
    assert (response.status_code, response.text) == (200, "ok")
