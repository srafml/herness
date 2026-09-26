"""Roles, pseudonymous user refs, server-side role checks, input validators and user messages.

One table of who may do what (design §9.2), shared by the dashboard and the CLI (impl 09
U09-29 to U09-33, U09-88).
"""

from __future__ import annotations

import functools
import hashlib
import hmac
import re
import types
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final, Literal

from herness.core import config as _config
from herness.core.audit import audit
from herness.core.errors import (
    AuthError,
    ConfigError,
    EgressBlocked,
    HernessError,
    ModelUnavailable,
    NotFound,
    PermissionDenied,
    RecoverableError,
    ReportContractError,
    StoreBusy,
)
from herness.core.logging import get_logger
from herness.core.secrets import resolve
from herness.core.settings import RolesConfig

__all__ = [
    "ACTION_ROLES", "ACTION_TEXT", "ITEM_ID_RE", "JOB_ID_RE", "MEMORY_ID_RE", "MESSAGE_ID_RE",
    "REC_ID_RE", "ROLE_RANK", "SESSION_ID_RE", "Actor", "UiRole", "UserInputError", "check_id",
    "load_user_ref_key", "require_role", "role_for", "user_message", "user_ref_for",
    "validate_answer", "validate_correction", "validate_note", "validate_question",
    "validate_reason",
]  # fmt: skip

# Named UiRole, not the spec's Role: impl 06 owns `Role` in core.types (OWN040; cf. RegistryKind).
type UiRole = Literal["denied", "viewer", "reviewer", "admin"]

_log = get_logger("app.auth")
_KEY: Final = "ui_user_ref_key"

# --- U09-29 roles and the action table ------------------------------------------------------

ROLE_RANK: Final[Mapping[UiRole, int]] = types.MappingProxyType(
    {"denied": 0, "viewer": 1, "reviewer": 2, "admin": 3}
)

_ACTIONS: Final[tuple[tuple[str, UiRole, str], ...]] = (
    ("view", "viewer", "view the dashboard"),
    ("chat", "viewer", "use chat"),
    ("chat_feedback", "viewer", "give feedback"),
    ("chat_correction", "viewer", "propose a correction"),
    ("decide_recommendation", "reviewer", "decide recommendations"),
    ("review_decide", "reviewer", "approve items"),
    ("review_decide_weight_change", "admin", "approve weight changes"),
    ("memory_decide", "reviewer", "approve memory items"),
    ("job_cancel", "admin", "cancel jobs"),
    ("job_retry", "admin", "retry jobs"),
    ("run_resume", "admin", "resume runs"),
    ("report_rerender", "admin", "re-render reports from the dashboard"),
    ("report_render", "viewer", "render reports"),
    ("view_trace_payload", "admin", "view trace payloads"),
    ("job_inline", "admin", "run jobs in this process with `--inline` (R-45)"),
)
ACTION_ROLES: Final[Mapping[str, UiRole]] = types.MappingProxyType({a: n for a, n, _ in _ACTIONS})
ACTION_TEXT: Final[Mapping[str, str]] = types.MappingProxyType({a: t for a, _, t in _ACTIONS})


def _check_tables(roles: Mapping[str, object], text: Mapping[str, str]) -> None:
    """ConfigError when the action tables disagree (U09-29 postcondition, no assert)."""
    if set(roles) != set(text):
        msg = "ACTION_ROLES and ACTION_TEXT keys differ"
        raise ConfigError(msg)


_check_tables(ACTION_ROLES, ACTION_TEXT)


@dataclass(frozen=True)
class Actor:
    """Who acts (``user_ref`` 32 hex or ``anonymous``); ``display`` is UI text, never logged."""

    user_ref: str
    role: UiRole
    channel: Literal["dashboard", "cli"]
    display: str = field(repr=False)


class UserInputError(RecoverableError):
    """A value typed by a person fails validation (CLI exit 2; shown next to the form)."""


def _norm(name: str) -> str:
    return name.strip().lower()


def role_for(username: str | None, roles: RolesConfig) -> UiRole:
    """Map an identity to its role; admins win over reviewers (U09-30)."""
    name = _norm(username or "")
    if not name:
        return "denied"
    if name in {_norm(n) for n in roles.admins}:
        return "admin"
    if name in {_norm(n) for n in roles.reviewers}:
        return "reviewer"
    return roles.default_role


def user_ref_for(username: str, key: bytes) -> str:
    """``hex(HMAC-SHA256(key, username.strip().lower()))[:32]`` (U09-31)."""
    name = _norm(username)
    if not name:
        msg = "Username must not be empty."
        raise UserInputError(msg)
    if not key:
        msg = "user reference key is empty"
        raise ConfigError(msg, details={"code": "secret_missing", "secret": _KEY})
    return hmac.new(key, name.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


@functools.cache
def load_user_ref_key() -> bytes:
    """The ``ui_user_ref_key`` secret as bytes, read once per process (U09-31)."""
    try:
        secret = resolve(_KEY)
    except ConfigError as exc:
        details = {"code": "secret_missing", "secret": _KEY}
        hint = "Run `herness secrets init`."
        raise ConfigError(exc.message, hint=hint, details=details) from None
    return secret.get_secret_value().encode("utf-8")


# Cleared by reset_config (the config fixture), like the other per-process caches (U10-10).
_config._RESET_HOOKS.append(load_user_ref_key.cache_clear)


def require_role(actor: Actor, action: str) -> None:
    """Return only when ``actor`` may do ``action``; else audit, log and PermissionDenied."""
    needed = ACTION_ROLES.get(action)
    if needed is None:
        msg = f"unknown action {action}"
        raise ConfigError(msg, details={"code": "unknown_action"})
    if ROLE_RANK[actor.role] >= ROLE_RANK[needed]:
        return
    try:
        # Controller ruling (T09-02): impl 10's `auth` audit schema allows only user_ref, role
        # and result, so the spec's `action=` field is not passed; the action is in the log line.
        audit("auth", actor.user_ref, user_ref=actor.user_ref, role=actor.role, result="denied")
    except Exception as exc:  # noqa: BLE001 - any audit failure is logged; the refusal stands
        _log.error("app.auth.audit_failed", action=action, error_type=type(exc).__name__)
    fields = {"user_ref": actor.user_ref, "role": actor.role, "action": action}
    _log.warning("app.auth.denied", **fields, channel=actor.channel)
    group = "admins" if needed == "admin" else "reviewers"
    msg = f"You need the {needed} role to {ACTION_TEXT[action]}."
    hint = f"Ask an admin to add you to security.ui.roles.{group}."
    details = {"code": "role_missing", "action": action, "role": needed}
    raise PermissionDenied(msg, hint=hint, details=details)


# --- U09-33 validators and id patterns --------------------------------------------------------

_CONTROL_RE: Final = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_ULID: Final = "[0-9A-HJKMNP-TV-Z]{26}$"
SESSION_ID_RE: Final = re.compile("^ses_" + _ULID)
MESSAGE_ID_RE: Final = re.compile("^msg_" + _ULID)
ITEM_ID_RE: Final = re.compile("^rev_" + _ULID)
REC_ID_RE: Final = re.compile("^rec_" + _ULID)
JOB_ID_RE: Final = re.compile("^job_" + _ULID)
MEMORY_ID_RE: Final = re.compile("^mem_" + _ULID)


def _clean(text: str) -> str:
    """Remove C0/C1 control characters except newline and tab, then strip."""
    return _CONTROL_RE.sub("", text).strip()


def _bounded(text: str, low: int, high: int, msg: str) -> str:
    cleaned = _clean(text)
    if not low <= len(cleaned) <= high:
        raise UserInputError(msg)
    return cleaned


def validate_reason(text: str) -> str:
    """Cleaned reason of 10 to 1000 characters (DD-27)."""
    return _bounded(text, 10, 1000, "Reason must be 10 to 1000 characters.")


def validate_note(text: str | None, *, required: bool, max_chars: int = 1000) -> str | None:
    """Cleaned note of at most ``max_chars``; empty -> None, or an error when ``required``."""
    cleaned = _clean(text or "")
    if not cleaned:
        if required:
            msg = "A note is required to reject."
            raise UserInputError(msg)
        return None
    return _bounded(cleaned, 1, max_chars, f"Note must be at most {max_chars} characters.")


def validate_question(text: str, *, max_chars: int) -> str:
    """Cleaned chat question of 1 to ``max_chars`` characters."""
    return _bounded(text, 1, max_chars, f"Question must be 1 to {max_chars} characters.")


def validate_correction(text: str) -> str:
    """Cleaned correction of 1 to 1000 characters (design §5.5 step 6)."""
    return _bounded(text, 1, 1000, "Correction must be 1 to 1000 characters.")


def validate_answer(text: str) -> str:
    """Cleaned ``label_check`` answer of 1 to 200 characters."""
    return _bounded(text, 1, 200, "Answer must be 1 to 200 characters.")


def check_id(pattern: re.Pattern[str], value: str, kind: str) -> str:
    """``value`` if it fully matches ``pattern``, else ``invalid <kind> id`` (value not echoed)."""
    if pattern.fullmatch(value) is None:
        msg = f"invalid {kind} id"
        raise UserInputError(msg)
    return value


# --- U09-88 user_message ----------------------------------------------------------------------

_LOG_FIX: Final = "See the log `data/logs/herness-<date>.jsonl`."
_DOCTOR_FIX: Final = "See `herness doctor` and the log `data/logs/herness-<date>.jsonl`."
_NO_CURRENT: Final = ("No promoted warehouse yet.", "Run `herness pipeline`.")
_LIST_FIX: Final = (
    "Check the id with the matching list command "
    "(`herness jobs list`, `herness review-queue list`, `herness memory list`)."
)


def _report_contract(exc: HernessError, code: str | None) -> tuple[str, str]:
    d = exc.details
    run_id = d.get("run_id", "<id>")
    if code == "build_retired":
        what = f"Build {d.get('build_id', '<id>')} used by this run was deleted by retention."
        return what, "Re-run the review: `herness report funding`."
    if code in {"draft_missing", "draft_invalid"}:
        what = f"Run {run_id} has no valid report draft."
        return what, f"Check `herness status`; resume with `herness resume {run_id}`."
    if code == "uncited":
        what = f"{d.get('count', 'Some')} numbers in the draft have no evidence."
        return what, "Re-run the review, or `--no-strict` to inspect."
    if code == "run_not_finished":
        what = f"Run {run_id} is not finished (status {d.get('status', 'unknown')})."
        return what, f"Wait for the run, or `herness resume {run_id}`."
    return exc.message, "Re-run the review, or render with `--no-strict` to inspect."


def _by_class(exc: HernessError, code: str | None) -> tuple[str, str] | None:
    d = exc.details
    out: tuple[str, str] | None = None
    if isinstance(exc, ReportContractError):
        out = _report_contract(exc, code)
    elif isinstance(exc, ConfigError) and code == "pdf_missing":
        out = "PDF engine not installed.", "`uv sync --extra pdf` (GTK/Pango on Windows, spec 10)."
    elif isinstance(exc, ConfigError) and code == "secret_missing" and d.get("secret") == _KEY:
        out = "The user reference key is missing.", "Run `herness secrets init`."
    elif isinstance(exc, StoreBusy) and "job_id" in d:
        out = f"Another pipeline is running (job {d['job_id']}).", "Wait, or `herness jobs list`."
    elif isinstance(exc, StoreBusy):
        out = "Ops store is locked.", "Retry; check for a stuck process in `herness status`."
    elif isinstance(exc, ModelUnavailable):
        what = f"Reasoning model not reachable at {d.get('url', 'the configured endpoint')}."
        out = what, "`herness deploy up reasoning`; `herness doctor`."
    elif isinstance(exc, AuthError):
        fix = f"`herness secrets set {d.get('secret', '<name>')}`; `herness doctor --sources`."
        out = f"{d.get('source', 'The source')} rejected the credentials.", fix
    elif isinstance(exc, EgressBlocked):
        what = "An off-network call was refused by the data policy."
        out = what, "Use profile `local`, or record the approval in `herness.yaml` (spec 10)."
    return out


def user_message(exc: BaseException) -> tuple[str, str]:
    """``(what, fix)`` for the CLI and the dashboard: class, then ``details["code"]`` (U09-88)."""
    if any(cls.__name__ == "NoCurrentBuild" for cls in type(exc).__mro__):
        return _NO_CURRENT  # app/common/wh.py NoCurrentBuild; herness never imports app/
    if not isinstance(exc, HernessError):
        return "Unexpected error.", _LOG_FIX
    code = exc.details.get("code")
    if isinstance(exc, NotFound):
        return _NO_CURRENT if code == "no_current" else (exc.message or "Not found.", _LIST_FIX)
    found = _by_class(exc, code)
    if found is not None:
        return found
    return exc.message or type(exc).__name__, exc.hint or _DOCTOR_FIX
