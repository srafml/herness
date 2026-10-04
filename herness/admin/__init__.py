"""Operator commands, job handlers and doctor checks (impl 10 §3.7; L5 package, R-07).

The ``commands_*`` modules hold the command bodies that impl 09's Typer commands call after the
role check (T09-24 ``herness._cli.cmd_admin``); each returns a ``CommandResult`` and never prints.
"""

from __future__ import annotations

__all__: list[str] = []

# T10-19: register_handlers (maintenance job handler)
