"""``herness config validate|show|hash`` behavior (impl 10 U10-65 to U10-67, design 10 §3.7).

Bodies only: impl 09 checks the role, renders the ``CommandResult`` and maps a ``ConfigError``
raised here at load to exit 3 (R-46). ``config validate`` reports config problems as issue
lines and exit 1; it never raises for them.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from herness._cli.output import CommandResult
from herness.core.config import config_hash, effective_dict, load_config, validate
from herness.core.config_sources import ProfileName, resolve_profile
from herness.core.config_view import ConfigIssue
from herness.core.errors import ConfigError

__all__ = ["OFFLINE_HASH_WARNING", "cmd_config_hash", "cmd_config_show", "cmd_config_validate"]

OFFLINE_HASH_WARNING: Final = "config_hash computed without key_id; not comparable to builds"
_OFFLINE_KEY_ID: Final = "unresolved"


def _result(data: dict[str, object], warnings: list[str], exit_code: int) -> CommandResult:
    return CommandResult(ok=exit_code == 0, data=data, warnings=warnings, exit_code=exit_code)


def _exit_code(issues: Sequence[ConfigIssue], *, strict: bool) -> int:
    """U10-65 step 4: 1 on any error; 1 on any warn with ``strict``; else 0 (R-46)."""
    severities = {issue.severity for issue in issues}
    return 1 if "error" in severities or (strict and "warn" in severities) else 0


def _hash_or_none(config_dir: Path, profile: ProfileName, *, offline: bool) -> str | None:
    """The config hash, or ``None`` when the config does not load (already an issue)."""
    try:
        cfg = load_config(profile, config_dir=config_dir)
    except ConfigError:
        return None
    return config_hash(cfg, key_id=_OFFLINE_KEY_ID if offline else None)


def cmd_config_validate(
    *, config_dir: Path, profile: ProfileName | None, offline: bool, strict: bool
) -> CommandResult:
    """``herness config validate`` (U10-65): issues, resolved profile and ``config_hash``."""
    try:
        name = resolve_profile(profile, os.environ)  # U10-09 step 1
    except ConfigError as exc:
        issue = ConfigIssue("error", "profile", exc.message, None)
        return _result({"profile": profile, "config_hash": None, "issues": [str(issue)]}, [], 1)
    issues = validate(config_dir, name, offline=offline)
    lines = [str(issue) for issue in issues]
    warnings: list[str] = []
    digest = _hash_or_none(config_dir, name, offline=offline)
    if offline and digest is not None:
        note = str(ConfigIssue("warn", "config_hash", OFFLINE_HASH_WARNING, None))
        lines.append(note)
        warnings.append(note)
    # The exit decision covers the validator issues only: the offline hash note is advisory,
    # so `--offline --strict` on a clean config exits 0 (IT10-11, sub-controller ruling).
    data: dict[str, object] = {"profile": name, "config_hash": digest, "issues": lines}
    return _result(data, warnings, _exit_code(issues, strict=strict))


def cmd_config_show(*, config_dir: Path, profile: ProfileName | None) -> CommandResult:
    """``herness config show`` (U10-66): the effective config, secrets as ``secret:<name>``."""
    cfg = load_config(profile, config_dir=config_dir)
    return _result(effective_dict(cfg, redact_secrets=True), [], 0)


def cmd_config_hash(*, config_dir: Path, profile: ProfileName | None) -> CommandResult:
    """``herness config hash`` (U10-67)."""
    cfg = load_config(profile, config_dir=config_dir)
    return _result({"config_hash": config_hash(cfg)}, [], 0)
