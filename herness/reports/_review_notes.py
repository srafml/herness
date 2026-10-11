"""Stored note of a review decision (T09-28): a ``label_check`` note is a JSON object (impl 02)."""

from __future__ import annotations

from herness.core.ids import canonical_json
from herness.reports import rules as r
from herness.reports.rules import UserInputError

_MAX_STORED = 2000  # impl 02 decide_review_item note limit


def stored_review_note(
    kind: str, *, approve: bool, note: str | None, answer: str | None
) -> str | None:
    """The note impl 02 stores; ``note`` is already sanitised (``validate_note``).

    ``label_check``: approve -> ``{"answer": ..., "reason": ...}`` (reason only with a note),
    reject -> ``{"reason": ...}``; other kinds keep the plain note.
    """
    if kind != "label_check":
        return note
    body: dict[str, str] = {}
    if approve:
        if not (answer or "").strip():
            text = "Choose the correct answer to approve a label check."
            raise UserInputError(text)
        body["answer"] = r.validate_answer(answer or "")
    if note is not None:
        body["reason"] = note
    stored = canonical_json(body)
    if len(stored) > _MAX_STORED:
        text = "The note is too long to store for a label check."
        raise UserInputError(text)
    return stored
