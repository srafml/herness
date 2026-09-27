"""Security tests of the SDK-source host allowlist (impl 01 ST01-17; TH01-18, TH01-01, TH01-03;
R-06; T01-22).

The MongoDB URI comes from a secret, so every host it names is checked against
`sources.mongodb.hosts` before `client_factory` is called, and neither the host nor the URI
reaches the error. Snowflake and Dataverse sections are rejected at settings validation when
their SDK host is not listed.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from pydantic import ValidationError
from tests.unit.connectors._mongo_data import Factory, connector
from tests.unit.connectors._settings_data import dataverse, snowflake

from herness.connectors.settings import DataverseSettings, SnowflakeSettings
from herness.core.errors import ConfigError

pytestmark = pytest.mark.unit

_HOST_MSG = "MongoDB URI names a host not listed in sources.mongodb.hosts"
_TLS_MSG = "MongoDB URI must enable verified TLS"
_Put = Callable[[str], None]


def _refused(put: _Put, uri: str, match: str, **extra: object) -> ConfigError:
    """Store `uri`, build the client lazily through `check`; assert ConfigError(match) with an
    empty factory call log and neither the URI nor its hosts in the message or context."""
    put(uri)
    factory = Factory()
    with pytest.raises(ConfigError, match=match) as info:
        connector(factory, **extra).check()
    assert factory.calls == []
    text = str(info.value) + repr(info.value.context) + str(info.value.hint)
    assert uri not in text
    assert "rogue" not in text
    assert info.value.__cause__ is None
    return info.value


def test_st01_17_rogue_uri_host_refused_before_factory(mongo_uri: _Put) -> None:
    """ST01-17 a URI secret naming `rogue.example` while `hosts` lists only `db1.example` →
    ConfigError before `client_factory` is called; the message lacks the host."""
    _refused(mongo_uri, "mongodb://rogue.example:27017/?tls=true", _HOST_MSG, hosts=["db1.example"])


@pytest.mark.parametrize(
    "uri",
    [
        "mongodb://db1.example,rogue.example:27018/?tls=true",
        "mongodb://svc@rogue.example/ops?tls=true",
        "mongodb+srv://rogue.example/ops",
        "mongodb+srv://ROGUE.example/?retryWrites=false",
        "mongodb://[2001:db8::1]:27017/?tls=true",
        "mongodb://%2Ftmp%2Fmongodb-27017.sock/?tls=true",
        "mongodb://:27017/?tls=true",
        "mongodb://localhost,rogue.example/?tls=true",
    ],
)
def test_st01_17_every_uri_host_must_be_listed(mongo_uri: _Put, uri: str) -> None:
    """ST01-17 every host of a seed list, of a user-info URI, the SRV name, an IP literal or a
    socket path must be in `hosts` (case-insensitive)."""
    _refused(mongo_uri, uri, _HOST_MSG, hosts=["db1.example"])


@pytest.mark.parametrize(
    "uri",
    [
        "mongodb://db1.example:27017/ops",
        "mongodb://db1.example:27017/?tls=false",
        "mongodb://db1.example/?tls=true&tls=false",
        "mongodb://db1.example/?ssl=false",
        "mongodb://db1.example/?tls=true&tlsInsecure=true",
        "mongodb://db1.example/?tls=true&tlsAllowInvalidCertificates=true",
        "mongodb://db1.example/?tls=true;tlsAllowInvalidHostnames=true",
        "mongodb+srv://db1.example/?tls=false",
        "mongodb+srv://db1.example/?TLSINSECURE=TRUE",
        "mongodb://localhost/?tlsInsecure=true",
        "postgres://db1.example/?tls=true",
        "db1.example:27017",
    ],
)
def test_st01_17_uri_must_enable_verified_tls(mongo_uri: _Put, uri: str) -> None:
    """ST01-17 (TH01-01) no TLS for a remote host, TLS switched off or its verification
    disabled, or a non-MongoDB scheme → ConfigError before the factory; URI not echoed."""
    _refused(mongo_uri, uri, _TLS_MSG, hosts=["db1.example"])


@pytest.mark.parametrize(
    "uri",
    [
        "mongodb://db1.example:27017,DB2.example/ops?replicaSet=rs0&tls=true",
        "mongodb://svc@db1.example/ops?ssl=true&tlsInsecure=false",
        "mongodb+srv://db1.example/ops",
        "mongodb://localhost:27017/ops",
        "mongodb://127.0.0.1:27017,[::1]:27018/ops",
    ],
)
def test_st01_17_listed_hosts_with_tls_reach_the_factory(mongo_uri: _Put, uri: str) -> None:
    """ST01-17 listed hosts with verified TLS, or an all-loopback plain URI (loopback needs no
    listing: `hosts` cannot hold it and the socket guard allows it), are handed to
    `client_factory` unchanged."""
    mongo_uri(uri)
    factory = Factory()
    connector(factory, hosts=["db1.example", "db2.example"]).check()
    assert factory.calls == [uri]


def test_st01_17_snowflake_hosts_omitted_rejected() -> None:
    """ST01-17 a snowflake section with `hosts` omitted is rejected at validation."""
    data = snowflake()
    del data["hosts"]
    with pytest.raises(ValidationError, match="hosts must list the Snowflake account host"):
        SnowflakeSettings.model_validate(data)
    assert SnowflakeSettings.model_validate(snowflake()).hosts


def test_st01_17_dataverse_hosts_without_msal_authority_rejected() -> None:
    """ST01-17 dataverse `hosts` without `login.microsoftonline.com` is rejected at validation."""
    with pytest.raises(ValidationError, match=r"hosts must list login\.microsoftonline\.com"):
        DataverseSettings.model_validate(dataverse(hosts=["acme.crm.dynamics.com"]))
    assert DataverseSettings.model_validate(dataverse()).hosts
