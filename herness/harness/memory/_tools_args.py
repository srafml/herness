"""Schemas and argument plumbing of the memory tools (impl 07 §3.12; private sibling of `tools`).

Split off for the 280-line budget of `tools.py` (T07-11 spec note under U07-63); only
`tools.py` imports it. The two strict input schemas (R-26) with spec 05's `NumberRef` under
`$defs` (its `row_key` travels as `{column, value}` pairs, U05-10), the U07-64 step 5 kind data,
the `NumberRef` conversion and the U07-63 step 8 `data.items` entry. Errors name fields only,
never argument values or memory text (ENG §3.4).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from typing import Final, cast, get_args

from pydantic import JsonValue, ValidationError

from herness.core import time as clock
from herness.core.errors import ToolInputError
from herness.core.types import Kind, Layer, MemoryItem, NumberRef, RecallHit
from herness.harness.swarm.tools import NUMBER_REF_SCHEMA

__all__ = [
    "ALL_KINDS", "PROPOSE_KINDS", "PROPOSE_MEMORY_SCHEMA", "RECALL_MEMORY_SCHEMA", "Schema",
    "invalid", "kind_data", "numbers", "recall_item", "slug",
]  # fmt: skip

type Schema = dict[str, JsonValue]

ALL_KINDS: Final[tuple[str, ...]] = get_args(Kind)
PROPOSE_KINDS: Final = ("glossary", "business_rule", "insight", "user_correction",
                        "analysis_recipe")  # fmt: skip
_ENTITY_TYPES: Final = ("service", "team", "org", "work_item", "cluster")
_TERM_MAX: Final = 80
_SLUG_WORDS: Final = 8
_SLUG_MAX: Final = 64
_NON_SLUG: Final = re.compile(r"[^a-z0-9]+")


def _obj(props: Schema, *, nullable: bool = False) -> Schema:
    return {"type": ["object", "null"] if nullable else "object", "properties": props,
            "required": list(props), "additionalProperties": False}  # fmt: skip


def _enum(values: Sequence[str]) -> Schema:
    return {"type": "string", "enum": list(values)}


def _array(items: JsonValue, **bounds: JsonValue) -> Schema:
    return {"type": ["array", "null"], "items": items, **bounds}


RECALL_MEMORY_SCHEMA: Final[Schema] = _obj({
    "query": {"type": "string", "minLength": 3, "maxLength": 500},
    "layers": _array(_enum(get_args(Layer)), uniqueItems=True),
    "k": {"type": ["integer", "null"], "minimum": 1, "maximum": 20},
    "kinds": _array(_enum(ALL_KINDS), maxItems=11),
    "entity": _obj({"type": _enum(_ENTITY_TYPES), "id": {"type": "string", "maxLength": 200}},
                   nullable=True),
})  # fmt: skip
PROPOSE_MEMORY_SCHEMA: Final[Schema] = {
    **_obj({
        "layer": _enum(("semantic", "procedural")),
        "kind": _enum(PROPOSE_KINDS),
        "content": {"type": "string", "minLength": 10, "maxLength": 2000},
        "query_ids": _array({"type": "string", "pattern": "^q_[0-9a-f]{16}$"}, maxItems=20),
        "numbers": _array({"$ref": "#/$defs/NumberRef"}, maxItems=20),
        "entities": _array(_obj({"type": {"type": "string"}, "id": {"type": "string"}}),
                           maxItems=20),
        "finding_ids": _array({"type": "string", "pattern": "^fnd_"}, maxItems=20),
        "confidence": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
        "rationale": {"type": ["string", "null"], "maxLength": 500},
    }),
    "$defs": {"NumberRef": NUMBER_REF_SCHEMA},
}  # fmt: skip


def invalid(tool: str, exc: ValidationError) -> ToolInputError:
    """A `ToolInputError` naming the failing fields only, never the offending values."""
    fields = sorted({str(err["loc"][0]) for err in exc.errors() if err["loc"]})
    return ToolInputError(f"invalid {tool} arguments: {', '.join(fields) or 'model'}")


def _author(item: MemoryItem) -> str:
    prov = item.provenance
    return f"agent:{prov.author_role or ''}" if prov.author_type == "agent" else prov.author_type


def recall_item(hit: RecallHit) -> JsonValue:
    """One design 07 §3.5 `data.items` entry (U07-63 step 8)."""
    item, prov = hit.item, hit.item.provenance
    raw = item.data.get("numbers")
    refs: list[JsonValue] = [n for n in raw if isinstance(n, dict)] if isinstance(raw, list) else []
    return {"memory_id": item.memory_id, "layer": item.layer, "kind": item.kind,
            "status": item.status, "unconfirmed": hit.unconfirmed, "confidence": item.confidence,
            "score": round(hit.score, 3), "content": item.content, "numbers": refs,
            "query_ids": list(prov.query_ids), "run_id": prov.run_id, "author": _author(item),
            "created_at": clock.format_utc(item.created_at)}  # fmt: skip


def slug(content: str) -> str:
    """`rule_id`: the first 8 words as lowercase ASCII joined by `_`, ≤ 64 chars."""
    words = " ".join(content.split()[:_SLUG_WORDS])
    text = unicodedata.normalize("NFKD", words).encode("ascii", "ignore").decode("ascii")
    out = _NON_SLUG.sub("_", text.lower()).strip("_")[:_SLUG_MAX].rstrip("_")
    return out or "rule"  # no ASCII letter or digit in the first words (T07-11 spec note)


def kind_data(kind: str, content: str, finding_ids: list[JsonValue]) -> dict[str, JsonValue]:
    """U07-64 step 5: the kind data derived from the arguments (§13 DD26)."""
    if kind == "glossary":
        term, sep, definition = content.partition(":")
        if not sep or not 1 <= len(term.strip()) <= _TERM_MAX:
            msg = "glossary content must be 'term: definition'"
            raise ToolInputError(msg)
        return {"term": term.strip(), "definition": definition.strip()}
    if kind == "business_rule":
        return {"rule_id": slug(content), "applies_to": []}
    if kind == "insight":
        return {"finding_ids": list(finding_ids), "valid_from": None, "valid_to": None}
    if kind == "user_correction":
        return {"statement": content, "effective_date": None, "suggested_action": "none"}
    return {"steps": [], "template_ids": []}  # analysis_recipe


def _row_key(pairs: JsonValue) -> JsonValue:
    """`[{column, value}, …]` → `{column: value}` (U05-10); anything else is left to validation."""
    if not isinstance(pairs, list):
        return pairs
    out: dict[str, JsonValue] = {}
    for pair in pairs:
        column = pair.get("column") if isinstance(pair, dict) else None
        if not isinstance(pair, dict) or not isinstance(column, str):
            return pairs
        if column in out:
            msg = f"duplicate row_key column {column[:128]}"
            raise ToolInputError(msg)
        out[column] = pair.get("value")
    return out


def numbers(raw: JsonValue) -> list[NumberRef]:
    """The `numbers` argument as `NumberRef`s; any malformed entry is a `ToolInputError`."""
    try:
        if not isinstance(raw, list) or not all(isinstance(n, dict) for n in raw):
            raise ValueError  # noqa: TRY301 - one error path for every malformed number
        refs = cast("list[dict[str, JsonValue]]", raw)
        return [NumberRef.model_validate({**n, "row_key": _row_key(n.get("row_key"))})
                for n in refs]  # fmt: skip
    except (ValueError, ValidationError):
        msg = "invalid propose_memory arguments: numbers"
        raise ToolInputError(msg) from None
