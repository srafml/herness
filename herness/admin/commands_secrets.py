"""``herness secrets init|set|status|rekey`` behavior (impl 10 U10-68 to U10-71, design 10 §3.7,
§5.2, §5.3).

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
from herness.core.audit import audit, last_secret_set_times
from herness.core.config import HernessConfig, get_config
from herness.core.errors import ConfigError, HernessError
from herness.core.jobs.ports import require_jobs_backend
from herness.core.jobs.scheduler import NEXT_KEY_REF, schedule_rekey
from herness.core.secrets import (
    SecretRef,
    delete_secret,
    exists,
    referenced_secret_names,
    set_secret,
)

__all__ = ["INIT_NAMES", "cmd_secrets_init", "cmd_secrets_rekey", "cmd_secrets_set"]
__all__ += ["cmd_secrets_status"]

INIT_NAMES: Final = ("redact.hmac_key", "ui_user_ref_key")
_ESCROWED: Final = "ESCROWED"
_KEY_BYTES: Final = 32  # secrets.token_hex(32): 64 hex characters (V11.5)
_MAX_VALUE_BYTES: Final = 16 * 1024  # U10-69 limit: value <= 16 KiB (UTF-8)
_ROTATE_AFTER: Final = timedelta(days=90)  # design 10 §5.2 rotation policy
_DIFFER: Final = "values differ"
_TOO_LONG: Final = "value exceeds 16 KiB"
_NOT_OBJECT: Final = "a value starting with { must be a JSON object of strings"


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
    if get_config().security.secrets.backend != "keyring":  # precondition, before any `show`
        msg = "secrets init needs the keyring backend"
        raise ConfigError(msg)
    rows: list[dict[str, object]] = []
    warnings: list[str] = []
    for name in INIT_NAMES:
        status = "present" if exists(name) else _create(name, actor=actor, prompt=prompt, show=show)
        rows.append({"name": name, "status": status})
        if status == "not created":
            warnings.append(f"{name} not created: escrow not confirmed")
    code = 1 if warnings else 0
    return CommandResult(ok=code == 0, data={"secrets": rows}, warnings=warnings, exit_code=code)


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
        data = {"name": ref.name, "stored": False}
        return CommandResult(ok=False, data=data, warnings=[reason], exit_code=1)
    set_secret(ref.name, v1, actor=actor)  # step 4: validates, audits by name, then writes
    data = {"name": ref.name, "stored": True}
    return CommandResult(ok=True, data=data, warnings=[], exit_code=0)


# --- U10-71 secrets rekey --------------------------------------------------------------------


def _fail(warning: str) -> CommandResult:
    return CommandResult(ok=False, data={}, warnings=[warning], exit_code=1)


def cmd_secrets_rekey(
    *, actor: str, prompt: Callable[[str], str], show: Callable[[str], None]
) -> CommandResult:
    """``herness secrets rekey`` (U10-71): stage an escrowed ``redact.hmac_key.next`` and plan
    the ``maintenance`` rekey job; the old key stays active until that job swaps it."""
    if exists(NEXT_KEY_REF):
        return _fail("a rekey is already staged")
    if _create(NEXT_KEY_REF, actor=actor, prompt=prompt, show=show) != "created":  # steps 1-2
        return _fail(f"{NEXT_KEY_REF} not created: escrow not confirmed")
    try:
        job_id = schedule_rekey()  # step 3
    except HernessError:
        # Typed planning failures (ConfigError, StoreBusy, ...): unstage so the command can be
        # rerun. Should a job row exist anyway, it fails "no staged key"; the old key stays.
        delete_secret(NEXT_KEY_REF, actor=actor)
        raise
    row = require_jobs_backend().get_job(job_id)
    when = None if row is None else row.scheduled_for
    if when is None:  # job planned but its row is unreadable: say so, never audit "None"
        audit("admin_action", actor, action="redact_rekey", target="staged")
        data: dict[str, object] = {"job_id": job_id, "planned_at": None}
        warning = f"rekey job {job_id} planned; planned time unknown, see `herness status`"
        return CommandResult(ok=True, data=data, warnings=[warning], exit_code=0)
    planned = clock.format_utc(when)
    audit("admin_action", actor, action="redact_rekey", target="staged", detail=planned)
    data = {"job_id": job_id, "planned_at": planned}
    return CommandResult(ok=True, data=data, warnings=[], exit_code=0)


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
    code = 1 if missing else 0
    return CommandResult(
        ok=code == 0, data={"secrets": rows}, warnings=missing + stale, exit_code=code
    )
