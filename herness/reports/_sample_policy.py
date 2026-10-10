"""Stored ``result_sample`` cell policy shared by the report and the dashboard (TH09-11; T09-15d).

No raw text from a stored sample reaches a rendered surface: ``title``, ``summary``,
``description``, ``notes`` and the FORBIDDEN_TEXT_COLUMNS are always dropped (names compared
stripped and case-folded). Only verbatim-safe values are shown as stored: ``None``, booleans,
dates, timestamps, times, query ids, (prefixed) ULIDs with at least one letter and build ids.
Every other column, including numeric ones (Python ints and floats and decimal-shaped strings
such as ``555.0123``, so a phone or account number stored as a JSON number is masked), is
passed cell by cell through ``redact_text`` on the value's string form with the loaded key
(``[redacted]`` when it fails closed), or dropped when no key is loaded. The impl 10
detectors leave counts, small ints and decimals unchanged. A nested dict/list cell is
redacted as JSON text after its always-dropped keys are removed at any depth. Cells are
plain strings cut to 200 chars; templates escape them (TH09-23). Column names render as
headers, so a name is kept only when ``redact_text`` returns it unchanged (key loaded) or,
without a key, when it is identifier-shaped (``[A-Za-z_][A-Za-z0-9_ .-]{0,63}``) with no run
of five or more digits; any other column is dropped (T09-15d review I1).

Extends T09-15c's ``app/common/_widgets_evidence.py`` (parity test UT09-113) with the T09-15d
numeric and column-name rules; that widget must import this module (follow-up, batch 48).
``FORBIDDEN_TEXT_COLUMNS`` repeats ``app.common.wh``'s set (L5 may not import ``app``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Final

from herness.core import redact
from herness.core.errors import HernessError

__all__ = ["ALWAYS_DROPPED", "CELL_MAX", "FORBIDDEN_TEXT_COLUMNS", "REDACTED", "parse_json"]
__all__ += ["sample_rows"]

CELL_MAX: Final = 200
REDACTED: Final = "[redacted]"
FORBIDDEN_TEXT_COLUMNS: Final = frozenset(
    {"short_description", "description", "close_notes", "root_cause_text"}
)
ALWAYS_DROPPED: Final = frozenset(
    {"title", "summary", "description", "notes", *FORBIDDEN_TEXT_COLUMNS}
)
# Verbatim-safe text as the evidence encoder writes it (impl 04 U04-02): dates, timestamps,
# times (``HH:MM:SS``: six digits, below any phone or account length), query ids, (prefixed)
# ULIDs with at least one letter and build ids. Decimal-shaped and digit-only strings are
# redacted like numbers (T09-15d ruling).
_VERBATIM_RE: Final = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?(?:Z|[+-]\d{2}:?\d{2})?)?"
    r"|\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?"
    r"|q_[0-9a-f]{16}|(?:[a-z]{1,16}_)?(?=[0-9]*[A-HJKMNP-TV-Z])[0-7][0-9A-HJKMNP-TV-Z]{25}"
    r"|\d{8}-\d{6}-[0-9A-HJKMNP-TV-Z]{6}",
    re.ASCII,
)
_NAME_RE: Final = re.compile(r"(?!.*\d{5})[A-Za-z_][A-Za-z0-9_ .-]{0,63}", re.ASCII)
_MAX_DEPTH: Final = 32


def parse_json(raw: object) -> object:
    """``raw`` decoded when it is JSON text (``None`` when it is not), else ``raw`` itself."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (ValueError, RecursionError):
            return None
    return raw


def _verbatim(value: object) -> bool:
    """True for ``None``, booleans and verbatim-safe strings; never for an int or float."""
    if value is None or isinstance(value, bool):  # bool before any int check (bool is an int)
        return True
    return isinstance(value, str) and _VERBATIM_RE.fullmatch(value) is not None


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
        value = json.dumps(_strip(value), ensure_ascii=False, default=str)
    return (redact.redact_text(str(value)) or REDACTED)[:CELL_MAX]


def _safe_names(names: list[str]) -> list[str]:
    """Names that ``redact_text`` keeps unchanged; identifier-shaped ones without a key."""
    try:
        return [n for n in names if redact.redact_text(n) == n]
    except HernessError:  # no redaction key loaded (ConfigError)
        return [n for n in names if _NAME_RE.fullmatch(n) is not None]


def sample_rows(raw: object, limit: int | None = None) -> list[dict[str, str]] | None:
    """String cells cut to 200 chars, text redacted or dropped; ``None`` when not stored.

    ``limit`` keeps only the first ``limit`` dict rows (the report's ``evidence_sample_rows``)
    before any cell is classified or redacted; every returned row has the same columns.
    """
    parsed = parse_json(raw)
    if not isinstance(parsed, list):
        return None
    rows = [{str(k): v for k, v in row.items()} for row in parsed if isinstance(row, Mapping)]
    rows = rows if limit is None else rows[: max(limit, 0)]
    names = _safe_names([n for n in dict.fromkeys(k for r in rows for k in r) if not _dropped(n)])
    text = [n for n in names if not all(_verbatim(row.get(n)) for row in rows)]
    try:
        cells = {n: [_redacted(row.get(n)) for row in rows] for n in text}
    except HernessError:  # no redaction key loaded (ConfigError): every text column is dropped
        cells, names = {}, [n for n in names if n not in text]
    return [{n: cells[n][i] if n in cells else _cell(row.get(n)) for n in names}
            for i, row in enumerate(rows)]  # fmt: skip
