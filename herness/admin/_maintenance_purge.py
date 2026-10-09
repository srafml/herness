"""Nightly retention purge for the ``maintenance`` job (impl 10 U10-98; private to
``herness.admin.maintenance``, which re-exports ``run_purge``).

Every deletion goes through ``_Deleter``: contained in ``paths.data``, never a symlink,
``dry_run`` aware, ``PermissionError`` counted ``skipped_in_use`` and retried next night.
"""

from __future__ import annotations

import calendar
import datetime
import json
import re
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from herness.core import time as clock
from herness.core.audit import audit
from herness.core.config import HernessConfig
from herness.core.errors import FatalError, HernessError
from herness.core.jobs.ports import JobContext
from herness.store.lake_purge import purge_partitions_before
from herness.store.layout import DataLayout
from herness.store.ops.chat import purge_chat
from herness.store.warehouse import read_current

__all__ = ["BEAT_EVERY", "counts_text", "parse_day", "run_purge"]

BEAT_EVERY: Final = 100
_LOG_RE: Final = re.compile(r"^(egress|audit|herness)-(\d{4}-\d{2}-\d{2})\.jsonl$")
_DT_RE: Final = re.compile(r"^dt=(\d{4}-\d{2}-\d{2})$")
_LOG_KEYS: Final = {"egress": "egress", "audit": "audit", "herness": "app"}  # prefix -> key
_KEYS: Final = ("lake_partitions", "traces", "egress_logs", "audit_logs", "app_logs", "reports")
_EXTRA_KEYS: Final = ("cache_files", "skipped_in_use", "skipped_unsafe", "rekey_marker_invalid")


def counts_text(counts: Mapping[str, int]) -> str:
    """Audit ``counts`` text ``k=v;k=v`` in key order."""
    return ";".join(f"{key}={value}" for key, value in sorted(counts.items()))


def parse_day(text: str) -> datetime.date | None:
    """``YYYY-MM-DD`` as a date, None when it is not one."""
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        return None


class _Deleter:
    """Contained deletions with counts, ``dry_run`` and a heartbeat every 100 (U10-98 7, 9)."""

    def __init__(self, data: Path, ctx: JobContext, *, dry_run: bool) -> None:
        self.data, self.ctx, self.dry_run, self.done = data.resolve(), ctx, dry_run, 0
        self.counts = dict.fromkeys((*_KEYS, *_EXTRA_KEYS), 0)

    def skipped(self) -> int:
        return self.counts["skipped_in_use"] + self.counts["skipped_unsafe"]

    def delete(self, path: Path, key: str | None) -> None:
        """Delete ``path`` and count it under ``key`` (None: not counted). A path that is
        already gone counts nothing; any other ``OSError`` is a ``FatalError``."""
        if path.is_symlink() or not path.resolve().is_relative_to(self.data):
            self.counts["skipped_unsafe"] += 1
            return
        try:
            if not self.dry_run:
                shutil.rmtree(path) if path.is_dir() else path.unlink()
        except PermissionError:
            self.counts["skipped_in_use"] += 1
            return
        except FileNotFoundError:
            return  # removed by someone else since it was listed
        except OSError as exc:
            msg = f"purge failed: {path.resolve().relative_to(self.data).as_posix()}"
            raise FatalError(msg) from exc
        if key is not None:
            self.counts[key] += 1
        self.done += 1
        if self.done % BEAT_EVERY == 0:
            self.ctx.heartbeat()


def _months_before(day: datetime.date, months: int) -> datetime.date:
    index = day.year * 12 + day.month - 1 - months
    year, month = divmod(index, 12)
    last = calendar.monthrange(year, month + 1)[1]
    return datetime.date(year, month + 1, min(day.day, last))


def _purge_lake(cfg: HernessConfig, deleter: _Deleter, now: datetime.datetime) -> None:
    cutoff = _months_before(now.date(), cfg.retention.raw_lake_months)
    raw = Path(cfg.paths.data) / "raw"
    if not deleter.dry_run:
        result = purge_partitions_before(cutoff, today=now.date(), root=raw)
        deleter.counts["lake_partitions"] += result.partitions_deleted
        deleter.counts["skipped_in_use"] += result.deferred_locked
        return
    for part in raw.glob("*/*/dt=*"):
        match = _DT_RE.match(part.name)
        day = parse_day(match.group(1)) if match else None
        if part.is_dir() and day is not None and day < cutoff:
            deleter.counts["lake_partitions"] += 1


def _mtime(path: Path) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(path.lstat().st_mtime, tz=datetime.UTC)


def _purge_files(cfg: HernessConfig, deleter: _Deleter, now: datetime.datetime) -> None:
    keep = cfg.retention
    traces_cut = now - datetime.timedelta(days=keep.traces_days)
    for trace in sorted((Path(cfg.paths.data) / "traces").glob("*.jsonl")):
        if _mtime(trace) < traces_cut:
            deleter.delete(trace, "traces")
    today = now.date()
    for log in sorted(Path(cfg.paths.logs).glob("*.jsonl")):
        match = _LOG_RE.match(log.name)
        day = parse_day(match.group(2)) if match else None
        if match is None or day is None or day >= today:
            continue
        key = _LOG_KEYS[match.group(1)]
        if day < today - datetime.timedelta(days=getattr(keep, f"{key}_log_days")):
            deleter.delete(log, f"{key}_logs")
    reports = Path(cfg.paths.data) / "reports"
    reports_cut = now - datetime.timedelta(days=keep.reports_days)
    runs = [p for p in sorted(reports.glob("*")) if p.name != "eval"]
    for run in [*runs, *sorted(reports.glob("eval/*"))]:
        manifest = run / "manifest.json"
        if run.is_dir() and _mtime(manifest if manifest.is_file() else run) < reports_cut:
            deleter.delete(run, "reports")


def _swapped_at(marker: Path) -> datetime.datetime | None:
    """``swapped_at`` of the SWAPPED marker; None when the marker is unreadable or malformed."""
    try:
        return clock.parse_utc(json.loads(marker.read_text(encoding="utf-8"))["swapped_at"])
    except (OSError, ValueError, KeyError, TypeError, HernessError):
        return None


def _purge_rekey_cache(cfg: HernessConfig, deleter: _Deleter) -> None:
    """Design 10 §5.3 step 4: drop pre-swap decision-cache files once a newer build is live;
    the marker goes only when every eligible file went (skipped files retry next night)."""
    data = Path(cfg.paths.data)
    marker = data / "cache" / "rekey" / "SWAPPED"
    if not marker.is_file():
        return
    swapped_at = _swapped_at(marker)
    if swapped_at is None:
        deleter.counts["rekey_marker_invalid"] += 1
        return
    current = read_current(layout=DataLayout.from_root(data))
    if current is None:
        return
    started = datetime.datetime.strptime(current[:15], "%Y%m%d-%H%M%S").replace(tzinfo=datetime.UTC)
    if started <= swapped_at:
        return
    skipped = deleter.skipped()
    cache = data / "cache" / "decisions"
    for file in sorted(p for p in cache.rglob("*") if p.is_file() or p.is_symlink()):
        if _mtime(file) < swapped_at:
            deleter.delete(file, "cache_files")
    if deleter.skipped() == skipped and not deleter.dry_run:
        deleter.delete(marker, None)


def run_purge(
    cfg: HernessConfig, ctx: JobContext, *, now: datetime.datetime, dry_run: bool = False
) -> dict[str, int]:
    """Nightly retention purge over lake, traces, logs, reports, chat and rekey cache (U10-98)."""
    deleter = _Deleter(Path(cfg.paths.data), ctx, dry_run=dry_run)
    _purge_lake(cfg, deleter, now)
    _purge_files(cfg, deleter, now)
    counts = deleter.counts
    if not dry_run:
        sessions, messages = purge_chat(now - datetime.timedelta(days=cfg.retention.chat_days))
        counts |= {"chat_sessions": sessions, "chat_messages": messages}
    _purge_rekey_cache(cfg, deleter)
    audit("admin_action", "system", action="purge", target="retention", counts=counts_text(counts))
    return counts
