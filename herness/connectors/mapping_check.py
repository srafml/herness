"""`herness sync --check-mapping`: staging SQL columns the config does not fetch (U01-56).

Design 01 §4.2: the staging file of a source (spec 02 §4.1) is rendered with T02-11
(herness.model.sqlfiles.render_sql) and walked with sqlglot. Every column read straight from a
``read_parquet`` over ``raw/<source>/<entity>/`` must be one the configuration fetches.
Nothing is written and no source is called.

The render sees a synthetic lake inventory: each configured entity of the source is present
and claims every column, so ``raw()`` emits the column the SQL asks for rather than a typed
NULL; an entity the configuration does not fetch renders as absent and is not checked.
"""

from __future__ import annotations

import dataclasses
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Final

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from herness.connectors.jira import JIRA_ISSUE_COLUMNS
from herness.connectors.monitoring.base import EVENT_COLUMNS, METRIC_COLUMNS
from herness.connectors.rows import to_snake
from herness.connectors.settings import (
    DataverseSettings,
    JiraSettings,
    MongoSettings,
    MonitoringSettings,
    ServiceNowSettings,
    SnowflakeSettings,
)
from herness.connectors.settings_base import SourceSettings
from herness.core.config import HernessConfig
from herness.core.errors import ConfigError
from herness.model.lakeinfo import EntityInventory, LakeInventory
from herness.model.render_context import build_render_context
from herness.model.sqlfiles import discover_sql_files, render_sql
from herness.store.lake import lake_glob

__all__ = ["STAGING_FILES", "MappingIssue", "check_mapping"]

STAGING_FILES: Final[Mapping[str, str]] = {
    "servicenow": "110_stg_servicenow.sql",
    "jira": "120_stg_jira.sql",
    "monitoring": "130_stg_monitoring.sql",
    "files": "140_stg_files.sql",
    "mongodb": "150_stg_mongodb.sql",
    "snowflake": "160_stg_snowflake.sql",
    "dataverse": "170_stg_dataverse.sql",
}
# files: headers are only known from the files themselves (U01-56 step 6).
_SKIPPED: Final = frozenset({"files"})
_SERVICENOW_ALWAYS: Final = ("sys_id", "sys_updated_on", "sys_class_name")
# The virtual column of ``read_parquet(..., filename = true)`` is never a source field.
_VIRTUAL: Final = frozenset({"filename"})
_RAW_ROOT: Final = Path("raw")
_BUILD_ID: Final = "20000101-000000-000000"  # staging SQL never reads it; valid shape only


@dataclasses.dataclass(frozen=True, slots=True)
class MappingIssue:
    """A raw column the staging ``file`` reads from ``entity`` that the config never fetches."""

    entity: str
    column: str
    file: str


class _EveryColumn(frozenset[str]):
    """An empty column set that claims to hold every column, so ``raw()`` emits each one."""

    __slots__ = ()

    def __contains__(self, item: object) -> bool:
        return True


def check_mapping(
    source: str, cfg: HernessConfig, *, sql_dir: Path | None = None
) -> list[MappingIssue]:
    """Issues of ``source`` sorted by ``(entity, column)``; empty means the mapping passes.

    ``sql_dir`` (default ``herness/model/sql``) is for tests. Raises ConfigError for a source
    that is not configured, and ``cannot parse <file>`` when rendering or parsing fails.
    """
    section = cfg.sources.source(source)
    if source in _SKIPPED:
        return []
    custom = _jira_custom(cfg) if isinstance(section, JiraSettings) else frozenset()
    fetched = {entity: _fetched(section, entity) | custom for entity in section.entities}
    name = STAGING_FILES[source]
    required = _required(source, name, _render(source, name, cfg, fetched, sql_dir))
    issues = [
        MappingIssue(entity, column, name)
        for entity, columns in required.items()
        for column in columns - fetched.get(entity, frozenset())
    ]
    return sorted(issues, key=lambda i: (i.entity, i.column))


def _fetched(section: SourceSettings, entity: str) -> frozenset[str]:
    """Lake column names the configuration fetches for ``entity`` (U01-56 step 6)."""
    names: Iterable[str] = ()
    if isinstance(section, ServiceNowSettings):
        fields = section.entities[entity].fields
        raw = (*fields, *(f + "_display" for f in fields), *_SERVICENOW_ALWAYS)
        names = map(to_snake, raw)
    elif isinstance(section, MongoSettings):
        m = section.entities[entity]
        names = map(to_snake, (*m.fields, m.key_field, m.updated_field))
    elif isinstance(section, SnowflakeSettings):
        names = map(to_snake, section.entities[entity].columns)
    elif isinstance(section, DataverseSettings):
        d = section.entities[entity]
        names = (*d.select, d.key_field, d.updated_field, *(c + "_display" for c in d.select))
    elif isinstance(section, MonitoringSettings):  # fixed lake columns (U01-80)
        names = EVENT_COLUMNS if entity == "event" else METRIC_COLUMNS
    elif isinstance(section, JiraSettings):  # the raw column contract (U01-71, U01-93)
        names = JIRA_ISSUE_COLUMNS
    return frozenset(names)


def _jira_custom(cfg: HernessConfig) -> frozenset[str]:
    """The configured Jira custom field ids, fetched as columns of their own name."""
    return frozenset(v for v in cfg.mappings.custom_fields.jira.model_dump().values() if v)


def _render(
    source: str,
    name: str,
    cfg: HernessConfig,
    fetched: Mapping[str, frozenset[str]],
    sql_dir: Path | None,
) -> str:
    """The staging file rendered over the synthetic inventory of the configured entities."""
    entities = {
        (source, entity): EntityInventory(
            source=source,
            entity=entity,
            glob=lake_glob(_RAW_ROOT, source, entity),
            present=True,
            files=1,
            bytes=0,
            columns=_EveryColumn(),
            from_synth=False,
        )
        for entity in fetched
    }
    try:
        context = build_render_context(cfg, LakeInventory(_RAW_ROOT, entities), _BUILD_ID)
        file = next(f for f in discover_sql_files(sql_dir=sql_dir) if f.name == name)
        return render_sql(file, context)
    except (ConfigError, StopIteration) as exc:
        msg = f"cannot parse {name}"
        raise ConfigError(msg) from exc


def _required(source: str, name: str, text: str) -> dict[str, set[str]]:
    """Raw columns per entity the rendered SQL reads (U01-56 steps 2-5)."""
    try:
        statements = sqlglot.parse(text, read="duckdb")
    except SqlglotError as exc:
        msg = f"cannot parse {name}"
        raise ConfigError(msg) from exc
    pattern = re.compile(rf"raw/{re.escape(source)}/(?P<entity>[a-z0-9_]+)/")
    required: dict[str, set[str]] = defaultdict(set)
    for statement in statements:
        if statement is not None:
            _walk(statement, pattern, required)
    return dict(required)


def _walk(statement: exp.Expr, pattern: re.Pattern[str], required: dict[str, set[str]]) -> None:
    scope = _Scope(statement, pattern)
    for column in statement.find_all(exp.Column):
        if not isinstance(column.this, exp.Identifier) or column.find_ancestor(exp.ReadParquet):
            continue
        entity = scope.entity_of(column)
        name = column.name.lower()
        if entity is not None and not name.startswith("_") and name not in _VIRTUAL:
            required[entity].add(name)


class _Scope:
    """The ``read_parquet`` sources of one statement, their aliases and CTE names."""

    def __init__(self, statement: exp.Expr, pattern: re.Pattern[str]) -> None:
        self.tables: dict[int, str] = {}  # id(Table node) -> entity
        self.names: dict[str, str] = {}  # table alias or CTE name -> entity
        for call in statement.find_all(exp.ReadParquet):
            entity = _entity(call, pattern)
            table = call.parent
            if entity is None or not isinstance(table, exp.Table):
                continue
            self.tables[id(table)] = entity
            if table.alias:
                self.names[table.alias.lower()] = entity
            select = table.parent_select
            cte = select.parent if select is not None else None
            if isinstance(cte, exp.CTE) and _only_source(select, table):
                self.names[cte.alias.lower()] = entity

    def entity_of(self, column: exp.Column) -> str | None:
        """The entity whose raw columns ``column`` reads, if any (U01-56 step 4)."""
        if column.table:
            return self.names.get(column.table.lower())
        select = column.find_ancestor(exp.Select)
        sources = _sources(select) if select is not None else []
        return self._source_entity(sources[0]) if len(sources) == 1 else None

    def _source_entity(self, source: exp.Expr) -> str | None:
        if id(source) in self.tables:
            return self.tables[id(source)]
        if isinstance(source, exp.Table) and not source.db:
            return self.names.get(source.name.lower())
        return None


def _entity(call: exp.ReadParquet, pattern: re.Pattern[str]) -> str | None:
    first = call.expressions[0] if call.expressions else None
    if not isinstance(first, exp.Literal) or not first.is_string:
        return None
    match = pattern.search(first.this)
    return match.group("entity") if match else None


def _only_source(select: exp.Select | None, table: exp.Table) -> bool:
    sources = _sources(select) if select is not None else []
    return len(sources) == 1 and sources[0] is table


def _sources(select: exp.Select) -> list[exp.Expr]:
    """The FROM item and every JOIN item of ``select``."""
    from_ = select.args.get("from_")
    items: list[exp.Expr] = [from_.this] if isinstance(from_, exp.From) else []
    items += [join.this for join in select.args.get("joins") or ()]
    return items
