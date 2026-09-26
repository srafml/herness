"""Scope helpers for the SQL guard (U05-37): CTE visibility, column sources, residual stars.

Private to `herness.harness.sql_guard`; split out to keep that module within its budget.
"""

from __future__ import annotations

from collections.abc import Mapping

from sqlglot import expressions as exp
from sqlglot.optimizer.scope import Scope


def visible_ctes(node: exp.Expr) -> set[str]:
    """CTE names visible from `node`: those of every enclosing query's `WITH` clause."""
    names: set[str] = set()
    parent = node.parent
    while parent is not None:
        with_ = parent.args.get("with_")
        if isinstance(with_, exp.With):
            names.update(cte.alias_or_name for cte in with_.expressions)
        parent = parent.parent
    return names


def enclosing_scope(node: exp.Expr, by_expr: Mapping[int, Scope]) -> Scope | None:
    """The innermost scope whose expression contains `node`."""
    parent = node.parent
    while parent is not None:
        scope = by_expr.get(id(parent))
        if scope is not None:
            return scope
        parent = parent.parent
    return None


def resolve(scope: Scope | None, name: str) -> exp.Table | Scope | None:
    """The source named `name` in `scope` or an enclosing scope, or None when unresolvable."""
    while scope is not None:
        source = scope.sources.get(name)
        if isinstance(source, exp.Table | Scope):
            return source
        scope = scope.parent
    return None


def has_residual_star(q: exp.Expr) -> bool:
    """True when qualify left a `*` / `t.*` unexpanded anywhere except `count(*)`."""
    return any(not isinstance(star.parent, exp.Count) for star in q.find_all(exp.Star))


def has_positional_column(q: exp.Expr) -> bool:
    """True when the query uses DuckDB `#n` positional column references."""
    return q.find(exp.PositionalColumn) is not None
