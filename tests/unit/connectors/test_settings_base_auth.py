"""Tests for settings_base auth, TLS, concurrency and R-03 rules (U01-01, U01-05, U01-06)."""

import ast
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import ClassVar

import pytest
from pydantic import ValidationError

from herness.connectors import settings_base as sb

pytestmark = pytest.mark.unit

TLS_MSG = "TLS verification cannot be disabled; set verify to a CA bundle path"
GUID = "12345678-9abc-def0-1234-56789abcdef0"


class _ServiceNow(sb.SourceSettings):
    SOURCE: ClassVar[str] = "servicenow"
    entities: Mapping[str, sb.EntitySettings] = {}


class _Dataverse(sb.SourceSettings):
    SOURCE: ClassVar[str] = "dataverse"


class _Files(sb.SourceSettings):
    SOURCE: ClassVar[str] = "files"


class _Jira(sb.SourceSettings):
    SOURCE: ClassVar[str] = "jira"

    def auth_key(self) -> str:
        return "jira:cloud"


# UT01-01 --------------------------------------------------------------------


def test_ut01_01_plain_text_credentials_rejected_without_echo() -> None:
    """UT01-01 A plain-text credential fails and the message lacks the value."""
    with pytest.raises(ValidationError) as info:
        sb.AuthSettings.model_validate({"method": "basic", "credentials": "synthetic-hunter2"})
    text = str(info.value)
    assert "synthetic-hunter2" not in text
    assert "auth.credentials must be a secret:<name> reference" in text


def test_ut01_01_long_secret_name_rejected() -> None:
    """UT01-01 A secret: reference with a 70-char name fails."""
    ref = "secret:" + "a" * 70
    with pytest.raises(ValidationError) as info:
        sb.AuthSettings.model_validate({"method": "basic", "credentials": ref})
    assert ref not in str(info.value)


def test_ut01_01_secret_pattern_bounds() -> None:
    """UT01-01 SECRET_REF_PATTERN accepts 2-64 char names and rejects the rest."""
    assert sb.SECRET_REF_PATTERN.fullmatch("secret:" + "a" * 64)
    assert sb.SECRET_REF_PATTERN.fullmatch("secret:ab")
    assert not sb.SECRET_REF_PATTERN.fullmatch("secret:" + "a" * 65)
    assert not sb.SECRET_REF_PATTERN.fullmatch("secret:a")
    assert not sb.SECRET_REF_PATTERN.fullmatch("secret:_ab")
    assert not sb.SECRET_REF_PATTERN.fullmatch("secret:ab\n")


def test_ut01_01_valid_auth_and_method_none_rules() -> None:
    """UT01-01 credentials required unless method none, forbidden with none."""
    auth = sb.AuthSettings.model_validate({"method": "basic", "credentials": "secret:sn.cred"})
    assert auth.credentials == "secret:sn.cred"
    assert sb.AuthSettings.model_validate({"method": "none"}).credentials is None
    with pytest.raises(ValidationError, match=r"auth.credentials is required"):
        sb.AuthSettings.model_validate({"method": "basic"})
    with pytest.raises(ValidationError, match="must be empty for method none"):
        sb.AuthSettings.model_validate({"method": "none", "credentials": "secret:ab"})


def test_ut01_01_tenant_id_rule() -> None:
    """UT01-01 tenant_id required for MSAL, forbidden otherwise, GUID shaped."""
    msal = {"method": "msal_client_credentials", "credentials": "secret:dv"}
    assert sb.AuthSettings.model_validate({**msal, "tenant_id": GUID}).tenant_id == GUID
    for bad in ({**msal}, {**msal, "tenant_id": "not-a-guid"}):
        with pytest.raises(ValidationError, match=r"auth\.tenant_id"):
            sb.AuthSettings.model_validate(bad)
    with pytest.raises(ValidationError, match=r"auth\.tenant_id"):
        sb.AuthSettings.model_validate(
            {"method": "basic", "credentials": "secret:ab", "tenant_id": GUID}
        )


def test_ut01_01_auth_is_frozen_and_strict() -> None:
    """UT01-01 AuthSettings is frozen and forbids unknown keys."""
    auth = sb.AuthSettings.model_validate({"method": "none"})
    with pytest.raises(ValidationError):
        auth.method = "basic"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        sb.AuthSettings.model_validate({"method": "none", "password": "x"})


def test_ut01_01_auth_method_checked_by_source() -> None:
    """UT01-01 auth.method must be in AUTH_METHODS[auth_key()]; oauth_3lo named."""
    ok = _ServiceNow.model_validate({"auth": {"method": "basic", "credentials": "secret:ab"}})
    assert ok.auth is not None
    with pytest.raises(ValidationError, match=r"auth.method is not allowed for servicenow"):
        _ServiceNow.model_validate({"auth": {"method": "pat", "credentials": "secret:ab"}})
    with pytest.raises(ValidationError, match=r"auth.method oauth_3lo is not supported in v1"):
        _Jira.model_validate({"auth": {"method": "oauth_3lo", "credentials": "secret:ab"}})
    with pytest.raises(ValidationError, match="not allowed for files"):
        _Files.model_validate({"auth": {"method": "none"}})


def test_ut01_01_constants_immutable() -> None:
    """UT01-01 closed-set constants are read-only with the spec values."""
    assert sb.AUTH_METHODS["servicenow"] == frozenset(
        {"oauth_client_credentials", "oauth_password", "basic"}
    )
    assert sb.AUTH_METHODS["prometheus"] == frozenset({"bearer", "basic", "none"})
    assert set(sb.AUTH_METHODS) == {
        "servicenow", "jira:cloud", "jira:datacenter", "prometheus", "datadog",
        "splunk", "dynatrace", "mongodb", "snowflake", "dataverse",
    }  # fmt: skip
    assert len(sb.SERVICENOW_ENTITIES) == 9
    assert "task_sla" in sb.SERVICENOW_ENTITIES
    assert (
        frozenset({"availability_pct", "error_rate", "request_count", "alert_firing_minutes"})
        == sb.DAILY_METRIC_NAMES
    )
    with pytest.raises(TypeError):
        sb.AUTH_METHODS["x"] = frozenset()  # type: ignore[index]
    with pytest.raises(TypeError):
        sb.CONCURRENCY_CAPS["x"] = 1  # type: ignore[index]


# UT01-03 / ST01-01 ----------------------------------------------------------


@pytest.mark.parametrize("value", [False, True, "does/not/exist.pem"])
def test_ut01_03_verify_cannot_disable_tls(value: object, tmp_path: Path) -> None:
    """UT01-03 verify false, true or a missing path fail with the TLS message."""
    if isinstance(value, str):
        value = str(tmp_path / value)
    with pytest.raises(ValidationError, match="TLS verification cannot be disabled"):
        _ServiceNow.model_validate({"verify": value})


def test_ut01_03_verify_accepts_ca_file(tmp_path: Path) -> None:
    """UT01-03 an existing CA file is accepted; httpx_verify returns the path."""
    ca = tmp_path / "ca.pem"
    ca.write_text("synthetic", encoding="utf-8")
    model = _ServiceNow.model_validate({"verify": str(ca)})
    assert model.verify == ca
    assert model.httpx_verify() == str(ca)
    assert _ServiceNow().httpx_verify() is True


def test_st01_01_verify_false_rejected_in_config() -> None:
    """ST01-01 (config part) verify: false in config is rejected."""
    with pytest.raises(ValidationError) as info:
        _ServiceNow.model_validate({"enabled": True, "verify": False})
    assert TLS_MSG in str(info.value)


# UT01-04 --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cls", "value"), [(_ServiceNow, 9), (_Dataverse, 53), (_Files, 2), (_ServiceNow, 0)]
)
def test_ut01_04_concurrency_above_cap_rejected(cls: type[sb.SourceSettings], value: int) -> None:
    """UT01-04 max_concurrency beyond the per-source cap fails."""
    with pytest.raises(ValidationError, match="max_concurrency"):
        cls.model_validate({"max_concurrency": value})


@pytest.mark.parametrize(("cls", "cap"), [(_ServiceNow, 8), (_Dataverse, 52), (_Files, 1)])
def test_ut01_04_concurrency_cap_accepted(cls: type[sb.SourceSettings], cap: int) -> None:
    """UT01-04 the cap values are accepted; the default comes from the table."""
    assert cls.model_validate({"max_concurrency": cap}).max_concurrency == cap
    assert cls().max_concurrency == sb.CONCURRENCY_DEFAULTS[cls.SOURCE]


def test_ut01_04_concurrency_tables() -> None:
    """UT01-04 defaults never exceed caps and both cover the same sources."""
    assert set(sb.CONCURRENCY_DEFAULTS) == set(sb.CONCURRENCY_CAPS)
    assert all(sb.CONCURRENCY_DEFAULTS[k] <= sb.CONCURRENCY_CAPS[k] for k in sb.CONCURRENCY_CAPS)
    assert sb.CONCURRENCY_CAPS["dataverse"] == 52


def test_ut01_04_source_without_source_name_rejected() -> None:
    """UT01-04 a source model without a known SOURCE cannot be built."""

    class _Nameless(sb.SourceSettings):
        pass

    with pytest.raises(ValidationError, match="SOURCE"):
        _Nameless()


def test_ut01_01_r03_settings_base_source_imports() -> None:
    """UT01-01 (R-03 acceptance check) source imports only stdlib, pydantic, core.types/errors.

    The runtime half, in a fresh interpreter, is tests/integration/connectors/
    test_settings_base_imports.py (a subprocess, so it cannot carry the unit marker).
    """
    path = Path(sb.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    for name in names:
        top = name.split(".")[0]
        ok = top in sys.stdlib_module_names or top == "pydantic"
        ok = ok or name in {"herness.core.errors", "herness.core.types"}
        ok = ok or name.startswith("herness.core.types.")
        assert ok, name
