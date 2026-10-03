"""SQL parameterisation, binding and checks of `procedural.py` (impl 07 U07-88, U07-91).

Private sibling of `herness.harness.memory.procedural`, split off for its §2 line budget
(T07-19 spec note); only `procedural` imports it, and it re-exports the public names.
`parameterize_sql` swaps literal nodes for named placeholder nodes; `bind_template` swaps
them back for sqlglot literal nodes; `unsafe_reason` and `explain_ok` put bound SQL through
the spec 05 guard. No value is ever formatted into SQL text.
"""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal, cast

import duckdb
import sqlglot
from pydantic import JsonValue
from sqlglot import expressions as exp
from sqlglot.errors import SqlglotError

from herness.core.errors import QueryError
from herness.harness.memory.policy import keyed_hash
from herness.harness.sql_guard import SqlGuard

__all__ = [
    "ParamSpec", "ParameterizedSql", "bind_template", "explain_ok", "ints", "metrics_used",
    "parameterize_sql", "strs", "unsafe_reason",
]  # fmt: skip

type ParamType = Literal["date", "list", "id", "number", "string"]

MAX_SQL_CHARS: Final = 8_000
_DATE_TYPES: Final = frozenset({exp.DType.DATE, exp.DType.TIMESTAMP, exp.DType.TIMESTAMPTZ})
_UNBOUND: Final = "unbound placeholder"
_NO_POS: Final = 1 << 30


@dataclass(frozen=True, slots=True)
class ParamSpec:
    """One named parameter of a template with the literal value it replaced."""

    name: str
    type: ParamType
    example: JsonValue

    def as_json(self) -> dict[str, JsonValue]:
        """The `data.params` entry."""
        return {"name": self.name, "type": self.type, "example": self.example}


@dataclass(frozen=True, slots=True)
class ParameterizedSql:
    """A parameterised template, its 16-hex fingerprint and its parameters (U07-88)."""

    template: str
    fingerprint: str
    params: list[ParamSpec]


def _value(lit: exp.Literal) -> JsonValue:
    """The JSON value of a literal: its text when a string, else an int or a float."""
    text = str(lit.this)
    if lit.is_string:
        return text
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


def _is_date(lit: exp.Literal) -> bool:
    """An ISO date or timestamp string, or the operand of a CAST to DATE/TIMESTAMP(TZ)."""
    parent = lit.parent
    if isinstance(parent, exp.Cast) and parent.to.this in _DATE_TYPES:
        return True
    try:
        return lit.is_string and bool(datetime.fromisoformat(str(lit.this)))
    except ValueError:
        return False


def _is_id(lit: exp.Literal) -> bool:
    """Compared (EQ, NEQ) with a column whose name ends in `_id`."""
    parent = lit.parent
    if not isinstance(parent, exp.EQ | exp.NEQ):
        return False
    other = parent.left if parent.right is lit else parent.right
    return isinstance(other, exp.Column) and other.name.lower().endswith("_id")


class _Namer:
    """Placeholder names in order of appearance; counters start at 1 per kind.

    Dates after the second and every number or string share the `p<n>` counter."""

    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self.params: list[ParamSpec] = []

    def add(self, ptype: ParamType, example: JsonValue) -> exp.Placeholder:
        self.counts[ptype] += 1
        n = self.counts[ptype]
        if ptype == "list":
            name = f"list_{n}"
        elif ptype == "id":
            name = "entity_id" if n == 1 else f"entity_id_{n}"
        elif ptype == "date" and n <= 2:  # noqa: PLR2004 - start and end
            name = ("start_date", "end_date")[n - 1]
        else:
            self.counts["p"] += 1
            name = f"p{self.counts['p']}"
        self.params.append(ParamSpec(name, ptype, example))
        return exp.Placeholder(this=name)


def _slots(tree: exp.Expr) -> list[exp.Expr]:
    """All-literal IN nodes and the remaining literals, in source order."""
    lists = [
        n for n in tree.find_all(exp.In)
        if n.expressions and all(isinstance(e, exp.Literal) for e in n.expressions)
    ]  # fmt: skip
    inside = {id(e) for n in lists for e in n.expressions}
    lits = [n for n in tree.find_all(exp.Literal) if id(n) not in inside]
    order = {id(n): i for i, n in enumerate(tree.walk(bfs=False))}

    def key(node: exp.Expr) -> tuple[int, int]:
        lit = node.expressions[0] if isinstance(node, exp.In) else node
        return int(lit.meta.get("start", _NO_POS)), order[id(node)]

    return sorted([*lists, *lits], key=key)


def _parse(sql: str) -> exp.Expr | None:
    try:
        parsed = sqlglot.parse(sql, read="duckdb")
    except (SqlglotError, RecursionError):
        return None
    return parsed[0] if len(parsed) == 1 else None


def parameterize_sql(sql: str) -> ParameterizedSql | None:
    """Replace literals by named parameters and fingerprint the template; None if unusable."""
    tree = _parse(sql) if len(sql) <= MAX_SQL_CHARS else None
    if tree is None:
        return None
    namer = _Namer()
    for node in _slots(tree):
        if isinstance(node, exp.In):
            values = [_value(cast(exp.Literal, e)) for e in node.expressions]
            node.set("expressions", [namer.add("list", values)])
        else:
            lit = cast(exp.Literal, node)
            kind: ParamType = "string" if lit.is_string else "number"
            kind = "date" if _is_date(lit) else "id" if _is_id(lit) else kind
            lit.replace(namer.add(kind, _value(lit)))
    for ident in tree.find_all(exp.Identifier):
        if not ident.quoted:
            ident.set("this", str(ident.this).lower())
    template = tree.sql(dialect="duckdb")
    digest = keyed_hash(template)[:16]  # SHA-256 over UTF-8; reviewed site (UT05-124)
    return ParameterizedSql(template, digest, namer.params)


def metrics_used(template: str) -> list[str]:
    """Sorted names of the tables in schema `metrics` that the template reads."""
    tree = sqlglot.parse_one(template, read="duckdb")
    return sorted({t.name for t in tree.find_all(exp.Table) if t.db == "metrics"})


def _literal(value: JsonValue) -> exp.Expr:
    if isinstance(value, str):
        return exp.Literal.string(value)
    if isinstance(value, int | float) and not isinstance(value, bool):
        return exp.Literal.number(value)
    raise ValueError(_UNBOUND)


def bind_template(template: str, params: Sequence[Mapping[str, JsonValue]]) -> str:
    """The template with every placeholder replaced by sqlglot literal nodes (U07-91 step 1).

    Raises ValueError for an unknown placeholder or an example of the wrong shape, and
    SqlglotError when the template does not parse; lists become tuples of literals."""
    tree = sqlglot.parse_one(template, read="duckdb")
    specs = {str(p.get("name")): p for p in params}
    for node in list(tree.find_all(exp.Placeholder)):
        spec = specs.get(node.name)
        if spec is None:
            raise ValueError(_UNBOUND)
        example, parent = spec.get("example"), node.parent
        if spec.get("type") == "list":
            if not (isinstance(parent, exp.In) and isinstance(example, list) and example):
                raise ValueError(_UNBOUND)
            parent.set("expressions", [_literal(v) for v in example])
        else:
            node.replace(_literal(example))
    return tree.sql(dialect="duckdb")


def ints(data: Mapping[str, JsonValue], key: str) -> int:
    """`data[key]` when it is an int, else 0."""
    value = data.get(key)
    return value if isinstance(value, int) else 0


def strs(data: Mapping[str, JsonValue], key: str) -> list[str]:
    """The strings of the list `data[key]` ([] when absent)."""
    value = data.get(key)
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def unsafe_reason(parsed: ParameterizedSql, guard: SqlGuard) -> str | None:
    """Why a template must not be created (T07-19 spec note), or None when it is safe.

    The template must hold no literal node, and the query rebuilt from its examples by
    `bind_template` must pass the spec 05 guard (table/column allowlist, SELECT only)."""
    try:
        if sqlglot.parse_one(parsed.template, read="duckdb").find(exp.Literal) is not None:
            return "template_literal"
        guard.check(bind_template(parsed.template, [p.as_json() for p in parsed.params]))
    except (ValueError, SqlglotError):
        return "unbound_placeholder"
    except QueryError:
        return "guard_rejected"
    return None


def explain_ok(
    data: Mapping[str, JsonValue], con: duckdb.DuckDBPyConnection, guard: SqlGuard, timeout_s: float
) -> bool:
    """U07-91 steps 1-3: bind, guard, then EXPLAIN under a timer that interrupts `con`."""
    raw = data.get("params")
    params = [p for p in raw if isinstance(p, dict)] if isinstance(raw, list) else []
    try:
        sql = guard.check(bind_template(str(data.get("sql_template", "")), params)).sql
    except (ValueError, SqlglotError, QueryError):
        return False
    timer = threading.Timer(timeout_s, con.interrupt)
    timer.start()
    try:
        con.execute("EXPLAIN " + sql)
    except duckdb.Error:  # including the interrupt
        return False
    finally:
        timer.cancel()
    return True
