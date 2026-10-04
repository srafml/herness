"""Untrusted-field wrapping of a swarm task input (impl 06 U06-141, R-20, TH06-19).

Private sibling of `herness.harness.swarm.routing`, which re-exports `build_task_input`. Every
model- or user-written text field of a task input is wrapped in one `<untrusted_data>` block by
spec 05 `wrap_untrusted`, which escapes `<`, `>` and `&` so a field cannot close its own block.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from herness.core.errors import ConfigError
from herness.core.types import Role
from herness.harness.tools import wrap_untrusted

__all__ = ["build_task_input"]

_ROLES: Final = frozenset({"planner", "analyst", "skeptic", "writer", "chat"})
# Already wrapped by spec 07 with source="memory": passed through untouched.
_PASS: Final = frozenset({"prior_context", "session"})


@dataclass(frozen=True, slots=True)
class _Ids:
    """Record ids in scope at one nesting level, inherited by nested values."""

    task_id: str = ""
    finding_id: str = ""
    message_id: str = ""


def build_task_input(role: Role, payload: Mapping[str, object]) -> dict[str, object]:
    """A copy of `payload` with every untrusted text field wrapped (U06-141); pure.

    Fields are matched by key at any nesting depth: `notes` (source `task_notes`, the task id),
    `objective` next to a set `revision_of` (`revision`, that finding id), `claim` (`finding`,
    the finding id), `required_actions[*]` and `challenge_summary` (`skeptic`, the finding id)
    and `question` (`chat`, the user `message_id` or empty). Ids come from the nearest
    enclosing mapping (`task_id`, `finding_id` or `finding.finding_id`, `message_id`). Empty
    or non-text values, `prior_context` and `session` are unchanged. `role` must be planner,
    analyst, skeptic, writer or chat, else `ConfigError`.
    """
    if role not in _ROLES:
        msg = f"no task input rules for role {role}"
        raise ConfigError(msg)
    return _mapping(payload, _Ids())


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _ids(m: Mapping[str, object], outer: _Ids) -> _Ids:
    finding = m.get("finding")
    nested = finding.get("finding_id") if isinstance(finding, Mapping) else None
    return _Ids(
        task_id=_text(m.get("task_id")) or outer.task_id,
        finding_id=_text(m.get("finding_id")) or _text(nested) or outer.finding_id,
        message_id=_text(m.get("message_id")) or outer.message_id,
    )


def _mapping(m: Mapping[str, object], outer: _Ids) -> dict[str, object]:
    ids = _ids(m, outer)
    revision = _text(m.get("revision_of"))
    return {key: _field(key, value, ids, revision) for key, value in m.items()}


def _rule(key: str, ids: _Ids, revision: str) -> tuple[str, str] | None:
    """`(source, record_id)` of a text field, or `None` when the field is trusted."""
    rules = {
        "notes": ("task_notes", ids.task_id),
        "claim": ("finding", ids.finding_id),
        "challenge_summary": ("skeptic", ids.finding_id),
        "question": ("chat", ids.message_id),
    }
    if key == "objective":
        return ("revision", revision) if revision else None
    return rules.get(key)


def _field(key: str, value: object, ids: _Ids, revision: str) -> object:
    if key in _PASS:
        return value
    if isinstance(value, str):
        rule = _rule(key, ids, revision)
        if rule is None or not value:
            return value
        return wrap_untrusted(value, source=rule[0], record_id=rule[1])
    if key == "required_actions" and isinstance(value, list):
        return [
            wrap_untrusted(item, source="skeptic", record_id=ids.finding_id)
            if isinstance(item, str) and item
            else _walk(item, ids)
            for item in value
        ]
    return _walk(value, ids)


def _walk(value: object, ids: _Ids) -> object:
    if isinstance(value, Mapping):
        return _mapping(value, ids)
    if isinstance(value, list):
        return [_walk(item, ids) for item in value]
    if isinstance(value, tuple):
        return tuple(_walk(item, ids) for item in value)
    return value
