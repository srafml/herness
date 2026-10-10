"""Private helpers of pages 08 - 10 (impl 09 U09-80 - U09-82): role gates, actions, cells.

Pages write only through ``herness.reports.actions``. ``allowed`` applies the same action table
as the action's own ``require_role``, so a control is shown exactly to the roles the action
accepts (TH09-07). ``act`` shows a refused or failed action with ``user_error`` next to its
form, whose keyed widgets keep their values (FT09-01); a ``NotFound`` or ``UserInputError``
refusal also clears the ops cache (widget state is untouched), so an item that is gone or
already decided drops out on the next rerun and a pending one is re-read. A success
clears the ops cache (``after_write``) and the form's keys (no second submit of the same
values), then reruns the page with a one-line confirmation.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Final, cast

import streamlit as st
from pydantic import ValidationError

from app.common import data
from app.common.widgets import CELL_MAX, user_error
from herness.core.errors import HernessError, NotFound
from herness.core.logging import get_logger
from herness.core.types import NumberRef
from herness.reports.rules import ACTION_ROLES, ROLE_RANK, Actor, UserInputError

ALL: Final = "All"
DONE_KEY: Final = "herness_action_done"
_REVIEW_ACTIONS: Final = {
    "weight_change": "review_decide_weight_change",
    "memory_write": "memory_decide",
}
type Row = Mapping[str, object]

_log = get_logger("app.page")


def allowed(actor: Actor, action: str) -> bool:
    """True when ``actor``'s role passes ``require_role(actor, action)``."""
    return ROLE_RANK[actor.role] >= ROLE_RANK[ACTION_ROLES[action]]


def review_action(kind: str) -> str:
    """The action ``decide_review`` checks for an item of ``kind`` (U09-38 step 4)."""
    return _REVIEW_ACTIONS.get(kind, "review_decide")


def rows(fn_name: str, /, **params: object) -> list[Row]:
    """The rows of an allowlisted ops read (cached)."""
    return list(cast("Sequence[Row]", data.ops_read(fn_name, **params)))


def cell(value: object, limit: int = CELL_MAX) -> str:
    """A plain-text cell: JSON for containers, ``—`` for null, cut to ``limit``."""
    if value is None:
        return "—"
    if isinstance(value, Mapping | list | tuple):
        value = json.dumps(value, sort_keys=True, default=str)
    return str(value)[:limit]


def choose(label: str, values: Sequence[str], *, key: str) -> str | None:
    """A select box with ``All`` first; returns the chosen value (``None`` for all)."""

    def show(value: str | None) -> str:
        return ALL if value is None else value[:CELL_MAX]

    choice: str | None = st.selectbox(label, [None, *values], format_func=show, key=key)
    return choice


def numbers(raw: object) -> list[NumberRef]:
    """Stored ``numbers`` as ``NumberRef`` values; malformed items skipped (DEBUG log, count)."""
    refs: list[NumberRef] = []
    for index, item in enumerate(raw if isinstance(raw, list) else ()):
        try:
            refs.append(NumberRef.model_validate(item))
        except ValidationError as exc:
            _log.debug("app.page.number_dropped", index=index, errors=exc.error_count())
    return refs


def flash() -> None:
    """Show (once) the confirmation of the action that triggered this rerun."""
    text = st.session_state.pop(DONE_KEY, None)
    if text is not None:
        st.success(str(text))


def act[T](
    label: str, call: Callable[[], T], done: Callable[[T], str], *, reset: Sequence[str] = ()
) -> None:
    """Run one action: refusal or failure → error card here (``NotFound`` / ``UserInputError``:
    ops cache cleared, never on ``PermissionDenied`` or ``StoreBusy``); success → clear cache
    and the ``reset`` widget keys, rerun."""
    try:
        result = call()
    except HernessError as exc:
        if isinstance(exc, NotFound | UserInputError):  # maybe stale: re-read, form kept
            data.after_write()
        user_error(exc, title=f"{label} failed")
        return
    data.after_write()
    for key in reset:
        st.session_state.pop(key, None)
    st.session_state[DONE_KEY] = done(result)
    st.rerun()
