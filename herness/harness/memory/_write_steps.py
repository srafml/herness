"""Per-proposal steps of `MemoryWriter` that need no collaborator state (impl 07 U07-50).

Private sibling of `herness.harness.memory.write`, split off for its §2 line budget (T07-08
spec note); only `write` imports it. Redaction (step 3), numeral rules (step 4), the chat
session check (step 6 d), confidence (step 10) and the step 12 row and review payload.
No function logs or raises memory text; `reject` names only the rule.
"""

import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final, Literal, NoReturn, get_args

import numpy as np
from pydantic import JsonValue

from herness.core import time as clock
from herness.core.errors import PolicyViolation
from herness.core.ids import new_ulid
from herness.core.redact import Redactor
from herness.core.resilience.metrics import record_counter
from herness.core.types import MemoryProposal, Provenance
from herness.harness.memory.policy import (
    HUMAN_CONFIDENCE,
    SYSTEM_EPISODIC_CONFIDENCE,
    PolicyDecision,
    agent_confidence,
    check_markers,
    find_uncited_numerals,
)
from herness.harness.memory.types import ProposeResult
from herness.store.ops import chat
from herness.store.ops.memory import MemoryItemRow

type Flag = Literal[
    "redacted", "instruction_like", "unverified_numbers", "conflict", "embedding_pending"
]
type Conn = sqlite3.Connection

# `data` values under these keys are identifiers and are never redacted (U07-50 step 3).
ID_KEYS: Final = frozenset({
    "rec_id", "outcome_id", "query_id", "query_ids", "template_id", "review_item_id",
    "finding_ids", "top_finding_ids", "rec_ids", "run_ids", "fingerprint", "content_hash",
    "service_id", "team_id", "org_id", "jira_project", "rule_id", "conflicts_with",
})  # fmt: skip
_ENTITY_KEEP: Final = frozenset({"type", "id"})
_FLAGS: Final = frozenset(get_args(Flag.__value__))
_SYSTEM_TEXT_KINDS: Final = frozenset(
    {"run_summary", "outcome_summary", "decision_note", "mapping"}
)
_LABEL_BAD: Final = re.compile(r"[^A-Za-z0-9_.:\-]")
_LABEL_MAX: Final = 64


def reject(rule: str) -> NoReturn:
    """Raise PolicyViolation naming only the rule (`details["rule"]`, R-19)."""
    msg = f"memory write policy: {rule}"
    raise PolicyViolation(msg, details={"rule": rule})


def count(name: str, **labels: str) -> None:
    """Add one to a memory counter on the component metric sink (impl 07 §8.2, R-12)."""
    clean = {k: _LABEL_BAD.sub("_", v)[:_LABEL_MAX] for k, v in labels.items()}
    record_counter(name, component="memory", labels=clean)


def entity_ids(data: dict[str, JsonValue]) -> set[str]:
    """The string ids of `data.entities`."""
    ents = data.get("entities")
    ids = [e.get("id") for e in ents if isinstance(e, dict)] if isinstance(ents, list) else []
    return {i for i in ids if isinstance(i, str)}


def stored_flags(data: dict[str, JsonValue]) -> list[Flag]:
    """The known flags of a stored `data.flags`."""
    raw = data.get("flags")
    return [f for f in raw if f in _FLAGS] if isinstance(raw, list) else []  # type: ignore[misc]


def result(
    row: MemoryItemRow, *, merged: bool = False, flags: list[Flag] | None = None
) -> ProposeResult:
    """The ProposeResult reporting `row` (merged: `merged_into` is the row's id)."""
    review = row["data"].get("review_item_id")
    return ProposeResult(
        memory_id=row["memory_id"],
        status=row["status"],  # type: ignore[arg-type]
        review_item_id=review if isinstance(review, str) else None,
        merged_into=row["memory_id"] if merged else None,
        flags=stored_flags(row["data"]) if flags is None else flags,
    )


@dataclass(slots=True)
class Draft:
    """One proposal after steps 1-10: redacted text, flags, policy decision, identity hash."""

    item: MemoryProposal
    content: str
    data: dict[str, JsonValue]
    flags: list[Flag]
    decision: PolicyDecision
    key: str
    system: bool
    confidence: float = 0.0
    vector: np.ndarray | None = None
    conflicts: list[str] = field(default_factory=list)

    @property
    def prov(self) -> Provenance:
        """The proposal's provenance."""
        return self.item.provenance


def _is_id_value(value: JsonValue) -> bool:
    """An identifier value: a string or a list of strings (anything else is redacted)."""
    return isinstance(value, str) or (
        isinstance(value, list) and all(isinstance(v, str) for v in value)
    )


def redact_payload(
    redactor: Redactor, item: MemoryProposal
) -> tuple[str, dict[str, JsonValue], list[Flag]]:
    """Step 3: content, every data string and every data key, through `redactor.redact`.

    Exempt are only a str / list-of-str value under an ID_KEYS key, and the str `type` and
    `id` of the entries of the top-level `data["entities"]` list (U07-50 step 3)."""
    changed: list[bool] = []

    def text(value: str) -> str:
        found = redactor.redact(value)
        out = value if found is None else found.text
        changed.append(out != value)
        return out

    def obj(
        value: dict[str, JsonValue], keep: frozenset[str] = frozenset()
    ) -> dict[str, JsonValue]:
        return {
            k if k in keep or k in ID_KEYS else text(k): (
                v if (k in keep and isinstance(v, str)) or (k in ID_KEYS and _is_id_value(v))
                else walk(v)
            )
            for k, v in value.items()
        }  # fmt: skip

    def walk(value: JsonValue) -> JsonValue:
        if isinstance(value, str):
            return text(value)
        if isinstance(value, list):
            return [walk(v) for v in value]
        return obj(value) if isinstance(value, dict) else value

    content = text(item.content)
    data = obj({k: v for k, v in item.data.items() if k != "entities"})
    if "entities" in item.data:
        ents = item.data["entities"]
        data["entities"] = (
            [obj(e, _ENTITY_KEEP) if isinstance(e, dict) else walk(e) for e in ents]
            if isinstance(ents, list) else walk(ents)
        )  # fmt: skip
    return content, data, ["redacted"] if any(changed) else []


def check_numerals(
    item: MemoryProposal, content: str, allowed: Sequence[re.Pattern[str]], flags: list[Flag]
) -> None:
    """Step 4 on the redacted content; procedural kinds are exempt."""
    if item.layer == "procedural":
        return
    author = item.provenance.author_type
    uncited = find_uncited_numerals(content, allowed)
    if author == "agent" or (author == "system" and item.kind == "insight"):  # model text
        if uncited:
            reject("numerals.uncited")
        if not check_markers(content, item.numbers).ok:
            reject("numerals.markers")
    elif item.kind in _SYSTEM_TEXT_KINDS:
        if uncited:
            reject("numerals.system")
    elif uncited or not check_markers(content, []).ok:  # human: any marker is unknown here
        flags.append("unverified_numbers")


def check_session(prov: Provenance) -> None:
    """Step 6 (d): the source message is a user message of the author's own session."""
    sid, mid = prov.session_id, prov.source_message_id
    message = chat.get_chat_message(mid) if sid and mid else None
    session = chat.get_chat_session(sid) if sid and mid else None
    if (
        message is None
        or session is None
        or message["session_id"] != sid
        or message["role"] != "user"
        or session["user_ref"] != prov.author_ref
    ):
        reject("provenance.session")


def confidence(item: MemoryProposal, verified: list[float]) -> float:
    """Step 10: human 0.9; agent capped by its verified findings; system episodic 1.0."""
    author = item.provenance.author_type
    if author == "human":
        return HUMAN_CONFIDENCE
    if author == "agent":
        return agent_confidence(item.confidence, verified)
    return SYSTEM_EPISODIC_CONFIDENCE if item.layer == "episodic" else item.confidence


def new_row(draft: Draft, now: datetime) -> MemoryItemRow:
    """The step 12 row: `data` gains numbers, entities, content_hash, flags, embedding_pending."""
    item, decision = draft.item, draft.decision
    numbers: list[JsonValue] = [n.model_dump(mode="json") for n in item.numbers]
    data: dict[str, JsonValue] = {
        **draft.data, "numbers": numbers, "entities": draft.data.get("entities", []),
        "content_hash": draft.key, "flags": list(draft.flags),
        "embedding_pending": "embedding_pending" in draft.flags,
    }  # fmt: skip
    if draft.conflicts:
        data["conflicts_with"] = list(draft.conflicts)
    expires = item.expires_at
    if expires is None and decision.expiry_days is not None:
        expires = now + timedelta(days=decision.expiry_days)
    return {
        "memory_id": "mem_" + new_ulid(), "layer": item.layer, "kind": item.kind,
        "content": draft.content, "data": data, "provenance": draft.prov.model_dump(mode="json"),
        "confidence": draft.confidence, "status": decision.status,
        "created_at": clock.format_utc(now),
        "expires_at": None if expires is None else clock.format_utc(expires),
        "last_used_at": None, "use_count": 0,
    }  # fmt: skip


def review_payload(row: MemoryItemRow) -> dict[str, object]:
    """The design 07 §4.1 `memory_write` review payload (content is the redacted text)."""
    data = row["data"]
    return {
        "memory_id": row["memory_id"], "layer": row["layer"], "kind": row["kind"],
        "content": row["content"], "numbers": data["numbers"], "entities": data["entities"],
        "provenance": row["provenance"], "flags": data["flags"],
        "conflicts_with": data.get("conflicts_with", []),
    }  # fmt: skip
