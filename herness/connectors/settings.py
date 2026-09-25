"""Per-source section models of `sources.yaml`, the `sources` section and host allowlist.

impl 01 §3.2 (U01-05 ... U01-15). Entity and adapter models live in
`herness.connectors.settings_entities` and are re-exported here. Settings import rule (R-03):
this module imports only the standard library, pydantic, `herness.core.errors` and the
connector settings modules. `SourcesConfig` holds `version` and `sources`; T10-03 composes it
with the sibling `dq` and `build` sections of `herness.model.settings` (R-69, D-16).
Validators raise `ValueError` naming the key path and never echo a value.
"""

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, ClassVar, Final, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, BeforeValidator, Field, model_validator

import herness.connectors.settings_base as sb
import herness.connectors.settings_entities as se
from herness.connectors.settings_entities import (
    DataverseEntity,
    FilesEntity,
    JiraEntity,
    MetricQuery,
    MongoEntity,
    MonitoringAdapterSettings,
    ServiceNowEntity,
    SnowflakeEntity,
    validate_spl,
)
from herness.core.errors import ConfigError

SourceSettings = sb.SourceSettings  # re-export (impl 01 §2)
__all__ = [
    "DataverseEntity", "DataverseSettings", "FilesEntity", "FilesSettings", "JiraEntity",
    "JiraSettings", "MetricQuery", "MongoEntity", "MongoSettings", "MonitoringAdapterSettings",
    "MonitoringSettings", "ServiceNowEntity", "ServiceNowSettings", "SnowflakeEntity",
    "SnowflakeSettings", "SourceSettings", "SourcesConfig", "SourcesSection", "allowed_hosts",
    "validate_spl",
]  # fmt: skip

_Key = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
_Tool = Literal["prometheus", "datadog", "splunk", "dynatrace"]
_JQL_ORDER: Final = re.compile(r"order\s+by", re.IGNORECASE)
_MONITORING_ENTITIES: Final = ("event", "metric_daily")
_ORDER: Final = ("servicenow", "jira", "monitoring", "mongodb", "snowflake", "dataverse", "files")


class _Source(sb.SourceSettings):
    """Shared per-source rules: required or forbidden `base_url`/`auth`, page-size bounds (M5)."""

    NEEDS_URL: ClassVar[bool] = True
    NEEDS_AUTH: ClassVar[bool] = True
    PAGE_BOUNDS: ClassVar[tuple[int, int | None]] = (1, None)

    def page_bounds(self) -> tuple[int, int | None]:
        """Inclusive `page_size` bounds for the source and each entity override."""
        return self.PAGE_BOUNDS

    def hosts_error(self) -> str | None:
        """Message when the `hosts` list lacks a host this source requires (R-06)."""
        return None

    @model_validator(mode="after")
    def _check_shape(self) -> Self:
        msg = _presence("base_url", self.base_url, needed=self.NEEDS_URL)
        msg = msg or _presence("auth", self.auth, needed=self.NEEDS_AUTH) or self.hosts_error()
        sizes: list[tuple[str, int | None]] = [("page_size", self.page_size)]
        sizes += [(f"entities.{n}.page_size", e.page_size) for n, e in self.entities.items()]
        low, high = self.page_bounds()
        for key, size in sizes:
            if msg is None and size is not None and not low <= size <= (high or size):
                msg = f"{key} must be >= {low}" + (f" and <= {high}" if high else "")
        if msg:
            raise ValueError(msg)
        return self


def _presence(key: str, value: object, *, needed: bool) -> str | None:
    if needed and value is None:
        return f"{key} is required"
    return f"{key} must not be set for this source" if not needed and value is not None else None


def _set_entities(model: sb.SourceSettings, entities: Mapping[str, sb.EntitySettings]) -> None:
    object.__setattr__(model, "entities", sb._ReadOnlyDict(entities))


class ServiceNowSettings(_Source):
    """ServiceNow source section (U01-07)."""

    SOURCE: ClassVar[str] = "servicenow"
    PAGE_BOUNDS: ClassVar[tuple[int, int | None]] = (100, 10_000)

    overlap_minutes: Annotated[int, Field(ge=0, le=1440)] = 60
    entities: Annotated[Mapping[_Key, ServiceNowEntity], Field(min_length=1)]

    @model_validator(mode="after")
    def _check_servicenow(self) -> Self:
        entities = dict(self.entities)
        for name, entity in entities.items():
            if name not in sb.SERVICENOW_ENTITIES:
                msg = f"entities.{name} is not a ServiceNow entity"
            elif (name == "cmdb_ci") != (entity.classes is not None):
                rule = "required" if name == "cmdb_ci" else "only allowed for cmdb_ci"
                msg = f"entities.{name}.classes is {rule}"
            else:
                continue
            raise ValueError(msg)
        if "incident" in entities:  # design 01 §5.5: incident backfill slices default to 7 days
            entities["incident"] = _incident_slice(entities["incident"])
        _set_entities(self, entities)
        return self


def _incident_slice(entity: ServiceNowEntity) -> ServiceNowEntity:
    backfill = entity.backfill
    if backfill is None:
        backfill = sb.BackfillSettings(slice_days=7)
    elif "slice_days" not in backfill.model_fields_set:
        backfill = backfill.model_copy(update={"slice_days": 7})
    return entity.model_copy(update={"backfill": backfill})


class JiraSettings(_Source):
    """Jira source section (U01-08); `flavor` decides auth methods and page bounds."""

    SOURCE: ClassVar[str] = "jira"

    flavor: Literal["cloud", "datacenter"]
    page_size: int = 100
    overlap_minutes: Annotated[int, Field(ge=0, le=1440)] = 60
    jql_scope: Annotated[
        str | None,
        sb._rule(
            lambda v: se.free_of(v, 2000, ("\n", "\r")) and not _JQL_ORDER.search(v),
            "jql_scope must be <= 2000 chars without ORDER BY or line breaks",
        ),
    ] = None
    fetch_remote_links: bool = False
    entities: Mapping[str, JiraEntity] = Field(default_factory=lambda: {"issue": JiraEntity()})

    def auth_key(self) -> str:
        """`jira:cloud` or `jira:datacenter`."""
        return f"jira:{self.flavor}"

    def page_bounds(self) -> tuple[int, int | None]:
        """1-100 on Cloud, 1-1000 on Data Center."""
        return (1, 100 if self.flavor == "cloud" else 1000)

    @model_validator(mode="after")
    def _check_jira(self) -> Self:
        if set(self.entities) != {"issue"}:
            msg = "entities must be exactly {issue}"
            raise ValueError(msg)
        return self


class MonitoringSettings(_Source):
    """Monitoring source section; URLs and credentials live on the adapters (U01-09)."""

    SOURCE: ClassVar[str] = "monitoring"
    NEEDS_URL: ClassVar[bool] = False
    NEEDS_AUTH: ClassVar[bool] = False

    entities: Mapping[str, sb.EntitySettings] = Field(
        default_factory=lambda: {name: sb.EntitySettings() for name in _MONITORING_ENTITIES}
    )
    adapters: Mapping[_Tool, MonitoringAdapterSettings] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_monitoring(self) -> Self:
        adapters: dict[str, MonitoringAdapterSettings] = {
            tool: se.with_tool_defaults(tool, a) for tool, a in self.adapters.items()
        }
        unknown = sorted(set(self.entities) - set(_MONITORING_ENTITIES))
        errors = [
            f"entities.{unknown[0]} is not a monitoring entity" if unknown else None,
            "monitoring is not reconciled" if "reconcile" in self.model_fields_set else None,
            *(se.adapter_error(tool, adapter) for tool, adapter in adapters.items()),
        ]
        if self.enabled and not any(a.enabled for a in adapters.values()):
            errors.append("monitoring.enabled requires at least one enabled adapter")
        if msg := next((e for e in errors if e), None):
            raise ValueError(msg)
        default = sb.EntitySettings()
        _set_entities(self, {n: self.entities.get(n, default) for n in _MONITORING_ENTITIES})
        object.__setattr__(self, "adapters", sb._ReadOnlyDict(adapters))
        return self


class MongoSettings(_Source):
    """MongoDB source section (U01-10); `hosts` lists every host of the URI (R-06)."""

    SOURCE: ClassVar[str] = "mongodb"
    NEEDS_URL: ClassVar[bool] = False
    PAGE_BOUNDS: ClassVar[tuple[int, int | None]] = (1, 10_000)

    database: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
    max_time_ms: Annotated[int, Field(ge=1000, le=600_000)] = 60_000
    entities: Annotated[Mapping[_Key, MongoEntity], Field(min_length=1)]

    def hosts_error(self) -> str | None:
        """`hosts` must be non-empty (checked against the URI at connect time, U01-86)."""
        return None if self.hosts else "hosts must list every MongoDB host of the connection URI"


class SnowflakeSettings(_Source):
    """Snowflake source section (U01-11); `hosts` must list the account host (R-06)."""

    SOURCE: ClassVar[str] = "snowflake"
    NEEDS_URL: ClassVar[bool] = False

    account: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]{1,255}$")]
    warehouse: se.Ident
    role: se.Ident
    statement_timeout_s: Annotated[int, Field(ge=1, le=86_400)] = 900
    max_scan_gb: Annotated[float, Field(gt=0, le=10_000)] = 50.0
    page_size: int = 10_000  # unused by the connector
    entities: Annotated[Mapping[_Key, SnowflakeEntity], Field(min_length=1)]

    def hosts_error(self) -> str | None:
        """The account host must be listed; it is never derived from `account`."""
        host = f"{self.account.lower()}.snowflakecomputing.com"
        return None if host in self.hosts else "hosts must list the Snowflake account host"


class DataverseSettings(_Source):
    """Dataverse source section (U01-12); `hosts` must list the MSAL authority (R-06)."""

    SOURCE: ClassVar[str] = "dataverse"
    PAGE_BOUNDS: ClassVar[tuple[int, int | None]] = (1, 5000)

    page_size: int = 5000
    entities: Annotated[Mapping[_Key, DataverseEntity], Field(min_length=1)]

    def hosts_error(self) -> str | None:
        """The MSAL authority host must be listed; the `base_url` host need not be."""
        ok = "login.microsoftonline.com" in self.hosts
        return None if ok else "hosts must list login.microsoftonline.com"


class FilesSettings(_Source):
    """Files source section (U01-13); `max_concurrency` is capped at 1."""

    SOURCE: ClassVar[str] = "files"
    NEEDS_URL: ClassVar[bool] = False
    NEEDS_AUTH: ClassVar[bool] = False

    # Lax: YAML has no path type (impl 10 X-4). Resolving against paths.data and the
    # symlink check need cfg.paths, so the files connector does them.
    inbox: Annotated[Path, Field(strict=False)] = Path("data/inbox")
    entities: Annotated[Mapping[_Key, FilesEntity], Field(min_length=1)]


def _not_bool(value: object) -> object:
    # Literal[1] alone would accept YAML `true` (True == 1).
    if isinstance(value, bool):
        msg = "version must be 1"
        raise ValueError(msg)  # noqa: TRY004 - pydantic reports only ValueError
    return value


class SourcesSection(BaseModel):
    """The `sources` key of `sources.yaml` (U01-14); unknown source names fail."""

    model_config = sb._CONFIG

    servicenow: ServiceNowSettings | None = None
    jira: JiraSettings | None = None
    monitoring: MonitoringSettings | None = None
    mongodb: MongoSettings | None = None
    snowflake: SnowflakeSettings | None = None
    dataverse: DataverseSettings | None = None
    files: FilesSettings | None = None


class SourcesConfig(BaseModel):
    """Versioned connectors part of `sources.yaml`; T10-03 subclasses it to add `dq`, `build`."""

    model_config = sb._CONFIG

    version: Annotated[Literal[1], BeforeValidator(_not_bool)]
    sources: SourcesSection = Field(default_factory=SourcesSection)

    def enabled_sources(self) -> list[tuple[str, sb.SourceSettings]]:
        """Enabled sections in the fixed order servicenow ... files."""
        sections = ((name, getattr(self.sources, name)) for name in _ORDER)
        return [(name, src) for name, src in sections if src is not None and src.enabled]

    def source(self, name: str) -> sb.SourceSettings:
        """The named section, enabled or not; `ConfigError` when it is not configured."""
        section = getattr(self.sources, name) if name in _ORDER else None
        if not isinstance(section, sb.SourceSettings):
            msg = f"source {name} is not configured"
            raise ConfigError(msg, source=name)
        return section


def allowed_hosts(cfg: SourcesConfig) -> frozenset[str]:
    """Lower-cased host names connectors may reach, for the socket guard (U01-15, R-06).

    `base_url` hosts of enabled sources and enabled monitoring adapters, plus every `hosts`
    entry of enabled sources. Nothing is derived from `account`, a URI or a tenant.
    """
    hosts: set[str] = set()
    for _, src in cfg.enabled_sources():
        urls = [src.base_url]
        if isinstance(src, MonitoringSettings):
            urls += [a.base_url for a in src.adapters.values() if a.enabled]
        hosts.update(host for url in urls if url and (host := urlsplit(url).hostname))
        hosts.update(src.hosts)
    return frozenset(hosts)
