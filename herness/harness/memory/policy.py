"""Pure write-policy checks for memory (impl 07 §3.6, U07-37 … U07-43; design 07 §5.8).

No I/O and no clock. Numeral scanning and marker parsing are the single implementation in
`herness.core.numbers` (R-16); this module only adapts them to memory.
"""

import re
import statistics
import unicodedata
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, NoReturn

from pydantic import JsonValue

from herness.core import ids
from herness.core import numbers as core_numbers
from herness.core.errors import PolicyViolation, SchemaViolation
from herness.core.numbers import NumeralHit
from herness.core.types import Kind, NumberRef, Provenance, Status
from herness.harness.memory.settings import WriteConfig

ZERO_WIDTH: Final = frozenset({"\u200b", "\u200c", "\u200d", "\u2060", "\ufeff"})
HUMAN_CONFIDENCE: Final = 0.9
SYSTEM_EPISODIC_CONFIDENCE: Final = 1.0
APPROVAL_FLOOR: Final = 0.8
MERGE_CAP: Final = 0.95
MAX_DATA_DEPTH: Final = 8
MAX_SCAN_STRINGS: Final = 2_000
MAX_ENTITIES: Final = 20
MAX_ENTITY_CHARS: Final = 200
_HASH_CHARS: Final = 32
_ZW_TABLE: Final = dict.fromkeys(map(ord, ZERO_WIDTH))
_WS_RE: Final = re.compile(r"\s+")
_OWNER_FIELDS: Final = ("team_id", "jira_project", "org_id")
_SUGGESTED_ACTIONS: Final = frozenset({"none", "weight_change", "mapping_suggestion"})

# JSON type names accepted per required data field (design 07 §4.2).
_S, _N = frozenset({"string"}), frozenset({"number"})
_I, _L = frozenset({"integer"}), frozenset({"array"})
_SN, _NN = frozenset({"string", "null"}), frozenset({"number", "null"})
_REQUIRED: Final[Mapping[Kind, tuple[tuple[str, frozenset[str]], ...]]] = {
    "run_summary": (("run_kind", _S), ("question", _SN), ("top_finding_ids", _L),
                    ("rec_ids", _L), ("dead_task_count", _I)),
    "outcome_summary": (("rec_id", _S), ("outcome_id", _S), ("measurement", _I),
                        ("verdict", _S), ("metric", _S), ("baseline", _NN), ("actual", _NN),
                        ("delta", _NN), ("rel", _NN), ("query_id", _S)),
    "decision_note": (("rec_id", _S), ("decision", _S)),
    "glossary": (("term", _S), ("definition", _S)),
    "business_rule": (("rule_id", _S), ("applies_to", _L)),
    "mapping": (("review_item_id", _S), ("service_id", _S)),
    "insight": (("finding_ids", _L), ("valid_from", _SN), ("valid_to", _SN)),
    "user_correction": (("statement", _S), ("effective_date", _SN), ("suggested_action", _S)),
    "sql_template": (("fingerprint", _S), ("sql_template", _S), ("params", _L),
                     ("question_examples", _L), ("passes", _I), ("fails", _I),
                     ("run_ids", _L), ("build_id_last_ok", _S), ("metrics_used", _L)),
    "qa_pair": (("question", _S), ("sql", _S), ("query_id", _S), ("template_id", _S)),
    "analysis_recipe": (("steps", _L), ("template_ids", _L)),
}  # fmt: skip


def _fail(rule: str, limit: object = None) -> NoReturn:
    """Raise PolicyViolation whose message names the rule and limit, `details["rule"]` = rule."""
    msg = f"memory write policy: {rule}" + ("" if limit is None else f" (limit {limit})")
    raise PolicyViolation(msg, details={"rule": rule})


# ---------------------------------------------------------------- U07-37


def normalize_content(text: str) -> str:
    """Canonical dedupe text: NFKC, zero-width removed, casefolded, whitespace collapsed."""
    folded = unicodedata.normalize("NFKC", text).translate(_ZW_TABLE).casefold()
    return _WS_RE.sub(" ", folded).strip()


def content_hash(text: str) -> str:
    """First 32 hex chars of SHA-256 over the UTF-8 of `normalize_content(text)` (R-14)."""
    return ids.sha256_hex(normalize_content(text))[:_HASH_CHARS]


def keyed_hash(key: str) -> str:
    """First 32 hex chars of SHA-256 over the UTF-8 of `key`, without normalization."""
    return ids.sha256_hex(key)[:_HASH_CHARS]


# ---------------------------------------------------------------- U07-38 / U07-39


def find_uncited_numerals(text: str, allowed: Sequence[re.Pattern[str]]) -> list[NumeralHit]:
    """Numerals outside markers not covered by an allowed pattern (shared scanner, R-16)."""
    return list(core_numbers.find_uncited(text, allowed))


@dataclass(frozen=True, slots=True)
class MarkerReport:
    """Marker ↔ NumberRef consistency; `unused` is informational only."""

    unknown: list[str]
    invalid: list[str]
    duplicate_ids: list[str]
    unused: list[str]
    ok: bool


def _distinct(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(values))


def check_markers(text: str, numbers: Sequence[NumberRef]) -> MarkerReport:
    """Compare the `[[nK]]` markers in `text` with the ids of `numbers`."""
    ref_ids = [n.id for n in numbers]
    counts = Counter(ref_ids)
    duplicates = _distinct([i for i in ref_ids if counts[i] > 1])
    scan = core_numbers.parse_markers(text)
    invalid = [m.text for m in scan.malformed]
    known = set(ref_ids)
    unknown = _distinct([i for i in scan.ids if i not in known])
    used = set(scan.ids)
    unused = _distinct([i for i in ref_ids if i not in used])
    ok = not (unknown or invalid or duplicates)
    return MarkerReport(unknown, invalid, duplicates, unused, ok)


# ---------------------------------------------------------------- U07-40


def _scan_form(text: str) -> str:
    return _WS_RE.sub(" ", unicodedata.normalize("NFKC", text).translate(_ZW_TABLE))


def _data_strings(data: Mapping[str, JsonValue]) -> list[str]:
    """String values of `data` in depth-first order, keys excluded, at most 2,000."""
    found: list[str] = []
    stack: list[JsonValue] = [dict(data)]
    while stack and len(found) < MAX_SCAN_STRINGS:
        node = stack.pop()
        if isinstance(node, str):
            found.append(node)
        elif isinstance(node, dict):
            stack.extend(reversed(list(node.values())))
        elif isinstance(node, list):
            stack.extend(reversed(node))
    return found


class InjectionScanner:
    """Flags instruction-like content with case-insensitive patterns (TH07-01, TH07-23)."""

    def __init__(self, patterns: Sequence[str]) -> None:
        self._patterns: tuple[str, ...] = tuple(patterns)
        self._compiled = tuple(re.compile(p, re.IGNORECASE) for p in self._patterns)

    @property
    def patterns(self) -> tuple[str, ...]:
        """The pattern sources, in index order."""
        return self._patterns

    def scan(self, text: str) -> list[int]:
        """Sorted indices of patterns that match the normalized `text`."""
        form = _scan_form(text)
        return [i for i, rx in enumerate(self._compiled) if rx.search(form)]

    def scan_payload(self, content: str, data: Mapping[str, JsonValue]) -> list[int]:
        """Union of `scan` over `content` and every string value in `data`."""
        hits = set(self.scan(content))
        for value in _data_strings(data):
            hits.update(self.scan(value))
        return sorted(hits)


# ---------------------------------------------------------------- U07-41


def _depth_exceeds(data: Mapping[str, JsonValue], limit: int) -> bool:
    """True when the JSON nesting depth of `data` (itself depth 1) is above `limit`."""
    stack: list[tuple[list[JsonValue], int]] = [(list(data.values()), 1)]
    while stack:
        children, depth = stack.pop()
        if depth > limit:
            return True
        for child in children:
            if isinstance(child, dict):
                stack.append((list(child.values()), depth + 1))
            elif isinstance(child, list):
                stack.append((child, depth + 1))
    return False


def _json_types(value: JsonValue) -> frozenset[str]:
    if value is None:
        return frozenset({"null"})
    if isinstance(value, bool):
        return frozenset({"boolean"})
    if isinstance(value, int):
        return frozenset({"integer", "number"})
    if isinstance(value, float):
        return _N
    if isinstance(value, str):
        return _S
    return _L if isinstance(value, list) else frozenset({"object"})


def _short_str(value: JsonValue) -> bool:
    return isinstance(value, str) and len(value) <= MAX_ENTITY_CHARS


def _entities_ok(entities: JsonValue) -> bool:
    if not isinstance(entities, list) or len(entities) > MAX_ENTITIES:
        return False
    return all(
        isinstance(e, dict)
        and all(_short_str(e.get(k)) for k in ("type", "id"))
        for e in entities
    )  # fmt: skip


def _check_required(kind: Kind, data: Mapping[str, JsonValue]) -> None:
    for field, accepted in _REQUIRED[kind]:
        if field not in data or not _json_types(data[field]) & accepted:
            _fail(f"data.required:{field}")
    if kind == "user_correction" and data["suggested_action"] not in _SUGGESTED_ACTIONS:
        _fail("data.required:suggested_action")
    if kind == "mapping" and not any(isinstance(data.get(f), str) for f in _OWNER_FIELDS):
        _fail("data.required:" + "|".join(_OWNER_FIELDS))


def _data_bytes(data: Mapping[str, JsonValue]) -> int:
    """UTF-8 length of canonical_json(data); unencodable data (NaN, Infinity) is a size.data."""
    try:
        return len(ids.canonical_json(dict(data)).encode("utf-8"))
    except SchemaViolation:
        _fail("size.data")


def check_limits(
    kind: Kind,
    content: str,
    data: Mapping[str, JsonValue],
    numbers: Sequence[NumberRef],
    cfg: WriteConfig,
) -> None:
    """Size, depth and required-field checks; the first failure raises PolicyViolation."""
    if len(content) > cfg.max_content_chars:
        _fail("size.content", cfg.max_content_chars)
    for key in ("sql_template", "sql"):
        sql = data.get(key)
        if isinstance(sql, str) and len(sql) > cfg.max_sql_chars:
            _fail("size.sql", cfg.max_sql_chars)
    if _depth_exceeds(data, MAX_DATA_DEPTH):
        _fail("data.depth", MAX_DATA_DEPTH)
    if _data_bytes(data) > cfg.max_data_bytes:
        _fail("size.data", cfg.max_data_bytes)
    if len(numbers) > cfg.max_numbers:
        _fail("size.numbers", cfg.max_numbers)
    if "entities" in data and not _entities_ok(data["entities"]):
        _fail("data.entities", MAX_ENTITIES)
    _check_required(kind, data)


# ---------------------------------------------------------------- U07-42


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """Outcome of the policy matrix: initial status, review need and default expiry."""

    status: Status
    needs_review: bool
    expiry_days: int | None


@dataclass(frozen=True, slots=True)
class _Row:
    roles: tuple[str, ...] | None
    via_status: Mapping[str, Status]
    required: tuple[str, ...]


_A: Final[Status] = "active"
_P: Final[Status] = "pending_approval"
_C: Final[Status] = "candidate"
_INSIGHT_REQ: Final = ("run_id", "finding_ids", "query_ids")
_POLICY_MATRIX: Final[Mapping[tuple[Kind, str], _Row]] = {
    ("run_summary", "system"): _Row(None, {"pipeline": _A}, ("run_id",)),
    ("outcome_summary", "system"): _Row(None, {"outcome_job": _A}, ("query_ids",)),
    ("decision_note", "human"): _Row(None, {"dashboard": _A, "cli": _A}, ("author_ref",)),
    ("glossary", "human"): _Row(None, {"cli": _A, "dashboard": _A, "chat": _P}, ("author_ref",)),
    ("glossary", "agent"): _Row(("analyst", "chat"), {"tool": _P}, ("run_id",)),
    ("business_rule", "human"): _Row(None, {"cli": _P, "dashboard": _P, "chat": _P},
                                     ("author_ref",)),
    ("business_rule", "agent"): _Row(("analyst", "chat"), {"tool": _P}, ("run_id", "query_ids")),
    ("mapping", "system"): _Row(None, {"pipeline": _A}, ("data.review_item_id",)),
    ("insight", "agent"): _Row(("analyst",), {"tool": _P}, _INSIGHT_REQ),
    ("insight", "system"): _Row(None, {"pipeline": _P}, _INSIGHT_REQ),
    ("user_correction", "human"): _Row(None, {"chat": _P, "dashboard": _P},
                                       ("author_ref", "session_id", "source_message_id")),
    ("user_correction", "agent"): _Row(("chat",), {"tool": _P}, ("run_id", "session_id")),
    ("sql_template", "system"): _Row(None, {"promotion": _C}, ("run_id", "query_ids")),
    ("qa_pair", "system"): _Row(None, {"promotion": _C}, ("run_id", "query_ids")),
    ("analysis_recipe", "agent"): _Row(("analyst",), {"tool": _P}, ("run_id",)),
    ("analysis_recipe", "human"): _Row(None, {"cli": _A, "dashboard": _A, "chat": _P},
                                       ("author_ref",)),
}  # fmt: skip


def _has_required(field: str, provenance: Provenance, data: Mapping[str, JsonValue]) -> bool:
    if field.startswith("data."):
        return data.get(field.removeprefix("data.")) not in (None, "")
    value = getattr(provenance, field)
    return bool(value) if isinstance(value, list) else value is not None


def decide_policy(
    kind: Kind,
    provenance: Provenance,
    data: Mapping[str, JsonValue],
    flags: Collection[str],
    expiry_days: Mapping[str, int],
) -> PolicyDecision:
    """Apply the impl 07 §4.2 policy matrix; no row lets an agent or chat write go active."""
    row = _POLICY_MATRIX.get((kind, provenance.author_type))
    if row is None:
        _fail("policy.not_allowed")
    role = provenance.author_role
    if row.roles is not None and not (role and role.startswith(row.roles)):
        _fail("policy.role")
    status = row.via_status.get(provenance.via)
    if status is None:
        _fail("policy.via")
    for field in row.required:
        if not _has_required(field, provenance, data):
            _fail(f"provenance.required:{field}")
    if status != _P and ("instruction_like" in flags or "conflict" in flags):
        status = _P
    return PolicyDecision(status, status == _P, expiry_days.get(kind))


# ---------------------------------------------------------------- U07-43


def agent_confidence(agent_value: float, finding_confidences: Sequence[float]) -> float:
    """Agent write confidence: never above the mean confidence of its cited findings."""
    if not finding_confidences:
        return agent_value
    return min(agent_value, statistics.fmean(finding_confidences))


def merge_confidence(c_old: float, c_new: float) -> float:
    """Merged confidence `1 - (1 - old)(1 - new)`, capped at MERGE_CAP."""
    return min(MERGE_CAP, 1 - (1 - c_old) * (1 - c_new))
