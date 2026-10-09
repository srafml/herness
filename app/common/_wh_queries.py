"""Named-query file loader and raw-text guard (impl 09 U09-61), re-exported by ``app.common.wh``.

Private helper split out of ``wh.py`` to keep that module inside its §2 line budget; the public
names stay ``app.common.wh.NamedQuery``, ``ParamSpec``, ``load_named_queries``,
``check_forbidden_columns`` and ``FORBIDDEN_TEXT_COLUMNS``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

import sqlglot
import yaml
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

from herness.core.errors import ConfigError

FORBIDDEN_TEXT_COLUMNS: Final = frozenset(
    {"short_description", "description", "close_notes", "root_cause_text"}
)
# U09-61 pattern plus a `_v<n>` variant suffix (U09-63 `evidence.meta_v1`).
_NAME_RE: Final = re.compile(r"^[a-z_]+\.[a-z_]+(?:_v\d+)?$", re.ASCII)

type ParamType = Literal["str", "int", "float", "bool", "date", "list_str"]


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
    default: str | int | float | bool | list[str] | None = None


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
        specs = {k: ParamSpec(p.type, p.required, p.default) for k, p in q.params.items()}
        out[name] = NamedQuery(name, q.sql, specs, cap)
    return out


def _star_over(column: exp.Expression, core: Mapping[str, str]) -> list[str]:
    if isinstance(column, exp.Star):
        return list(core.values())
    if isinstance(column, exp.Column) and isinstance(column.this, exp.Star):
        return [core[column.table]] if column.table in core else []
    return []


def check_forbidden_columns(queries: Mapping[str, NamedQuery]) -> list[str]:
    """Raw-text guard (U09-61): forbidden ``core`` columns and ``core`` stars, sorted."""
    found: set[str] = set()
    for name, nq in queries.items():
        for scope in traverse_scope(sqlglot.parse_one(nq.sql, read="duckdb")):
            core = {
                alias: f"core.{src.name}"
                for alias, src in scope.sources.items()
                if isinstance(src, exp.Table) and src.db == "core"
            }
            if not core:
                continue
            for column in scope.columns:
                if column.name.lower() not in FORBIDDEN_TEXT_COLUMNS:
                    continue
                # an unqualified column is attributed to every core table of its scope
                tables = [core.get(column.table)] if column.table else list(core.values())
                found.update(f"{name}: {t}.{column.name}" for t in tables if t is not None)
            if isinstance(scope.expression, exp.Select):
                for projection in scope.expression.expressions:
                    found.update(f"{name}: {t}.*" for t in _star_over(projection, core))
    return sorted(found)
