"""Evidence collection and the evidence appendix loader (impl 09 U09-13, U09-14; design §4.3).

Ops ``evidence`` rows win over the build's warehouse ``meta.evidence``; an ops row whose
``query_id`` does not recompute from its ``sql``, ``params`` and ``build_id`` is treated as
tampered (TH05-12), logged and dropped, so the id falls through to the warehouse. Sample
cells are plain strings here; templates escape them (TH09-23).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

import duckdb

from herness.core import time as clock
from herness.core.errors import QueryError, ReportContractError, SchemaViolation
from herness.core.ids import query_id as compute_query_id
from herness.core.logging import get_logger
from herness.reports.contract import QUERY_ID_RE
from herness.store import ops

__all__ = ["EvidenceCollector", "EvidenceEntry", "load_evidence_entries"]

_USED_BY_CAP: Final = 50
_CELL_MAX: Final = 200
_RAW_MAX: Final = 500
_CHUNK: Final = 500
_HAS_SAMPLE_SQL: Final = (
    "SELECT count(*) FROM information_schema.columns WHERE table_schema = 'meta'"
    " AND table_name = 'evidence' AND column_name = 'result_sample'"
)
_META_SQL: Final = (
    "SELECT query_id, sql, params, row_count, executed_at, result_sample FROM meta.evidence"
    " WHERE list_contains($ids, query_id)"
)
_META_V1_SQL: Final = (
    "SELECT query_id, sql, params, row_count, executed_at, NULL AS result_sample"
    " FROM meta.evidence WHERE list_contains($ids, query_id)"
)
_log = get_logger("reports.evidence")


@dataclass(frozen=True, slots=True)
class EvidenceEntry:
    """One evidence appendix entry; ``result_sample=None`` means "Sample not stored"."""

    query_id: str
    sql: str
    params: dict[str, object]
    row_count: int | None
    executed_at: str | None
    build_id: str
    result_sample: list[dict[str, str]] | None
    sample_columns: list[str]
    used_by: list[str]
    found: bool


class EvidenceCollector:
    """Every cited ``query_id`` in first-use order with its back-links (U09-13); one per render."""

    def __init__(self) -> None:
        self._uses: dict[str, dict[str, None]] = {}

    def use(self, query_id: str, where: str) -> None:
        """Record ``query_id`` (first use fixes its order) and add ``where`` once."""
        if QUERY_ID_RE.fullmatch(query_id) is None:
            msg = "invalid query_id"
            raise ReportContractError(msg)
        self._uses.setdefault(query_id, {})[where] = None

    def ordered_ids(self) -> list[str]:
        """Query ids in first-use order, without duplicates."""
        return list(self._uses)

    def used_by(self, query_id: str) -> list[str]:
        """Locations in insertion order: at most 50, then one ``"… and N more"``."""
        places = list(self._uses.get(query_id, {}))
        if len(places) <= _USED_BY_CAP:
            return places
        return [*places[:_USED_BY_CAP], f"… and {len(places) - _USED_BY_CAP} more"]


def _trusted(row: ops.UiEvidenceRow) -> bool:
    """True when the ops row's ``query_id`` recomputes from its content (TH05-12)."""
    params = row["params"]
    try:
        expected = (
            compute_query_id(str(row["sql"]), params, str(row["build_id"]))
            if isinstance(params, dict)
            else None
        )
    except SchemaViolation:
        expected = None
    if expected != row["query_id"]:
        _log.warning("reports.evidence.tampered", query_id=str(row["query_id"]))
        return False
    return True


def _params(raw: object) -> dict[str, object]:
    """Parsed params: a dict, ``{}`` for NULL, else ``{"_raw": <text cut to 500>}``."""
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return {str(key): value for key, value in raw.items()}
    text = raw if isinstance(raw, str) else str(raw)
    try:
        parsed = json.loads(text)
    except (ValueError, RecursionError):
        parsed = None
    if isinstance(parsed, dict):
        return {str(key): value for key, value in parsed.items()}
    return {"_raw": text[:_RAW_MAX]}


def _cell(value: object) -> str:
    return ("" if value is None else str(value))[:_CELL_MAX]


def _sample(raw: object, sample_rows: int) -> tuple[list[dict[str, str]] | None, list[str]]:
    """The first ``sample_rows`` rows as string cells, and the first row's columns."""
    parsed = raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (ValueError, RecursionError):
            parsed = None
    if not isinstance(parsed, list):
        return None, []
    rows = [row for row in parsed if isinstance(row, Mapping)][: max(sample_rows, 0)]
    sample = [{str(key): _cell(value) for key, value in row.items()} for row in rows]
    return sample, list(sample[0]) if sample else []


def _executed_at(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return clock.format_utc(aware)
    return str(value)


def _meta_rows(
    wh_con: duckdb.DuckDBPyConnection, ids: Sequence[str], build_id: str
) -> dict[str, tuple[object, ...]]:
    """``meta.evidence`` rows for ``ids``, at most 500 ids per statement."""
    found: dict[str, tuple[object, ...]] = {}
    try:
        has_sample = wh_con.execute(_HAS_SAMPLE_SQL).fetchone()
        sql = _META_SQL if has_sample is not None and has_sample[0] else _META_V1_SQL
        for start in range(0, len(ids), _CHUNK):
            rows = wh_con.execute(sql, {"ids": list(ids[start : start + _CHUNK])}).fetchall()
            found.update((str(row[0]), tuple(row)) for row in rows)
    except duckdb.Error as exc:
        msg = f"warehouse evidence lookup failed for build {build_id}"
        raise QueryError(msg, details={"build_id": build_id}) from exc
    return found


def _from_ops(row: ops.UiEvidenceRow, used_by: list[str], sample_rows: int) -> EvidenceEntry:
    sample, columns = _sample(row["result_sample"], sample_rows)
    return EvidenceEntry(
        query_id=row["query_id"], sql=str(row["sql"]), params=_params(row["params"]),
        row_count=row["row_count"], executed_at=_executed_at(row["executed_at"]),
        build_id=str(row["build_id"]), result_sample=sample, sample_columns=columns,
        used_by=used_by, found=True,
    )  # fmt: skip


def _from_meta(
    row: tuple[object, ...], build_id: str, used_by: list[str], sample_rows: int
) -> EvidenceEntry:
    qid, sql, params, row_count, executed_at, raw_sample = row
    sample, columns = _sample(raw_sample, sample_rows)
    return EvidenceEntry(
        query_id=str(qid), sql="" if sql is None else str(sql), params=_params(params),
        row_count=row_count if isinstance(row_count, int) else None,
        executed_at=_executed_at(executed_at), build_id=build_id, result_sample=sample,
        sample_columns=columns, used_by=used_by, found=True,
    )  # fmt: skip


def load_evidence_entries(
    collector: EvidenceCollector,
    wh_con: duckdb.DuckDBPyConnection,
    *,
    build_id: str,
    sample_rows: int,
) -> list[EvidenceEntry]:
    """Evidence appendix entries in ``collector.ordered_ids()`` order (U09-14).

    Raises StoreBusy from the ops read; QueryError naming ``build_id`` on a DuckDB error.
    Ids found nowhere give ``found=False`` entries with ``sql=""``.
    """
    ids = collector.ordered_ids()
    if not ids:
        return []
    ops_rows = {q: r for q, r in ops.ui_get_evidence_rows(ids).items() if _trusted(r)}
    rest = [qid for qid in ids if qid not in ops_rows]
    meta_rows = _meta_rows(wh_con, rest, build_id) if rest else {}
    entries: list[EvidenceEntry] = []
    for qid in ids:
        used_by = collector.used_by(qid)
        if qid in ops_rows:
            entries.append(_from_ops(ops_rows[qid], used_by, sample_rows))
        elif qid in meta_rows:
            entries.append(_from_meta(meta_rows[qid], build_id, used_by, sample_rows))
        else:
            entries.append(
                EvidenceEntry(qid, "", {}, None, None, build_id, None, [], used_by, found=False)
            )
    return entries
