"""Type-driven encoding of STRUCT, MAP and UNION cells for ``result_hash`` (U04-01 step 14).

Nested members are encoded by the member types parsed from the DuckDB type string, not by
Python type, because ``fetchall`` and Arrow give different Python values for the same member
(HUGEINT as ``int`` vs ``Decimal``; MAP as ``dict`` vs ``(key, value)`` pairs).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Final

from herness.core.errors import SchemaViolation

# (value, normalized type) -> JSON token; a member encoder also maps None to null.
Encoder = Callable[[object, str], str]

COMPOUND_PREFIXES: Final = ("STRUCT(", "MAP(", "UNION(")
# No common encoding exists: fetchall gives BIT and BIGNUM as text but Arrow as DuckDB-internal
# bytes, and Arrow drops the TIME WITH TIME ZONE offset. These fail closed at any depth.
UNSUPPORTED_TYPES: Final = frozenset(
    {"BIT", "BITSTRING", "BIGNUM", "VARINT", "TIME WITH TIME ZONE", "TIMETZ"}
)
# UNION values carry no member tag, so a multi-member UNION is encoded by Python type; these
# member types give different Python values from fetchall and Arrow (int vs Decimal, ...).
_PATH_DEPENDENT: Final = UNSUPPORTED_TYPES | {"HUGEINT", "UHUGEINT", "INTERVAL"}


def unsupported_type(duckdb_type: str) -> SchemaViolation:
    """The fail-closed error for a column or member type without a path-independent encoding."""
    msg = f"unsupported column type {duckdb_type}: fetchall and Arrow values differ"
    return SchemaViolation(msg)


def split_top(inner: str) -> list[str]:
    """Split a type argument list on commas outside parentheses and quotes."""
    parts: list[str] = []
    depth, quote, start = 0, "", 0
    for i, ch in enumerate(inner):
        if quote:
            quote = "" if ch == quote else quote
        elif ch in "'\"":
            quote = ch
        elif ch in "()":
            depth += 1 if ch == "(" else -1
        elif ch == "," and depth == 0:
            parts.append(inner[start:i].strip())
            start = i + 1
    parts.append(inner[start:].strip())
    return parts


def _drop_name(member: str) -> str:
    if not member.startswith('"'):
        return member.partition(" ")[2].strip()
    i = 1
    while True:  # a quoted name ends at a quote that is not doubled
        i = member.index('"', i)
        if member[i + 1 : i + 2] != '"':
            return member[i + 1 :].strip()
        i += 2


def member_types(norm: str) -> list[str]:
    """Member types of ``STRUCT(name T, ...)``, ``UNION(tag T, ...)`` or ``MAP(K, V)``."""
    parts = split_top(norm[norm.index("(") + 1 : -1])
    return parts if norm.startswith("MAP(") else [_drop_name(p) for p in parts]


def _is_compound(norm: str) -> bool:
    return norm.endswith("]") or norm.startswith(COMPOUND_PREFIXES)


def _struct(value: object, norm: str, encode: Encoder) -> str:
    types = member_types(norm)
    if not isinstance(value, dict) or len(value) != len(types):
        msg = "struct value does not match its members"
        raise TypeError(msg)
    items = zip(value.items(), types, strict=True)
    return (
        "{"
        + ",".join(json.dumps(k, ensure_ascii=False) + ":" + encode(v, t) for (k, v), t in items)
        + "}"
    )


def _map_pairs(value: object, key_type: str) -> Sequence[Sequence[object]]:
    if isinstance(value, list):
        return value  # Arrow: (key, value) pairs
    if not isinstance(value, dict):
        msg = "not a map value"
        raise TypeError(msg)
    if _is_compound(key_type):
        # fetchall gives {"key": [...], "value": [...]} when keys are unhashable in Python.
        return list(zip(value["key"], value["value"], strict=True))
    return list(value.items())


def _map(value: object, norm: str, encode: Encoder) -> str:
    key_type, value_type = member_types(norm)
    items: list[str] = []
    for key, item in _map_pairs(value, key_type):
        token = encode(key, key_type)
        items.append(
            (token if token.startswith('"') else json.dumps(token)) + ":" + encode(item, value_type)
        )
    return "{" + ",".join(items) + "}"


def _union(value: object, norm: str, encode: Encoder, by_python: Encoder) -> str:
    types = member_types(norm)
    if len(types) == 1:
        return encode(value, types[0])
    for member in types:
        if member in _PATH_DEPENDENT or member.startswith("DECIMAL(") or _is_compound(member):
            raise unsupported_type(norm)
    return by_python(value, norm)


def encode_nested(value: object, norm: str, encode: Encoder, by_python: Encoder) -> str:
    """Encode a STRUCT, MAP or UNION value by its member types. Raises SchemaViolation."""
    if norm.startswith("STRUCT("):
        return _struct(value, norm, encode)
    if norm.startswith("MAP("):
        return _map(value, norm, encode)
    return _union(value, norm, encode, by_python)
