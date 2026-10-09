"""``maintenance`` job dispatch, nightly backup and retention purge (impl 10 U10-96 to U10-98,
U10-104; impl 08 §5.1, R-42).

``handle_maintenance`` reads ``ctx.job.payload["action"]`` and calls one unit from a static
table; nothing in the payload selects a callable. Units owned by later cards are placeholders
that raise ``FatalError`` naming the action and the owner card (that card replaces the line).
"""

from __future__ import annotations

import calendar
import contextlib
import datetime
import json
import os
import re
import shutil
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Final
from urllib.parse import quote

from pydantic import JsonValue

from herness.core import time as clock
from herness.core.audit import audit
from herness.core.config import HernessConfig, get_config
from herness.core.errors import ConfigError, FatalError
from herness.core.jobs.ports import JobContext
from herness.core.types import JobOutcome
from herness.store.lake_purge import purge_partitions_before
from herness.store.layout import DataLayout
from herness.store.ops.chat import purge_chat
from herness.store.warehouse import read_current

__all__ = ["handle_maintenance", "run_backup", "run_purge", "select_backup_keep"]

type _Unit = Callable[[JobContext, Callable[[str], int]], JobOutcome | dict[str, JsonValue]]

_BEAT_EVERY: Final = 100
_OPS_RE: Final = re.compile(r"^ops-(\d{4}-\d{2}-\d{2})\.sqlite$")
_LOG_RE: Final = re.compile(r"^(egress|audit|herness)-(\d{4}-\d{2}-\d{2})\.jsonl$")
_DT_RE: Final = re.compile(r"^dt=(\d{4}-\d{2}-\d{2})$")
_LOG_KEYS: Final = {"egress": "egress", "audit": "audit", "herness": "app"}  # prefix -> key


def _counts_text(counts: Mapping[str, int]) -> str:
    return ";".join(f"{key}={value}" for key, value in sorted(counts.items()))


def _parse_day(text: str) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        return None


def select_backup_keep(
    dates: Sequence[datetime.date], *, keep_daily: int, keep_weekly: int, today: datetime.date
) -> set[datetime.date]:
    """The ``keep_daily`` newest dates up to ``today`` plus the newest of each of the
    ``keep_weekly`` newest ISO weeks (U10-97)."""
    eligible = sorted({day for day in dates if day <= today}, reverse=True)
    newest_of_week: dict[tuple[int, int], datetime.date] = {}
    for day in eligible:
        newest_of_week.setdefault(day.isocalendar()[:2], day)
    weeks = sorted(newest_of_week, reverse=True)[:keep_weekly]
    return set(eligible[:keep_daily]) | {newest_of_week[week] for week in weeks}


# --- backup (U10-96) ------------------------------------------------------------------------


def _backup_ops(source: Path, temp: Path) -> None:
    """Online copy of the ops store (consistent under WAL) into ``temp``."""
    uri = "file:" + quote(source.as_posix(), safe="/:") + "?mode=ro"
    src = sqlite3.connect(uri, uri=True)
    try:
        dst = sqlite3.connect(temp)
        try:
            src.backup(dst, pages=4096, sleep=0.05)
        finally:
            dst.close()
    finally:
        src.close()


def _integrity_ok(path: Path) -> bool:
    try:
        conn = sqlite3.connect(path)
        try:
            return conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return False


def _copy_ops(cfg: HernessConfig, ops_dir: Path, day: str) -> None:
    final = ops_dir / f"ops-{day}.sqlite"
    temp = ops_dir / f".ops-{day}.sqlite.tmp"
    try:
        ops_dir.mkdir(exist_ok=True)
        temp.unlink(missing_ok=True)
        _backup_ops(Path(cfg.paths.data) / "ops.sqlite", temp)
        ok = _integrity_ok(temp)
        if ok:
            os.replace(temp, final)
    except (OSError, sqlite3.Error) as exc:
        with contextlib.suppress(OSError):
            temp.unlink(missing_ok=True)
        msg = f"backup copy failed: ops/{final.name}"
        raise FatalError(msg) from exc
    if not ok:
        with contextlib.suppress(OSError):
            temp.unlink(missing_ok=True)
        msg = "backup integrity_check failed"
        raise FatalError(msg)


def _mirror_sources(cfg: HernessConfig) -> list[tuple[Path, str, Iterable[Path]]]:
    """``(source root, target subdirectory, files)`` for every mirrored tree (U10-96 step 2)."""
    data, logs = Path(cfg.paths.data), Path(cfg.paths.logs)

    def tree(path: Path) -> Iterable[Path]:
        return (p for p in sorted(path.rglob("*")) if p.is_file() and not p.is_symlink())

    trees = [(data / "config_snapshots", "config_snapshots"), (data / "models", "models")]
    trees.append((data / "reports", "reports"))
    if cfg.backup.include_cache:
        trees.append((data / "cache" / "decisions", "cache/decisions"))
    if cfg.backup.include_lake:
        trees.append((data / "raw", "raw"))
    found = [(root, sub, tree(root)) for root, sub in trees]
    log_files = [p for glob in ("audit-*.jsonl", "egress-*.jsonl") for p in sorted(logs.glob(glob))]
    found.append((logs, "logs", (p for p in log_files if p.is_file() and not p.is_symlink())))
    return found


def _needs_copy(src: Path, dst: Path) -> bool:
    try:
        have = dst.stat()
    except FileNotFoundError:
        return True
    want = src.stat()
    return have.st_size != want.st_size or int(have.st_mtime) != int(want.st_mtime)


def _copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    temp = dst.with_name(f".{dst.name}.tmp")
    try:
        shutil.copy2(src, temp)
        os.replace(temp, dst)
    except OSError:
        with contextlib.suppress(OSError):
            temp.unlink(missing_ok=True)
        raise


def _mirror(
    cfg: HernessConfig, ctx: JobContext, target: Path, counts: dict[str, int], *, dry_run: bool
) -> None:
    seen = 0
    for root, sub, files in _mirror_sources(cfg):
        for src in files:
            rel = Path(sub) / src.relative_to(root)
            try:
                if not _needs_copy(src, target / rel):
                    counts["files_unchanged"] += 1
                else:
                    if not dry_run:
                        _copy_file(src, target / rel)
                    counts["files_copied"] += 1
            except OSError as exc:
                msg = f"backup copy failed: {rel.as_posix()}"
                raise FatalError(msg) from exc
            seen += 1
            if seen % _BEAT_EVERY == 0:
                ctx.heartbeat()


def _prune_ops(cfg: HernessConfig, ops_dir: Path, today: datetime.date, *, dry_run: bool) -> int:
    copies: dict[datetime.date, Path] = {}
    for path in ops_dir.glob("ops-*.sqlite") if ops_dir.is_dir() else ():
        match = _OPS_RE.match(path.name)
        day = _parse_day(match.group(1)) if match else None
        if day is not None:
            copies[day] = path
    dates = [*copies, today] if dry_run else list(copies)
    keep = select_backup_keep(
        dates, keep_daily=cfg.backup.keep_daily, keep_weekly=cfg.backup.keep_weekly, today=today
    )
    dropped = [path for day, path in copies.items() if day not in keep]
    for path in dropped if not dry_run else ():
        path.unlink(missing_ok=True)
    return len(dropped)


def run_backup(
    cfg: HernessConfig, ctx: JobContext, *, now: datetime.datetime, dry_run: bool = False
) -> dict[str, int]:
    """Nightly backup: ops copy checked by ``integrity_check``, then mirrors (U10-96)."""
    target = Path(cfg.paths.backup_target)
    if not target.is_dir():
        msg = "backup target missing"
        raise FatalError(msg)
    day = clock.utc_day(now)
    counts = {"ops_copied": 1, "files_copied": 0, "files_unchanged": 0, "ops_deleted": 0}
    if not dry_run:
        _copy_ops(cfg, target / "ops", day)
    _mirror(cfg, ctx, target, counts, dry_run=dry_run)
    try:
        counts["ops_deleted"] = _prune_ops(
            cfg, target / "ops", datetime.date.fromisoformat(day), dry_run=dry_run
        )
    except OSError as exc:
        msg = "backup copy failed: ops"
        raise FatalError(msg) from exc
    audit(
        "admin_action", "system", action="backup", target=str(target), counts=_counts_text(counts)
    )
    return counts


# --- purge (U10-98) -------------------------------------------------------------------------


class _Deleter:
    """Contained deletions with counts, ``dry_run`` and a heartbeat every 100 (U10-98 7, 9)."""

    def __init__(self, data: Path, ctx: JobContext, *, dry_run: bool) -> None:
        self.data, self.ctx, self.dry_run, self.done = data.resolve(), ctx, dry_run, 0
        keys = ("lake_partitions", "traces", "egress_logs", "audit_logs", "app_logs", "reports")
        self.counts = dict.fromkeys((*keys, "cache_files", "skipped_in_use", "skipped_unsafe"), 0)

    def delete(self, path: Path, key: str) -> None:
        if path.is_symlink() or not path.resolve().is_relative_to(self.data):
            self.counts["skipped_unsafe"] += 1
            return
        try:
            if not self.dry_run:
                shutil.rmtree(path) if path.is_dir() else path.unlink()
        except PermissionError:
            self.counts["skipped_in_use"] += 1
            return
        self.counts[key] += 1
        self.done += 1
        if self.done % _BEAT_EVERY == 0:
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
        day = _parse_day(match.group(1)) if match else None
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
        day = _parse_day(match.group(2)) if match else None
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


def _purge_rekey_cache(cfg: HernessConfig, deleter: _Deleter) -> None:
    """Design 10 §5.3 step 4: drop pre-swap decision-cache files once a newer build is live."""
    data = Path(cfg.paths.data)
    marker = data / "cache" / "rekey" / "SWAPPED"
    if not marker.is_file():
        return
    swapped_at = clock.parse_utc(json.loads(marker.read_text(encoding="utf-8"))["swapped_at"])
    current = read_current(layout=DataLayout.from_root(data))
    if current is None:
        return
    started = datetime.datetime.strptime(current[:15], "%Y%m%d-%H%M%S").replace(tzinfo=datetime.UTC)
    if started <= swapped_at:
        return
    cache = data / "cache" / "decisions"
    for file in sorted(p for p in cache.rglob("*") if p.is_file() or p.is_symlink()):
        if _mtime(file) < swapped_at:
            deleter.delete(file, "cache_files")
    if not deleter.dry_run:
        marker.unlink()


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
    audit("admin_action", "system", action="purge", target="retention", counts=_counts_text(counts))
    return counts


# --- dispatch (U10-104) ---------------------------------------------------------------------


def _placeholder(action: str, owner: str) -> _Unit:
    """A table entry for a unit whose card is not built: fail the job, typed, never silently."""

    def unit(_ctx: JobContext, _memory_purge: Callable[[str], int]) -> JobOutcome:
        msg = f"maintenance action {action} is not available: owned by {owner}"
        raise FatalError(msg)

    return unit


def _nightly(run: Callable[..., dict[str, int]]) -> _Unit:
    """Bind a nightly unit to the loaded config, the clock and ``payload.dry_run``."""

    def unit(ctx: JobContext, _memory_purge: Callable[[str], int]) -> dict[str, JsonValue]:
        dry_run = ctx.job.payload.get("dry_run") is True
        return dict(run(get_config(), ctx, now=clock.now(), dry_run=dry_run))

    return unit


_ACTIONS: Final[Mapping[str, _Unit]] = {
    "backup": _nightly(run_backup),
    "purge": _nightly(run_purge),
    "privacy_delete": _placeholder("privacy_delete", "T10-29"),  # T10-29: run_privacy_delete
    "rekey": _placeholder("rekey", "T10-30"),  # T10-30: run_rekey (U10-101)
}


def handle_maintenance(ctx: JobContext, *, memory_purge: Callable[[str], int]) -> JobOutcome:
    """Dispatch one ``maintenance`` job by ``payload.action`` (U10-104).

    An action outside the table raises ``ConfigError("unknown maintenance action")``. A unit
    returns its own ``JobOutcome`` or the counts for ``JobOutcome(status="done", ...)``.
    """
    action = ctx.job.payload.get("action")
    unit = _ACTIONS.get(action) if isinstance(action, str) else None
    if unit is None:
        msg = "unknown maintenance action"
        raise ConfigError(msg)
    outcome = unit(ctx, memory_purge)
    if isinstance(outcome, JobOutcome):
        return outcome
    return JobOutcome(status="done", result=outcome)
