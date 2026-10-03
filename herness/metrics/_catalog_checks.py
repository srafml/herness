"""Pure SQL and template checks behind `validate_catalog` (impl 04 U04-26 steps 2-5 and 9).

Private split of `herness.metrics.catalog` (module budget). Every function returns problem
messages that hold names and statement types only, never SQL text or values (TH04-05).
"""

import re
import string
from collections.abc import Iterator
from typing import Final

import sqlglot
from sqlglot import exp

__all__ = ["LEVER_PLACEHOLDERS", "raw_sql_problems", "sql_problems", "template_problems"]

# U04-71 single source of truth: herness.metrics.levers re-exports this set (levers imports the
# step modules, which import the catalog, so the catalog checks cannot import levers).
LEVER_PLACEHOLDERS: Final[frozenset[str]] = frozenset(
    {"entity_name", "metric_label", "current_value", "target_value", "target_kind"}
    | {"unit", "delta_usd", "n_basis", "peer_group", "period"}
)
_FORBIDDEN_WORDS: Final = re.compile(
    r"(?<![A-Za-z0-9_])(description|short_description|close_notes|summary|root_cause_text"
    r"|text_redacted|text|label|top_terms|alert_name|host|answer)(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_FORBIDDEN_TOKENS: Final = ("$", "--", "/*", ";")
_FORBIDDEN_NODES: Final[tuple[type[exp.Expression], ...]] = (
    *(exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Alter),
    *(exp.Command, exp.Pragma, exp.Set, exp.Use, exp.Attach, exp.Detach, exp.Copy),
    *(exp.Export, exp.Transaction, exp.LoadData, exp.Install),
)
_FORBIDDEN_FUNC_PREFIXES: Final = ("read_", "sqlite_", "postgres_", "pragma_")
_FORBIDDEN_FUNCS: Final = frozenset(
    {"glob", "query", "query_table", "getenv", "current_setting", "load_extension"}
)
_SCHEMAS: Final = frozenset({"core", "enrich", "metrics"})
_REQUIRED_COLUMNS: Final = (
    *("entity_id", "period_start", "value"),
    *("numerator", "denominator", "sample_size"),
)
_OPTIONAL_COLUMNS: Final = ("coverage", "estimated_count", "unweighted")
_PLACEHOLDER: Final = re.compile(r"[a-z_]+")


def raw_sql_problems(sql: str, /) -> list[str]:
    """Steps 2-3 on the raw template: forbidden text columns and raw SQL tokens."""
    problems: list[str] = []
    words = sorted({w.lower() for w in _FORBIDDEN_WORDS.findall(sql)})
    if words:
        problems.append(f"sql uses text column {', '.join(words)}")
    tokens = [t for t in _FORBIDDEN_TOKENS if t in sql]
    if tokens:
        problems.append(f"sql contains forbidden token {' '.join(tokens)}")
    return problems


def _func_name(func: exp.Func) -> str:
    return (func.name if isinstance(func, exp.Anonymous) else func.sql_name()).lower()


def _tree_problems(tree: exp.Expr) -> Iterator[str]:
    """Forbidden node types, tables outside the schema allowlist and forbidden functions."""
    for node in tree.find_all(*_FORBIDDEN_NODES):
        yield f"statement type {type(node).__name__} not allowed"
    ctes = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
    for table in tree.find_all(exp.Table):
        plain = isinstance(table.this, exp.Identifier) and not table.catalog
        if not plain or not (table.db in _SCHEMAS or (not table.db and table.name in ctes)):
            yield f"table {table.name[:64] or type(table.this).__name__} not allowed"
    for func in tree.find_all(exp.Func):
        name = _func_name(func)
        if name.startswith(_FORBIDDEN_FUNC_PREFIXES) or name in _FORBIDDEN_FUNCS:
            yield f"function {name} not allowed"


def _inner_query(tree: exp.Expr) -> exp.Expr | None:
    """The catalog SELECT: the wrapper's `FROM (...) q` subquery."""
    frm = next((c for c in tree.iter_expressions() if isinstance(c, exp.From)), None)
    sub = None if frm is None else frm.this
    return sub.this if isinstance(sub, exp.Subquery) and sub.alias == "q" else None


def _columns_ok(names: list[str]) -> bool:
    """Step 5: required columns in order, then an ordered subset of the optional ones."""
    if tuple(names[: len(_REQUIRED_COLUMNS)]) != _REQUIRED_COLUMNS:
        return False
    rest = names[len(_REQUIRED_COLUMNS) :]
    positions = [_OPTIONAL_COLUMNS.index(c) if c in _OPTIONAL_COLUMNS else -1 for c in rest]
    return all(p >= 0 for p in positions) and positions == sorted(set(positions))


def sql_problems(sql: str, /) -> list[str]:
    """Steps 4-5 on one rendered wrapper: one statement, root, tree and output columns."""
    try:
        statements = sqlglot.parse(sql, read="duckdb")
    except sqlglot.errors.SqlglotError:
        return ["rendered sql does not parse"]
    if len(statements) != 1 or statements[0] is None:
        return ["rendered sql must be exactly one statement"]
    tree = statements[0]
    problems = list(_tree_problems(tree))
    inner = _inner_query(tree)
    if not isinstance(tree, exp.Select) or not isinstance(inner, exp.Select | exp.SetOperation):
        problems.append("catalog template must be one SELECT or set operation of SELECTs")
    elif not _columns_ok(inner.named_selects):
        required = ", ".join(_REQUIRED_COLUMNS)
        problems.append(f"catalog SELECT must output {required}, then optional columns")
    return problems


def template_problems(template: str, /) -> list[str]:
    """Step 9: allowlisted plain placeholders only, no conversion or format spec."""
    try:
        parts = list(string.Formatter().parse(template))
    except ValueError:
        return ["unbalanced braces"]
    problems: list[str] = []
    for _, field, spec, conversion in parts:
        if field is None:
            continue
        if _PLACEHOLDER.fullmatch(field) is None:
            problems.append(f"placeholder {field[:40]!r} is not a plain name")
        elif field not in LEVER_PLACEHOLDERS:
            problems.append(f"unknown placeholder {field}")
        if conversion is not None or spec:
            problems.append(f"placeholder {field[:40]!r} has a conversion or format spec")
    return problems
