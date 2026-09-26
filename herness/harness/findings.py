"""Pure finding rules and entity lookups (impl 06 U06-47, U06-49, U06-50, U06-143).

Marker and NumberRef consistency (spec 00 §12.1, design 06 §4.2), the stable task dedup key
(design 06 §5.5), a finding's USD impact, and the per-run `EntityCatalog` that checks scope
and finding entities against the warehouse build (and the ops `run` table). Markers come
only from `herness.core.numbers.parse_markers` (R-16); no marker regex is defined here.
SQL identifiers come only from the fixed allowlist map `_ENTITY_TABLES`; ids are bound.
"""

from __future__ import annotations

import hashlib
import types
import unicodedata
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Final, Protocol, cast

from herness.core.ids import canonical_json
from herness.core.numbers import parse_markers
from herness.core.types import EntityScope, Finding, NumberRef, ScopeEntityType, WarehouseHandle
from herness.store.ops.runs import get_run

__all__ = [
    "EntityCatalog",
    "compute_dedup_key",
    "extract_markers",
    "impact_usd",
    "normalize_objective",
    "validate_markers",
]

DEDUP_KEY_CHARS: Final = 16
_TRAILING_PUNCT: Final = ".!?;: "

# entity_type → (qualified table, id column, name column); the only source of SQL identifiers.
_ENTITY_TABLES: Final[Mapping[str, tuple[str, str, str]]] = types.MappingProxyType(
    {
        "service": ("core.service", "service_id", "name"),
        "team": ("core.team", "team_id", "name"),
        "org": ("core.org", "org_id", "name"),
        "work_item": ("core.work_item", "record_id", "key"),
        "cluster": ("enrich.cluster", "cluster_id", "label"),
        "candidate": ("score.funding", "candidate_id", "title"),
    }
)
_RUN: Final = "run"


# --- markers (U06-47) ------------------------------------------------------------------------


def extract_markers(text: str) -> tuple[list[str], list[str]]:
    """Valid marker ids in order of appearance and malformed marker strings (inner text)."""
    scan = parse_markers(text)
    return list(scan.ids), [bad.text for bad in scan.malformed]


def _duplicates(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    dups: dict[str, None] = {}
    for value in values:
        if value in seen:
            dups[value] = None
        seen.add(value)
    return list(dups)


def validate_markers(
    text: str, numbers: Sequence[NumberRef], *, require_all_used: bool = True
) -> list[str]:
    """Error messages for marker ↔ NumberRef inconsistencies; empty when valid. Pure.

    Order: malformed markers, duplicate number ids, markers without a number, then (when
    `require_all_used`) numbers never referenced. Each id is reported once.
    """
    ids, malformed = extract_markers(text)
    number_ids = [n.id for n in numbers]
    known = set(number_ids)
    used = set(ids)
    errors = [f"malformed marker [[{bad}]]" for bad in malformed]
    errors += [f"duplicate number id {nid}" for nid in _duplicates(number_ids)]
    errors += [f"marker [[{mid}]] has no number" for mid in dict.fromkeys(ids) if mid not in known]
    if require_all_used:
        unused = (nid for nid in dict.fromkeys(number_ids) if nid not in used)
        errors += [f"number {nid} is not referenced in the text" for nid in unused]
    return errors


# --- dedup key (U06-49) ----------------------------------------------------------------------


def normalize_objective(text: str) -> str:
    """NFKC, lower case, whitespace runs → one space, stripped, trailing `.!?;:` removed."""
    collapsed = " ".join(unicodedata.normalize("NFKC", text).lower().split())
    return collapsed.rstrip(_TRAILING_PUNCT)


def compute_dedup_key(role: str, specialty: str, scope: EntityScope, objective: str) -> str:
    """Stable task identity: 16 lowercase hex chars of SHA-1 over canonical JSON (R-14). Pure."""
    start, end = scope.period_start, scope.period_end
    payload = canonical_json(
        [
            role,
            specialty,
            scope.entity_type,
            sorted(scope.entity_ids),
            None if start is None else start.isoformat(),
            None if end is None else end.isoformat(),
            normalize_objective(objective),
        ]
    )
    digest = hashlib.sha1(payload.encode("utf-8"), usedforsecurity=False).hexdigest()
    return digest[:DEDUP_KEY_CHARS]


# --- impact (U06-143) ------------------------------------------------------------------------


def impact_usd(f: Finding, /) -> Decimal:
    """Largest USD value the finding cites, `Decimal("0")` when it cites none. Pure."""
    values = [Decimal(str(n.value)) for n in f.numbers if n.unit == "usd"]
    return max(values, default=Decimal("0"))


# --- entity catalog (U06-50) -----------------------------------------------------------------


class _Rows(Protocol):
    def fetchall(self) -> list[tuple[object, ...]]: ...


class _Cursor(Protocol):
    def execute(self, query: str, parameters: object = ...) -> _Rows: ...


class EntityCatalog:
    """Existence and name lookups for scope and finding entities, cached for one run.

    Each `missing`/`names` call issues at most one indexed warehouse query (only for ids not
    yet cached); `run` ids are read from the ops store with `get_run` per uncached id.
    """

    def __init__(self, warehouse: WarehouseHandle) -> None:
        self._warehouse = warehouse
        self._cache: dict[tuple[str, str], str | None] = {}

    def missing(self, entity_type: ScopeEntityType, ids: Sequence[str]) -> set[str]:
        """The ids in `ids` that do not exist for `entity_type`."""
        found = self._lookup(entity_type, ids)
        return {eid for eid, name in found.items() if name is None}

    def names(self, entity_type: ScopeEntityType, ids: Sequence[str]) -> dict[str, str]:
        """id → display name for the ids that exist (a NULL name falls back to the id)."""
        found = self._lookup(entity_type, ids)
        return {eid: name for eid, name in found.items() if name is not None}

    def _lookup(self, entity_type: str, ids: Sequence[str]) -> dict[str, str | None]:
        if entity_type != _RUN and entity_type not in _ENTITY_TABLES:
            msg = "unknown entity type"
            raise ValueError(msg)
        wanted = list(dict.fromkeys(ids))
        uncached = [eid for eid in wanted if (entity_type, eid) not in self._cache]
        if uncached:
            fetched = (
                self._fetch_runs(uncached)
                if entity_type == _RUN
                else self._query(entity_type, uncached)
            )
            for eid in uncached:
                self._cache[(entity_type, eid)] = fetched.get(eid)
        return {eid: self._cache[(entity_type, eid)] for eid in wanted}

    @staticmethod
    def _fetch_runs(ids: Sequence[str]) -> dict[str, str]:
        return {eid: eid for eid in ids if get_run(eid) is not None}

    def _query(self, entity_type: str, ids: Sequence[str]) -> dict[str, str]:
        table, id_col, name_col = _ENTITY_TABLES[entity_type]
        sql = (
            f"SELECT {id_col}, {name_col} FROM {table}"  # noqa: S608 - allowlisted identifiers
            f" WHERE {id_col} IN (SELECT unnest(?))"
        )
        cursor = cast("_Cursor", self._warehouse.cursor())
        rows = cursor.execute(sql, [list(ids)]).fetchall()
        return {str(eid): str(eid) if name is None else str(name) for eid, name in rows}
