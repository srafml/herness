"""Evidence widget of ``app.common.widgets`` (impl 09 U09-67; T09-15c split for the line budget).

Stored ``result_sample`` cells reach the page only as dataframe string cells (TH09-23) and never
as raw text (TH09-11): ``title``, ``summary``, ``description``, ``notes`` and the
FORBIDDEN_TEXT_COLUMNS are always dropped (names compared stripped and case-folded); every other
column whose values are not all numeric, boolean, date/time or ids is passed through
``redact_text`` with the loaded key, or dropped when no key is loaded. A nested dict/list cell is
redacted as JSON text after its always-dropped keys are removed. Decimal-shaped strings
(``555.1234``) count as numeric and are shown. Public names are re-exported by
``app.common.widgets``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Final

import pyarrow as pa
import streamlit as st

from app.common import data, wh
from herness.core import redact
from herness.core.errors import HernessError
from herness.reports.contract import QUERY_ID_RE

__all__ = ["ALWAYS_DROPPED", "CELL_MAX", "EVIDENCE_SHOWN", "REDACTED", "evidence", "parse_json"]
__all__ += ["sample_rows"]

EVIDENCE_SHOWN: Final = 50
CELL_MAX: Final = 200
REDACTED: Final = "[redacted]"
ALWAYS_DROPPED: Final = frozenset(
    {"title", "summary", "description", "notes", *wh.FORBIDDEN_TEXT_COLUMNS}
)
# Typed text as the evidence encoder writes it (impl 04 U04-02): decimals with a fraction,
# non-finite floats, dates, timestamps, times, query ids, (prefixed) ULIDs with at least one
# letter and build ids. A digit-only string (phone, account number) counts as text.
_TYPED_RE: Final = re.compile(
    r"[-+]?\d+\.\d+|-?inf|nan"
    r"|\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?(?:Z|[+-]\d{2}:?\d{2})?)?"
    r"|\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?"
    r"|q_[0-9a-f]{16}|(?:[a-z]{1,16}_)?(?=[0-9]*[A-HJKMNP-TV-Z])[0-7][0-9A-HJKMNP-TV-Z]{25}"
    r"|\d{8}-\d{6}-[0-9A-HJKMNP-TV-Z]{6}",
    re.ASCII,
)
_MAX_DEPTH: Final = 32


def parse_json(raw: object) -> object:
    """``raw`` decoded when it is JSON text (``None`` when it is not), else ``raw`` itself."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (ValueError, RecursionError):
            return None
    return raw


def _typed(value: object) -> bool:
    if value is None or isinstance(value, bool | int | float):
        return True
    return isinstance(value, str) and _TYPED_RE.fullmatch(value) is not None


def _dropped(name: str) -> bool:
    return name.strip().casefold() in ALWAYS_DROPPED


def _strip(value: object, depth: int = 0) -> object:
    """``value`` without always-dropped keys at any depth; ``None`` below ``_MAX_DEPTH``."""
    if depth > _MAX_DEPTH:
        return None
    if isinstance(value, Mapping):
        return {str(k): _strip(v, depth + 1) for k, v in value.items() if not _dropped(str(k))}
    if isinstance(value, list):
        return [_strip(v, depth + 1) for v in value]
    return value


def _cell(value: object) -> str:
    return ("" if value is None else str(value))[:CELL_MAX]


def _redacted(value: object) -> str:
    """The redacted cell; ``[redacted]`` when redaction fails closed (``None``)."""
    if value is None:
        return ""
    if isinstance(value, Mapping | list):
        value = json.dumps(_strip(value), ensure_ascii=False)
    return (redact.redact_text(str(value)) or REDACTED)[:CELL_MAX]


def sample_rows(raw: object) -> list[dict[str, str]] | None:
    """String cells cut to 200 chars, text redacted or dropped; ``None`` when not stored."""
    parsed = parse_json(raw)
    if not isinstance(parsed, list):
        return None
    rows = [{str(k): v for k, v in row.items()} for row in parsed if isinstance(row, Mapping)]
    names = [n for n in dict.fromkeys(k for row in rows for k in row) if not _dropped(n)]
    text = [n for n in names if not all(_typed(row.get(n)) for row in rows)]
    try:
        cells = {n: [_redacted(row.get(n)) for row in rows] for n in text}
    except HernessError:  # no redaction key loaded (ConfigError): every text column is dropped
        cells, names = {}, [n for n in names if n not in text]
    return [{n: cells[n][i] if n in cells else _cell(row.get(n)) for n in names}
            for i, row in enumerate(rows)]  # fmt: skip


def _meta_rows(ids: list[str]) -> tuple[dict[str, dict[str, object]], str | None]:
    """``meta.evidence`` rows of the current build for ``ids`` (none without a build)."""
    try:
        result = wh.query("evidence.meta", ids=ids)
    except wh.NoCurrentBuild:
        return {}, None
    rows = result.table.to_pylist()
    return {str(row["query_id"]): row for row in rows}, result.build_id


def _entry(query_id: str, row: Mapping[str, object] | None, build_id: str | None) -> None:
    st.subheader(query_id, anchor=f"ev-{query_id}")
    if row is None:
        st.caption("Evidence not found")
        return
    st.code(str(row.get("sql") or ""), language="sql")
    params = parse_json(row.get("params"))
    st.json(params if isinstance(params, dict | list) else {})
    st.text(f"Build {row.get('build_id') or build_id} · {row.get('row_count')} rows")
    sample = sample_rows(row.get("result_sample"))
    if sample is None:
        st.caption("Sample not stored for this build")
    else:
        st.dataframe(pa.Table.from_pylist(sample), hide_index=True)


def evidence(query_ids: Sequence[str], *, label: str | None = None) -> None:
    """The evidence expander: one anchored sub-section per query id (design §5.3, §5.5)."""
    ids = list(dict.fromkeys(q for q in query_ids if QUERY_ID_RE.fullmatch(q) is not None))
    shown = ids[:EVIDENCE_SHOWN]
    with st.expander(label or f"Evidence ({len(ids)} queries)"):
        ops = data.ops_read("ui_get_evidence_rows", ids=tuple(shown))
        found: Mapping[str, Mapping[str, object]] = ops if isinstance(ops, Mapping) else {}
        missing = [q for q in shown if q not in found]
        meta, meta_build = _meta_rows(missing) if missing else ({}, None)
        for query_id in shown:
            row = found.get(query_id)
            _entry(query_id, row if row is not None else meta.get(query_id), meta_build)
        if len(ids) > EVIDENCE_SHOWN:
            st.caption(f"… and {len(ids) - EVIDENCE_SHOWN} more")
