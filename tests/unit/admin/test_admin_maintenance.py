"""Tests for ``select_backup_keep``, ``run_backup`` and the ``purge`` maintenance action.

Impl 10 U10-96, U10-97, U10-98 via U10-104 (UT10-67, UT10-68). Real files under ``tmp_path``,
the real migrated ops store (``ops_store``) and the real audit writer.
"""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path

import pytest
from tests.support.maintenance_env import (  # noqa: F401 - fixture import
    FakeCtx,
    MaintEnv,
    admin_actions,
    maint_env,
    parse_counts,
    plant,
)

from herness.admin.maintenance import (
    handle_maintenance,
    run_backup,
    run_purge,
    select_backup_keep,
)
from herness.core import time as clock
from herness.core.errors import FatalError
from herness.store.ops.chat import create_chat_session

pytestmark = pytest.mark.unit

TODAY = datetime.date(2026, 9, 24)
NOW = datetime.datetime(2026, 9, 24, 1, 30, tzinfo=datetime.UTC)
USER = "0" * 32


def _purge(_record_id: str) -> int:
    return 0


# --- select_backup_keep ---------------------------------------------------------------------


def _expected_keep(dates: list[datetime.date], daily: int, weekly: int) -> set[datetime.date]:
    eligible = sorted((d for d in dates if d <= TODAY), reverse=True)
    keep = set(eligible[:daily])
    monday = TODAY - datetime.timedelta(days=TODAY.weekday())
    for n in range(weekly):
        start = monday - datetime.timedelta(weeks=n)
        week = [d for d in eligible if start <= d < start + datetime.timedelta(days=7)]
        keep.add(max(week))
    return keep


def test_ut10_67_select_backup_keep_daily_and_weekly() -> None:
    """UT10-67 60 daily dates: the 14 newest plus the newest of each of the last 8 ISO weeks."""
    dates = [TODAY - datetime.timedelta(days=n) for n in range(60)]
    kept = select_backup_keep(dates, keep_daily=14, keep_weekly=8, today=TODAY)
    assert kept == _expected_keep(dates, 14, 8)
    assert set(dates[:14]) <= kept
    weekly_only = kept - set(dates[:14])
    assert weekly_only, "older weeks contribute their newest date"
    assert all(d.weekday() == 6 for d in weekly_only)  # newest date of a past ISO week: Sunday
    # Thu 2026-09-24: the daily window 09-11..09-24 already holds the newest date of the three
    # newest ISO weeks (W39 09-24, W38 09-20, W37 09-13), so the weeks add 8 - 3 dates.
    assert len(kept) == 14 + 8 - 3


def test_ut10_67_select_backup_keep_ignores_future_and_handles_zero_weekly() -> None:
    """UT10-67 dates after today are never kept; keep_weekly=0 keeps only the daily set."""
    future = TODAY + datetime.timedelta(days=1)
    dates = [future, *(TODAY - datetime.timedelta(days=n) for n in range(10))]
    kept = select_backup_keep(dates, keep_daily=3, keep_weekly=0, today=TODAY)
    assert kept == {TODAY - datetime.timedelta(days=n) for n in range(3)}
    assert select_backup_keep([], keep_daily=14, keep_weekly=8, today=TODAY) == set()


# --- run_backup -----------------------------------------------------------------------------


def _plant_sources(env: MaintEnv) -> None:
    plant(env.data / "config_snapshots" / "LAST", body=b"snap")
    plant(env.data / "models" / "laya" / "adapter.bin", body=b"weights")
    plant(env.data / "reports" / "run_1" / "manifest.json", body=b"{}")
    plant(env.logs / "audit-2026-09-23.jsonl", body=b"{}\n")
    plant(env.logs / "egress-2026-09-23.jsonl", body=b"{}\n")
    plant(env.logs / "herness-2026-09-23.jsonl", body=b"{}\n")  # app logs are not backed up
    plant(env.data / "cache" / "decisions" / "q" / "c.parquet", body=b"cache")
    plant(env.data / "raw" / "sn" / "incident" / "dt=2026-09-23" / "part-1.parquet", body=b"l")


def test_ut10_67_run_backup_copies_ops_and_mirrors(maint_env: MaintEnv) -> None:  # noqa: F811
    """UT10-67 backup: ops copy passes integrity_check; mirrors copied; secrets/app logs not."""
    _plant_sources(maint_env)
    ctx = FakeCtx()
    counts = run_backup(maint_env.cfg, ctx.as_ctx(), now=NOW)
    target = maint_env.target
    copy = target / "ops" / "ops-2026-09-24.sqlite"
    with sqlite3.connect(copy) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    assert not list((target / "ops").glob(".*.tmp"))
    assert (target / "config_snapshots" / "LAST").read_bytes() == b"snap"
    assert (target / "models" / "laya" / "adapter.bin").read_bytes() == b"weights"
    assert (target / "reports" / "run_1" / "manifest.json").is_file()
    assert sorted(p.name for p in (target / "logs").iterdir()) == [
        "audit-2026-09-23.jsonl",
        "egress-2026-09-23.jsonl",
    ]
    assert not (target / "cache").exists()
    assert not (target / "raw").exists()
    assert counts == {"ops_copied": 1, "files_copied": 5, "files_unchanged": 0, "ops_deleted": 0}
    lines = admin_actions(maint_env.logs, "backup")
    assert len(lines) == 1
    assert lines[0]["target"] == str(target)
    assert parse_counts(lines[0]["counts"]) == counts


def test_ut10_67_run_backup_is_incremental_and_honours_includes(maint_env: MaintEnv) -> None:  # noqa: F811
    """UT10-67 a second run copies only changed files; include_cache/include_lake add trees."""
    _plant_sources(maint_env)
    run_backup(maint_env.cfg, FakeCtx().as_ctx(), now=NOW)
    plant(maint_env.data / "reports" / "run_1" / "manifest.json", body=b'{"v": 2}')
    backup = maint_env.cfg.backup.model_copy(update={"include_cache": True, "include_lake": True})
    cfg = maint_env.cfg.model_copy(update={"backup": backup})
    counts = run_backup(cfg, FakeCtx().as_ctx(), now=NOW)
    # changed manifest + cache file + lake file + the first run's own audit day file
    assert counts["files_copied"] == 4
    assert counts["files_unchanged"] == 4
    target = maint_env.target
    assert (target / "reports" / "run_1" / "manifest.json").read_bytes() == b'{"v": 2}'
    assert (target / "cache" / "decisions" / "q" / "c.parquet").is_file()
    assert (target / "raw" / "sn" / "incident" / "dt=2026-09-23" / "part-1.parquet").is_file()


def test_ut10_67_run_backup_prunes_unselected_ops_copies(maint_env: MaintEnv) -> None:  # noqa: F811
    """UT10-67 ops copies not selected by select_backup_keep are deleted; others kept."""
    ops = maint_env.target / "ops"
    dates = [TODAY - datetime.timedelta(days=n) for n in range(1, 60)]
    for day in dates:
        plant(ops / f"ops-{day.isoformat()}.sqlite")
    plant(ops / "notes.txt")
    counts = run_backup(maint_env.cfg, FakeCtx().as_ctx(), now=NOW)
    left = {datetime.date.fromisoformat(p.name[4:14]) for p in ops.glob("ops-*.sqlite")}
    assert left == _expected_keep([TODAY, *dates], 14, 8)
    assert counts["ops_deleted"] == 60 - len(left)
    assert (ops / "notes.txt").is_file()


def test_ut10_67_run_backup_dry_run_writes_nothing(maint_env: MaintEnv) -> None:  # noqa: F811
    """UT10-67 dry_run counts the copies and writes nothing at the target."""
    _plant_sources(maint_env)
    plant(maint_env.target / "ops" / "ops-2026-01-01.sqlite")
    backup = maint_env.cfg.backup.model_copy(update={"keep_daily": 1, "keep_weekly": 0})
    cfg = maint_env.cfg.model_copy(update={"backup": backup})
    counts = run_backup(cfg, FakeCtx().as_ctx(), now=NOW, dry_run=True)
    assert counts == {"ops_copied": 1, "files_copied": 5, "files_unchanged": 0, "ops_deleted": 1}
    assert sorted(p.name for p in maint_env.target.rglob("*")) == ["ops", "ops-2026-01-01.sqlite"]


def test_ut10_67_run_backup_target_missing(maint_env: MaintEnv, tmp_path: Path) -> None:  # noqa: F811
    """UT10-67 a missing backup target is FatalError("backup target missing")."""
    paths = maint_env.cfg.paths.model_copy(update={"backup_target": tmp_path / "absent"})
    cfg = maint_env.cfg.model_copy(update={"paths": paths})
    with pytest.raises(FatalError, match=r"^backup target missing$"):
        run_backup(cfg, FakeCtx().as_ctx(), now=NOW)


def test_ut10_67_run_backup_heartbeats_every_100_files(maint_env: MaintEnv) -> None:  # noqa: F811
    """UT10-67 ctx.heartbeat() runs every 100 mirrored files."""
    for n in range(250):
        plant(maint_env.data / "reports" / "bulk" / f"f{n:03d}.json")
    ctx = FakeCtx()
    run_backup(maint_env.cfg, ctx.as_ctx(), now=NOW)
    assert ctx.beats == 2


# --- handle_maintenance purge (UT10-68) ----------------------------------------------------


def _plant_purge_tree(env: MaintEnv, now: datetime.datetime) -> None:
    old, new = now - datetime.timedelta(days=800), now - datetime.timedelta(days=1)
    today = clock.utc_day(now)
    raw = env.data / "raw" / "sn" / "incident"
    plant(raw / f"dt={(now - datetime.timedelta(days=1200)).date()}" / "part-1.parquet")
    plant(raw / f"dt={(now - datetime.timedelta(days=40)).date()}" / "part-1.parquet")
    plant(env.data / "traces" / "old.jsonl", mtime=old)
    plant(env.data / "traces" / "new.jsonl", mtime=new)
    for prefix in ("egress", "audit", "herness"):
        plant(env.logs / f"{prefix}-{old.date().isoformat()}.jsonl")
        plant(env.logs / f"{prefix}-{new.date().isoformat()}.jsonl")
    plant(env.logs / f"herness-{today}.jsonl")
    plant(env.data / "reports" / "run_old" / "manifest.json", mtime=old)
    plant(env.data / "reports" / "run_new" / "manifest.json", mtime=new)
    plant(env.data / "reports" / "eval" / "ev_old" / "manifest.json", mtime=old)


def test_ut10_68_purge_dry_run_then_real(maint_env: MaintEnv) -> None:  # noqa: F811
    """UT10-68 purge dry_run then real: counts equal; old files deleted; today's logs kept;
    audit carries the counts."""
    now = clock.now()
    _plant_purge_tree(maint_env, now)
    create_chat_session(USER, now=now - datetime.timedelta(days=400))
    before = sorted(p.relative_to(maint_env.data) for p in maint_env.data.rglob("*"))
    dry_ctx = FakeCtx({"action": "purge", "dry_run": True})
    dry = handle_maintenance(dry_ctx.as_ctx(), memory_purge=_purge)
    after_dry = sorted(p.relative_to(maint_env.data) for p in maint_env.data.rglob("*"))
    assert [p for p in after_dry if p not in before and not p.name.startswith(".")] == [
        Path("logs") / f"audit-{clock.utc_day(clock.now())}.jsonl"  # the dry run's audit line
    ]
    real = handle_maintenance(FakeCtx({"action": "purge"}).as_ctx(), memory_purge=_purge)
    assert dry.status == real.status == "done"
    assert dry.result is not None
    assert real.result is not None
    expected = {"lake_partitions": 1, "traces": 1, "egress_logs": 1, "audit_logs": 1}
    expected |= {"app_logs": 1, "reports": 2, "cache_files": 0, "skipped_in_use": 0}
    expected |= {"skipped_unsafe": 0, "rekey_marker_invalid": 0}
    assert dry.result == expected
    assert real.result == expected | {"chat_sessions": 1, "chat_messages": 0}
    names = {p.name for p in maint_env.logs.iterdir()}
    old_day = (now - datetime.timedelta(days=800)).date().isoformat()
    assert not any(old_day in name for name in names)
    assert f"herness-{clock.utc_day(now)}.jsonl" in names
    assert not (maint_env.data / "traces" / "old.jsonl").exists()
    assert (maint_env.data / "traces" / "new.jsonl").exists()
    assert sorted(p.name for p in (maint_env.data / "reports").iterdir()) == ["eval", "run_new"]
    lines = admin_actions(maint_env.logs, "purge")
    assert [line["target"] for line in lines] == ["retention", "retention"]
    assert parse_counts(lines[1]["counts"]) == real.result


def test_ut10_68_purge_skips_logs_outside_data(
    maint_env: MaintEnv,  # noqa: F811
    tmp_path: Path,
) -> None:
    """UT10-68 containment: an expired log under a ``paths.logs`` outside ``paths.data`` is
    counted ``skipped_unsafe`` and kept."""
    now = clock.now()
    stale = plant(tmp_path / "outside_logs" / "herness-2020-01-01.jsonl")
    paths = maint_env.cfg.paths.model_copy(update={"logs": stale.parent})
    cfg = maint_env.cfg.model_copy(update={"paths": paths})
    counts = run_purge(cfg, FakeCtx().as_ctx(), now=now)
    assert counts["skipped_unsafe"] == 1
    assert counts["app_logs"] == 0
    assert stale.is_file()


def test_ut10_68_purge_skips_symlinks(
    maint_env: MaintEnv,  # noqa: F811
    tmp_path: Path,
) -> None:
    """UT10-68 containment: an old trace that is a symlink (to a file outside data) is kept."""
    now = clock.now()
    old = now - datetime.timedelta(days=800)
    outside = plant(tmp_path / "elsewhere" / "victim.jsonl", mtime=old)
    link = maint_env.data / "traces" / "link.jsonl"
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks need developer mode or admin rights on this host")
    counts = handle_maintenance(FakeCtx({"action": "purge"}).as_ctx(), memory_purge=_purge)
    assert counts.result is not None
    assert counts.result["skipped_unsafe"] == 1
    assert outside.is_file()
    assert link.is_symlink()
