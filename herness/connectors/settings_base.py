"""Common pydantic section models and closed-set constants of `sources.yaml` (impl 01 §3.1).

Settings import rule (R-03, ENG §2.1): this module imports only the standard library,
pydantic and `herness.core.errors`. Validators raise `ValueError` naming the key path and
never echo a value; the config loader (T10-03) turns `ValidationError` into `ConfigError`.
"""

import datetime
import ipaddress
import math
import re
import types
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, ClassVar, Final, Self
from urllib.parse import urlsplit

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from herness.core.errors import ConfigError

SECRET_REF_PATTERN: Final[re.Pattern[str]] = re.compile(r"^secret:[A-Za-z0-9][A-Za-z0-9_.-]{1,63}$")
# fmt: off
AUTH_METHODS: Final[Mapping[str, frozenset[str]]] = types.MappingProxyType({
    "servicenow": frozenset({"oauth_client_credentials", "oauth_password", "basic"}),
    "jira:cloud": frozenset({"api_token"}), "jira:datacenter": frozenset({"pat"}),
    "prometheus": frozenset({"bearer", "basic", "none"}),
    "datadog": frozenset({"api_and_app_key"}), "splunk": frozenset({"bearer"}),
    "dynatrace": frozenset({"api_token"}), "mongodb": frozenset({"connection_string"}),
    "snowflake": frozenset({"key_pair"}), "dataverse": frozenset({"msal_client_credentials"}),
})
CONCURRENCY_DEFAULTS: Final[Mapping[str, int]] = types.MappingProxyType({
    "servicenow": 4, "jira": 2, "monitoring": 4, "prometheus": 4, "datadog": 2,
    "splunk": 2, "dynatrace": 2, "mongodb": 4, "snowflake": 2, "dataverse": 4, "files": 1,
})
CONCURRENCY_CAPS: Final[Mapping[str, int]] = types.MappingProxyType({
    "servicenow": 8, "jira": 4, "monitoring": 4, "prometheus": 4, "datadog": 2,
    "splunk": 2, "dynatrace": 2, "mongodb": 4, "snowflake": 2, "dataverse": 52, "files": 1,
})
DAILY_METRIC_NAMES: Final[frozenset[str]] = frozenset(
    {"availability_pct", "error_rate", "request_count", "alert_firing_minutes"}
)
SERVICENOW_ENTITIES: Final[frozenset[str]] = frozenset({
    "incident", "change_request", "problem", "cmdb_ci", "cmdb_ci_service",
    "cmdb_rel_ci", "sys_user_group", "cmn_department", "task_sla",
})
# fmt: on

# Strict floats accept YAML ints and reject booleans. hide_input_in_errors keeps rejected
# values (possibly plain-text secrets) out of the ValidationError text (TH01-03, UT01-01).
_CONFIG: Final = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
_GUID: Final = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
_CRON: Final = re.compile(r"[0-9A-Za-z*/,\-]+(?:[ \t]+[0-9A-Za-z*/,\-]+){4}")
_HOST: Final = re.compile(
    r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)
_LOOPBACK: Final = frozenset({"127.0.0.1", "localhost", "::1"})
_MAX_HOSTS: Final = 50
_BACKFILL_DAYS: Final = 1096
_EPOCH: Final = datetime.date(1970, 1, 1)
_TLS_MSG: Final = "TLS verification cannot be disabled; set verify to a CA bundle path"
_URL_MSG: Final = "base_url must be https (http only on loopback) without userinfo, query, fragment"


def _range(
    key: str, low: float, high: float = math.inf, *, open_low: bool = False
) -> AfterValidator:
    """Bounds check (inclusive, or open at `low`) whose message names `key`."""

    def check(value: float | None) -> float | None:
        if value is not None and not (
            (low < value if open_low else low <= value) and value <= high
        ):
            msg = f"{key} must be {'>' if open_low else '>='} {low:g} and <= {high:g}"
            raise ValueError(msg)
        return value

    return AfterValidator(check)


def _schedule(key: str) -> AfterValidator:
    def check(value: str | None) -> str | None:
        if value is not None and _CRON.fullmatch(value) is None:
            msg = f"{key} must have five cron fields"
            raise ValueError(msg)
        return value

    return AfterValidator(check)


def _check_credentials(value: str | None) -> str | None:
    if value is not None and SECRET_REF_PATTERN.fullmatch(value) is None:
        msg = "auth.credentials must be a secret:<name> reference"
        raise ValueError(msg)
    return value


def _check_start(value: datetime.date | None) -> datetime.date | None:
    if value is not None and value < _EPOCH:
        msg = "backfill.start must be on or after 1970-01-01"
        raise ValueError(msg)
    return value


def _check_base_url(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parts = urlsplit(value)
        host, _ = parts.hostname, parts.port
    except ValueError:
        raise ValueError(_URL_MSG) from None
    secure = parts.scheme == "https" or (parts.scheme == "http" and host in _LOOPBACK)
    clean = not ({"@", "?", "#", " "} & set(value)) and value.isprintable()
    if not (host and secure and clean):
        raise ValueError(_URL_MSG)
    return value.removesuffix("/")


def _check_verify(value: object) -> object:
    # ValueError, not TypeError: pydantic turns only ValueError into a ValidationError.
    if isinstance(value, bool):
        raise ValueError(_TLS_MSG)  # noqa: TRY004
    path = Path(value) if isinstance(value, str) else value
    if isinstance(path, Path) and not path.is_file():
        raise ValueError(_TLS_MSG)
    return path


def _check_hosts(value: object) -> object:
    if not isinstance(value, list | tuple):
        return value  # strict tuple[str, ...] validation reports the type error
    if len(value) > _MAX_HOSTS:
        msg = f"hosts has more than {_MAX_HOSTS} entries"
        raise ValueError(msg)
    hosts: list[object] = []
    for index, entry in enumerate(value):
        host = entry.lower() if isinstance(entry, str) else entry
        if not isinstance(host, str) or _HOST.fullmatch(host) is None or _is_ip(host):
            msg = f"hosts entry {index} is not a host name"
            raise ValueError(msg)
        if host in hosts:
            msg = f"hosts entry {index} is a duplicate"
            raise ValueError(msg)
        hosts.append(host)
    return tuple(hosts)


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


class AuthSettings(BaseModel):
    """Auth method and `secret:` reference for one source or adapter (U01-01)."""

    model_config = _CONFIG

    method: str
    credentials: Annotated[str | None, AfterValidator(_check_credentials)] = None
    tenant_id: str | None = None

    @model_validator(mode="after")
    def _check_presence(self) -> Self:
        if self.method == "none" and self.credentials is not None:
            msg = "auth.credentials must be empty for method none"
            raise ValueError(msg)
        if self.method != "none" and self.credentials is None:
            msg = "auth.credentials is required"
            raise ValueError(msg)
        msal = self.method == "msal_client_credentials"
        if not msal and self.tenant_id is not None:
            msg = "auth.tenant_id is only allowed for method msal_client_credentials"
            raise ValueError(msg)
        if msal and (self.tenant_id is None or _GUID.fullmatch(self.tenant_id) is None):
            msg = "auth.tenant_id must be a GUID for method msal_client_credentials"
            raise ValueError(msg)
        return self


class ReconcileSettings(BaseModel):
    """Weekly reconciliation schedule and safety-valve threshold (U01-02)."""

    model_config = _CONFIG

    schedule: Annotated[str, _schedule("reconcile.schedule")] = "0 3 * * SUN"
    max_delete_pct: Annotated[float, _range("reconcile.max_delete_pct", 0, 100, open_low=True)] = (
        2.0
    )


class BackfillSettings(BaseModel):
    """Initial backfill range start and slice width (U01-03)."""

    model_config = _CONFIG

    start: Annotated[datetime.date | None, AfterValidator(_check_start)] = None
    slice_days: Annotated[int, _range("backfill.slice_days", 1, 366)] = 30

    def resolve_start(self, now: datetime.datetime) -> datetime.datetime:
        """Return the backfill start at 00:00:00 UTC; `None` means `now - 1096 days`."""
        if now.utcoffset() is None:
            msg = "naive datetime"
            raise ConfigError(msg)
        day = self.start
        if day is None:
            day = (now.astimezone(datetime.UTC) - datetime.timedelta(days=_BACKFILL_DAYS)).date()
        result = datetime.datetime(day.year, day.month, day.day, tzinfo=datetime.UTC)
        if result >= now:
            msg = "backfill.start is in the future"
            raise ConfigError(msg)
        return result


class EntitySettings(BaseModel):
    """Per-entity overrides of source-level settings (U01-04)."""

    model_config = _CONFIG

    overlap_minutes: Annotated[int | None, _range("overlap_minutes", 0, 1440)] = None
    page_size: int | None = None
    backfill: BackfillSettings | None = None


class SourceSettings(BaseModel):
    """Common keys of every source section (U01-05); subclasses set `SOURCE`."""

    model_config = _CONFIG

    SOURCE: ClassVar[str]

    enabled: bool = False
    base_url: Annotated[str | None, AfterValidator(_check_base_url)] = None
    auth: AuthSettings | None = None
    page_size: Annotated[int, _range("page_size", 1)] = 1000
    batch_rows: Annotated[int, _range("batch_rows", 1000, 100_000)] = 10_000
    checkpoint_rows: Annotated[int, _range("checkpoint_rows", 1000, 5_000_000)] = 500_000
    overlap_minutes: Annotated[int, _range("overlap_minutes", 0, 1440)] = 30
    settle_seconds: Annotated[int, _range("settle_seconds", 0, 3600)] = 60
    max_concurrency: int = 0  # placeholder: _default_concurrency fills the per-source default
    schedule: Annotated[str | None, _schedule("schedule")] = None
    reconcile: ReconcileSettings = Field(default_factory=ReconcileSettings)
    backfill: BackfillSettings = Field(default_factory=BackfillSettings)
    timeout_s: Annotated[float, _range("timeout_s", 1, 600)] = 60.0
    verify: Annotated[Path | None, BeforeValidator(_check_verify)] = None
    entities: Mapping[str, EntitySettings] = Field(default_factory=dict)
    hosts: Annotated[tuple[str, ...], BeforeValidator(_check_hosts)] = ()

    @model_validator(mode="before")
    @classmethod
    def _default_concurrency(cls, data: object) -> object:
        source: object = getattr(cls, "SOURCE", None)
        if not isinstance(source, str) or source not in CONCURRENCY_CAPS:
            msg = "source model must set SOURCE to a known connector name"
            raise ValueError(msg)
        if isinstance(data, Mapping) and "max_concurrency" not in data:
            return {**data, "max_concurrency": CONCURRENCY_DEFAULTS[source]}
        return data

    @model_validator(mode="after")
    def _check_common(self) -> Self:
        cap = CONCURRENCY_CAPS[self.SOURCE]
        if self.checkpoint_rows < self.batch_rows:
            msg = "checkpoint_rows must be >= batch_rows"
        elif not 1 <= self.max_concurrency <= cap:
            msg = f"max_concurrency must be >= 1 and <= {cap} for {self.SOURCE}"
        elif self.auth is not None and self.auth.method == "oauth_3lo":
            msg = "auth.method oauth_3lo is not supported in v1"
        elif self.auth is not None and self.auth.method not in AUTH_METHODS.get(
            self.auth_key(), frozenset()
        ):
            msg = f"auth.method is not allowed for {self.auth_key()}"
        else:
            return self
        raise ValueError(msg)

    def auth_key(self) -> str:
        """Key into `AUTH_METHODS`; `SOURCE` unless a subclass overrides it."""
        return self.SOURCE

    def entity(self, name: str) -> EntitySettings:
        """Return the entity model or raise `ConfigError` for an unknown name."""
        try:
            return self.entities[name]
        except KeyError:
            msg = f"unknown entity {name} for source {self.SOURCE}"
            raise ConfigError(msg, source=self.SOURCE, entity=name) from None

    def overlap_for(self, entity: str) -> datetime.timedelta:
        """Entity overlap if set, else the source value."""
        minutes = self.entity(entity).overlap_minutes
        return datetime.timedelta(minutes=self.overlap_minutes if minutes is None else minutes)

    def page_size_for(self, entity: str) -> int:
        """Entity page size if set, else the source value."""
        size = self.entity(entity).page_size
        return self.page_size if size is None else size

    def backfill_for(self, entity: str) -> BackfillSettings:
        """Source backfill with each field the entity backfill sets replaced."""
        override = self.entity(entity).backfill
        if override is None:
            return self.backfill
        fields = {name: getattr(override, name) for name in override.model_fields_set}
        return self.backfill.model_copy(update=fields)

    def httpx_verify(self) -> bool | str:
        """`True` (system trust store) when `verify` is unset, else the CA bundle path."""
        return True if self.verify is None else str(self.verify)
