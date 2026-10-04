"""Operator commands, job handlers and doctor checks (impl 10 §3.7; L5 package, R-07)."""

from __future__ import annotations

import functools
from collections.abc import Callable

from herness.admin.maintenance import handle_maintenance
from herness.core.errors import ConfigError
from herness.core.jobs import register_handler, resolve_handler

__all__ = ["register_handlers"]


def register_handlers(*, memory_purge: Callable[[str], int]) -> None:
    """Register the ``maintenance`` handler (U10-75); impl 08 calls it with ``ctx`` only (R-42).

    Same ``memory_purge`` again: no-op (``partial`` is new each call); else ConfigError.
    """
    try:
        held = resolve_handler("maintenance")
    except ConfigError:
        held = None
    bound: dict[str, object] = {}
    if isinstance(held, functools.partial) and held.func is handle_maintenance:
        bound = held.keywords
    if bound.get("memory_purge") is memory_purge:
        return
    handler = functools.partial(handle_maintenance, memory_purge=memory_purge)
    register_handler("maintenance", handler)
