"""Sandboxed Jinja rendering of metric, score and check SQL with typed bind parameters.

Design 04 §4.1 and §9: one `ImmutableSandboxedEnvironment` per render, identifiers-only
context, every runtime value a `CAST($name AS <type>)` placeholder (U04-33 … U04-39).
"""

import dataclasses
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, Final, NoReturn, cast, get_args

import jinja2
import sqlglot
from jinja2.runtime import Macro
from jinja2.sandbox import ImmutableSandboxedEnvironment
from sqlglot import exp

from herness.core.errors import ConfigError
from herness.metrics._binds import BIND_TYPES, CatalogView, default_binds, weight_binds
from herness.metrics.settings import (
    EntityType,
    FilterKey,
    MetricDef,
    Period,
    Source,
    WeightsConfig,
    unconfirmed_blocks,
)
from herness.metrics.windows import Window

__all__ = [
    "BIND_TYPES",
    "CatalogView",
    "RenderState",
    "RenderedQuery",
    "default_binds",
    "make_environment",
    "render_metric_query",
    "render_named",
    "weight_binds",
]

_ALIAS: Final = re.compile(r"[a-z][a-z0-9_]{0,15}")
_IDENT: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")
_CHECK_MARK: Final = re.compile(r"^-- @check ([a-z][a-z0-9_]{0,63})[ \t]*\r?$", re.MULTILINE)
_NAMED: Final = frozenset(
    {"peer_group", "funding_attribution", "funding_score", "org_score", "levers", "portfolio_input"}
)
_RESERVED_CONTEXT: Final = frozenset({"rs", "filters"})
_REQUIRED_COLUMNS: Final = (
    "entity_id",
    "period_start",
    "value",
    "numerator",
    "denominator",
    "sample_size",
)
_OPTIONAL_COLUMNS: Final = ("coverage", "estimated_count", "unweighted")


class RenderState:
    """Per-render state the macros call into: used binds, sources and the entity expression."""

    def __init__(self) -> None:
        self.used: set[str] = set()
        self.sources: set[Source] = set()
        self._entity: str | None = None

    def p(self, name: object) -> str:
        """Record `name` and return its typed placeholder; unknown names fail."""
        if not isinstance(name, str) or name not in BIND_TYPES:
            self.fail(f"template uses unknown parameter {name}")
        self.used.add(name)
        return f"CAST(${name} AS {BIND_TYPES[name]})"

    def set_entity(self, expr: str) -> str:
        """Remember the entity expression for `entity_filter`; renders as empty text."""
        self._entity = expr
        return ""

    def entity(self) -> str:
        """The entity expression set by `entity_col`."""
        if self._entity is None:
            self.fail("entity_filter needs entity_col first")
        return self._entity

    def use_source(self, source: object) -> str:
        """Record a fact source; renders as empty text."""
        if source not in get_args(Source):
            self.fail(f"unknown source {source}")
        self.sources.add(cast("Source", source))
        return ""

    def alias(self, alias: object) -> str:
        """Return `alias` when it is a safe SQL alias, else fail."""
        if not isinstance(alias, str) or _ALIAS.fullmatch(alias) is None:
            self.fail("bad alias")
        return alias

    def fail(self, message: str) -> NoReturn:
        """Abort the render with a ConfigError."""
        raise ConfigError(message)


@dataclasses.dataclass(frozen=True, slots=True)
class RenderedQuery:
    """A rendered SELECT, the bind values it uses and its identifying template (U04-38)."""

    sql: str
    bind: dict[str, object]
    template: dict[str, object]
    sources: frozenset[Source]


def make_environment() -> ImmutableSandboxedEnvironment:
    """A fresh sandboxed environment over `herness/metrics/sql` (U04-33)."""
    env = ImmutableSandboxedEnvironment(
        loader=jinja2.PackageLoader("herness.metrics", "sql"),
        undefined=jinja2.StrictUndefined,
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    keep = env.globals["range"]
    env.globals.clear()
    env.globals["range"] = keep
    return env


def _guard[T](step: Callable[[], T]) -> T:
    try:
        return step()
    except jinja2.TemplateError as exc:
        msg = f"template render failed: {type(exc).__name__}: {exc}"
        raise ConfigError(msg) from exc


def _macros(env: ImmutableSandboxedEnvironment, ctx: Mapping[str, object]) -> dict[str, Any]:
    module = env.get_template("_macros.sql.j2").make_module(vars=dict(ctx))
    return {k: v for k, v in vars(module).items() if isinstance(v, Macro)}


def _output_columns(inner: str) -> list[str]:
    try:
        tree = sqlglot.parse_one(inner, read="duckdb")
    except sqlglot.errors.SqlglotError as exc:
        msg = "catalog SELECT does not parse"
        raise ConfigError(msg) from exc
    names = tree.named_selects if isinstance(tree, exp.Query) else []
    if any(col not in names for col in _REQUIRED_COLUMNS):
        msg = "catalog SELECT output columns must include " + ", ".join(_REQUIRED_COLUMNS)
        raise ConfigError(msg)
    return [col for col in _OPTIONAL_COLUMNS if col in names]


def _sorted_unique(values: Sequence[object]) -> list[object]:
    unique: list[Any] = list(dict.fromkeys(values))
    try:
        return sorted(unique)
    except TypeError as exc:
        msg = "filter values must share one type"
        raise ConfigError(msg) from exc


def _pick(used: set[str], candidates: Mapping[str, object]) -> dict[str, object]:
    missing = sorted(name for name in used if name not in candidates)
    if missing:
        msg = f"template uses {missing[0]} with no candidate value"
        raise ConfigError(msg)
    return {name: candidates[name] for name in sorted(used)}


def _check_request(entity_type: str, period: str, filters: Iterable[str]) -> None:
    if entity_type not in get_args(EntityType):
        msg = "unknown entity type"
        raise ConfigError(msg)
    if period not in get_args(Period):
        msg = "unknown period"
        raise ConfigError(msg)
    for key in filters:
        if key not in get_args(FilterKey):
            msg = "unknown filter key"
            raise ConfigError(msg)


def render_metric_query(  # noqa: PLR0913 - keyword-only signature fixed by U04-38
    metric: MetricDef,
    *,
    entity_type: EntityType,
    window: Window,
    filters: Mapping[FilterKey, list[object]],
    entity_ids: Sequence[str] | None,
    catalog: CatalogView,
    weights: WeightsConfig,
) -> RenderedQuery:
    """Render one metric for one grain, period and window into the wrapped SELECT (U04-38)."""
    _check_request(entity_type, window.period, filters.keys())
    env = make_environment()
    rs = RenderState()
    ctx: dict[str, object] = {
        "entity_type": entity_type,
        "period": window.period,
        "filters": {key: f"f_{key}" for key in filters},
        "rs": rs,
    }
    macros = _guard(lambda: _macros(env, ctx))
    inner = _guard(lambda: env.from_string(metric.sql).render({**ctx, **macros}))
    optional = _output_columns(inner)
    wrapper = {**ctx, **macros, "inner_sql": inner, "optional": optional}
    wrapper["is_custom"] = window.is_custom
    sql = _guard(lambda: env.get_template("metric_wrapper.sql.j2").render(wrapper))
    static_flags = sorted(
        (["estimate"] if metric.estimate else [])
        + (["unconfirmed_weights"] if unconfirmed_blocks(weights, metric.uses_weights) else [])
    )
    values = {key: _sorted_unique(filters[key]) for key in sorted(filters)}
    candidates: dict[str, object] = {
        **window.binds(),
        **default_binds(catalog),
        **weight_binds(weights),
        "entity_ids": None if entity_ids is None else sorted(set(entity_ids)),
        "min_n": metric.min_sample_size,
        "static_flags": static_flags,
        "metric": metric.name,
        "entity_type": entity_type,
        "period": window.period,
        "unit": metric.unit,
        **{f"f_{key}": vals for key, vals in values.items()},
    }
    template: dict[str, object] = {
        "name": metric.name,
        "entity_type": entity_type,
        "period": window.period,
        "filters": values,
    }
    return RenderedQuery(sql, _pick(rs.used, candidates), template, frozenset(rs.sources))


def _named_source(env: ImmutableSandboxedEnvironment, name: str) -> str:
    loader = env.loader
    assert loader is not None  # noqa: S101 - make_environment always sets a loader
    if name in _NAMED:
        return _guard(lambda: loader.get_source(env, f"{name}.sql.j2")[0])
    check = name.removeprefix("checks:")
    if check == name or _IDENT.fullmatch(check) is None:
        msg = "unknown template"
        raise ConfigError(msg)
    text = _guard(lambda: loader.get_source(env, "checks.sql.j2")[0])
    parts = _CHECK_MARK.split(text)
    bodies = dict(zip(parts[1::2], parts[2::2], strict=True))
    if check not in bodies:
        msg = f"unknown check {check}"
        raise ConfigError(msg)
    return bodies[check]


def _check_context(context: Mapping[str, object]) -> None:
    for key, value in context.items():
        if _IDENT.fullmatch(key) is None or key in _RESERVED_CONTEXT:
            msg = "bad template context key"
            raise ConfigError(msg)
        if isinstance(value, bool | int):
            continue
        if not isinstance(value, str) or _IDENT.fullmatch(value) is None:
            msg = f"template context {key} is not an identifier"
            raise ConfigError(msg)


def render_named(
    name: str, context: Mapping[str, str | int | bool], candidates: Mapping[str, object], /
) -> RenderedQuery:
    """Render a named score, peer or check template (`checks:<name>`) (U04-39)."""
    _check_context(context)
    env = make_environment()
    rs = RenderState()
    source = _named_source(env, name)
    ctx: dict[str, object] = {"filters": {}, **context, "rs": rs}
    macros = _guard(lambda: _macros(env, ctx))
    sql = _guard(lambda: env.from_string(source).render({**ctx, **macros}))
    template: dict[str, object] = {"name": name, **context}
    return RenderedQuery(sql, _pick(rs.used, candidates), template, frozenset(rs.sources))
