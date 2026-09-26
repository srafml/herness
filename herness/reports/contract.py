"""Report draft loading, design §4.1 contract checks and the draft-level uncited scan.

impl 09 U09-03, U09-04, U09-06 … U09-10. Marker parsing and the numeral scanner come from
``herness.core.numbers`` (R-16): this module compiles no marker or numeral pattern of its own.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

import duckdb
from pydantic import ValidationError

from herness.core import config as _config
from herness.core.errors import QueryError, ReportContractError
from herness.core.logging import get_logger
from herness.core.numbers import find_uncited, parse_markers
from herness.core.types import NumberRef, ReportDraft
from herness.store import ops

__all__ = [
    "DRAFT_MAX_BYTES", "QUERY_ID_RE", "RENDERABLE_RUN_STATUSES", "RUN_ID_RE", "SECTION_IDS",
    "SUPPORTED_SCHEMA_VERSIONS", "UNCITED_TEXT_MAX", "ContractLookups", "OpsContractLookups",
    "TextField", "UncitedHit", "check_render_contract", "iter_text_fields", "load_draft",
    "scan_draft_uncited", "unconfirmed_weight_keys",
]  # fmt: skip

# --- U09-03 constants ---------------------------------------------------------------------------

SUPPORTED_SCHEMA_VERSIONS: Final = frozenset({"1"})
RENDERABLE_RUN_STATUSES: Final = frozenset({"done", "partial"})
RUN_ID_RE: Final = re.compile(r"^run_[0-9A-HJKMNP-TV-Z]{26}$")
QUERY_ID_RE: Final = re.compile(r"^q_[0-9a-f]{16}$")
SECTION_IDS: Final = (
    "executive_summary", "recommendations", "portfolio", "org_scorecards", "actions",
    "retrospective", "risks_and_caveats", "method",
)  # fmt: skip
UNCITED_TEXT_MAX: Final = 80
DRAFT_MAX_BYTES: Final = 20_971_520

_MAX_LISTED: Final = 200
_CHUNK: Final = 500
_LEVER_KEYS: Final = ("entity_type", "entity_id", "metric", "delta_usd_ref")
_REF_FIELDS: Final = ("expected_usd_ref", "expected_delta_ref", "confidence_ref", "effort_usd_ref")
_USD_REFS: Final = frozenset({"expected_usd_ref", "effort_usd_ref"})
_log = get_logger("reports.contract")


@dataclass(frozen=True, slots=True)
class TextField:
    """One model-written text field of a draft and the numbers its markers resolve against."""

    where: str
    text: str
    numbers: tuple[NumberRef, ...] = ()
    finding_ids: tuple[str, ...] = ()
    refs: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class UncitedHit:
    """An uncited numeral: field path, span text (cut to UNCITED_TEXT_MAX) and offsets."""

    where: str
    text: str
    start: int
    end: int


# --- U09-04 iter_text_fields --------------------------------------------------------------------


def _lever_refs(k: int, levers: Sequence[object]) -> Iterator[tuple[str, str]]:
    for m, lever in enumerate(levers):
        where = f"recommendations[{k}].action_levers[{m}]"
        entry: Mapping[str, object] = lever if isinstance(lever, Mapping) else {}
        missing = next((key for key in _LEVER_KEYS if key not in entry), None)
        if missing is not None:
            msg = f"{where} is missing {missing}"
            raise ReportContractError(msg, details={"code": "draft_invalid", "where": where})
        yield f"action_levers[{m}].delta_usd_ref", str(entry["delta_usd_ref"])


def iter_text_fields(draft: ReportDraft) -> Iterator[TextField]:
    """Yield every model-written text field in the fixed U09-04 order."""
    yield TextField("title", draft.title)
    for i, section in enumerate(draft.sections):
        for j, par in enumerate(section.paragraphs):
            where = f"sections[{i}].paragraphs[{j}]"
            yield TextField(where, par.text, tuple(par.numbers), tuple(par.finding_ids))
    for k, rec in enumerate(draft.recommendations):
        named = [(name, getattr(rec, name)) for name in _REF_FIELDS]
        refs = (*((n, v) for n, v in named if v is not None), *_lever_refs(k, rec.action_levers))
        numbers, fids = tuple(rec.numbers), tuple(rec.finding_ids)
        yield TextField(f"recommendations[{k}].headline", rec.headline, numbers, fids)
        yield TextField(f"recommendations[{k}].summary", rec.summary, numbers, fids, refs)
    for c, caveat in enumerate(draft.caveats):
        yield TextField(f"caveats[{c}]", caveat)
    commentary = draft.prior_outcomes_commentary
    if commentary is not None:
        numbers, fids = tuple(commentary.numbers), tuple(commentary.finding_ids)
        yield TextField("prior_outcomes_commentary", commentary.text, numbers, fids)


# --- U09-06 load_draft --------------------------------------------------------------------------


def _invalid(run_id: str, msg: str, **details: str) -> ReportContractError:
    return ReportContractError(msg, details={"code": "draft_invalid", "run_id": run_id, **details})


def _error_where(exc: ValidationError) -> str:
    locs = (".".join(str(part) for part in err["loc"]) for err in exc.errors())
    return ", ".join(loc or "(root)" for loc in locs)


def load_draft(run_id: str, *, reports_root: Path | None = None) -> ReportDraft:
    """Read and validate ``<reports_root>/<run_id>/draft.json``; the file is never modified."""
    if RUN_ID_RE.fullmatch(run_id) is None:
        msg = "invalid run_id"
        raise ReportContractError(msg, details={"code": "invalid_run_id"})
    root = reports_root if reports_root is not None else _default_root()
    path = root / run_id / "draft.json"
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise _invalid(run_id, "report draft path is a symlink or outside the reports root")
    if not path.is_file():
        msg = f"Run `{run_id}` has no valid report draft."
        raise ReportContractError(msg, details={"code": "draft_missing", "run_id": run_id})
    with path.open("rb") as fh:
        data = fh.read(DRAFT_MAX_BYTES + 1)
    if len(data) > DRAFT_MAX_BYTES:
        size = str(path.stat().st_size)
        raise _invalid(run_id, "report draft is too large", size_bytes=size)
    try:
        draft = ReportDraft.model_validate_json(data)
    except ValidationError as exc:
        where = _error_where(exc)
        raise _invalid(run_id, "report draft fails its schema", where=where) from None
    if draft.run_id != run_id:
        raise _invalid(run_id, "report draft belongs to another run", draft_run_id=draft.run_id)
    return draft


def _default_root() -> Path:
    return Path(_config.get_config().paths.data) / "reports"


# --- U09-07 lookups -----------------------------------------------------------------------------


class ContractLookups(Protocol):
    """Existence checks of design §4.1 rules 3 and 4."""

    def known_query_ids(self, query_ids: Collection[str]) -> set[str]: ...

    def run_rec_ids(self, run_id: str) -> set[str]: ...

    def verified_finding_ids(self, finding_ids: Collection[str]) -> set[str]: ...


class OpsContractLookups:
    """Lookups against the ops store and the draft build's read-only warehouse connection."""

    def __init__(self, wh_con: duckdb.DuckDBPyConnection, *, build_id: str = "unknown") -> None:
        self._con = wh_con
        self._build_id = build_id

    def known_query_ids(self, query_ids: Collection[str]) -> set[str]:
        """The subset present in ops ``evidence`` or in the warehouse ``meta.evidence``."""
        ids = list(dict.fromkeys(query_ids))
        found = ops.ui_evidence_ids_present(ids) & set(ids)
        rest = [q for q in ids if q not in found]
        sql = "SELECT query_id FROM meta.evidence WHERE list_contains($ids, query_id)"
        try:
            for start in range(0, len(rest), _CHUNK):
                rows = self._con.execute(sql, {"ids": rest[start : start + _CHUNK]}).fetchall()
                found.update(str(row[0]) for row in rows)
        except duckdb.Error as exc:
            msg = f"warehouse evidence lookup failed for build {self._build_id}"
            raise QueryError(msg, details={"build_id": self._build_id}) from exc
        return found & set(ids)

    def run_rec_ids(self, run_id: str) -> set[str]:
        """``recommendation.rec_id`` values recorded by ``run_id``."""
        return ops.ui_run_rec_ids(run_id)

    def verified_finding_ids(self, finding_ids: Collection[str]) -> set[str]:
        """The subset whose finding is ``verified``."""
        ids = set(finding_ids)
        return ops.ui_finding_ids_with_status(ids, "verified") & ids


# --- U09-08 check_render_contract ---------------------------------------------------------------


class _Violations:
    def __init__(self) -> None:
        self.items: list[tuple[str, str]] = []

    def add(self, where: str, what: str, rule: str) -> None:
        self.items.append((f"{where}: {what}", rule))


def _check_field(field: TextField, out: _Violations) -> None:
    scan = parse_markers(field.text)
    by_id = {n.id: n for n in field.numbers}
    for _ in scan.malformed:
        out.add(field.where, "malformed marker", "malformed_marker")
    for marker_id in dict.fromkeys(scan.ids):
        if marker_id not in by_id:
            out.add(field.where, f"marker [[{marker_id}]] has no NumberRef", "unresolved_marker")
    if len(by_id) != len(field.numbers):
        out.add(field.where, "duplicate NumberRef id", "duplicate_number_id")
    for name, ref in field.refs:
        target = by_id.get(ref)
        if target is None:
            out.add(f"{field.where}.{name}", "unknown ref", "unknown_ref")
        elif (name in _USD_REFS or name.endswith(".delta_usd_ref")) and target.unit != "usd":
            out.add(f"{field.where}.{name}", "wrong unit", "wrong_unit")


def _check_query_ids(draft: ReportDraft, fields: Sequence[TextField], lookups: ContractLookups,
                     out: _Violations) -> None:  # fmt: skip
    first: dict[str, str] = {}
    for field in fields:
        for number in field.numbers:
            first.setdefault(number.query_id, field.where)
    for index, qid in enumerate(draft.query_ids):
        first.setdefault(qid, f"query_ids[{index}]")
    valid = [q for q in first if QUERY_ID_RE.fullmatch(q) is not None]
    for qid, where in first.items():
        if QUERY_ID_RE.fullmatch(qid) is None:
            out.add(where, "invalid query_id", "invalid_query_id")
    known = lookups.known_query_ids(valid) if valid else set()
    for qid in valid:
        if qid not in known:
            out.add(first[qid], f"unknown query_id {qid}", "unknown_query_id")


def _check_refs(draft: ReportDraft, fields: Sequence[TextField], lookups: ContractLookups,
                out: _Violations) -> None:  # fmt: skip
    recs = [(k, rec.rec_id) for k, rec in enumerate(draft.recommendations) if rec.rec_id]
    run_recs = lookups.run_rec_ids(draft.run_id) if recs else set()
    for k, rec_id in recs:
        if rec_id not in run_recs:
            out.add(f"recommendations[{k}].rec_id", "unknown rec_id", "unknown_rec_id")
    first: dict[str, str] = {}
    for field in fields:
        for fid in field.finding_ids:
            first.setdefault(fid, field.where)
    verified = lookups.verified_finding_ids(list(first)) if first else set()
    for fid, where in first.items():
        if fid not in verified:
            out.add(where, f"finding {fid} is not verified", "unverified_finding")


def check_render_contract(draft: ReportDraft, *, lookups: ContractLookups) -> None:
    """Enforce design §4.1 rules 1, 3, 4, the schema version and the named-ref rules."""
    out = _Violations()
    if draft.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        out.add("schema_version", "unsupported schema_version", "schema_version")
    fields = list(iter_text_fields(draft))
    for field in fields:
        _check_field(field, out)
    _check_query_ids(draft, fields, lookups, out)
    _check_refs(draft, fields, lookups, out)
    if not out.items:
        return
    count = len(out.items)
    rules = ", ".join(dict.fromkeys(rule for _, rule in out.items))
    _log.warning("reports.contract.violated", run_id=draft.run_id, count=count, rules=rules)
    msg = f"{count} report contract violations in run {draft.run_id}"
    where = _listed([where for where, _ in out.items])
    details = {"code": "contract_violation", "where": where, "rules": rules}
    raise ReportContractError(msg, details={**details, "run_id": draft.run_id})


def _listed(wheres: Sequence[str]) -> str:
    """The first _MAX_LISTED locations joined by ", ", plus "… and N more" past the cap."""
    listed = list(wheres[:_MAX_LISTED])
    if len(wheres) > _MAX_LISTED:
        listed.append(f"… and {len(wheres) - _MAX_LISTED} more")
    return ", ".join(listed)


# --- U09-09 scan_draft_uncited, U09-10 unconfirmed_weight_keys ----------------------------------


def scan_draft_uncited(draft: ReportDraft, patterns: Sequence[re.Pattern[str]]) -> list[UncitedHit]:
    """Every uncited numeral of every model-written field, in U09-04 field order then offset."""
    return [
        UncitedHit(field.where, hit.text[:UNCITED_TEXT_MAX], hit.start, hit.end)
        for field in iter_text_fields(draft)
        for hit in find_uncited(field.text, patterns)
    ]


def _unconfirmed(value: object) -> bool:
    return isinstance(value, Mapping) and value.get("unconfirmed") is True


def unconfirmed_weight_keys(weights: Mapping[str, object]) -> list[str]:
    """Top-level and one-level-nested weight blocks flagged ``unconfirmed: true``, sorted."""
    keys: list[str] = []
    for key, value in weights.items():
        if not isinstance(value, Mapping):
            continue
        if _unconfirmed(value):
            keys.append(key)
        keys.extend(f"{key}.{sub}" for sub, inner in value.items() if _unconfirmed(inner))
    return sorted(keys)
