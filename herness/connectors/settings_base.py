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
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Annotated, Any, ClassVar, Final, NoReturn, Self
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
_EPOCH: Final = datetime.date(1970, 1, 1)
_TLS_MSG: Final = "TLS verification cannot be disabled; set verify to a CA bundle path"
_URL_MSG: Final = "base_url must be https (http only on loopback) without userinfo, query, fragment"


def _rule(ok: Callable[[Any], bool], msg: str) -> AfterValidator:
    """After-validator raising `ValueError(msg)` when a non-None value fails `ok`."""

    def check(value: object) -> object:
        if value is not None and not ok(value):
            raise ValueError(msg)
        return value

    return AfterValidator(check)


def _range(
    key: str, low: float, high: float = math.inf, *, open_low: bool = False
) -> AfterValidator:
    lower = f"{key} must be {'>' if open_low else '>='} {low:g}"
    return _rule(
        lambda v: (low < v if open_low else low <= v) and v <= high,
        lower if high == math.inf else f"{lower} and <= {high:g}",
    )


def _schedule(key: str) -> AfterValidator:
    return _rule(lambda v: _CRON.fullmatch(v) is not None, f"{key} must have five cron fields")


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
    if len(value) > 50:  # noqa: PLR2004 - R-06 limit
        msg = "hosts has more than 50 entries"
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
        return bool(ipaddress.ip_address(host))
    except ValueError:
        return False


class _ReadOnlyDict(dict[str, Any]):
    """A dict that refuses mutation; serializes like a plain dict."""

    def _refuse(self, *args: object, **kwargs: object) -> NoReturn:
        msg = "settings mappings are read-only"
        raise TypeError(msg)

    __setitem__ = __delitem__ = __ior__ = _refuse
    clear = pop = popitem = setdefault = update = _refuse


class AuthSettings(BaseModel):
    """Auth method and `secret:` reference for one source or adapter (U01-01)."""

    model_config = _CONFIG

    method: str
    credentials: Annotated[
        str | None,
        _rule(
            lambda v: SECRET_REF_PATTERN.fullmatch(v) is not None,
            "auth.credentials must be a secret:<name> reference",
        ),
    ] = None
    tenant_id: str | None = None

    @model_validator(mode="after")
    def _check_rules(self) -> Self:
        msal = self.method == "msal_client_credentials"
        if self.method == "none" and self.credentials is not None:
            msg = "auth.credentials must be empty for method none"
        elif self.method != "none" and self.credentials is None:
            msg = "auth.credentials is required"
        elif not msal and self.tenant_id is not None:
            msg = "auth.tenant_id is only allowed for method msal_client_credentials"
        elif msal and (self.tenant_id is None or _GUID.fullmatch(self.tenant_id) is None):
            msg = "auth.tenant_id must be a GUID for method msal_client_credentials"
        else:
            return self
        raise ValueError(msg)


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

    start: Annotated[
        datetime.date | None,
        _rule(lambda v: v >= _EPOCH, "backfill.start must be on or after 1970-01-01"),
    ] = None
    slice_days: Annotated[int, _range("backfill.slice_days", 1, 366)] = 30

    def resolve_start(self, now: datetime.datetime) -> datetime.datetime:
        """Return the backfill start at 00:00:00 UTC; `None` means `now - 1096 days`."""
        if now.utcoffset() is None:
            msg = "naive datetime"
            raise ConfigError(msg)
        day = self.start or (now.astimezone(datetime.UTC) - datetime.timedelta(days=1096)).date()
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
    max_concurrency: int  # default CONCURRENCY_DEFAULTS[SOURCE], set per subclass below
    schedule: Annotated[str | None, _schedule("schedule")] = None
    reconcile: ReconcileSettings = Field(default_factory=ReconcileSettings)
    backfill: BackfillSettings = Field(default_factory=BackfillSettings)
    timeout_s: Annotated[float, _range("timeout_s", 1, 600)] = 60.0
    verify: Annotated[Path | None, BeforeValidator(_check_verify)] = None
    entities: Mapping[str, EntitySettings] = Field(default_factory=_ReadOnlyDict)
    hosts: Annotated[tuple[str, ...], BeforeValidator(_check_hosts)] = ()

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: object) -> None:
        """Give `max_concurrency` the default of the subclass's `SOURCE`."""
        super().__pydantic_init_subclass__(**kwargs)
        source = cls.__dict__.get("SOURCE")
        if isinstance(source, str) and source in CONCURRENCY_DEFAULTS:
            cls.model_fields["max_concurrency"].default = CONCURRENCY_DEFAULTS[source]
            cls.model_rebuild(force=True)

    @model_validator(mode="before")
    @classmethod
    def _check_source(cls, data: object) -> object:
        if getattr(cls, "SOURCE", None) not in CONCURRENCY_CAPS:
            msg = "source model must set SOURCE to a known connector name"
            raise ValueError(msg)
        return data

    @model_validator(mode="after")
    def _check_common(self) -> Self:
        cap = CONCURRENCY_CAPS[self.SOURCE]
        allowed = AUTH_METHODS.get(self.auth_key(), frozenset())
        if self.checkpoint_rows < self.batch_rows:
            msg = "checkpoint_rows must be >= batch_rows"
        elif not 1 <= self.max_concurrency <= cap:
            msg = f"max_concurrency must be >= 1 and <= {cap} for {self.SOURCE}"
        elif self.auth is not None and self.auth.method == "oauth_3lo":
            msg = "auth.method oauth_3lo is not supported in v1"
        elif self.auth is not None and self.auth.method not in allowed:
            msg = f"auth.method is not allowed for {self.auth_key()}"
        else:
            object.__setattr__(self, "entities", _ReadOnlyDict(self.entities))
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
