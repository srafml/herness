"""Evidence widget of ``app.common.widgets`` (impl 09 U09-67; T09-15c split, T09-15e policy).

Stored ``result_sample`` cells reach the page only as dataframe string cells (TH09-23) and never
as raw text (TH09-11). The classification rule is not repeated here: ``sample_rows`` and the
constants are those of ``herness.reports._sample_policy``, shared with the report appendix, so
both surfaces drop, redact and keep the same columns (numeric cells and column names included).
Public names are re-exported by ``app.common.widgets``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

import pyarrow as pa
import streamlit as st

from app.common import data, wh
from herness.reports._sample_policy import (
    ALWAYS_DROPPED,
    CELL_MAX,
    REDACTED,
    parse_json,
    sample_rows,
)
from herness.reports.contract import QUERY_ID_RE

__all__ = ["ALWAYS_DROPPED", "CELL_MAX", "EVIDENCE_SHOWN", "REDACTED", "evidence", "parse_json"]
__all__ += ["sample_rows"]

EVIDENCE_SHOWN: Final = 50


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
