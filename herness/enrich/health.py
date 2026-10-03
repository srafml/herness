"""Enrichment component health for `herness doctor` (U03-146, ENG §4).

Read-only except one probe file created and removed in the cache root; loads no model.
The Laya hash check is memoized per version and weights file stat. Never raises.
"""

from __future__ import annotations

import os
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Final, Literal

from herness.core.config import get_config
from herness.enrich.calibrate import CalibrationStore
from herness.enrich.laya_models import read_current, verify_model_dir
from herness.enrich.layout import EnrichPaths

__all__ = ["health"]

type Health = tuple[Literal["ok", "degraded", "down"], str]

_LOCK: Final = threading.Lock()
_MEMO: dict[tuple[str, int, int], bool] = {}  # (version, weights mtime_ns, size) -> verified


def _cache_writable(paths: EnrichPaths) -> bool:
    """Create and remove a probe file in the cache root (the data root while it is absent)."""
    root = paths.data_root / "cache"
    target = root if root.exists() else paths.data_root
    if not target.is_dir():
        return False
    handle, probe = tempfile.mkstemp(prefix=".health-", dir=target)
    try:
        os.close(handle)
    finally:
        Path(probe).unlink(missing_ok=True)
    return True


def _laya_version(paths: EnrichPaths) -> str:
    """The `CURRENT` version once `verify_model_dir` passed for it (memoized); ConfigError."""
    version = read_current(paths)
    stat = (paths.laya_dir(version) / "model.safetensors").stat()
    key = (version, stat.st_mtime_ns, stat.st_size)
    with _LOCK:
        if not _MEMO.get(key):
            verify_model_dir(paths, version)
            _MEMO.clear()
            _MEMO[key] = True
    return version


def _calibrated(paths: EnrichPaths, laya_version: str) -> bool:
    """Every statically known decider version in use has calibration entries for the qsv."""
    cfg = get_config()
    decisions, deciders = cfg.decisions, cfg.models.deciders
    used = {decisions.primary_decider, *decisions.escalation_chain}
    used |= {q.primary_decider for q in decisions.questions if q.primary_decider is not None}
    versions = {  # llm versions come from the spec 05 role binding at run time: not checked
        "laya": laya_version,
        "openjev": deciders.openjev.model if deciders.openjev.enabled else None,
        "jev": deciders.jev.model if deciders.jev.enabled else None,
    }
    store = CalibrationStore(paths)
    qsv = decisions.question_set_version
    return all(
        store.load(name, version, qsv)
        for name in sorted(used)
        if (version := versions.get(name)) is not None
    )


def _attempt[T](check: Callable[[], T]) -> T | None:
    try:
        return check()
    except Exception:  # noqa: BLE001 - health never raises; a failed check is its code
        return None


def health() -> Health:
    """`(status, reason)` with a fixed reason code (U03-146); thread-safe, never raises."""
    try:
        paths = EnrichPaths.from_config(get_config())
    except Exception:  # noqa: BLE001 - any config failure means DecisionsConfig is unusable
        return ("down", "config_invalid")
    if not _attempt(lambda: _cache_writable(paths)):
        return ("down", "cache_not_writable")
    version = _attempt(lambda: _laya_version(paths))
    if version is None:
        return ("degraded", "laya_degraded")
    if not _attempt(lambda: paths.embedding_model_dir().is_dir()):
        return ("degraded", "embedding_model_missing")
    if not _attempt(lambda: _calibrated(paths, version)):
        return ("degraded", "calibration_missing")
    return ("ok", "ok")
