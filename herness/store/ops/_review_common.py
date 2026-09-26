"""Private helper module of `shared` (impl 02 §2.3): pure `review_item` SQL text and validation.

Not an area: no state, no connection, and never an import of `herness.store.ops.shared` (that
import runs the other way, like `core` importing `_shims`). Added by T02-24 to keep `shared.py`
within its impl 02 §2 module-map budget; holds validation used by U02-58, U02-59 and the U02-130
… U02-132 functions that `shared.py` itself defines.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Final

from herness.core.errors import ConfigError

MATCH_KEY_RE: Final = re.compile(r"[a-z_][a-z0-9_]{0,63}")
USER_REF_RE: Final = re.compile(r"[0-9a-f]{32}")
DECISIONS: Final = frozenset({"approved", "rejected"})
MAX_NOTE_CHARS: Final = 2000
MAX_FIELDS: Final = 32

# U02-130 step 2, U02-131, U02-132 step 3: match and field values reach SQL only as bound
# JSON data or a bound JSON path parameter, never as SQL text (TH02-07).
MATCH_LOOKUP: Final = (
    "SELECT item_id FROM review_item WHERE kind = ?"
    " AND status IN (SELECT value FROM json_each(?)) AND NOT EXISTS ("
    "SELECT 1 FROM json_each(?) AS m WHERE json_extract(review_item.payload,"
    " '$.' || m.key) IS NOT m.value) ORDER BY created_at, item_id LIMIT 1"
)
COUNT: Final = "SELECT count(*) FROM review_item WHERE kind = ? AND status = ?"
COUNT_GROUPED: Final = (
    "SELECT coalesce(CAST(json_extract(payload, ?) AS TEXT), '') AS k, count(*)"
    " FROM review_item WHERE kind = ? AND status = ? GROUP BY k ORDER BY k"
)
PAYLOAD_ONLY: Final = "SELECT payload FROM review_item WHERE item_id = ?"
UPDATE_PAYLOAD: Final = "UPDATE review_item SET payload = ? WHERE item_id = ?"


def keys_ok(keys: Sequence[str], high: int, payload: Mapping[str, object] | None = None) -> bool:
    """1..``high`` distinct ``MATCH_KEY_RE`` keys, each present in ``payload`` when given."""
    return (
        1 <= len(keys) <= high
        and len(set(keys)) == len(keys)
        and all(MATCH_KEY_RE.fullmatch(k) and (payload is None or k in payload) for k in keys)
    )


def is_count(value: object, low: int, high: int) -> bool:
    """True if ``value`` is a plain (non-bool) ``int`` within ``[low, high]`` (U02-58)."""
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def is_json_object(text: str) -> bool:
    """True if ``text`` parses as a JSON object (U02-59 ``label_check`` note check)."""
    try:
        return isinstance(json.loads(text), dict)
    except (ValueError, RecursionError):
        return False


def check_decision(item_id: object, status: object, decided_by: object, note: object) -> None:
    """Precondition checks for `decide_review_item` (U02-59): raise ``ConfigError`` else return."""
    if not isinstance(item_id, str) or status not in DECISIONS:
        msg = "item_id must be a string and status approved or rejected"
        raise ConfigError(msg)
    if not (
        isinstance(decided_by, str)
        and (decided_by == "system" or USER_REF_RE.fullmatch(decided_by))
    ):
        msg = "decided_by must be a 32-hex user_ref or system"
        raise ConfigError(msg)
    if note is not None and not (isinstance(note, str) and len(note) <= MAX_NOTE_CHARS):
        msg = f"note must be a string of at most {MAX_NOTE_CHARS} characters"
        raise ConfigError(msg)
