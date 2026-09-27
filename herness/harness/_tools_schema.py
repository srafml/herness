"""JSON Schema rules of the tool registry and dispatch (impl 05 U05-33, U05-34 step 5); private.

Split out so `tools.py` stays within its 400-line budget. `check_tool_schema` is the
registration rule: a valid Draft 2020-12 schema that is strict-compatible at every object
level (R-26: `additionalProperties: false`, every property listed in `required`; optional
values are nullable). `schema_hint` is the dispatch step 5 hint: the first validation error
with its JSON path, never echoing the offending argument value (TH05-16).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Final

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import JsonValue

from herness.core.errors import ConfigError
from herness.core.types import AsyncTool, Tool

HINT_MAX_CHARS: Final = 300
_SUB_ONE: Final = ("items", "not", "if", "then", "else", "contains", "additionalItems")
_SUB_MANY: Final = ("prefixItems", "anyOf", "oneOf", "allOf")
_SUB_MAP: Final = ("properties", "$defs", "definitions", "patternProperties")
_JSON_TYPE: Final = {
    bool: "boolean",
    int: "integer",
    float: "number",
    str: "string",
    list: "array",
    dict: "object",
}


def _subschemas(schema: Mapping[str, JsonValue]) -> Iterator[JsonValue]:
    for key in _SUB_ONE:
        if key in schema:
            yield schema[key]
    for key in _SUB_MANY:
        items = schema.get(key)
        if isinstance(items, list):
            yield from items
    for key in _SUB_MAP:
        members = schema.get(key)
        if isinstance(members, dict):
            yield from members.values()


def _strict_ok(schema: JsonValue) -> bool:
    """R-26 at every object level: `additionalProperties: false`, every property required."""
    if not isinstance(schema, dict):
        return True
    kind = schema.get("type")
    props = schema.get("properties")
    is_object = kind == "object" or (isinstance(kind, list) and "object" in kind)
    if is_object or props is not None:
        names = set(props) if isinstance(props, dict) else set()
        required = schema.get("required", [])
        if schema.get("additionalProperties") is not False:
            return False
        if not isinstance(required, list) or set(map(str, required)) != names:
            return False
    return all(_strict_ok(sub) for sub in _subschemas(schema))


def check_tool_schema(tool: Tool | AsyncTool) -> None:
    """`ConfigError` unless `tool.input_schema` is valid and strict-compatible (R-26)."""
    name = tool.name[:64]
    try:
        Draft202012Validator.check_schema(tool.input_schema)
    except SchemaError as exc:
        msg = f"tool {name} input schema is not a valid JSON Schema"
        raise ConfigError(msg) from exc
    if not _strict_ok(tool.input_schema):
        msg = f"tool {name} schema is not strict-compatible"
        raise ConfigError(msg)


def schema_hint(schema: Mapping[str, JsonValue], arguments: Mapping[str, JsonValue]) -> str | None:
    """Step 5: the first JSON Schema error as `<json path>: <message>`, else None.

    The offending value is replaced by its JSON type so no argument text (possibly a secret or
    ticket text) is echoed back; the hint is cut to `HINT_MAX_CHARS`.
    """
    error = next(iter(Draft202012Validator(schema).iter_errors(arguments)), None)
    if error is None:
        return None
    message = error.message
    shown = repr(error.instance)
    if shown in message:
        kind = _JSON_TYPE.get(type(error.instance), "null")
        message = message.replace(shown, f"<{kind} value>", 1)
    return f"{error.json_path}: {message}"[:HINT_MAX_CHARS]
