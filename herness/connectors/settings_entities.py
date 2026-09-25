"""Entity models, monitoring adapter models and pure validators of `sources.yaml` sections.

impl 01 §3.2 (U01-07 ... U01-13). Split out of `herness.connectors.settings`, which defines the
source sections and re-exports every public name here, to keep each module under the ENG
400-line limit. Settings import rule (R-03): this module imports only the standard library,
pydantic and `herness.connectors.settings_base`. Validators raise `ValueError` naming the key
path and never echo a value.
"""

import os
import re
from pathlib import Path
from typing import Annotated, Any, Final, Literal, Self

from pydantic import AfterValidator, BaseModel, BeforeValidator, Field, model_validator

import herness.connectors.settings_base as sb

_IDENT: Final = r"^[A-Za-z_][A-Za-z0-9_$]{0,254}$"
_SnName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,79}$")]
_MongoName = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$")]
Ident = Annotated[str, Field(pattern=_IDENT)]  # Snowflake identifier, safe to double-quote
_OData = Annotated[str, Field(pattern=r"^[a-z_][a-z0-9_]{0,127}$")]
_Label = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.]{0,63}$")]
_Column = Annotated[str, Field(min_length=1, max_length=128)]

_SPL_AGG: Final = re.compile(r"\|\s*(stats|tstats|table)\b")
_SPL_BANNED: Final = re.compile(
    r"\|\s*(outputlookup|collect|sendemail|script|run|delete|map|sendalert|outputcsv|dbxquery"
    r"|rest)\b"
)
_SQL_WORDS: Final = re.compile(
    r"\b(insert|update|delete|merge|drop|alter|create|grant|revoke|call|put|get|copy|use"
    r"|execute|truncate|undrop)\b",
    re.IGNORECASE,
)
_STEP: Final = re.compile(r"(\d{1,9})([smhd])")
_STEP_SECONDS: Final = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_PAGE_CAPS: Final = {"datadog": 1000, "dynatrace": 500}
_MONGO_BANNED: Final = frozenset({"$where", "$function", "$accumulator"})
_JSON_SCALARS: Final = (str, int, float, bool, type(None))
_FILE_SUFFIXES: Final = frozenset({".csv", ".xlsx", ".parquet"})


def _unique(key: str) -> AfterValidator:
    return sb._rule(lambda v: len(set(v)) == len(v), f"{key} must not contain duplicates")


def free_of(text: str, limit: int, tokens: tuple[str, ...]) -> bool:
    """`text` is at most `limit` chars and contains none of `tokens`."""
    return len(text) <= limit and not any(token in text for token in tokens)


def validate_spl(spl: str) -> None:
    """Raise `ValueError` unless `spl` is one read-only aggregating Splunk search (U01-09)."""
    text = spl.lower()
    if len(spl) > 4000:  # noqa: PLR2004 - U01-09 limit
        msg = "SPL must be <= 4000 chars"
    elif "`" in spl:
        msg = "SPL must not contain a backtick (macro expansion)"
    elif _SPL_BANNED.search(text):
        msg = "SPL command is not allowed (write, alert, script, map or REST)"
    elif not (_SPL_AGG.search(text) or text.startswith("| tstats")):
        msg = "SPL must aggregate with stats, tstats or table"
    else:
        return
    raise ValueError(msg)


class ServiceNowEntity(sb.EntitySettings):
    """One ServiceNow table (U01-07)."""

    fields: Annotated[list[_SnName], Field(min_length=1), _unique("fields")]
    window_hours: Annotated[int, Field(ge=1, le=168)] = 24
    classes: list[_SnName] | None = None
    filter: Annotated[
        str | None,
        sb._rule(
            lambda v: free_of(v, 1000, ("^NQ", "^EQ", "ORDERBY", "\n", "\r")),
            "filter must be <= 1000 chars without ^NQ, ^EQ, ORDERBY or line breaks",
        ),
    ] = None


class JiraEntity(sb.EntitySettings):
    """The Jira `issue` entity; adds no fields to `EntitySettings` (U01-08)."""


class MetricQuery(BaseModel):
    """One daily metric query of a monitoring adapter (U01-09)."""

    model_config = sb._CONFIG

    name: Annotated[
        str,
        sb._rule(lambda v: v in sb.DAILY_METRIC_NAMES, "name must be a DAILY_METRIC_NAMES entry"),
    ]
    query: Annotated[str, Field(min_length=1, max_length=4000)]
    agg: Literal["avg", "sum", "min", "max", "count"] | None = None
    unit: Annotated[str, Field(pattern=r"^[a-z_]{1,20}$")]
    step: Annotated[str, Field(max_length=16)] = "1d"
    service_label: _Label = "service"
    value_field: _Label = "value"
    service_dimension: _Label = "dt.entity.service"


class MonitoringAdapterSettings(BaseModel):
    """One monitoring tool; `MonitoringSettings` applies the tool-specific rules (U01-09)."""

    model_config = sb._CONFIG

    enabled: bool = False
    base_url: Annotated[str, AfterValidator(sb._check_base_url)]
    auth: sb.AuthSettings
    page_size: Annotated[int, Field(ge=1)] = 1000  # dynatrace: 500 when unset
    timeout_s: Annotated[float, Field(ge=1, le=600)] = 60.0
    verify: Annotated[Path | None, BeforeValidator(sb._check_verify)] = None
    tenant: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]{1,150}$")] | None = None
    event_query: str | None = None
    metric_queries: Annotated[
        list[MetricQuery],
        sb._rule(
            lambda v: len({q.name for q in v}) == len(v), "metric_queries names must be unique"
        ),
    ] = Field(default_factory=list)
    max_concurrency: int = 1  # CONCURRENCY_DEFAULTS[<tool>] when unset


def with_tool_defaults(tool: str, adapter: MonitoringAdapterSettings) -> MonitoringAdapterSettings:
    """Fill the tool's `max_concurrency` and (dynatrace) `page_size` defaults when unset."""
    updates: dict[str, int] = {}
    if "max_concurrency" not in adapter.model_fields_set:
        updates["max_concurrency"] = sb.CONCURRENCY_DEFAULTS[tool]
    if "page_size" not in adapter.model_fields_set and tool == "dynatrace":
        updates["page_size"] = _PAGE_CAPS[tool]
    return adapter.model_copy(update=updates) if updates else adapter


def adapter_error(tool: str, adapter: MonitoringAdapterSettings) -> str | None:
    """First tool-specific rule the adapter breaks, as `adapters.<tool>.<field> ...`."""
    key, cap = f"adapters.{tool}", sb.CONCURRENCY_CAPS[tool]
    page_cap = _PAGE_CAPS.get(tool, adapter.page_size)
    if adapter.auth.method == "oauth_3lo":
        return f"{key}.auth.method oauth_3lo is not supported in v1"
    if adapter.auth.method not in sb.AUTH_METHODS[tool]:
        return f"{key}.auth.method is not allowed for {tool}"
    if adapter.page_size > page_cap:
        return f"{key}.page_size must be >= 1 and <= {page_cap}"
    if not 1 <= adapter.max_concurrency <= cap:
        return f"{key}.max_concurrency must be >= 1 and <= {cap}"
    if adapter.tenant is not None and tool != "prometheus":
        return f"{key}.tenant is only allowed for prometheus"
    return _event_query_error(tool, adapter.event_query) or _queries_error(tool, adapter)


def _event_query_error(tool: str, query: str | None) -> str | None:
    key = f"adapters.{tool}.event_query"
    if query is None:
        return None
    if tool == "prometheus":
        return f"{key} must not be set for prometheus"
    if tool == "splunk":
        return _spl_error(query, key)
    return f"{key} must be <= 2000 chars" if len(query) > 2000 else None  # noqa: PLR2004


def _queries_error(tool: str, adapter: MonitoringAdapterSettings) -> str | None:
    for index, query in enumerate(adapter.metric_queries):
        key = f"adapters.{tool}.metric_queries.{index}"
        if tool == "datadog" and query.agg is None:
            return f"{key}.agg is required for datadog"
        if tool == "prometheus" and not _one_day(query.step):
            return f"{key}.step: prometheus step must be 1d"
        if tool == "splunk" and (error := _spl_error(query.query, f"{key}.query")):
            return error
    return None


def _one_day(step: str) -> bool:
    match = _STEP.fullmatch(step)
    return match is not None and int(match[1]) * _STEP_SECONDS[match[2]] == 86_400  # noqa: PLR2004


def _spl_error(spl: str, key: str) -> str | None:
    try:
        validate_spl(spl)
    except ValueError as exc:
        return f"{key}: {exc}"
    return None


def _check_filter(value: dict[str, Any]) -> dict[str, Any]:
    """Walk a MongoDB filter with an explicit stack: JSON only, no server-side JavaScript."""
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, dict | list) and depth > 8:  # noqa: PLR2004 - U01-10 limit
            msg = "filter nesting must be <= 8 levels"
            raise ValueError(msg)
        if isinstance(node, dict | list):
            stack.extend((child, depth + 1) for child in _filter_children(node))
        elif not isinstance(node, _JSON_SCALARS):
            msg = "filter must be JSON-compatible"
            raise ValueError(msg)  # noqa: TRY004 - pydantic reports only ValueError
    return value


def _filter_children(node: dict[Any, Any] | list[Any]) -> list[object]:
    if isinstance(node, list):
        return list(node)
    if len(node) > 64:  # noqa: PLR2004 - U01-10 limit
        msg = "filter must have <= 64 keys per level"
    elif not all(isinstance(key, str) for key in node):
        msg = "filter keys must be strings"
    elif banned := sorted(_MONGO_BANNED.intersection(node)):
        msg = f"filter must not use {banned[0]}"
    else:
        return list(node.values())
    raise ValueError(msg)


class MongoEntity(sb.EntitySettings):
    """One MongoDB collection (U01-10); `filter` is passed to pymongo unchanged."""

    collection: Annotated[
        str,
        Field(pattern=r"^[A-Za-z0-9_.-]{1,120}$"),
        sb._rule(lambda v: not v.startswith("system."), "collection must not start with system."),
    ]
    key_field: _MongoName = "_id"
    updated_field: _MongoName
    fields: Annotated[list[_MongoName], Field(min_length=1), _unique("fields")]
    filter: Annotated[dict[str, Any], AfterValidator(_check_filter)] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_top_level(self) -> Self:
        if self.updated_field in self.filter or "$or" in self.filter:
            msg = "filter must not set the updated_field or $or at the top level"
            raise ValueError(msg)
        return self


def _three_part(table: str) -> bool:
    parts = table.split(".")
    return len(parts) == 3 and all(re.fullmatch(_IDENT, p) for p in parts)  # noqa: PLR2004


class SnowflakeEntity(sb.EntitySettings):
    """One Snowflake table (U01-11); every identifier is safe to double-quote."""

    table: Annotated[str, sb._rule(_three_part, "table must be <database>.<schema>.<table>")]
    key_field: Ident
    updated_field: Ident
    columns: Annotated[list[Ident], Field(min_length=1), _unique("columns")]
    filter: Annotated[
        str | None,
        sb._rule(
            lambda v: free_of(v, 1000, (";", "--", "/*", "*/")) and not _SQL_WORDS.search(v),
            "filter must be one read-only predicate of <= 1000 chars",
        ),
    ] = None

    @model_validator(mode="after")
    def _check_columns(self) -> Self:
        if not {self.key_field, self.updated_field} <= set(self.columns):
            msg = "key_field and updated_field must be listed in columns"
            raise ValueError(msg)
        return self


class DataverseEntity(sb.EntitySettings):
    """One Dataverse entity set (U01-12); names are plain OData identifiers."""

    entityset: _OData
    key_field: _OData
    updated_field: _OData = "modifiedon"
    select: Annotated[list[_OData], Field(min_length=1), _unique("select")]


def _file_glob(pattern: str) -> bool:
    suffix = os.path.splitext(pattern)[1].lower()
    return free_of(pattern, 200, ("/", "\\", "..", "\x00")) and suffix in _FILE_SUFFIXES


_Sheet = Annotated[
    str,
    Field(min_length=1, max_length=31),
    sb._rule(lambda v: not set(v) & set("[]:*?/\\"), "sheet must not contain []:*?/\\"),
]


class FilesEntity(sb.EntitySettings):
    """One inbox sub-folder (U01-13); globs cannot leave the entity folder."""

    pattern: Annotated[
        str, sb._rule(_file_glob, "pattern must be a glob of <= 200 chars for .csv/.xlsx/.parquet")
    ]
    sheet: _Sheet | None = None
    key_field: Annotated[list[_Column], Field(min_length=1, max_length=10)]
    updated_field: _Column | None = None
    mode: Literal["delta", "snapshot"] = "delta"

    @model_validator(mode="after")
    def _check_sheet(self) -> Self:
        if self.sheet is not None and not self.pattern.lower().endswith(".xlsx"):
            msg = "sheet is only allowed with an .xlsx pattern"
            raise ValueError(msg)
        return self
