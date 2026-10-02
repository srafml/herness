"""Security tests for the process socket guard (impl 10 ST10-08, ST10-29, ST10-55; card T10-18).

Every scenario installs the guard from a config built by ``load_bootstrap`` over a temporary
``config/`` tree (impl 10 U10-21), so no real DNS lookup is needed for a blocked host: the
allowlist check runs before ``socket.getaddrinfo`` ever resolves anything.
"""

from __future__ import annotations

import http.client
import os
import socket
import urllib.request  # noqa: TID251 - proves ST10-08 blocks this banned client too
from pathlib import Path
from typing import Any

import pytest
from tests.support.config_harness import write_config
from tests.support.config_tree import register_checked_names, write_checked_config

from herness.core import config as c
from herness.core import config_sources as cs
from herness.core import config_validate as cv
from herness.core import egress_socket as es
from herness.core.errors import EgressBlocked

pytestmark = pytest.mark.unit


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _restore_offline_env() -> Any:
    saved = {k: os.environ.get(k) for k in ("HF_HUB_OFFLINE", "HF_HUB_DISABLE_TELEMETRY")}
    saved["DO_NOT_TRACK"] = os.environ.get("DO_NOT_TRACK")
    yield
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _https_connection_get(host: str) -> None:
    conn = http.client.HTTPSConnection(host, timeout=1)
    conn.request("GET", "/")


def test_st10_08_various_clients_are_blocked_before_any_socket_opens(tmp_path: Path) -> None:
    """ST10-08 TH10-15: create_connection, HTTPSConnection and urlopen all raise EgressBlocked."""
    boot = cs.load_bootstrap(config_dir=write_config(tmp_path), env={})
    es.install_socket_guard(boot)
    with pytest.raises(EgressBlocked):
        socket.create_connection(("evil.example.com", 443), timeout=1)
    with pytest.raises(EgressBlocked):
        _https_connection_get("evil.example.com")
    with pytest.raises(EgressBlocked):
        urllib.request.urlopen("https://evil.example.com", timeout=1)  # noqa: TID251


def test_st10_29_huggingface_blocked_and_offline_env_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ST10-29 TH10-24: huggingface.co is blocked; HF_HUB_OFFLINE=1 is set on install."""
    for var in ("HF_HUB_OFFLINE", "HF_HUB_DISABLE_TELEMETRY", "DO_NOT_TRACK"):
        monkeypatch.delenv(var, raising=False)
    boot = cs.load_bootstrap(config_dir=write_config(tmp_path), env={})
    es.install_socket_guard(boot)
    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert os.environ["HF_HUB_DISABLE_TELEMETRY"] == "1"
    assert os.environ["DO_NOT_TRACK"] == "1"
    with pytest.raises(EgressBlocked):
        socket.create_connection(("huggingface.co", 443), timeout=1)


_SNOWFLAKE_NO_HOSTS = """\
version: 1
sources:
  snowflake:
    enabled: true
    base_url: https://acme.snowflakecomputing.com
    hosts: []
"""

_SNOWFLAKE_WITH_HOSTS = """\
version: 1
sources:
  snowflake:
    enabled: true
    base_url: https://acme.snowflakecomputing.com
    hosts: [acme.snowflakecomputing.com]
"""


_ACME_IP = "192.0.2.55"  # TEST-NET-1: the stubbed address of the listed host (no real DNS)


def test_st10_55_sdk_base_url_never_allowlisted_hosts_key_admits_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST10-55 TH10-48: a Snowflake base_url host is never derived; listing it admits it."""
    cfg_dir = write_config(tmp_path)
    _write(cfg_dir / "sources.yaml", _SNOWFLAKE_NO_HOSTS)
    boot = cs.load_bootstrap(config_dir=cfg_dir, env={})
    assert "acme.snowflakecomputing.com" not in boot.source_hosts
    es.install_socket_guard(boot)
    for host in ("acme.snowflakecomputing.com", "evil.snowflakecomputing.com"):
        with pytest.raises(EgressBlocked):
            socket.create_connection((host, 443), timeout=1)

    _write(cfg_dir / "sources.yaml", _SNOWFLAKE_WITH_HOSTS)
    boot_listed = cs.load_bootstrap(config_dir=cfg_dir, env={})
    assert boot_listed.source_hosts == ("acme.snowflakecomputing.com",)
    es.install_socket_guard(boot_listed)
    assert es._POLICY is not None
    real_getaddrinfo = socket.getaddrinfo

    def resolver(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        """The admitted host resolves locally (no real DNS, hermetic); any other name takes
        the real audited path, where the guard refuses it before any lookup."""
        if host == "acme.snowflakecomputing.com":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_ACME_IP, port))]
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolver)
    es._POLICY.check_getaddrinfo("acme.snowflakecomputing.com", 443)
    assert es._fresh(_ACME_IP)  # admitted: resolved and its address cached for connect
    with pytest.raises(EgressBlocked):  # the unlisted sibling host is still always blocked
        socket.create_connection(("evil.snowflakecomputing.com", 443), timeout=1)


def test_st10_55_c20_reports_missing_snowflake_hosts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST10-55 TH10-48: C20 flags an enabled SDK source that lists no hosts."""
    register_checked_names()
    cfg_dir = write_checked_config(tmp_path)
    real_tree = cv._tree

    def patched(cfg: c.HernessConfig) -> dict[str, Any]:
        tree = real_tree(cfg)
        tree["sources"]["sources"]["snowflake"] = {"enabled": True, "account": "Acme"}
        return tree

    monkeypatch.setattr(cv, "_tree", patched)
    issues = c.validate(cfg_dir, "local", offline=True)
    assert any(
        i.path == "sources.sources.snowflake.hosts" and i.severity == "error" for i in issues
    )
