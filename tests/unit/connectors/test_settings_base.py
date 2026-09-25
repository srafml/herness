"""Tests for herness.connectors.settings_base (U01-01 … U01-06)."""

import ast
import datetime
import importlib
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar

import pytest
from pydantic import ValidationError

from herness.connectors import settings_base as sb
from herness.core.errors import ConfigError

pytestmark = pytest.mark.unit

UTC = datetime.UTC
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


def _now() -> datetime.datetime:
    return datetime.datetime(2026, 9, 25, 14, 30, tzinfo=UTC)


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


# UT01-05 --------------------------------------------------------------------


def _source(**extra: Any) -> _ServiceNow:
    data: dict[str, Any] = {
        "overlap_minutes": 45,
        "page_size": 500,
        "backfill": {"start": datetime.date(2024, 1, 1), "slice_days": 30},
        "entities": {
            "incident": {"overlap_minutes": 5, "page_size": 50, "backfill": {"slice_days": 7}},
            "problem": {},
        },
    }
    data.update(extra)
    return _ServiceNow.model_validate(data)


def test_ut01_05_entity_overrides_win() -> None:
    """UT01-05 entity overlap and page size win; unset fields inherit."""
    src = _source()
    assert src.overlap_for("incident") == datetime.timedelta(minutes=5)
    assert src.overlap_for("problem") == datetime.timedelta(minutes=45)
    assert src.page_size_for("incident") == 50
    assert src.page_size_for("problem") == 500


def test_ut01_05_backfill_merged_field_by_field() -> None:
    """UT01-05 backfill_for replaces only the fields the entity sets."""
    src = _source()
    merged = src.backfill_for("incident")
    assert merged.start == datetime.date(2024, 1, 1)
    assert merged.slice_days == 7
    assert src.backfill_for("problem") == src.backfill


def test_ut01_05_unknown_entity() -> None:
    """UT01-05 entity() raises ConfigError for an unknown name."""
    with pytest.raises(ConfigError, match="unknown entity nope for source servicenow") as info:
        _source().entity("nope")
    assert info.value.context == {"source": "servicenow", "entity": "nope"}


def test_ut01_05_resolve_start_default_and_date() -> None:
    """UT01-05 start=None gives now - 1096 d at midnight UTC; a date gives midnight."""
    assert sb.BackfillSettings().resolve_start(_now()) == datetime.datetime(2023, 9, 25, tzinfo=UTC)
    start = sb.BackfillSettings(start=datetime.date(2024, 2, 3)).resolve_start(_now())
    assert start == datetime.datetime(2024, 2, 3, tzinfo=UTC)
    other_tz = _now().astimezone(datetime.timezone(datetime.timedelta(hours=-12)))
    assert sb.BackfillSettings().resolve_start(other_tz) == datetime.datetime(
        2023, 9, 25, tzinfo=UTC
    )


def test_ut01_05_resolve_start_errors() -> None:
    """UT01-05 future start and naive now raise ConfigError."""
    with pytest.raises(ConfigError, match=r"backfill.start is in the future"):
        sb.BackfillSettings(start=datetime.date(2026, 9, 26)).resolve_start(_now())
    midnight = datetime.datetime(2026, 9, 25, tzinfo=UTC)
    with pytest.raises(ConfigError, match=r"backfill.start is in the future"):
        sb.BackfillSettings(start=datetime.date(2026, 9, 25)).resolve_start(midnight)
    with pytest.raises(ConfigError, match="naive datetime"):
        sb.BackfillSettings().resolve_start(datetime.datetime(2026, 9, 25))  # noqa: DTZ001


@pytest.mark.parametrize(
    "data",
    [
        {"start": datetime.date(1969, 12, 31)},
        {"slice_days": 0},
        {"slice_days": 367},
        {"start": "2024-01-01"},
    ],
)
def test_ut01_05_backfill_constraints(data: dict[str, Any]) -> None:
    """UT01-05 backfill start >= 1970-01-01 and 1 <= slice_days <= 366."""
    with pytest.raises(ValidationError):
        sb.BackfillSettings.model_validate(data)


def test_ut01_05_reconcile_defaults_and_constraints() -> None:
    """UT01-05 reconcile defaults; schedule shape and max_delete_pct bounds."""
    rec = sb.ReconcileSettings()
    assert (rec.schedule, rec.max_delete_pct) == ("0 3 * * SUN", 2.0)
    assert sb.ReconcileSettings.model_validate({"max_delete_pct": 100}).max_delete_pct == 100.0
    assert sb.ReconcileSettings.model_validate({"schedule": "*/5 0-6 1,15 * MON"}).schedule
    for bad in ({"max_delete_pct": 0}, {"max_delete_pct": 100.5}):
        with pytest.raises(ValidationError, match=r"reconcile\.max_delete_pct"):
            sb.ReconcileSettings.model_validate(bad)
    for wrong_type in (True, "2.0"):
        with pytest.raises(ValidationError, match="max_delete_pct"):
            sb.ReconcileSettings.model_validate({"max_delete_pct": wrong_type})
    for sched in ("0 3 * *", "0 3 * * SUN *", "0 3 * * S?N", "0 3 * *\nSUN"):
        with pytest.raises(ValidationError, match=r"reconcile\.schedule"):
            sb.ReconcileSettings.model_validate({"schedule": sched})


def test_ut01_05_entity_constraints() -> None:
    """UT01-05 entity overlap bounds; unset entity fields are None."""
    ent = sb.EntitySettings()
    assert (ent.overlap_minutes, ent.page_size, ent.backfill) == (None, None, None)
    assert sb.EntitySettings.model_validate({"overlap_minutes": 1440}).overlap_minutes == 1440
    with pytest.raises(ValidationError, match="overlap_minutes"):
        sb.EntitySettings.model_validate({"overlap_minutes": 1441})


def test_ut01_05_source_defaults() -> None:
    """UT01-05 common source defaults match the table."""
    src = _ServiceNow()
    assert src.enabled is False
    assert (src.batch_rows, src.checkpoint_rows) == (10000, 500000)
    assert (src.overlap_minutes, src.settle_seconds, src.timeout_s) == (30, 60, 60.0)
    assert (src.schedule, src.base_url, src.auth, src.verify, src.hosts) == (
        None, None, None, None, (),
    )  # fmt: skip
    assert src.reconcile == sb.ReconcileSettings()
    assert src.backfill == sb.BackfillSettings()
    assert _ServiceNow.model_validate({"base_url": None, "schedule": None}).base_url is None


@pytest.mark.parametrize(
    "data",
    [
        {"batch_rows": 999},
        {"batch_rows": 100001},
        {"batch_rows": 20000, "checkpoint_rows": 19999},
        {"checkpoint_rows": 5000001},
        {"overlap_minutes": -1},
        {"settle_seconds": 3601},
        {"timeout_s": 0.5},
        {"timeout_s": 601},
        {"schedule": "daily"},
        {"page_size": 0},
        {"enabled": "yes"},
        {"surprise": 1},
    ],
)
def test_ut01_05_source_constraints(data: dict[str, Any]) -> None:
    """UT01-05 source-level constraints reject out-of-range values."""
    with pytest.raises(ValidationError):
        _ServiceNow.model_validate(data)


def test_ut01_05_source_accepts_int_timeout_and_schedule() -> None:
    """UT01-05 a YAML int is accepted for a float field; a schedule is kept."""
    src = _ServiceNow.model_validate({"timeout_s": 600, "schedule": "15 * * * *"})
    assert src.timeout_s == 600.0
    assert src.schedule == "15 * * * *"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://acme.service-now.com/", "https://acme.service-now.com"),
        ("https://acme.example.com/api", "https://acme.example.com/api"),
        ("http://127.0.0.1:8080", "http://127.0.0.1:8080"),
        ("http://localhost/", "http://localhost"),
        ("http://[::1]:9090", "http://[::1]:9090"),
    ],
)
def test_ut01_05_base_url_accepted(url: str, expected: str) -> None:
    """UT01-05 base_url https, or http on loopback; trailing slash stripped."""
    assert _ServiceNow.model_validate({"base_url": url}).base_url == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://acme.example.com",
        "ftp://acme.example.com",
        "https://user:synthetic-pw@acme.example.com",
        "https://acme.example.com/?token=synthetic-q",
        "https://acme.example.com/#frag",
        "https:///path",
        "acme.example.com",
        "https://acme.example.com:99999",
        "https://acme.example.com/a b",
        "https://acme.example.com/\tx",
    ],
)
def test_ut01_05_base_url_rejected(url: str) -> None:
    """UT01-05 base_url shape violations fail without echoing the URL."""
    with pytest.raises(ValidationError, match="base_url") as info:
        _ServiceNow.model_validate({"base_url": url})
    assert "synthetic" not in str(info.value).replace("input_value", "")


def test_ut01_05_hosts_rules() -> None:
    """UT01-05 hosts are lower-cased; IP, port, duplicates and > 50 entries fail."""
    src = _ServiceNow.model_validate({"hosts": ["SSO.Example.com", "api.example.com"]})
    assert src.hosts == ("sso.example.com", "api.example.com")
    for bad in (["10.0.0.1"], ["a.example.com:443"], ["a.example.com", "A.example.com"],
                ["localhost"], [f"h{i}.example.com" for i in range(51)], [7],
                "a.example.com"):  # fmt: skip
        with pytest.raises(ValidationError, match="hosts"):
            _ServiceNow.model_validate({"hosts": bad})
    assert _ServiceNow.model_validate({"hosts": ("a.example.com",)}).hosts == ("a.example.com",)


def test_ut01_05_source_is_frozen() -> None:
    """UT01-05 source models are immutable."""
    src = _ServiceNow()
    with pytest.raises(ValidationError):
        src.enabled = True  # type: ignore[misc]


# R-03 import rule -------------------------------------------------------------

_ALLOWED_HERNESS = {
    "herness",
    "herness.core",
    "herness.core.errors",
    "herness.core.types",
    "herness.connectors",
    "herness.connectors.settings_base",
}


def test_ut01_01_settings_base_imports_only_allowed_modules() -> None:
    """UT01-01 importing settings_base loads only stdlib, pydantic, core.types, core.errors."""
    saved = dict(sys.modules)
    try:
        for name in [n for n in sys.modules if n == "herness" or n.startswith("herness.")]:
            del sys.modules[name]
        before = set(sys.modules)
        importlib.import_module("herness.connectors.settings_base")
        loaded = set(sys.modules) - before
    finally:
        sys.modules.clear()
        sys.modules.update(saved)
    herness_mods = {n for n in loaded if n.split(".")[0] == "herness"}
    assert herness_mods <= _ALLOWED_HERNESS | {
        n for n in herness_mods if n.startswith("herness.core.types.")
    }
    third_party = {n.split(".")[0] for n in loaded} - set(sys.stdlib_module_names) - {"herness"}
    assert third_party <= {"pydantic", "pydantic_core", "typing_extensions", "annotated_types"}


def test_ut01_01_settings_base_source_imports() -> None:
    """UT01-01 the module source imports only stdlib, pydantic, core.types, core.errors."""
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
