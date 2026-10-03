"""CLI output: JSON envelope, `Error:`/`Fix:` lines and R-46 exit codes (impl 09 U09-86, U09-87,
U09-105).

stdout carries only command output (one JSON object with ``--json``); warnings, errors, logs
and tracebacks go to stderr (TH09-22).
"""

from __future__ import annotations

import json
import re
import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import PurePath
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel
from rich.console import Console
from rich.markup import escape
from typer._click.exceptions import UsageError

from herness._cli._exit_codes import EXIT_CODES, exit_code_for, exit_code_for_class_name
from herness.core import errors as e
from herness.core.secrets import scrub_secrets
from herness.reports.rules import user_message

if TYPE_CHECKING:
    from herness.cli import GlobalOptions

__all__ = [
    "EXIT_CODES", "CliResult", "CommandResult", "emit", "emit_error", "exit_code_for",
    "exit_code_for_class_name", "json_default",
]  # fmt: skip

_USAGE_FIX: Final = "Run `herness --help`."
_FORBIDDEN_CODES: Final = frozenset({2, 130})


# --- terminal-safe text -------------------------------------------------------------------------

# T09-25: replace with herness._cli.term.safe_terminal_text (U09-100) once term.py exists.
_ESCAPES: Final = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_CONTROLS: Final = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_MAX_CHARS: Final = 20_000


def _clean(text: str) -> str:
    """U09-100 steps 1-3: no CSI/OSC sequences, no control character but newline and tab."""
    text = _CONTROLS.sub("", _ESCAPES.sub("", text))
    return text if len(text) <= _MAX_CHARS else text[: _MAX_CHARS - 1] + "…"


def _markup(text: str) -> str:
    """Clean text escaped for ``rich`` markup (U09-100 step 4)."""
    return escape(_clean(text))


# --- results and envelope -----------------------------------------------------------------------


@dataclass
class CliResult:
    """What a command handler returns for ``emit`` (U09-86)."""

    command: str
    data: dict[str, object] | None
    warnings: list[str]
    human: Callable[[Console], None] | None
    exit_code: int = 0


def _table_printer(data: dict[str, object] | None) -> Callable[[Console], None]:
    def show(console: Console) -> None:
        for key, value in (data or {}).items():
            text = value if isinstance(value, str) else json.dumps(value, default=json_default)
            console.print(f"{_markup(str(key))}: {_markup(text)}")

    return show


@dataclass(frozen=True)
class CommandResult:
    """Result of a ``herness.admin`` command function, rendered by this spec (U09-105)."""

    ok: bool
    data: dict[str, object] | None
    warnings: list[str]
    exit_code: int

    def to_cli_result(self, path: str) -> CliResult:
        """The ``CliResult`` for command ``path``; SchemaViolation for a disallowed code."""
        allowed = self.exit_code in EXIT_CODES and self.exit_code not in _FORBIDDEN_CODES
        if not allowed or self.ok != (self.exit_code == 0):
            msg = f"command {path} returned exit code {self.exit_code}"
            raise e.SchemaViolation(msg)
        human = _table_printer(self.data)
        return CliResult(path, self.data, list(self.warnings), human, self.exit_code)


def json_default(value: object) -> object:
    """``json.dumps`` fallback for the envelope; TypeError for anything else (U09-86)."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            msg = "naive datetime is not serialisable (ISO-8601 UTC needs a time zone)"
            raise TypeError(msg)
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, PurePath):
        return value.as_posix()
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, set | frozenset):
        return sorted(value)
    msg = f"{type(value).__name__} is not JSON serialisable"
    raise TypeError(msg)


def _write_json(envelope: dict[str, object], command: str) -> None:
    try:
        line = json.dumps(envelope, default=json_default, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        msg = f"cannot serialise output of {command}"
        raise e.SchemaViolation(msg) from None
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def emit(opts: GlobalOptions, result: CliResult) -> int:
    """Print ``result`` as the JSON envelope or for humans; return its exit code (U09-86)."""
    if opts.json:
        data = None if result.data is None else {"schema": "cli/1", **result.data}
        envelope: dict[str, object] = {
            "ok": result.exit_code == 0,
            "command": result.command,
            "data": data,
            "warnings": list(result.warnings),
            "error": None,
        }
        _write_json(envelope, result.command)
        return result.exit_code
    if result.human is not None:
        result.human(Console(file=sys.stdout, highlight=False, soft_wrap=True))
    for warning in result.warnings:
        sys.stderr.write(f"Warning: {_clean(warning)}\n")
    return result.exit_code


def _describe(exc: BaseException) -> tuple[str, str, str, dict[str, str]]:
    """``(type, what, fix, details)``; non-Herness, non-usage errors are ``InternalError``."""
    if isinstance(exc, UsageError):
        return type(exc).__name__, exc.format_message() or "Usage error.", _USAGE_FIX, {}
    what, fix = user_message(exc)
    if isinstance(exc, e.HernessError):
        return type(exc).__name__, what, fix, dict(exc.details)
    return "InternalError", what, fix, {}


def emit_error(opts: GlobalOptions, command: str, exc: BaseException) -> int:
    """Print ``exc`` as the JSON error envelope or `Error:`/`Fix:` lines; return the code."""
    code = exit_code_for(exc)
    kind, what, fix, details = _describe(exc)
    if opts.json:
        error = {"type": kind, "exit_code": code, "message": what, "hint": fix, "details": details}
        envelope: dict[str, object] = {
            "ok": False,
            "command": command,
            "data": None,
            "warnings": [],
            "error": error,
        }
        _write_json(envelope, command)
        return code
    sys.stderr.write(f"Error: {_clean(what)}\nFix: {_clean(fix)}\n")
    if opts.verbose:
        text = "".join(traceback.format_exception(exc))
        scrubbed = scrub_secrets(None, "emit_error", {"event": text}).get("event")
        sys.stderr.write(_clean(scrubbed if isinstance(scrubbed, str) else "log.scrub.failed"))
    return code
