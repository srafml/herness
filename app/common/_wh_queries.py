"""Named-query file loader and raw-text guard (impl 09 U09-61), re-exported by ``app.common.wh``.

Private helper split out of ``wh.py`` to keep that module inside its §2 line budget; the public
names stay ``app.common.wh.NamedQuery``, ``ParamSpec``, ``load_named_queries``,
``check_forbidden_columns`` and ``FORBIDDEN_TEXT_COLUMNS``. ``PARAM_CHECKS`` is shared with
``wh.query`` so defaults (at load) and bound values (at call) pass the same strict checks.
"""

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
# U09-61 pattern plus a `_v<n>` variant suffix (U09-63 `evidence.meta_v1`).
_NAME_RE: Final = re.compile(r"^[a-z_]+\.[a-z_]+(?:_v\d+)?$", re.ASCII)
MAX_STR: Final = 200
MAX_LIST: Final = 500

type ParamType = Literal["str", "int", "float", "bool", "date", "list_str"]


def _short(v: object) -> bool:
    return isinstance(v, str) and len(v) <= MAX_STR


# Strict checks (U09-62 step 2): `int` refuses bool, `date` refuses datetime.
PARAM_CHECKS: Final[dict[str, Callable[[object], bool]]] = {
    "str": _short,
    "int": lambda v: type(v) is int,
    "float": lambda v: type(v) in {int, float},
    "bool": lambda v: isinstance(v, bool),
    "date": lambda v: type(v) is datetime.date,
    "list_str": lambda v: (
        isinstance(v, list | tuple) and len(v) <= MAX_LIST and all(_short(s) for s in v)
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
    used = {p.name for p in statements[0].find_all(exp.Placeholder)}
    if used != declared:
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


def _core_sources(scope: Scope) -> dict[str, str]:
    """Casefolded alias (and table name) -> ``core.<table>`` for the core tables of ``scope``."""
    core: dict[str, str] = {}
    for alias, src in scope.sources.items():
        if isinstance(src, exp.Table) and src.db.casefold() == "core":
            table = f"core.{src.name.casefold()}"
            core[alias.casefold()] = core[src.name.casefold()] = table
    return core


def _scope_violations(scope: Scope, core: Mapping[str, str]) -> set[str]:
    """``<table>.<column>`` / ``<table>.*`` reads of raw text in one scope (case-insensitive)."""
    tables = set(core.values())
    found: set[str] = set()
    for node in scope.find_all(exp.Column, exp.Columns, exp.Star):
        if isinstance(node, exp.Columns) or (  # COLUMNS(...) and projected `*`
            isinstance(node, exp.Star) and isinstance(node.parent, exp.Select | exp.Columns)
        ):
            found.update(f"{t}.*" for t in tables)
            continue
        if not isinstance(node, exp.Column):
            continue  # count(*) and similar aggregates read no column
        qualifier, name = node.table.casefold(), node.name.casefold()
        if isinstance(node.this, exp.Star):  # t.*
            if qualifier in core:
                found.add(f"{core[qualifier]}.*")
        elif name in core:  # whole-row reference: `i`, `to_json(i)`, `x.i`
            found.add(f"{core[name]}.*")
        elif name in FORBIDDEN_TEXT_COLUMNS:
            owners = [core[qualifier]] if qualifier in core else [] if qualifier else tables
            found.update(f"{t}.{node.name}" for t in owners)
    return found


def check_forbidden_columns(queries: Mapping[str, NamedQuery]) -> list[str]:
    """Raw-text guard (U09-61): forbidden ``core`` columns, ``core`` stars, whole-row reads."""
    found: set[str] = set()
    for name, nq in queries.items():
        for scope in traverse_scope(sqlglot.parse_one(nq.sql, read="duckdb")):
            if core := _core_sources(scope):
                found.update(f"{name}: {v}" for v in _scope_violations(scope, core))
    return sorted(found)
