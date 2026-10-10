"""Integration tests for the nightly backup and retention purge (impl 10 T10-20).

IT10-08 (F10-11): backup while a writer thread commits to the ops store. IT10-09 (F10-12): a
data tree spanning three years is purged exactly at each retention cut-off. Real files, the
real migrated ops store, the real lake primitive (T02-03) and the real audit writer.
"""

from __future__ import annotations

import datetime
import json
import sqlite3
import sys
import threading
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

from herness.admin.maintenance import run_backup, run_purge
from herness.core import time as clock
from herness.core.config import HernessConfig
from herness.store.ops.chat import create_chat_session, get_chat_session

pytestmark = pytest.mark.integration

NOW = datetime.datetime(2026, 9, 24, 12, 0, tzinfo=datetime.UTC)
USER = "0" * 32
MARGIN = datetime.timedelta(minutes=1)
BUILD = "20260920-020000-ABCDEF"  # promoted CURRENT build started 2026-09-20T02:00:00Z


# --- IT10-08 --------------------------------------------------------------------------------


def _writer(db: Path, stop: threading.Event, written: list[int], errors: list[str]) -> None:
    conn = sqlite3.connect(db, timeout=30)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS it_writes (n INTEGER, pad TEXT)")
        conn.commit()
        n = 0
        while not stop.is_set():
            n += 1
            conn.execute("INSERT INTO it_writes VALUES (?, ?)", (n, "p" * 512))
            conn.commit()
            written.append(n)
    except sqlite3.Error as exc:  # pragma: no cover - reported by the assertion below
        errors.append(repr(exc))
    finally:
        conn.close()


def test_it10_08_backup_under_concurrent_writer(maint_env: MaintEnv) -> None:  # noqa: F811
    """IT10-08 backup while a writer thread commits: the copy passes integrity_check and
    holds a consistent prefix of the writer's rows; the writer never fails."""
    db = maint_env.data / "ops.sqlite"
    stop, written, errors = threading.Event(), [], []
    thread = threading.Thread(target=_writer, args=(db, stop, written, errors), daemon=True)
    thread.start()
    try:
        deadline = clock.now() + datetime.timedelta(seconds=30)
        while len(written) < 200 and clock.now() < deadline:
            stop.wait(0.01)
        counts = run_backup(maint_env.cfg, FakeCtx().as_ctx(), now=NOW)
        during = len(written)
    finally:
        stop.set()
        thread.join(timeout=30)
    assert errors == []
    assert during >= 200
    assert counts["ops_copied"] == 1
    copy = maint_env.target / "ops" / "ops-2026-09-24.sqlite"
    with sqlite3.connect(copy) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        rows = [r[0] for r in conn.execute("SELECT n FROM it_writes ORDER BY n")]
    assert rows == list(range(1, len(rows) + 1))  # a committed prefix, no torn transaction
    assert 0 < len(rows) <= len(written)
    assert parse_counts(admin_actions(maint_env.logs, "backup")[0]["counts"]) == counts


# --- IT10-09 --------------------------------------------------------------------------------


def _with_retention(cfg: HernessConfig, **values: int) -> HernessConfig:
    return cfg.model_copy(update={"retention": cfg.retention.model_copy(update=values)})


def _days(n: int) -> datetime.timedelta:
    return datetime.timedelta(days=n)


class _Tree:
    """Planted files keyed by count name, each marked expired or kept."""

    def __init__(self, env: MaintEnv) -> None:
        self.env = env
        self.expired: dict[str, list[Path]] = {}
        self.kept: list[Path] = []

    def add(self, key: str, path: Path, *, expired: bool) -> None:
        (self.expired.setdefault(key, []) if expired else self.kept).append(path)
        self.expired.setdefault(key, [])

    def lake(self, cutoff: datetime.date) -> None:
        raw = self.env.data / "raw" / "servicenow" / "incident"
        days = {cutoff, cutoff - _days(1), *(NOW.date() - _days(n) for n in range(40, 1150, 30))}
        for day in sorted(days):
            part = raw / f"dt={day.isoformat()}"
            plant(part / "part-0001.parquet")
            self.add("lake_partitions", part, expired=day < cutoff)

    def by_mtime(self, key: str, paths: list[Path], cut: datetime.datetime) -> None:
        stamps = [cut - MARGIN, cut + MARGIN, *(NOW - _days(n) for n in range(0, 1100, 45))]
        for path, stamp in zip(paths, stamps, strict=True):
            target = path / "manifest.json" if key == "reports" else path
            plant(target, mtime=stamp)
            self.add(key, path, expired=stamp < cut)

    def logs(self, prefix: str, key: str, days: int) -> None:
        today = NOW.date()
        names = {today, today - _days(days), today - _days(days + 1)}
        names |= {today - _days(n) for n in range(0, 1100, 30)}
        for day in sorted(names):
            path = plant(self.env.logs / f"{prefix}-{day.isoformat()}.jsonl", body=b"{}\n")
            self.add(key, path, expired=day < today - _days(days))


def _plant_three_years(env: MaintEnv) -> _Tree:
    cfg = env.cfg.retention
    tree = _Tree(env)
    tree.lake(datetime.date(2023, 9, 24))  # NOW minus 36 calendar months
    n_stamps = 2 + len(range(0, 1100, 45))
    traces = [env.data / "traces" / f"t{n:03d}.jsonl" for n in range(n_stamps)]
    tree.by_mtime("traces", traces, NOW - _days(cfg.traces_days))
    reports = [env.data / "reports" / f"run_{n:03d}" for n in range(n_stamps - 1)]
    reports.append(env.data / "reports" / "eval" / "ev_001")
    tree.by_mtime("reports", reports, NOW - _days(cfg.reports_days))
    tree.logs("egress", "egress_logs", cfg.egress_log_days)
    tree.logs("audit", "audit_logs", cfg.audit_log_days)
    tree.logs("herness", "app_logs", cfg.app_log_days)
    plant(env.logs / "notes-2020-01-01.jsonl")  # not a retention log name: never touched
    return tree


def test_it10_09_purge_three_years_exactly_at_cutoffs(maint_env: MaintEnv) -> None:  # noqa: F811
    """IT10-09 a data tree spanning three years: every key deletes exactly the entries older
    than its cut-off (boundary entries one step either side), nothing else."""
    tree = _plant_three_years(maint_env)
    chat_cut = NOW - _days(maint_env.cfg.retention.chat_days)
    stale = create_chat_session(USER, now=chat_cut - MARGIN)
    fresh = create_chat_session(USER, now=chat_cut + MARGIN)
    ctx = FakeCtx()
    counts = run_purge(maint_env.cfg, ctx.as_ctx(), now=NOW)
    expected = {key: len(paths) for key, paths in tree.expired.items()}
    expected |= {"chat_sessions": 1, "chat_messages": 0, "cache_files": 0}
    expected |= {"skipped_in_use": 0, "skipped_unsafe": 0, "rekey_marker_invalid": 0}
    assert counts == expected
    assert all(not p.exists() for paths in tree.expired.values() for p in paths)
    assert all(p.exists() for p in tree.kept)
    assert (maint_env.logs / "notes-2020-01-01.jsonl").exists()
    assert get_chat_session(stale) is None
    assert get_chat_session(fresh) is not None
    file_keys = ("traces", "reports", "egress_logs", "audit_logs", "app_logs", "cache_files")
    assert ctx.beats == sum(counts[key] for key in file_keys) // 100  # lake: T02-03 deletes
    audit_counts = parse_counts(admin_actions(maint_env.logs, "purge")[0]["counts"])
    assert audit_counts == counts


def test_it10_09_purge_lake_cutoff_clamps_month_end(maint_env: MaintEnv) -> None:  # noqa: F811
    """IT10-09 calendar-month arithmetic clamps the day: 2026-03-31 minus 1 month cuts at
    2026-02-28, so dt=2026-02-27 goes and dt=2026-02-28 stays."""
    raw = maint_env.data / "raw" / "servicenow" / "incident"
    gone = plant(raw / "dt=2026-02-27" / "part-0001.parquet").parent
    kept = plant(raw / "dt=2026-02-28" / "part-0001.parquet").parent
    cfg = _with_retention(maint_env.cfg, raw_lake_months=1)
    now = datetime.datetime(2026, 3, 31, 12, 0, tzinfo=datetime.UTC)
    dry = run_purge(cfg, FakeCtx().as_ctx(), now=now, dry_run=True)
    assert dry["lake_partitions"] == 1
    assert gone.exists()
    counts = run_purge(cfg, FakeCtx().as_ctx(), now=now)
    assert counts["lake_partitions"] == 1
    assert not gone.exists()
    assert kept.exists()


def _plant_rekey(env: MaintEnv, *, swapped_at: datetime.datetime, build: str | None) -> Path:
    marker = env.data / "cache" / "rekey" / "SWAPPED"
    body = {"swapped_at": clock.format_utc(swapped_at), "old_key_id": "k1", "new_key_id": "k2"}
    plant(marker, body=json.dumps(body).encode())
    if build is not None:
        plant(env.data / "warehouse" / f"wh-{build}.duckdb", body=b"")
        plant(env.data / "warehouse" / "CURRENT", body=build.encode())
    return marker


def test_it10_09_purge_rekey_cleanup_after_newer_build(maint_env: MaintEnv) -> None:  # noqa: F811
    """IT10-09 rekey cleanup: with CURRENT started after swapped_at, decision-cache files
    older than swapped_at are deleted, newer ones kept, then the marker."""
    swapped_at = datetime.datetime(2026, 9, 18, tzinfo=datetime.UTC)
    marker = _plant_rekey(maint_env, swapped_at=swapped_at, build=BUILD)
    cache = maint_env.data / "cache" / "decisions" / "q1"
    old = plant(cache / "old.parquet", mtime=swapped_at - MARGIN)
    new = plant(cache / "new.parquet", mtime=swapped_at + MARGIN)
    dry = run_purge(maint_env.cfg, FakeCtx().as_ctx(), now=NOW, dry_run=True)
    assert dry["cache_files"] == 1
    assert marker.exists()
    assert old.exists()
    counts = run_purge(maint_env.cfg, FakeCtx().as_ctx(), now=NOW)
    assert counts["cache_files"] == 1
    assert not old.exists()
    assert new.exists()
    assert not marker.exists()


@pytest.mark.parametrize("build", [None, "20260917-235959-ABCDEF"])
def test_it10_09_purge_rekey_waits_for_newer_build(
    maint_env: MaintEnv,  # noqa: F811
    build: str | None,
) -> None:
    """IT10-09 rekey cleanup waits: no CURRENT, or CURRENT started before swapped_at, keeps
    the cache and the marker."""
    swapped_at = datetime.datetime(2026, 9, 18, tzinfo=datetime.UTC)
    marker = _plant_rekey(maint_env, swapped_at=swapped_at, build=build)
    old = plant(maint_env.data / "cache" / "decisions" / "a.parquet", mtime=swapped_at - MARGIN)
    counts = run_purge(maint_env.cfg, FakeCtx().as_ctx(), now=NOW)
    assert counts["cache_files"] == 0
    assert old.exists()
    assert marker.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="an open handle blocks unlink on Windows only")
def test_it10_09_purge_rekey_keeps_marker_while_a_cache_file_is_in_use(
    maint_env: MaintEnv,  # noqa: F811
) -> None:
    """IT10-09 a pre-swap cache file in use is counted skipped_in_use and the SWAPPED marker
    stays, so the next night retries; once the file is free, both go."""
    swapped_at = datetime.datetime(2026, 9, 18, tzinfo=datetime.UTC)
    marker = _plant_rekey(maint_env, swapped_at=swapped_at, build=BUILD)
    cache = maint_env.data / "cache" / "decisions" / "q1"
    free = plant(cache / "free.parquet", mtime=swapped_at - MARGIN)
    busy = plant(cache / "busy.parquet", mtime=swapped_at - MARGIN)
    with busy.open("rb"):
        first = run_purge(maint_env.cfg, FakeCtx().as_ctx(), now=NOW)
    assert (first["cache_files"], first["skipped_in_use"]) == (1, 1)
    assert not free.exists()
    assert busy.exists()
    assert marker.exists()
    second = run_purge(maint_env.cfg, FakeCtx().as_ctx(), now=NOW)
    assert (second["cache_files"], second["skipped_in_use"]) == (1, 0)
    assert not busy.exists()
    assert not marker.exists()


@pytest.mark.parametrize(
    "body",
    [b"{not json", b'{"old_key_id": "k1"}', b'{"swapped_at": "yesterday"}', b'["swapped_at"]'],
)
def test_it10_09_purge_malformed_rekey_marker_is_counted_not_fatal(
    maint_env: MaintEnv,  # noqa: F811
    body: bytes,
) -> None:
    """IT10-09 a malformed SWAPPED marker skips only the rekey step (rekey_marker_invalid=1);
    retention still runs, the marker and the cache stay."""
    marker = plant(maint_env.data / "cache" / "rekey" / "SWAPPED", body=body)
    cached = plant(maint_env.data / "cache" / "decisions" / "a.parquet", mtime=NOW - _days(30))
    trace = plant(maint_env.data / "traces" / "old.jsonl", mtime=NOW - _days(400))
    counts = run_purge(maint_env.cfg, FakeCtx().as_ctx(), now=NOW)
    assert counts["rekey_marker_invalid"] == 1
    assert counts["traces"] == 1
    assert not trace.exists()
    assert marker.exists()
    assert cached.exists()
