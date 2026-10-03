"""Jira changelog and remote-link projections (impl 01 U01-94; design 01 §4.2, §5.8).

The values stored in the ``changelog`` and ``remotelinks`` columns of the Jira ``issue``
row: each history keeps ``{id, created, items[{field, from, fromString, to, toString}]}``
and each remote link ``{id, object{url, title}}``. Every other member (``author``,
``application``, ``relationship``, ...) is dropped (personal-data minimisation, TH01-05).
Both projections are pure and idempotent. The paged fetches of complete changelogs and
remote links (U01-76, U01-77) join this module with T01-18.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from herness.core.errors import SchemaViolation

__all__ = ["project_history", "project_remote_link"]

_SOURCE: Final = "jira"
_ITEM_MEMBERS: Final = ("field", "from", "fromString", "to", "toString")


def project_history(history: Mapping[str, object]) -> dict[str, object]:
    """``{"id", "created", "items": [...]}`` of one changelog history (U01-94).

    Missing item members become ``None``; ``items`` absent gives ``[]``; items that are not
    mappings are skipped. ``SchemaViolation("bad changelog history")`` when ``id`` or
    ``created`` is not a string or ``items`` is not a list.
    """
    hid, created, items = history.get("id"), history.get("created"), history.get("items")
    if items is None:
        items = []
    if not (isinstance(hid, str) and isinstance(created, str) and isinstance(items, list)):
        msg = "bad changelog history"
        raise SchemaViolation(msg, source=_SOURCE)
    projected = [
        {member: item.get(member) for member in _ITEM_MEMBERS}
        for item in items
        if isinstance(item, Mapping)
    ]
    return {"id": hid, "created": created, "items": projected}


def project_remote_link(link: Mapping[str, object]) -> dict[str, object]:
    """``{"id", "object": {"url", "title"}}`` of one remote link, ``id`` as a string (U01-94).

    ``SchemaViolation("bad remote link")`` when ``id`` is not a number or string or
    ``object`` is not a mapping.
    """
    lid, obj = link.get("id"), link.get("object")
    valid_id = isinstance(lid, str) or (isinstance(lid, int) and not isinstance(lid, bool))
    if not (valid_id and isinstance(obj, Mapping)):
        msg = "bad remote link"
        raise SchemaViolation(msg, source=_SOURCE)
    return {"id": str(lid), "object": {"url": obj.get("url"), "title": obj.get("title")}}
