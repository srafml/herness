"""Human promotion, rollback and status of Laya versions (U03-138 ... U03-140, T03-33).

`accept_model`/`rollback_model` are the CLI-wired human gate against poisoning (TH03-04)
and the repudiation record (TH03-11); path validation is `verify_model_dir`'s (TH03-08).
Both hold `data/locks/laya.lock` through the write, then audit and log after releasing it.
`laya_status` is read-only and never raises for a single bad entry.
"""

from __future__ import annotations

import getpass
import json
import re
from typing import TYPE_CHECKING, Final, cast

from pydantic import ValidationError

from herness.core import time as clock
from herness.core.audit import audit, log_lock
from herness.core.config import get_config
from herness.core.errors import ConfigError
from herness.core.logging import get_logger
from herness.enrich.cache import replace_atomic
from herness.enrich.labels import LabelStore, gold_digest
from herness.enrich.laya_models import LayaManifest, read_current, verify_model_dir, write_current
from herness.enrich.layout import EnrichPaths
from herness.enrich.questions import load_question_set

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

__all__ = ["accept_model", "laya_status", "rollback_model"]

_ACCEPT_STATUSES: Final = frozenset({"candidate", "accepted"})
_ROLLBACK_STATUSES: Final = frozenset({"accepted"})
_LOCK_TIMEOUT_S: Final = 10.0
_MAX_EVAL_BYTES: Final = 2 * 1024 * 1024
_MAX_MANIFEST_BYTES: Final = 256 * 1024
_ENTRY_RE: Final = re.compile(r"^laya-(\d{8})-(\d+)$")
_log = get_logger("enrich.laya_admin")


def _load_eval(directory: Path) -> dict[str, object]:
    """The parsed ``eval.json`` of ``directory``; raises ``ValueError`` for any problem."""
    try:
        with (directory / "eval.json").open("rb") as handle:
            raw = handle.read(_MAX_EVAL_BYTES + 1)
    except OSError as exc:
        msg = "eval.json missing or unreadable"
        raise ValueError(msg) from exc
    if len(raw) > _MAX_EVAL_BYTES:
        msg = "eval.json too large"
        raise ValueError(msg)
    doc = json.loads(raw) if raw.strip() else None
    if not isinstance(doc, dict) or not isinstance(doc.get("questions"), dict):
        msg = "eval.json invalid"
        raise ValueError(msg)  # noqa: TRY004 - a malformed file, not a caller type error
    return doc


def _proposed_ids(doc: dict[str, object]) -> list[str]:
    questions = cast("dict[str, object]", doc["questions"])
    return sorted(
        qid
        for qid, entry in questions.items()
        if isinstance(entry, dict) and entry.get("accepted_proposed") is True
    )


def _check_stale(
    doc: dict[str, object], manifest: LayaManifest, active_qsv: str, gold: str
) -> None:
    eval_qsv = doc.get("question_set_version")
    if eval_qsv != manifest.question_set_version or eval_qsv != active_qsv:
        msg = f"{manifest.version}: stale eval (question set version mismatch)"
        raise ConfigError(msg, version=manifest.version)
    if doc.get("gold_sha256") != gold:
        msg = f"{manifest.version}: stale eval (gold digest mismatch)"
        raise ConfigError(msg, version=manifest.version)


def _resolve_questions(doc: dict[str, object], questions: Sequence[str] | None) -> list[str]:
    proposed = _proposed_ids(doc)
    wanted = sorted(set(questions)) if questions is not None else proposed
    if not wanted:
        msg = "accept_model needs at least one accepted_proposed question"
        raise ConfigError(msg)
    refused = sorted(set(wanted) - set(proposed))
    if refused:
        msg = "accept refused: questions are not accepted_proposed"
        raise ConfigError(msg, details={"refused": ",".join(refused)})
    return wanted


def _laya_lock(paths: EnrichPaths) -> Path:
    lock = paths.data_root / "locks" / "laya.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    return lock


def accept_model(
    version: str, questions: Sequence[str] | None = None, *, actor: str = "system"
) -> None:
    """Human-confirmed promotion of `version` to `CURRENT` (U03-138, design 03 §5.8 step 7).

    Defaults to every question with `accepted_proposed = true` in `eval.json`; a requested
    id outside that set is refused (`ConfigError`, listing the refused ids).
    """
    cfg = get_config()
    paths = EnrichPaths.from_config(cfg)
    qsv = load_question_set(cfg.decisions).version
    with log_lock(_laya_lock(paths), timeout_s=_LOCK_TIMEOUT_S):
        manifest = verify_model_dir(paths, version, require_status=_ACCEPT_STATUSES)
        try:
            doc = _load_eval(paths.laya_dir(version))
        except ValueError as exc:
            msg = f"{version}: {exc}"
            raise ConfigError(msg, version=version) from exc
        gold = gold_digest(LabelStore(paths, qsv).read("gold"))
        _check_stale(doc, manifest, qsv, gold)
        accepted_questions = _resolve_questions(doc, questions)
        updated = manifest.model_copy(
            update={
                "status": "accepted",
                "accepted_questions": accepted_questions,
                "accepted_by": f"os:{getpass.getuser()}",
                "accepted_at": clock.now(),
            }
        )
        target = paths.laya_dir(version) / "manifest.json"
        payload = updated.model_dump_json().encode("utf-8")
        replace_atomic(target, lambda tmp: tmp.write_bytes(payload), kind="manifest")
        write_current(paths, version)
    audit("admin_action", actor, action="laya_accept", target=version, detail=accepted_questions)
    _log.info("enrich.laya.accepted", version=version, questions=accepted_questions)


def rollback_model(to_version: str, *, actor: str = "system") -> None:
    """Point `CURRENT` at an earlier accepted version (U03-139); no re-inference needed."""
    cfg = get_config()
    paths = EnrichPaths.from_config(cfg)
    with log_lock(_laya_lock(paths), timeout_s=_LOCK_TIMEOUT_S):
        verify_model_dir(paths, to_version, require_status=_ROLLBACK_STATUSES)
        try:
            previous: str | None = read_current(paths)
        except ConfigError:
            previous = None
        write_current(paths, to_version)
    audit(
        "admin_action",
        actor,
        action="laya_rollback",
        target=to_version,
        detail=f"from={previous or 'none'}",
    )
    _log.info("enrich.laya.rolled_back", from_version=previous, to_version=to_version)


def _read_manifest_bounded(directory: Path, version: str) -> LayaManifest | None:
    try:
        with (directory / "manifest.json").open("rb") as handle:
            raw = handle.read(_MAX_MANIFEST_BYTES + 1)
    except OSError:
        return None
    if len(raw) > _MAX_MANIFEST_BYTES:
        return None
    try:
        manifest = LayaManifest.model_validate_json(raw)
    except ValidationError:
        return None
    return manifest if manifest.version == version else None


def _status_entry(paths: EnrichPaths, version: str) -> dict[str, object]:
    directory = paths.laya_root() / version
    try:
        ok = directory.is_dir() and not directory.is_symlink()
    except OSError:
        ok = False
    manifest = _read_manifest_bounded(directory, version) if ok else None
    if manifest is None:
        return {"version": version, "status": "invalid"}
    try:
        doc: dict[str, object] | None = _load_eval(directory)
    except ValueError:
        doc = None
    return {
        "version": version,
        "status": manifest.status,
        "created_at": manifest.created_at,
        "teacher": manifest.teacher,
        "accepted_questions": list(manifest.accepted_questions),
        "accepted_proposed": _proposed_ids(doc) if doc is not None else [],
        "macro_metric": doc.get("macro_metric") if doc is not None else None,
    }


def _sort_key(version: str) -> tuple[int, int]:
    match = _ENTRY_RE.fullmatch(version)
    return (int(match.group(1)), int(match.group(2))) if match else (0, 0)


def laya_status() -> dict[str, object]:
    """Read-only summary of every Laya version for the operator, newest first (U03-140)."""
    cfg = get_config()
    paths = EnrichPaths.from_config(cfg)
    root = paths.laya_root()
    names = [e.name for e in root.iterdir() if _ENTRY_RE.fullmatch(e.name)] if root.is_dir() else []
    versions = [_status_entry(paths, name) for name in sorted(names, key=_sort_key, reverse=True)]
    try:
        current: str | None = read_current(paths)
    except ConfigError:
        current = None
    return {"current": current, "versions": versions}
