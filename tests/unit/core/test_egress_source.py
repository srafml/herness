"""Tests for the source client factory (impl 10 U10-110; card T10-33).

UT10-82: ``source_http_client`` builds an ``httpx2.Client`` restricted to one source's hosts,
with the client-configuration checks of U10-110 (timeouts, limits, disabled/unknown sources,
the response-too-large cap). No socket is opened: ``MockNet`` replaces the ``httpx2`` pool
transport (respx patches only ``httpx``).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx2
import pytest
from tests.support.config_tree import write_full_config
from tests.support.egress_mock import MockNet
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
    hosts: []
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
  jira:
    enabled: false
    flavor: cloud
    base_url: https://acme.atlassian.net
    auth: {method: api_token, credentials: "secret:jira"}
"""


@pytest.fixture(autouse=True)
def _isolate(fake_keyring: MemoryKeyring) -> Iterator[None]:
    yield
    c.reset_config()


@pytest.fixture
def net(monkeypatch: pytest.MonkeyPatch) -> MockNet:
    return MockNet().install(monkeypatch)


def _load(tmp_path: Path, sources_yaml: str = SOURCES_YAML) -> c.HernessConfig:
    cfg_dir = write_full_config(tmp_path)
    (cfg_dir / "sources.yaml").write_text(sources_yaml, encoding="utf-8")
    return c.init_config("hybrid", config_dir=cfg_dir, env={})


def test_ut10_82_servicenow_client_configuration(tmp_path: Path) -> None:
    """UT10-82 a servicenow client: no redirects, no env trust, the given timeout and limits."""
    _load(tmp_path)
    client = ec.source_http_client(
        "servicenow", "https://corp.service-now.com", timeout_s=30.0, max_connections=7
    )
    try:
        assert isinstance(client, httpx2.Client)
        assert (client.follow_redirects, client.trust_env) == (False, False)
        assert client.timeout == httpx2.Timeout(30.0, connect=10.0)
        transport = client._transport
        assert isinstance(transport, ec.SourceHostTransport)
        assert isinstance(transport._inner, httpx2.HTTPTransport)  # noqa: TID251 - type check
        pool = transport._inner._pool
        assert pool._retries == 0
    finally:
        client.close()


def test_ut10_82_snowflake_client_is_allowed(tmp_path: Path) -> None:
    """UT10-82 an SDK source client is built when its base_url host is in ``hosts``."""
    _load(tmp_path)
    with ec.source_http_client(
        "snowflake", "https://acme.snowflakecomputing.com", timeout_s=30.0
    ) as client:
        assert isinstance(client, httpx2.Client)


def test_ut10_82_disabled_source_is_config_error(tmp_path: Path) -> None:
    """UT10-82 a source with ``enabled: false`` is refused before any client is built."""
    _load(tmp_path)
    with pytest.raises(ConfigError, match="disabled source jira"):
        ec.source_http_client("jira", "https://acme.atlassian.net", timeout_s=30.0)


def test_ut10_82_unknown_source_is_config_error(tmp_path: Path) -> None:
    """UT10-82 a source name that is not a ``sources.yaml`` key is a ``ConfigError`` too."""
    _load(tmp_path)
    with pytest.raises(ConfigError, match="unknown or disabled source bogus"):
        ec.source_http_client("bogus", "https://x.example.com", timeout_s=30.0)


@pytest.mark.parametrize("timeout_s", [0.0, -1.0, 601.0, float("nan")])
def test_ut10_82_timeout_bounds(tmp_path: Path, timeout_s: float) -> None:
    """UT10-82 timeout_s outside (0, 600] is a ConfigError (over the loopback/guarded 3600 cap)."""
    _load(tmp_path)
    with pytest.raises(ConfigError):
        ec.source_http_client("servicenow", "https://corp.service-now.com", timeout_s=timeout_s)


@pytest.mark.parametrize("max_connections", [0, -1, 65, 4.0, True])
def test_ut10_82_max_connections_bounds(tmp_path: Path, max_connections: object) -> None:
    """UT10-82 max_connections must be an int in [1, 64]."""
    _load(tmp_path)
    with pytest.raises(ConfigError):
        ec.source_http_client(
            "servicenow",
            "https://corp.service-now.com",
            timeout_s=30.0,
            max_connections=max_connections,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("verify", [False, "not-a-path", 1])
def test_ut10_82_verify_cannot_be_disabled(tmp_path: Path, verify: object) -> None:
    """UT10-82 verify anything but True or an existing file path: ConfigError."""
    _load(tmp_path)
    with pytest.raises(ConfigError, match="TLS verification cannot be disabled"):
        ec.source_http_client(
            "servicenow",
            "https://corp.service-now.com",
            timeout_s=30.0,
            verify=verify,  # type: ignore[arg-type]
        )


def test_ut10_82_verify_missing_ca_bundle_is_config_error(tmp_path: Path) -> None:
    """UT10-82 a CA bundle path that does not exist: the same verbatim message as verify=False.

    Fix round 1 (review Important #1): U10-110's Preconditions row is unconditional about the
    message text for "any other value" than True or an existing file, so a missing path must
    not get a different wording from ``verify=False``.
    """
    _load(tmp_path)
    with pytest.raises(ConfigError, match="TLS verification cannot be disabled"):
        ec.source_http_client(
            "servicenow",
            "https://corp.service-now.com",
            timeout_s=30.0,
            verify=tmp_path / "no-such-ca.pem",
        )


def test_ut10_82_response_too_large_is_blocked(tmp_path: Path, net: MockNet) -> None:
    """UT10-82 a response over max_response_bytes raises EgressBlocked(response_too_large)."""
    _load(tmp_path)
    net.route("corp.service-now.com", content=b"x" * 11)
    with (
        ec.source_http_client(
            "servicenow", "https://corp.service-now.com", timeout_s=30.0, max_response_bytes=10
        ) as client,
        pytest.raises(EgressBlocked) as info,
    ):
        client.get("/api/now/table/incident")
    assert info.value.reason == "response_too_large"


def test_ut10_82_response_at_the_limit_is_allowed(tmp_path: Path, net: MockNet) -> None:
    """UT10-82 a response exactly at max_response_bytes is allowed."""
    _load(tmp_path)
    net.route("corp.service-now.com", content=b"x" * 10)
    with ec.source_http_client(
        "servicenow", "https://corp.service-now.com", timeout_s=30.0, max_response_bytes=10
    ) as client:
        response = client.get("/api/now/table/incident")
    assert response.read() == b"x" * 10


# --- fix round 1 (review): inclusive bounds succeed; Accept-Encoding identity is fail-closed --


def test_ut10_82_inclusive_bounds_succeed(tmp_path: Path) -> None:
    """UT10-82 the inclusive edges of every bound build a client (review Minor #2)."""
    _load(tmp_path)
    with ec.source_http_client(
        "servicenow",
        "https://corp.service-now.com",
        timeout_s=600.0,
        max_connections=64,
        max_response_bytes=1_073_741_824,
    ) as client:
        assert client.timeout == httpx2.Timeout(600.0, connect=10.0)
    with ec.source_http_client(
        "servicenow",
        "https://corp.service-now.com",
        timeout_s=1.0,
        max_connections=1,
        max_response_bytes=1,
    ) as client:
        assert isinstance(client, httpx2.Client)


def test_ut10_82_request_carries_accept_encoding_identity(tmp_path: Path, net: MockNet) -> None:
    """UT10-82 every outgoing request forces Accept-Encoding: identity (T10-17 gzip-bomb fix)."""
    _load(tmp_path)
    route = net.route("corp.service-now.com", json={})
    with ec.source_http_client("servicenow", "https://corp.service-now.com", timeout_s=30.0) as (
        client
    ):
        client.get("/api/now/table/incident", headers={"Accept-Encoding": "gzip"})
    assert route.calls[0].headers["accept-encoding"] == "identity"


def test_ut10_82_unsupported_encoding_is_blocked(tmp_path: Path, net: MockNet) -> None:
    """UT10-82 a response encoded despite Accept-Encoding: identity fails closed."""
    _load(tmp_path)
    net.route("corp.service-now.com", content=b"\x8b\x00", headers={"content-encoding": "br"})
    with (
        ec.source_http_client(
            "servicenow", "https://corp.service-now.com", timeout_s=30.0
        ) as client,
        pytest.raises(EgressBlocked) as info,
    ):
        client.get("/api/now/table/incident")
    assert info.value.reason == "unsupported_encoding"
