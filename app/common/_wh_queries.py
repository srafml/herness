"""Named-query loader, strict parameter checks and the fail-closed raw-text guard (U09-61).

Private helper of ``wh.py`` (§2 line budget); ``app.common.wh`` re-exports the public names."""

from __future__ import annotations

import datetime
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

import sqlglot
import yaml
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, traverse_scope

from herness.core.errors import ConfigError

FORBIDDEN_TEXT_COLUMNS: Final = frozenset(
    {"short_description", "description", "close_notes", "root_cause_text"}
)
_NAME_RE: Final = re.compile(r"^[a-z_]+\.[a-z_]+(?:_v\d+)?$", re.ASCII)  # + `_v<n>` variants
MAX_STR: Final = 200
_SQL_OR_FILE_FUNCS: Final = ("query", "read_", "glob", "sniff_", "parquet_", "iceberg_")  # never
MAX_LIST: Final = 500

type ParamType = Literal["str", "int", "float", "bool", "date", "list_str"]


PARAM_CHECKS: Final[dict[str, Callable[[object], bool]]] = {  # strict (U09-62 step 2)
    "str": lambda v: isinstance(v, str) and len(v) <= MAX_STR,
    "int": lambda v: type(v) is int,  # not bool
    "float": lambda v: type(v) in {int, float},
    "bool": lambda v: isinstance(v, bool),
    "date": lambda v: type(v) is datetime.date,  # not datetime
    "list_str": lambda v: (
        isinstance(v, list | tuple) and len(v) <= MAX_LIST and all(map(PARAM_CHECKS["str"], v))
    ),
}


@dataclass(frozen=True)
class ParamSpec:
    type: ParamType
    required: bool
    default: object | None


@dataclass(frozen=True)
class NamedQuery:
    name: str
    sql: str
    params: dict[str, ParamSpec]
    row_cap: int


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _ParamModel(_Strict):
    type: ParamType
    required: bool = False
    default: object = None  # type-checked against `type` by load_named_queries


class _QueryModel(_Strict):
    sql: str
    params: dict[str, _ParamModel] = {}
    row_cap: int | None = None


class _FileModel(_Strict):
    version: Literal[1]
    queries: dict[str, _QueryModel]


def _bad(name: str, reason: str) -> ConfigError:
    return ConfigError(f"named query {name}: {reason}")


def _check_sql(name: str, sql: str, declared: set[str]) -> None:
    try:
        statements = sqlglot.parse(sql, read="duckdb")
    except sqlglot.errors.SqlglotError as exc:
        raise _bad(name, "SQL does not parse") from exc
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        raise _bad(name, "must be exactly one SELECT statement")
    if {p.name for p in statements[0].find_all(exp.Placeholder)} != declared:
        raise _bad(name, "placeholders do not match the declared params")


def _specs(name: str, q: _QueryModel) -> dict[str, ParamSpec]:
    for key, p in q.params.items():
        if p.default is not None and not PARAM_CHECKS[p.type](p.default):
            raise _bad(name, f"default of {key} does not match type {p.type}")
    return {k: ParamSpec(p.type, p.required, p.default) for k, p in q.params.items()}


def load_named_queries(path: Path, *, page_row_limit: int) -> dict[str, NamedQuery]:
    """Load and validate ``queries.sql.yaml`` (U09-61); any failure -> ``ConfigError``."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        model = _FileModel.model_validate(raw)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise _bad(path.name, "file unreadable or not a valid query file") from exc
    out: dict[str, NamedQuery] = {}
    for name, q in model.queries.items():
        if _NAME_RE.fullmatch(name) is None:
            raise _bad(name[:64], "invalid name")
        _check_sql(name, q.sql, set(q.params))
        cap = page_row_limit if q.row_cap is None else q.row_cap
        if not 1 <= cap <= page_row_limit:
            raise _bad(name, f"row_cap must be 1..{page_row_limit}")
        out[name] = NamedQuery(name, q.sql, _specs(name, q), cap)
    return out


def _core_sources(scope: Scope | None, *, outer: bool = True) -> dict[str, str]:
    """Casefolded alias/table -> ``core.<table>`` of ``scope`` (``outer``: and its parents)."""
    core: dict[str, str] = {}
    while scope is not None:  # inner names shadow outer ones
        for alias, src in scope.sources.items():
            if isinstance(src, exp.Table) and src.db.casefold() == "core":
                table = f"core.{src.name.casefold()}"
                core = {alias.casefold(): table, src.name.casefold(): table} | core
        scope = scope.parent if outer else None
    return core


def _structural(tree: exp.Expr) -> set[str]:
    """Fail-closed shapes: positional refs, PIVOT, non-table sources, SQL-string/file functions."""
    found = {f"positional column reference {p.sql()}" for p in tree.find_all(exp.PositionalColumn)}
    found |= {"PIVOT/UNPIVOT"} if tree.find(exp.Pivot) else set()
    ctes = {cte.alias.casefold() for cte in tree.find_all(exp.CTE)}
    for src in (node.this for node in tree.find_all(exp.From, exp.Join, exp.Lateral)):
        named = isinstance(src, exp.Table) and isinstance(src.this, exp.Identifier)
        if not (isinstance(src, exp.Subquery) or (named and (src.db or src.name in ctes))):
            shown = (src.this if isinstance(src, exp.Table) else src).sql("duckdb").casefold()
            found.add(f"source {shown} is not a table, CTE or subquery")
    for fn in tree.find_all(exp.Func):
        fname = (fn.name if isinstance(fn, exp.Anonymous) else fn.sql_name()).casefold()
        if fname.startswith(_SQL_OR_FILE_FUNCS):
            found.add(f"function {fname}")
    return found


def _scope_violations(scope: Scope) -> set[str]:
    """Raw-text reads in one scope; qualifiers resolve against this and every enclosing scope."""
    core, found = _core_sources(scope), set[str]()
    own = set(_core_sources(scope, outer=False).values())  # stars expand this scope only
    for node in scope.find_all(exp.Column, exp.Columns, exp.Star):
        star = isinstance(node, exp.Star) and isinstance(node.parent, exp.Select | exp.Columns)
        if star or isinstance(node, exp.Columns):  # projected `*` and COLUMNS(...)
            found.update(f"{t}.*" for t in own)
        if not isinstance(node, exp.Column):
            continue  # count(*) and similar aggregates read no column
        qualifier, name = node.table.casefold(), node.name.casefold()
        if isinstance(node.this, exp.Star) and qualifier in core:  # t.*
            found.add(f"{core[qualifier]}.*")
        elif name in core and (not qualifier or qualifier in core or qualifier == "core"):
            found.add(f"{core[name]}.*")  # whole-row reference: `i`, `to_json(i)`, `i.i`
        elif name in FORBIDDEN_TEXT_COLUMNS:  # unqualified: every core table in reach
            owners = [core[qualifier]] if qualifier in core else [] if qualifier else core.values()
            found.update(f"{t}.{node.name}" for t in owners)
    return found


def check_forbidden_columns(queries: Mapping[str, NamedQuery]) -> list[str]:
    """Raw-text guard (U09-61), fail closed: core text/star/whole-row reads, refused shapes."""
    found: set[str] = set()
    for name, nq in queries.items():
        tree = sqlglot.parse_one(nq.sql, read="duckdb")
        scopes = traverse_scope(tree) if isinstance(tree, exp.Query) else []
        hits = _structural(tree).union(*(_scope_violations(scope) for scope in scopes))
        found.update(f"{name}: {v}" for v in hits)
    return sorted(found)
