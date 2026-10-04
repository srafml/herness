"""The OS user from process credentials, shared by the CLI and the dashboard (U09-89, U09-55).

``getpass.getuser()`` reads ``LOGNAME``, ``USER``, ``LNAME`` and ``USERNAME`` before the password
database, so any local user could claim another account's role through the environment (TB10,
TH09-27). These helpers read the process credentials instead: the Windows token
(``GetUserNameW``) or the effective uid's password-database entry; ``getpass`` is only the last
resort, and the caller decides how to log that fallback. Lifted from ``herness._cli.identity``
(T09-20b) so ``app/`` need not import ``herness._cli`` (impl 09 §2 import rule; T09-13).
"""

from __future__ import annotations

import ctypes
import getpass
import importlib
import os
from collections.abc import Callable
from typing import Any, Final, cast

__all__ = ["credential_user", "os_user", "posix_user", "windows_user"]

_UNLEN: Final = 256  # Windows UNLEN; the buffer holds one more for the terminator


def windows_user() -> str:
    """Bare account name of the calling thread's token (``GetUserNameW``; not the environment)."""
    from ctypes import wintypes  # noqa: PLC0415 - only meaningful, and only imported, on Windows

    api = cast("Any", ctypes).WinDLL("advapi32", use_last_error=True)
    api.GetUserNameW.argtypes = [wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    api.GetUserNameW.restype = wintypes.BOOL
    buffer = ctypes.create_unicode_buffer(_UNLEN + 1)
    size = wintypes.DWORD(_UNLEN + 1)
    if not api.GetUserNameW(buffer, ctypes.byref(size)):
        raise cast("Any", ctypes).WinError(cast("Any", ctypes).get_last_error())
    return str(buffer.value)


def posix_user() -> str:
    """Account name of the effective uid from the password database (not the environment)."""
    pwd = cast("Any", importlib.import_module("pwd"))  # POSIX only; typed per platform
    return str(pwd.getpwuid(cast("Any", os).geteuid()).pw_name)


def credential_user(
    windows: Callable[[], str] | None = None, posix: Callable[[], str] | None = None
) -> str:
    """The user from process credentials; empty when the lookup fails.

    ``windows`` and ``posix`` replace the two platform lookups (the CLI passes its own module
    names so its tests can patch them).
    """
    lookup = (windows or windows_user) if os.name == "nt" else (posix or posix_user)
    try:
        return lookup()
    except (OSError, KeyError, AttributeError, ImportError):
        return ""


def os_user(
    credential: Callable[[], str] | None = None, *, on_fallback: Callable[[], None] | None = None
) -> str:
    """The OS user from process credentials; ``getpass.getuser()`` only when that fails.

    ``on_fallback`` runs before the ``getpass`` fallback (the caller logs it once, without the
    name: personal data).
    """
    if name := (credential or credential_user)():
        return name
    if on_fallback is not None:
        on_fallback()
    return getpass.getuser()
