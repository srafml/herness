"""CLI identity: OS user -> actor, command -> role table, role and elevation checks (U09-89).

The CLI identity is the OS user mapped through ``security.ui.roles`` (design 09 §5.6, TB10).
``denied`` users run only ``--help``, ``--version``, ``doctor`` and ``config validate``.
"""

from __future__ import annotations

import ctypes
import functools
import getpass
import importlib
import inspect
import os
import types
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Final, cast

from typer._click.globals import get_current_context

from herness.core.audit import audit
from herness.core.errors import ConfigError, PermissionDenied
from herness.core.logging import get_logger
from herness.reports.rules import (
    ROLE_RANK,
    Actor,
    UiRole,
    load_user_ref_key,
    role_for,
    user_ref_for,
)

if TYPE_CHECKING:
    from herness.cli import GlobalOptions
    from herness.core.config import HernessConfig

__all__ = [
    "COMMAND_ROLES", "DENIED_ALLOWED", "ELEVATED_COMMANDS", "WRITE_COMMANDS", "check_command_role",
    "cli_actor", "guarded",
]  # fmt: skip

_log = get_logger("cli")

DENIED_ALLOWED: Final = frozenset({"doctor", "config validate"})
WRITE_COMMANDS: Final = frozenset({
    "decide", "review-queue approve", "review-queue reject", "memory approve", "memory reject",
    "memory purge", "chat", "resume", "jobs cancel", "jobs retry", "secrets set", "secrets rekey",
    "deploy install", "privacy delete",
})  # fmt: skip
ELEVATED_COMMANDS: Final = frozenset(
    {"secrets init", "secrets set", "secrets rekey", "deploy pull", "deploy install"}
)


def _rows(role: UiRole, paths: str) -> dict[str, UiRole]:
    return dict.fromkeys(paths.split(";"), role)


# U09-89 table (design §5.6 "Role"; "any" = viewer; the two DENIED_ALLOWED paths are viewer
# here and never reach the rank check).
COMMAND_ROLES: Final[Mapping[str, UiRole]] = types.MappingProxyType(
    _rows(
        "admin",
        "init;config show;secrets init;secrets set;secrets status;secrets rekey;deploy render;"
        "deploy pull;deploy up;deploy down;deploy rollback;deploy prune;deploy install;gpu load;"
        "gpu unload;sync;build;enrich;score;pipeline;eval;distill;privacy delete;report funding;"
        "report org;review;resume;worker;jobs cancel;jobs retry;memory export-lora;memory purge;"
        "laya status;laya accept;laya rollback;maintenance backup;maintenance purge",
    )
    | _rows("reviewer", "decide;review-queue list;review-queue approve;review-queue reject")
    | _rows("reviewer", "memory approve;memory reject")
    | _rows(
        "viewer",
        "doctor;config validate;config hash;metrics list;report render;chat;ui;status;jobs list;"
        "memory list",
    )
)


_UNLEN: Final = 256  # Windows UNLEN; the buffer holds one more for the terminator
_fallback_logged: list[bool] = []


def _windows_user() -> str:
    """Bare account name of the calling thread's token (``GetUserNameW``; not the environment)."""
    from ctypes import wintypes  # noqa: PLC0415 - only meaningful, and only imported, on Windows

    api = cast("Any", ctypes).WinDLL("advapi32", use_last_error=True)
    api.GetUserNameW.argtypes = [wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    api.GetUserNameW.restype = wintypes.BOOL
    buffer = ctypes.create_unicode_buffer(_UNLEN + 1)
    size = wintypes.DWORD(_UNLEN + 1)
    if not api.GetUserNameW(buffer, ctypes.byref(size)):
        raise ctypes.WinError(ctypes.get_last_error())
    return str(buffer.value)


def _posix_user() -> str:
    """Account name of the effective uid from the password database (not the environment)."""
    pwd = cast("Any", importlib.import_module("pwd"))  # POSIX only; typed per platform
    return str(pwd.getpwuid(cast("Any", os).geteuid()).pw_name)


def _credential_user() -> str:
    """The user from process credentials; empty when the lookup fails."""
    try:
        return (_windows_user if os.name == "nt" else _posix_user)()
    except (OSError, KeyError, AttributeError, ImportError):
        return ""


def _os_user() -> str:
    """The OS user from process credentials; ``getpass.getuser()`` only when that fails.

    ``getpass.getuser()`` reads ``LOGNAME``, ``USER``, ``LNAME`` and ``USERNAME`` first, so it
    would let any local user claim another account's role (TB10, TH09-27).
    """
    if name := _credential_user():
        return name
    if not _fallback_logged:
        _fallback_logged.append(True)
        _log.warning("cli.identity.fallback", source="getpass")  # no user name: personal data
    return getpass.getuser()


def cli_actor(cfg: HernessConfig, *, need_ref: bool) -> Actor:
    """The OS user as an ``Actor``; ``unkeyed`` without the key unless a ref is needed."""
    username = _os_user()
    role = role_for(username, cfg.security.ui.roles)
    try:
        user_ref = user_ref_for(username, load_user_ref_key())
    except ConfigError:
        if need_ref:
            raise
        user_ref = "unkeyed"
    return Actor(user_ref, role, "cli", display=username)


def _is_elevated() -> bool:
    """True in an elevated shell (Windows administrator, else effective uid 0)."""
    if os.name == "nt":
        return bool(cast("Any", ctypes).windll.shell32.IsUserAnAdmin())
    return bool(cast("Any", os).geteuid() == 0)  # typed per platform; checked on the host


def _refuse(actor: Actor, path: str, message: str) -> PermissionDenied:
    try:
        # Controller ruling (T09-02): the `auth` audit schema has no `action` field; the
        # action is in the log line.
        audit("auth", actor.user_ref, user_ref=actor.user_ref, role=actor.role, result="denied")
    except Exception as exc:  # noqa: BLE001 - any audit failure is logged; the refusal stands
        _log.error("cli.auth.audit_failed", action=f"cli:{path}", error_type=type(exc).__name__)
    _log.warning("cli.auth.denied", user_ref=actor.user_ref, role=actor.role, action=f"cli:{path}")
    return PermissionDenied(message, details={"code": "role_missing", "command": path})


def check_command_role(opts: GlobalOptions, path: str, *, inline: bool = False) -> Actor | None:
    """Return the actor allowed to run ``herness <path>``; else audit, log and refuse."""
    if path in DENIED_ALLOWED:
        return None
    if path == "init" and not (opts.config_dir / "herness.yaml").is_file():
        return None  # bootstrap (OI-05): no config to map the user against yet
    actor = opts.actor(need_ref=path in WRITE_COMMANDS)
    needed: UiRole = COMMAND_ROLES.get(path, "admin")  # an unlisted path fails closed
    if inline:
        needed = "admin"  # R-45
    if ROLE_RANK[actor.role] < ROLE_RANK[needed]:
        raise _refuse(actor, path, f"You need the {needed} role to run herness {path}.")
    if path in ELEVATED_COMMANDS and not _is_elevated():
        raise _refuse(actor, path, "Run this command from an elevated shell.")
    return actor


def guarded[F: Callable[..., Any]](path: str) -> Callable[[F], F]:
    """Run ``check_command_role`` before the handler and pass the result as ``actor``."""

    def decorate(handler: F) -> F:
        signature = inspect.signature(handler)
        params = [p for name, p in signature.parameters.items() if name != "actor"]

        @functools.wraps(handler)
        def wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 - any handler result
            opts = cast("GlobalOptions", get_current_context().find_root().obj)
            opts.command = path
            actor = check_command_role(opts, path, inline=bool(kwargs.get("inline", False)))
            return handler(*args, actor=actor, **kwargs)

        wrapper.__signature__ = signature.replace(parameters=params)  # type: ignore[attr-defined]
        return cast("F", wrapper)

    return decorate
