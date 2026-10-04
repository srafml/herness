"""``herness secrets init|set|status`` behavior (impl 10 U10-68 to U10-70, design 10 §3.7, §5.2).

Bodies only: impl 09 checks the role and the elevated shell, supplies ``prompt`` (hidden console
input) and ``show`` (a console write that bypasses logging), renders the ``CommandResult`` and
maps a backend ``ConfigError`` raised here to exit 3 (R-46). Secret values reach the operator
only through ``show``; ``data``, ``warnings``, errors and logs carry names only (TH10-07).
"""

from __future__ import annotations

import json
import secrets as stdlib_secrets
from collections.abc import Callable
from datetime import timedelta
from typing import Final

from herness._cli.output import CommandResult
from herness.core import time as clock
from herness.core.audit import last_secret_set_times
from herness.core.config import HernessConfig
from herness.core.secrets import SecretRef, exists, referenced_secret_names, set_secret

__all__ = ["INIT_NAMES", "cmd_secrets_init", "cmd_secrets_set", "cmd_secrets_status"]

INIT_NAMES: Final = ("redact.hmac_key", "ui_user_ref_key")
_ESCROWED: Final = "ESCROWED"
_KEY_BYTES: Final = 32  # secrets.token_hex(32): 64 hex characters (V11.5)
_MAX_VALUE_BYTES: Final = 16 * 1024  # U10-69 limit: value <= 16 KiB (UTF-8)
_ROTATE_AFTER: Final = timedelta(days=90)  # design 10 §5.2 rotation policy
_DIFFER: Final = "values differ"
_TOO_LONG: Final = "value exceeds 16 KiB"
_NOT_OBJECT: Final = "a value starting with { must be a JSON object of strings"


def _result(data: dict[str, object], warnings: list[str], exit_code: int) -> CommandResult:
    return CommandResult(ok=exit_code == 0, data=data, warnings=warnings, exit_code=exit_code)


# --- U10-68 secrets init ---------------------------------------------------------------------


def _create(
    name: str, *, actor: str, prompt: Callable[[str], str], show: Callable[[str], None]
) -> str:
    """Steps 2-5 for one absent key: show it once, confirm escrow, then store it."""
    value = stdlib_secrets.token_hex(_KEY_BYTES)
    show(
        f"{name}: {value}\n"
        "Store this value in the corporate password vault now; it is shown only once."
    )
    if prompt(f"Type {_ESCROWED} after storing {name} in the vault") != _ESCROWED:
        return "not created"
    set_secret(name, value, actor=actor)
    return "created"


def cmd_secrets_init(
    *, actor: str, prompt: Callable[[str], str], show: Callable[[str], None]
) -> CommandResult:
    """``herness secrets init`` (U10-68): create the two keys if absent, escrow-confirmed."""
    rows: list[dict[str, object]] = []
    warnings: list[str] = []
    for name in INIT_NAMES:
        status = "present" if exists(name) else _create(name, actor=actor, prompt=prompt, show=show)
        rows.append({"name": name, "status": status})
        if status == "not created":
            warnings.append(f"{name} not created: escrow not confirmed")
    return _result({"secrets": rows}, warnings, 1 if warnings else 0)


# --- U10-69 secrets set ----------------------------------------------------------------------


def _json_object_of_strings(value: str) -> bool:
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return False
    return isinstance(data, dict) and all(isinstance(item, str) for item in data.values())


def _rejection(v1: str, v2: str) -> str | None:
    """U10-69 steps 2-3 and the 16 KiB limit; the reason never holds the value."""
    if v1 != v2:
        return _DIFFER
    if len(v1.encode("utf-8")) > _MAX_VALUE_BYTES:
        return _TOO_LONG
    if v1.startswith("{") and not _json_object_of_strings(v1):
        return _NOT_OBJECT
    return None


def cmd_secrets_set(name: str, *, actor: str, prompt: Callable[[str], str]) -> CommandResult:
    """``herness secrets set NAME`` (U10-69): the value comes from two hidden prompts only."""
    ref = SecretRef.parse(name)  # step 1: ConfigError (exit 3) before any prompt
    v1 = prompt(f"Value for {ref.name}")
    v2 = prompt("Repeat")
    reason = _rejection(v1, v2)
    if reason is not None:
        return _result({"name": ref.name, "stored": False}, [reason], 1)
    set_secret(ref.name, v1, actor=actor)  # step 4: validates, audits by name, then writes
    return _result({"name": ref.name, "stored": True}, [], 0)


# T10-30: cmd_secrets_rekey


# --- U10-70 secrets status -------------------------------------------------------------------


def cmd_secrets_status(*, cfg: HernessConfig) -> CommandResult:
    """``herness secrets status`` (U10-70): names, presence and last-set time; never values."""
    names = referenced_secret_names(cfg)
    last_set = last_secret_set_times(cfg.paths.logs, names)
    now = clock.now()
    rows: list[dict[str, object]] = []
    missing: list[str] = []
    stale: list[str] = []
    for name in names:
        present, when = exists(name), last_set.get(name)
        rows.append(
            {
                "name": name,
                "present": present,
                "last_set": None if when is None else clock.format_utc(when),
            }
        )
        if not present:
            missing.append(f"secret missing: {name}")
        if when is not None and now - when > _ROTATE_AFTER:
            stale.append(f"{name}: last set {(now - when).days} days ago; rotate per policy")
    return _result({"secrets": rows}, missing + stale, 1 if missing else 0)
