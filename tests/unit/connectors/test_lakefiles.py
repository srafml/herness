"""Tests for herness.connectors.lakefiles: cleanup_orphan_temp_files, SchemaTracker,
SchemaDrift (U01-34, U01-35)."""

from __future__ import annotations

import datetime
import os
import subprocess
from pathlib import Path

import pyarrow as pa
import pytest
from structlog.testing import capture_logs

from herness.connectors.lakefiles import SchemaDrift, SchemaTracker, cleanup_orphan_temp_files

pytestmark = pytest.mark.unit

_NOW = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)
_ULID = "0" * 26


def _touch(path: Path, *, age: datetime.timedelta) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    mtime = (_NOW - age).timestamp()
    os.utime(path, (mtime, mtime))


# --- UT01-26: cleanup_orphan_temp_files --------------------------------------------


def test_ut01_26_missing_source_dir_returns_zero(tmp_path: Path) -> None:
    """UT01-26 a source with no raw directory yet is a no-op."""
    assert cleanup_orphan_temp_files(tmp_path, "servicenow", now=_NOW) == 0


def test_ut01_26_removes_only_old_matching_temp_files(tmp_path: Path) -> None:
    """UT01-26 only temp files older than max_age are removed; new ones, a non-matching
    dotfile and a too-short suffix are left untouched; a nested subdirectory is also
    walked."""
    raw_root = tmp_path / "raw"
    root = raw_root / "servicenow"
    root.mkdir(parents=True)

    old_temp = root / f".part.parquet.tmp-{_ULID}"
    _touch(old_temp, age=datetime.timedelta(hours=2))

    new_temp = root / f".other.parquet.tmp-{_ULID}"
    _touch(new_temp, age=datetime.timedelta(minutes=10))

    dotfile = root / ".not-a-temp-file"
    _touch(dotfile, age=datetime.timedelta(hours=5))

    short_suffix = root / ".part.parquet.tmp-short"
    _touch(short_suffix, age=datetime.timedelta(hours=5))

    nested_old = root / "sub" / f".nested.parquet.tmp-{_ULID}"
    _touch(nested_old, age=datetime.timedelta(hours=3))

    with capture_logs() as logs:
        removed = cleanup_orphan_temp_files(raw_root, "servicenow", now=_NOW)

    assert removed == 2
    assert not old_temp.exists()
    assert not nested_old.exists()
    assert new_temp.exists()
    assert dotfile.exists()
    assert short_suffix.exists()

    info_events = [entry for entry in logs if entry["event"] == "connectors.lake.orphans_removed"]
    assert len(info_events) == 1
    assert info_events[0]["count"] == 2
    assert info_events[0]["source"] == "servicenow"
    assert info_events[0]["log_level"] == "info"


def test_ut01_26_no_log_when_nothing_removed(tmp_path: Path) -> None:
    """UT01-26 the orphans_removed event is only logged when at least one file was removed."""
    raw_root = tmp_path / "raw"
    root = raw_root / "servicenow"
    new_temp = root / f".other.parquet.tmp-{_ULID}"
    _touch(new_temp, age=datetime.timedelta(minutes=10))

    with capture_logs() as logs:
        removed = cleanup_orphan_temp_files(raw_root, "servicenow", now=_NOW)

    assert removed == 0
    assert new_temp.exists()
    assert not any(entry["event"] == "connectors.lake.orphans_removed" for entry in logs)


def test_ut01_26_skips_symlinked_temp_files_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT01-26 a temp file that is a symlink is never deleted, even when old and matching.

    Simulated with a monkeypatch because creating a real symlink needs a privilege Windows
    denies non-admin accounts (see the real-symlink test below for the live-OS variant).
    """
    raw_root = tmp_path / "raw"
    root = raw_root / "servicenow"
    link_name = f".link.parquet.tmp-{_ULID}"
    linked = root / link_name
    _touch(linked, age=datetime.timedelta(hours=5))

    real_is_symlink = Path.is_symlink

    def fake_is_symlink(self: Path) -> bool:
        return self.name == link_name or real_is_symlink(self)

    monkeypatch.setattr(Path, "is_symlink", fake_is_symlink)

    removed = cleanup_orphan_temp_files(raw_root, "servicenow", now=_NOW)

    assert removed == 0
    assert linked.exists()


def test_ut01_26_skips_real_symlink_to_outside_target(tmp_path: Path) -> None:
    """UT01-26 a real symlink pointing outside the source folder is skipped and its target
    is left untouched; skipped on hosts where creating a symlink is not permitted."""
    raw_root = tmp_path / "raw"
    root = raw_root / "servicenow"
    root.mkdir(parents=True)

    outside_target = tmp_path / "outside.txt"
    _touch(outside_target, age=datetime.timedelta(hours=5))
    symlink = root / f".link.parquet.tmp-{_ULID}"
    try:
        symlink.symlink_to(outside_target)
    except OSError:
        pytest.skip("symlink creation is not permitted on this host")

    removed = cleanup_orphan_temp_files(raw_root, "servicenow", now=_NOW)

    assert removed == 0
    assert symlink.is_symlink()
    assert outside_target.exists()


def test_ut01_26_skips_files_reached_through_a_junction(tmp_path: Path) -> None:
    """UT01-26 a junction to an outside directory may still be walked (os.walk does not
    recognise junctions as symlinks), but the resolved-path containment check refuses to
    touch anything outside raw_root/source; skipped off Windows or without junction rights.
    """
    if os.name != "nt":
        pytest.skip("junctions are a Windows-only reparse point")
    raw_root = tmp_path / "raw"
    root = raw_root / "servicenow"
    root.mkdir(parents=True)

    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_file = outside_dir / f".escaped.parquet.tmp-{_ULID}"
    _touch(outside_file, age=datetime.timedelta(hours=5))

    junction = root / "junctioned"
    result = subprocess.run(  # noqa: S603 - fixed cmd.exe args built from tmp_path, test-only
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside_dir)],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("could not create a junction on this host")

    removed = cleanup_orphan_temp_files(raw_root, "servicenow", now=_NOW)

    assert removed == 0
    assert outside_file.exists()


def test_ut01_26_logs_and_continues_on_unlink_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT01-26 a PermissionError on unlink is logged (WARNING) and the file is left in
    place; other old files are still removed."""
    raw_root = tmp_path / "raw"
    root = raw_root / "servicenow"
    locked_name = f".locked.parquet.tmp-{_ULID}"
    locked = root / locked_name
    _touch(locked, age=datetime.timedelta(hours=5))
    other_old = root / f".other.parquet.tmp-{_ULID}"
    _touch(other_old, age=datetime.timedelta(hours=5))

    real_unlink = Path.unlink

    def fake_unlink(self: Path, missing_ok: bool = False) -> None:
        if self.name == locked_name:
            msg = "denied"
            raise PermissionError(msg)
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fake_unlink)

    with capture_logs() as logs:
        removed = cleanup_orphan_temp_files(raw_root, "servicenow", now=_NOW)

    assert removed == 1
    assert locked.exists()
    assert not other_old.exists()

    failures = [entry for entry in logs if entry["event"] == "connectors.lake.orphan_remove_failed"]
    assert len(failures) == 1
    assert failures[0]["source"] == "servicenow"
    assert failures[0]["file"] == locked_name
    assert failures[0]["error_class"] == "PermissionError"
    assert failures[0]["log_level"] == "warning"


# --- UT01-27: SchemaTracker, SchemaDrift ---------------------------------------------


def test_ut01_27_first_observe_returns_none() -> None:
    """UT01-27 the first observed schema becomes the baseline and returns no drift."""
    tracker = SchemaTracker()
    schema = pa.schema([("a", pa.string()), ("b", pa.int64())])
    assert tracker.observe(schema) is None


def test_ut01_27_identical_schema_ignoring_order_returns_none() -> None:
    """UT01-27 a later schema with the same name->type mapping, reordered, returns None."""
    tracker = SchemaTracker()
    tracker.observe(pa.schema([("a", pa.string()), ("b", pa.int64())]))
    reordered = pa.schema([("b", pa.int64()), ("a", pa.string())])
    assert tracker.observe(reordered) is None


def test_ut01_27_detects_added_removed_and_changed_columns() -> None:
    """UT01-27 an added, a removed and a retyped column are all reported, sorted."""
    tracker = SchemaTracker()
    tracker.observe(pa.schema([("a", pa.string()), ("b", pa.int64()), ("c", pa.string())]))

    drift = tracker.observe(pa.schema([("a", pa.string()), ("c", pa.int64()), ("d", pa.bool_())]))

    assert drift == SchemaDrift(added=("d",), removed=("b",), changed=("c",))


def test_ut01_27_baseline_updates_after_drift() -> None:
    """UT01-27 the schema that produced a drift becomes the new baseline."""
    tracker = SchemaTracker()
    tracker.observe(pa.schema([("a", pa.string())]))
    drifted = pa.schema([("a", pa.string()), ("b", pa.int64())])

    assert tracker.observe(drifted) == SchemaDrift(added=("b",), removed=(), changed=())
    assert tracker.observe(drifted) is None
