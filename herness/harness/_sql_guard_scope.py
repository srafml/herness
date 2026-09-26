"""Scope helpers for the SQL guard (U05-37): CTE visibility, column sources, residual stars.

Private to `herness.harness.sql_guard`; split out to keep that module within its budget.
"""

from __future__ import annotations

from collections.abc import Mapping

from sqlglot import expressions as exp
from sqlglot.optimizer.scope import Scope


def visible_ctes(node: exp.Expr) -> set[str]:
    """CTE names DuckDB binds at `node`, in binding order (I2b).

    From a query body: every CTE of its `WITH`. From inside a CTE body: only the CTEs
    defined earlier in that `WITH`, plus the CTE itself when the `WITH` is RECURSIVE.
    """
    names: set[str] = set()
    inside: exp.CTE | None = None
    parent = node.parent
    while parent is not None:
        if isinstance(parent, exp.CTE) and inside is None:
            inside = parent
        with_ = parent.args.get("with_")
        if isinstance(with_, exp.With):
            ctes = list(with_.expressions)
            idx = next((i for i, c in enumerate(ctes) if c is inside), None)
            if idx is not None:
                ctes = ctes[: idx + 1] if with_.args.get("recursive") else ctes[:idx]
                inside = None
            names.update(cte.alias_or_name for cte in ctes)
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


def _is_table_function(source: exp.Table | Scope) -> bool:
    if isinstance(source, exp.Table):
        return isinstance(source.this, exp.Func)
    return not isinstance(source.expression, exp.Query)  # UDTF scope such as unnest


def has_residual_star(q: exp.Expr) -> bool:
    """True when qualify left a `*` / `t.*` unexpanded anywhere except `count(*)`."""
    return any(not isinstance(star.parent, exp.Count) for star in q.find_all(exp.Star))


def has_leaky_star(q: exp.Expr, by_expr: Mapping[int, Scope]) -> bool:
    """True when an unexpanded `*` (not `count(*)`) has a scope source that is not a table
    function, or an unknown scope (fail closed)."""
    for star in q.find_all(exp.Star):
        if isinstance(star.parent, exp.Count):
            continue
        scope = enclosing_scope(star, by_expr)
        if scope is None or not all(_is_table_function(s) for s in scope.sources.values()):
            return True
    return False


def has_positional_column(q: exp.Expr) -> bool:
    """True when the query uses DuckDB `#n` positional column references."""
    return q.find(exp.PositionalColumn) is not None
