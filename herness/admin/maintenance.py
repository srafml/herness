"""``maintenance`` job dispatch, nightly backup and retention purge (impl 10 U10-96 to U10-98,
U10-104; impl 08 §5.1, R-42).

``handle_maintenance`` reads ``ctx.job.payload["action"]`` and calls one unit from a static
table; nothing in the payload selects a callable (the purge unit lives in ``_maintenance_purge``).
Units owned by later cards are placeholders that raise ``FatalError`` naming the action and
the owner card (that card replaces the line).
"""

from __future__ import annotations

import contextlib
import datetime
import os
import re
import shutil
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Final
from urllib.parse import quote

from pydantic import JsonValue

from herness.admin import privacy
from herness.admin._maintenance_purge import BEAT_EVERY, counts_text, parse_day, run_purge
from herness.core import time as clock
from herness.core.audit import audit
from herness.core.config import HernessConfig, get_config
from herness.core.errors import ConfigError, FatalError
from herness.core.jobs.ports import JobContext
from herness.core.types import JobOutcome

__all__ = ["handle_maintenance", "run_backup", "run_purge", "select_backup_keep"]

type _Unit = Callable[[JobContext, Callable[[str], int]], JobOutcome | dict[str, JsonValue]]

_OPS_RE: Final = re.compile(r"^ops-(\d{4}-\d{2}-\d{2})\.sqlite$")


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
    """Online copy of the ops store into ``temp``: a read transaction held on the source pins
    one WAL snapshot, so writer commits neither tear the copy nor restart the backup."""
    uri = "file:" + quote(source.as_posix(), safe="/:") + "?mode=ro"
    src = sqlite3.connect(uri, uri=True, isolation_level=None)
    try:
        src.execute("BEGIN")
        src.execute("SELECT count(*) FROM sqlite_master").fetchall()
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


def _mirror_sources(cfg: HernessConfig) -> list[tuple[Path, str, tuple[str, ...]]]:
    """``(source root, target subdirectory, globs)`` for every mirrored tree (U10-96 step 2)."""
    data, every = Path(cfg.paths.data), ("**/*",)
    trees = [(data / "config_snapshots", "config_snapshots", every)]
    trees += [(data / "models", "models", every), (data / "reports", "reports", every)]
    if cfg.backup.include_cache:
        trees.append((data / "cache" / "decisions", "cache/decisions", every))
    if cfg.backup.include_lake:
        trees.append((data / "raw", "raw", every))
    return [*trees, (Path(cfg.paths.logs), "logs", ("audit-*.jsonl", "egress-*.jsonl"))]


def _listing(root: Path, globs: tuple[str, ...]) -> list[Path]:
    """Regular files (never symlinks) under ``root`` matching ``globs``, sorted per glob."""
    found = (path for glob in globs for path in sorted(root.glob(glob)))
    return [path for path in found if path.is_file() and not path.is_symlink()]


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
    for root, sub, globs in _mirror_sources(cfg):
        try:
            files = _listing(root, globs)
        except OSError as exc:
            msg = f"backup copy failed: {sub}"
            raise FatalError(msg) from exc
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
            if seen % BEAT_EVERY == 0:
                ctx.heartbeat()


def _prune_ops(cfg: HernessConfig, ops_dir: Path, today: datetime.date, *, dry_run: bool) -> int:
    copies: dict[datetime.date, Path] = {}
    for path in ops_dir.glob("ops-*.sqlite") if ops_dir.is_dir() else ():
        match = _OPS_RE.match(path.name)
        day = parse_day(match.group(1)) if match else None
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
    audit("admin_action", "system", action="backup", target=str(target), counts=counts_text(counts))
    return counts


# --- dispatch (U10-104) ---------------------------------------------------------------------


def _placeholder(action: str, owner: str) -> _Unit:
    """A table entry for a unit whose card is not built: fail the job, typed, never silently."""

    def unit(_ctx: JobContext, _memory_purge: Callable[[str], int]) -> JobOutcome:
        msg = f"maintenance action {action} is not available: owned by {owner}"
        raise FatalError(msg)

    return unit


def _rekey(ctx: JobContext) -> JobOutcome:
    """``run_rekey`` on the cached config at the current time; imported lazily so the table
    does not load DuckDB and pyarrow for the other actions."""
    from herness.admin.rekey import run_rekey  # noqa: PLC0415 - lazy, see docstring

    return run_rekey(get_config(), ctx, now=clock.now())


def _nightly(run: Callable[..., dict[str, int]]) -> _Unit:
    """Bind a nightly unit to the loaded config, the clock and ``payload.dry_run``."""

    def unit(ctx: JobContext, _memory_purge: Callable[[str], int]) -> dict[str, JsonValue]:
        dry_run = ctx.job.payload.get("dry_run") is True
        return dict(run(get_config(), ctx, now=clock.now(), dry_run=dry_run))

    return unit


def _privacy_delete(ctx: JobContext, memory_purge: Callable[[str], int]) -> JobOutcome:
    """U10-99 with the loaded config, the clock and the bound ``memory_purge`` (R-54)."""
    return privacy.run_privacy_delete(get_config(), ctx, now=clock.now(), memory_purge=memory_purge)


_ACTIONS: Final[Mapping[str, _Unit]] = {
    "backup": _nightly(run_backup),
    "purge": _nightly(run_purge),
    "privacy_delete": _privacy_delete,
    "rekey": lambda ctx, _purge: _rekey(ctx),  # T10-30: run_rekey (U10-101)
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
