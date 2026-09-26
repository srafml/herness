"""SQL guard: every agent SQL passes here before the read-only warehouse runs it (U05-37).

Design 05 §5.4.3: rules 1-7, lineage and a DuckDB second parse; the SQL that runs is the
original text. Identifiers compare after NFKC + casefold (TH05-05). Fail closed: unresolved
columns reach every query table that has them, a table alias column list reads every column,
and a lineage failure marks every output. VI-10 on DuckDB 1.5.5: roots are `SELECT_NODE`
(plain, WITH, WITH RECURSIVE) or `SET_OPERATION_NODE`; the spec's four names are kept.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Final, NamedTuple, NoReturn, cast

import duckdb
import sqlglot
from rapidfuzz import fuzz, process
from sqlglot import expressions as exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, traverse_scope
from sqlglot.schema import MappingSchema

from herness.core.errors import QueryError
from herness.core.ids import normalize_sql
from herness.core.logging import get_logger

ALLOWED_SCHEMAS: Final = frozenset({"core", "enrich", "metrics", "score", "meta"})
DENIED_SCHEMAS: Final = frozenset({"stg", "information_schema", "pg_catalog"})
ALLOWED_TABLE_FUNCTIONS: Final = frozenset({"unnest", "range", "generate_series"})
CATALOG_TABLE_FUNCTIONS: Final = frozenset({"duckdb_tables", "duckdb_columns"})
DENIED_NODE_NAMES: Final = (
    "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter", "Command", "Pragma",
    "Set", "Use", "Attach", "Detach", "Copy", "Export", "Transaction", "Commit", "Rollback",
    "LoadData", "Install",
)  # fmt: skip


class FunctionRules(NamedTuple):
    """Denied function names: by prefix, by suffix, or exact (lower case)."""

    prefixes: tuple[str, ...]
    suffixes: tuple[str, ...]
    exact: frozenset[str]

    def denies(self, name: str) -> bool:
        return name.startswith(self.prefixes) or name.endswith(self.suffixes) or name in self.exact


DENIED_FUNCTION_RULES: Final = FunctionRules(
    prefixes=("read_", "sqlite_", "postgres_", "pragma_"),
    suffixes=("_scan",),
    exact=frozenset({"glob", "query", "query_table", "getenv", "current_setting"})
    | {"json_serialize_sql", "load_extension", "columns"},
)
MAX_SQL_CHARS: Final = 8000
MAX_JOINS: Final = 20
MAX_CTES: Final = 10
UNTRUSTED_TEXT_COLUMNS: Final = (
    "enrich.text_redacted.text", "core.work_item.summary", "score.funding.title",
    "enrich.cluster.label", "core.event.alert_name",
)  # fmt: skip
REDACT_ON_READ_COLUMNS: Final = ("core.work_item.summary", "score.funding.title")

_ROOT_TYPES: Final = (exp.Select, exp.Union, exp.Intersect, exp.Except)
_DENIED_NODE_TYPES: Final = tuple(
    t for t in (getattr(exp, name, None) for name in DENIED_NODE_NAMES) if isinstance(t, type)
)
_VI10_ROOTS = frozenset({"SELECT_NODE", "SET_OPERATION_NODE", "RECURSIVE_CTE_NODE", "CTE_NODE"})
_COLUMN_RE: Final = re.compile(r"Column '([^']+)'")
_ANSI_RE: Final = re.compile(r"\x1b\[[0-9;]*m")
_NAME_RE: Final = re.compile(r"[a-z_][a-z0-9_]*")
_SELECT_ONLY: Final = "only SELECT queries are allowed"
_TEXT_HINT: Final = "free text is not available; join enrich.text_redacted on record_id"
_PARSER_CONFIG: Final = {
    "enable_external_access": False, "autoinstall_known_extensions": False,
    "autoload_known_extensions": False,
}  # fmt: skip
_qualify_duckdb = partial(qualify, dialect="duckdb", expand_stars=True, quote_identifiers=False)
_logger = get_logger("harness.sql_guard")

type _SchemaMap = Mapping[str, Mapping[str, Mapping[str, str]]]

# sqlglot's "falling back to Command" warning quotes the SQL text; never log SQL text.
logging.getLogger("sqlglot").addFilter(lambda r: "unsupported syntax" not in r.getMessage())


@dataclass(frozen=True, slots=True)
class GuardedQuery:
    """A query that passed every rule, with its output-column lineage flags."""

    sql: str
    normalized_sql: str
    ordered: bool
    tables: frozenset[str]
    untrusted_output_columns: frozenset[str]
    redact_output_columns: frozenset[str]


class _Rejected(Exception):  # noqa: N818 - internal control flow, never escapes check()
    """`args` is `(rule, hint)`."""


def _fail(rule: str, hint: str) -> NoReturn:
    raise _Rejected(rule, hint)


def _norm(name: str) -> str:
    """NFKC + casefold, so quoted, mixed-case and lookalike identifiers compare equal."""
    return unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", name).casefold())


def _query_hash(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def _parse_one(sql: str) -> exp.Expr:
    try:
        parsed = [e for e in sqlglot.parse(sql, read="duckdb") if e is not None]
    except (SqlglotError, RecursionError) as err:
        _fail("parse", f"SQL could not be parsed: {_ANSI_RE.sub('', str(err))[:200]}")
    if len(parsed) != 1:
        _fail("single_statement", "send exactly one statement")
    return parsed[0]


def _select_root(expr: exp.Expr) -> tuple[exp.Query, bool]:
    """Rule 2: unwrap `Paren`/`Subquery`; return the root and whether any level is ordered."""
    ordered = bool(expr.args.get("order"))
    while isinstance(expr, exp.Paren | exp.Subquery):
        expr = expr.this
        ordered = ordered or bool(expr.args.get("order"))
    if not isinstance(expr, _ROOT_TYPES):
        _fail("select_only", _SELECT_ONLY)
    return expr, ordered


def _check_nodes(root: exp.Expr) -> None:
    """Rule 3: no node of a denied statement class anywhere in the tree."""
    for node in root.walk():
        if isinstance(node, _DENIED_NODE_TYPES):
            _fail("denied_node", f"statement type {type(node).__name__} is not allowed")


def _func_names(func: exp.Func) -> frozenset[str]:
    """Every lower-case name a function node may carry (DuckDB and sqlglot spellings)."""
    if isinstance(func, exp.Anonymous):
        return frozenset({_norm(func.name)})
    names = {_norm(func.sql_name()), func.key}
    head = _norm(func.sql(dialect="duckdb").split("(", 1)[0].strip())
    if _NAME_RE.fullmatch(head):
        names.add(head)
    return frozenset(names)


def _table_position_funcs(root: exp.Expr) -> Iterable[exp.Func]:
    for node in root.find_all(exp.Table, exp.Lateral, exp.TableFromRows, exp.From, exp.Join):
        source = node.this
        if isinstance(source, exp.Table):
            source = source.this
        if isinstance(source, exp.Func):
            yield source


def _check_functions(root: exp.Expr, *, allow_catalog: bool) -> bool:
    """Rule 5; returns whether a catalog table function is used."""
    for func in root.find_all(exp.Func):
        for name in _func_names(func):
            if DENIED_FUNCTION_RULES.denies(name):
                _fail("function", f"function {name} is not allowed")
    allowed = ALLOWED_TABLE_FUNCTIONS | (CATALOG_TABLE_FUNCTIONS if allow_catalog else frozenset())
    uses_catalog = False
    for func in _table_position_funcs(root):
        names = _func_names(func)
        if not names & allowed:
            hint = (
                f"table function {min(names)} is not allowed; use unnest, range or generate_series"
            )
            _fail("table_function", hint)
        uses_catalog = uses_catalog or bool(names & CATALOG_TABLE_FUNCTIONS)
    return uses_catalog


def _check_limits(root: exp.Expr) -> None:
    """Rule 7, checked before the costlier rule 6."""
    for node_type, cap, what in ((exp.Join, MAX_JOINS, "joins"), (exp.CTE, MAX_CTES, "CTEs")):
        if sum(1 for _ in root.find_all(node_type)) > cap:
            _fail("limits", f"too many {what} (max {cap})")


def _enclosing_scope(node: exp.Expr, by_expr: Mapping[int, Scope]) -> Scope | None:
    parent = node.parent
    while parent is not None:
        scope = by_expr.get(id(parent))
        if scope is not None:
            return scope
        parent = parent.parent
    return None


def _resolve(scope: Scope | None, name: str) -> exp.Table | Scope | None:
    while scope is not None:
        source = scope.sources.get(name)
        if isinstance(source, exp.Table | Scope):
            return source
        scope = scope.parent
    return None


class SqlGuard:
    """Rejects any SQL that is not one read-only query over the allowed schemas (U05-37)."""

    def __init__(
        self,
        schema: _SchemaMap,
        blocked_columns: Sequence[str],
        *,
        untrusted_text_columns: Sequence[str] = UNTRUSTED_TEXT_COLUMNS,
    ) -> None:
        self._schema: dict[str, dict[str, dict[str, str]]] = {
            _norm(db): {
                _norm(t): {_norm(c): k for c, k in cols.items()} for t, cols in tabs.items()
            }
            for db, tabs in schema.items()
        }
        self._blocked = frozenset(_norm(c) for c in blocked_columns)
        self._untrusted = frozenset(_norm(c) for c in untrusted_text_columns)
        self._redact = frozenset(REDACT_ON_READ_COLUMNS)
        self._mapping = MappingSchema(cast("dict[str, object]", self._schema), dialect="duckdb")
        self._local = threading.local()

    def check(self, sql: str, *, allow_catalog: bool = False) -> GuardedQuery:
        """Return a `GuardedQuery` when every rule passes; else raise `QueryError` with a hint."""
        try:
            return self._check(sql, allow_catalog=allow_catalog)
        except RecursionError:
            rule, hint = "parse", "SQL could not be parsed: nesting too deep"
        except _Rejected as rej:
            rule, hint = rej.args
        _logger.info("harness.sql_guard.rejected", rule=rule, query_hash=_query_hash(sql))
        msg = f"SQL guard: {rule}"
        raise QueryError(msg, hint=hint)

    def _check(self, sql: str, *, allow_catalog: bool) -> GuardedQuery:
        if len(sql) > MAX_SQL_CHARS:
            _fail("size", "query longer than 8000 characters; aggregate in fewer steps")
        root, ordered = _select_root(_parse_one(sql))
        for ident in root.find_all(exp.Identifier):
            ident.set("this", _norm(ident.this))
        _check_nodes(root)
        tables = self._check_tables(root)
        uses_catalog = _check_functions(root, allow_catalog=allow_catalog)
        _check_limits(root)
        qualified = self._qualify(root, tables, validate=not uses_catalog)
        scopes = traverse_scope(qualified)
        by_expr = {id(s.expression): s for s in scopes}
        refs = self._check_columns(qualified, by_expr)
        untrusted, redact = self._lineage(qualified, refs, scopes[-1], by_expr)
        self._second_parse(sql)
        return GuardedQuery(sql, normalize_sql(sql), ordered, tables, untrusted, redact)

    def _check_tables(self, root: exp.Expr) -> frozenset[str]:
        """Rule 4; returns the referenced `<schema>.<table>` names."""
        ctes = {cte.alias_or_name for cte in root.find_all(exp.CTE)}
        found: set[str] = set()
        for table in root.find_all(exp.Table):
            if isinstance(table.this, exp.Func):
                continue  # table functions are rule 5
            if not isinstance(table.this, exp.Identifier):
                _fail("table", "qualify as core.<table>")
            if not table.db and not table.catalog and table.name in ctes:
                continue
            found.add(self._check_table_name(table))
        return frozenset(found)

    def _check_table_name(self, table: exp.Table) -> str:
        db, name = table.db, table.name
        if not db:
            _fail("table", f"qualify as core.{name}")
        if db in DENIED_SCHEMAS or db.startswith("duckdb_") or db not in ALLOWED_SCHEMAS:
            _fail("table", f"schema {db} is not available")
        if table.catalog:
            _fail("table", f"schema {table.catalog}.{db} is not available")
        if name not in self._schema.get(db, {}):
            _fail("table", f"table {db}.{name} does not exist; call list_tables")
        return f"{db}.{name}"

    def _qualify(self, root: exp.Query, tables: frozenset[str], *, validate: bool) -> exp.Query:
        """Rule 6, first half: qualification and star expansion against the schema."""
        try:
            return _qualify_duckdb(
                root.copy(), schema=self._mapping, validate_qualify_columns=validate
            )
        except Exception as err:  # noqa: BLE001 - any qualify failure rejects (fail closed)
            match = _COLUMN_RE.search(str(err))
            _fail("column", self._column_hint(match[1] if match else "", tables))

    def _column_hint(self, missing: str, tables: frozenset[str]) -> str:
        pairs = (t.split(".", 1) for t in tables)
        choices = sorted({col for db, table in pairs for col in self._schema[db][table]})
        matches = process.extract(_norm(missing), choices, scorer=fuzz.WRatio, score_cutoff=60)
        if not matches:
            return "column not found; check names with describe_table"
        return f"column not found — did you mean {', '.join(m[0] for m in matches[:3])}?"

    def _table_columns(self, table: exp.Table, column: str) -> set[str]:
        """Source names a column read through `table` may reach (all, for aliased lists)."""
        cols = self._schema.get(table.db, {}).get(table.name, {})
        alias = table.args.get("alias")
        aliased = isinstance(alias, exp.TableAlias) and bool(alias.columns)
        return {f"{table.db}.{table.name}.{c}" for c in cols if aliased or c == column}

    def _column_sources(self, col: exp.Column, by_expr: Mapping[int, Scope]) -> set[str]:
        source = _resolve(_enclosing_scope(col, by_expr), col.table)
        if isinstance(source, exp.Table):
            return self._table_columns(source, col.name)
        if source is not None:
            return set()  # a derived scope: its own columns are checked in that scope
        tables = col.root().find_all(exp.Table)  # unresolved: all tables with it (fail closed)
        return set[str]().union(*(self._table_columns(t, col.name) for t in tables))

    def _check_columns(self, q: exp.Expr, by_expr: Mapping[int, Scope]) -> set[str]:
        """Rule 6, second half: no referenced column (after star expansion) is blocked."""
        cols = q.find_all(exp.Column)
        refs = set[str]().union(*(self._column_sources(c, by_expr) for c in cols))
        if refs & self._blocked:
            _fail("column", _TEXT_HINT)
        return refs

    def _lineage(
        self, q: exp.Query, refs: set[str], root: Scope, by_expr: Mapping[int, Scope]
    ) -> tuple[frozenset[str], frozenset[str]]:
        """Output names reaching untrusted and redact-on-read columns (all on failure)."""
        if not refs & self._untrusted:
            return frozenset(), frozenset()
        outputs = list(q.named_selects)
        try:
            reached = [self._reach(root, i, by_expr, set()) for i in range(len(outputs))]
        except Exception:  # noqa: BLE001 - fail closed: every output is untrusted
            return frozenset(outputs), frozenset(outputs)
        pairs = list(zip(outputs, reached, strict=True))
        untrusted = frozenset(name for name, src in pairs if src & self._untrusted)
        return untrusted, frozenset(name for name, src in pairs if src & self._redact)

    def _reach(
        self, scope: Scope, idx: int, by_expr: Mapping[int, Scope], seen: set[tuple[int, int]]
    ) -> set[str]:
        """Source columns reachable from output `idx` of `scope`, through CTEs and subqueries."""
        if (id(scope), idx) in seen:
            return set()
        seen.add((id(scope), idx))
        expr = scope.expression
        if isinstance(expr, exp.SetOperation):
            branches = (by_expr[id(b.unnest())] for b in (expr.left, expr.right))
            return set[str]().union(*(self._reach(b, idx, by_expr, seen) for b in branches))
        out: set[str] = set()
        for col in cast("exp.Select", expr).selects[idx].find_all(exp.Column):
            source = _resolve(_enclosing_scope(col, by_expr), col.table)
            if isinstance(source, Scope):
                sub_idx = cast("exp.Query", source.expression).named_selects.index(col.name)
                out |= self._reach(source, sub_idx, by_expr, seen)
            else:
                out |= self._column_sources(col, by_expr)
        return out

    def _parser(self) -> duckdb.DuckDBPyConnection:  # step 11: private, one per thread
        con: duckdb.DuckDBPyConnection | None = getattr(self._local, "con", None)
        if con is None:
            con = duckdb.connect(":memory:", config=dict(_PARSER_CONFIG))
            self._local.con = con
        return con

    def _second_parse(self, sql: str) -> None:
        try:
            row = self._parser().execute("SELECT json_serialize_sql(?)", [sql]).fetchone()
            doc = json.loads(row[0]) if row is not None else {}
        except (duckdb.Error, RuntimeError, ValueError, UnicodeError):
            doc = {}
        statements = doc.get("statements") or []
        root_type = statements[0].get("node", {}).get("type") if len(statements) == 1 else None
        if doc.get("error", True) or root_type not in _VI10_ROOTS:
            _fail("second_parse", _SELECT_ONLY)
