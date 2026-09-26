"""Tests for herness.store.warehouse (U02-24 … U02-33, U02-133): the warehouse read side."""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any

import duckdb
import pytest
from structlog.testing import capture_logs

from herness.core.errors import ConfigError, SchemaViolation, StoreBusy
from herness.store import warehouse
from herness.store.errors import NotFoundError
from herness.store.layout import DataLayout
from herness.store.warehouse import (
    BUILD_ID_RE,
    CurrentPointer,
    build_exists,
    build_path,
    delete_build_files,
    list_builds,
    new_build_id,
    open_readonly,
    read_current,
    warehouse_health,
)

pytestmark = pytest.mark.unit

ID_A = "20260101-000000-AAAAAA"
ID_B = "20260102-000000-BBBBBB"
ID_C = "20260103-000000-CCCCCC"
NOW = datetime.datetime(2026, 1, 5, 12, 0, tzinfo=datetime.UTC)


@pytest.fixture
def layout(tmp_path: Path) -> DataLayout:
    lay = DataLayout.from_root(tmp_path / "data")
    lay.warehouse.mkdir(parents=True)
    return lay


def make_build(
    lay: DataLayout,
    build_id: str,
    *,
    status: str | None = "promoted",
    finished_at: datetime.datetime | None = NOW,
) -> Path:
    path = build_path(build_id, layout=lay)
    con = duckdb.connect(str(path))
    try:
        con.execute("SET TimeZone = 'UTC'")
        con.execute("CREATE SCHEMA meta")
        con.execute(
            "CREATE TABLE meta.build (build_id VARCHAR, started_at TIMESTAMPTZ,"
            " finished_at TIMESTAMPTZ, status VARCHAR)"
        )
        if status is not None:
            con.execute(
                "INSERT INTO meta.build VALUES (?, ?, ?, ?)",
                [build_id, NOW - datetime.timedelta(hours=1), finished_at, status],
            )
    finally:
        con.close()
    return path


def set_current(lay: DataLayout, content: str | bytes) -> None:
    data = content.encode("ascii") if isinstance(content, str) else content
    (lay.warehouse / "CURRENT").write_bytes(data)


def no_connect(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    calls: list[Any] = []

    def fake(*args: Any, **kwargs: Any) -> None:
        calls.append(args)
        msg = "must not open"
        raise AssertionError(msg)

    monkeypatch.setattr(duckdb, "connect", fake)
    return calls


# --- UT02-17: build IDs and paths -------------------------------------------------


def test_ut02_17_same_second_ids_differ_and_match() -> None:
    """UT02-17 two new_build_id in the same second both match and differ."""
    first, second = new_build_id(NOW), new_build_id(NOW)
    assert BUILD_ID_RE.fullmatch(first)
    assert BUILD_ID_RE.fullmatch(second)
    assert first != second
    assert first.startswith("20260105-120000-")


def test_ut02_17_lexical_order_follows_time() -> None:
    """UT02-17 lexical order of IDs equals creation-time order to the second."""
    later = new_build_id(NOW + datetime.timedelta(seconds=1))
    assert new_build_id(NOW) < later


def test_ut02_17_new_build_id_rejects_non_utc() -> None:
    """UT02-17 new_build_id requires a UTC time (ConfigError)."""
    with pytest.raises(ConfigError):
        new_build_id(datetime.datetime(2026, 1, 1))  # noqa: DTZ001 - naive on purpose
    plus_two = datetime.timezone(datetime.timedelta(hours=2))
    with pytest.raises(ConfigError):
        new_build_id(datetime.datetime(2026, 1, 1, tzinfo=plus_two))


@pytest.mark.parametrize(
    "bad",
    ["../x", "", "20260101-000000-aaaaaa", f"{ID_A}/../x", f"{ID_A}\n", "20260101-000000-AAAAAI"],
)
def test_ut02_17_build_path_rejects_invalid(layout: DataLayout, bad: str) -> None:
    """UT02-17 build_path rejects ../x and every other invalid ID."""
    with pytest.raises(SchemaViolation, match="invalid build_id"):
        build_path(bad, layout=layout)


def test_ut02_17_build_path_is_direct_child(layout: DataLayout) -> None:
    """UT02-17 build_path is wh-<id>.duckdb directly under the warehouse folder."""
    path = build_path(ID_A, layout=layout)
    assert path.parent == layout.warehouse
    assert path.name == f"wh-{ID_A}.duckdb"


def test_ut02_17_build_id_re_is_ascii_only() -> None:
    """UT02-17 non-ASCII digits never pass the build-ID gate."""
    assert BUILD_ID_RE.fullmatch("٢" * 8 + "-000000-AAAAAA") is None


# --- UT02-77: build_exists --------------------------------------------------------


def test_ut02_77_build_exists(layout: DataLayout, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT02-77 existing file True; valid ID without file False; ../x False, no path built."""
    make_build(layout, ID_A)
    assert build_exists(ID_A, layout=layout) is True
    assert build_exists(ID_B, layout=layout) is False
    built: list[str] = []
    monkeypatch.setattr(warehouse, "build_path", lambda b, **_: built.append(b))
    assert build_exists("../x", layout=layout) is False
    assert built == []


def test_ut02_77_build_exists_directory_and_oserror(
    layout: DataLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT02-77 a directory is not a build; an OSError from stat returns False."""
    build_path(ID_A, layout=layout).mkdir()
    assert build_exists(ID_A, layout=layout) is False

    def boom(self: Path) -> bool:
        msg = "stat failed"
        raise OSError(msg)

    monkeypatch.setattr(Path, "is_file", boom)
    assert build_exists(ID_B, layout=layout) is False


# --- UT02-18 / ST02-03: read_current ----------------------------------------------


def test_ut02_18_read_current(layout: DataLayout) -> None:
    """UT02-18 no CURRENT -> None; valid -> ID; file missing -> NotFoundError."""
    assert read_current(layout=layout) is None
    make_build(layout, ID_A)
    set_current(layout, f"{ID_A}\n")
    assert read_current(layout=layout) == ID_A
    set_current(layout, f"{ID_B}\n")
    with pytest.raises(NotFoundError) as info:
        read_current(layout=layout)
    assert (info.value.kind, info.value.key) == ("build", ID_B)


def test_ut02_18_read_current_sharing_violation(
    layout: DataLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT02-18 a sharing violation while CURRENT is replaced -> StoreBusy."""
    set_current(layout, f"{ID_A}\n")

    def locked(self: Path, *args: Any, **kwargs: Any) -> Any:
        msg = "sharing violation"
        raise PermissionError(msg)

    monkeypatch.setattr(Path, "open", locked)
    with pytest.raises(StoreBusy):
        read_current(layout=layout)


def test_ut02_18_read_current_other_oserror(
    layout: DataLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT02-18 another OSError reading CURRENT -> SchemaViolation."""
    set_current(layout, f"{ID_A}\n")

    def broken(self: Path, *args: Any, **kwargs: Any) -> Any:
        msg = "device error"
        raise OSError(msg)

    monkeypatch.setattr(Path, "open", broken)
    with pytest.raises(SchemaViolation):
        read_current(layout=layout)


@pytest.mark.parametrize(
    "content",
    [
        b"..\\..\\evil",
        f"{ID_A}\\..\\..\\evil".encode(),
        f"{ID_A}/x".encode(),
        b"x" * 10_240,
        ID_A.encode() + b" " * 60 + b"\n",
        "20260101-000000-ÄAAAAA".encode(),
        b"",
    ],
)
def test_st02_03_current_tampering_rejected(
    layout: DataLayout, monkeypatch: pytest.MonkeyPatch, content: bytes
) -> None:
    """ST02-03 traversal, a valid ID with trailing path and 10 KB junk -> SchemaViolation."""
    make_build(layout, ID_A)
    set_current(layout, content)
    calls = no_connect(monkeypatch)
    with pytest.raises(SchemaViolation, match="CURRENT content invalid"):
        read_current(layout=layout)
    with pytest.raises(SchemaViolation):
        open_readonly(layout=layout)
    assert calls == []


# --- UT02-19: CurrentPointer ------------------------------------------------------


def test_ut02_19_current_pointer_rechecks(layout: DataLayout) -> None:
    """UT02-19 old value at 30 s, new at 61 s; current_changed logged once."""
    make_build(layout, ID_A)
    make_build(layout, ID_B)
    set_current(layout, f"{ID_A}\n")
    now = [1000.0]
    pointer = CurrentPointer(layout=layout, clock=lambda: now[0])
    with capture_logs() as logs:
        assert pointer.get() == ID_A
        assert pointer.changed_at is None
        set_current(layout, f"{ID_B}\n")
        now[0] = 1030.0
        assert pointer.get() == ID_A
        now[0] = 1061.0
        assert pointer.get() == ID_B
        assert pointer.get() == ID_B
    changed = [e for e in logs if e["event"] == "store.warehouse.current_changed"]
    assert len(changed) == 1
    assert (changed[0]["old"], changed[0]["new"]) == (ID_A, ID_B)
    assert pointer.changed_at == 1061.0


def test_ut02_19_current_pointer_errors_keep_cache(layout: DataLayout) -> None:
    """UT02-19 read errors propagate and leave the cached value unchanged."""
    make_build(layout, ID_A)
    set_current(layout, f"{ID_A}\n")
    now = [0.0]
    pointer = CurrentPointer(layout=layout, recheck_s=1.0, clock=lambda: now[0])
    assert pointer.get() == ID_A
    set_current(layout, b"../evil")
    now[0] = 5.0
    with pytest.raises(SchemaViolation):
        pointer.get()
    set_current(layout, f"{ID_A}\n")
    now[0] = 5.5
    assert pointer.get() == ID_A
    assert pointer.changed_at is None


@pytest.mark.parametrize("recheck", [0.5, 3601.0])
def test_ut02_19_current_pointer_recheck_bounds(layout: DataLayout, recheck: float) -> None:
    """UT02-19 recheck_s outside 1-3600 -> ConfigError."""
    with pytest.raises(ConfigError):
        CurrentPointer(layout=layout, recheck_s=recheck)


# --- UT02-20: open_readonly -------------------------------------------------------


def test_ut02_20_open_readonly_settings(layout: DataLayout) -> None:
    """UT02-20 read-only connection: setting names and values on the pinned DuckDB."""
    make_build(layout, ID_A)
    set_current(layout, f"{ID_A}\n")
    with open_readonly(layout=layout, threads=2, memory_limit="1GB") as con:

        def setting(name: str) -> object:
            row = con.execute(f"SELECT current_setting('{name}')").fetchone()
            assert row is not None
            return row[0]

        assert setting("enable_external_access") is False
        assert setting("lock_configuration") is True
        assert setting("autoinstall_known_extensions") is False
        assert setting("autoload_known_extensions") is False
        assert setting("TimeZone") == "UTC"
        assert setting("threads") == 2
        assert setting("access_mode") == "read_only"
        assert con.execute("SELECT status FROM meta.build").fetchone() == ("promoted",)


def test_ut02_20_open_readonly_explicit_build(layout: DataLayout) -> None:
    """UT02-20 an explicit build ID opens that file without CURRENT."""
    make_build(layout, ID_B, status="building")
    con = open_readonly(ID_B, layout=layout)
    try:
        assert con.execute("SELECT build_id FROM meta.build").fetchone() == (ID_B,)
    finally:
        con.close()


def test_ut02_20_open_readonly_not_found(layout: DataLayout) -> None:
    """UT02-20 no CURRENT -> NotFoundError(current); missing file -> NotFoundError(build)."""
    with pytest.raises(NotFoundError) as info:
        open_readonly(layout=layout)
    assert (info.value.kind, info.value.key) == ("current", "CURRENT")
    with pytest.raises(NotFoundError) as info:
        open_readonly(ID_C, layout=layout)
    assert (info.value.kind, info.value.key) == ("build", ID_C)


@pytest.mark.parametrize(
    "kwargs", [{"threads": 0}, {"threads": 257}, {"memory_limit": "abc"}, {"memory_limit": "0%"}]
)
def test_ut02_20_open_readonly_bad_limits(layout: DataLayout, kwargs: dict[str, Any]) -> None:
    """UT02-20 threads outside 1-256 or a bad memory_limit -> ConfigError."""
    make_build(layout, ID_A)
    with pytest.raises(ConfigError):
        open_readonly(ID_A, layout=layout, **kwargs)


@pytest.mark.parametrize(
    ("message", "error"),
    [
        ('IO Error: Could not set lock on file "x": Conflicting lock is held', StoreBusy),
        ("IO Error: Cannot open file: being used by another process", StoreBusy),
        ("IO Error: not a valid DuckDB database file", SchemaViolation),
    ],
)
def test_ut02_20_open_readonly_error_mapping(
    layout: DataLayout, monkeypatch: pytest.MonkeyPatch, message: str, error: type[Exception]
) -> None:
    """UT02-20 lock IOException -> StoreBusy; other duckdb errors -> SchemaViolation."""
    make_build(layout, ID_A)

    def fail(*args: Any, **kwargs: Any) -> None:
        raise duckdb.IOException(message)

    monkeypatch.setattr(duckdb, "connect", fail)
    with pytest.raises(error):
        open_readonly(ID_A, layout=layout)


def test_ut02_20_open_readonly_corrupt(layout: DataLayout) -> None:
    """UT02-20 a file that is not a DuckDB database -> SchemaViolation."""
    build_path(ID_A, layout=layout).write_bytes(b"junk" * 100)
    with pytest.raises(SchemaViolation, match="cannot open warehouse"):
        open_readonly(ID_A, layout=layout)


# --- UT02-21: list_builds ---------------------------------------------------------


def test_ut02_21_list_builds(layout: DataLayout) -> None:
    """UT02-21 statuses building, promoted, unreadable; newest first; foreign files logged."""
    make_build(layout, ID_A, status="promoted")
    make_build(layout, ID_B, status="building", finished_at=None)
    build_path(ID_C, layout=layout).write_bytes(b"junk" * 100)
    (layout.warehouse / "wh-notanid.duckdb").write_bytes(b"x")
    (layout.warehouse / "notes.txt").write_bytes(b"x")
    set_current(layout, f"{ID_A}\n")
    with capture_logs() as logs:
        builds = list_builds(layout=layout)
    assert [b.build_id for b in builds] == [ID_C, ID_B, ID_A]
    assert [b.status for b in builds] == ["unreadable", "building", "promoted"]
    assert [b.is_current for b in builds] == [False, False, True]
    unreadable, building, promoted = builds
    assert unreadable.started_at is None
    assert unreadable.finished_at is None
    assert building.started_at == NOW - datetime.timedelta(hours=1)
    assert building.finished_at is None
    assert promoted.finished_at == NOW
    assert promoted.size_bytes == promoted.path.stat().st_size
    foreign = [e for e in logs if e["event"] == "store.warehouse.foreign_file"]
    assert [e["file"] for e in foreign] == ["wh-notanid.duckdb"]


def test_ut02_21_list_builds_edge_cases(layout: DataLayout) -> None:
    """UT02-21 missing meta.build row or unknown status -> unreadable; bad CURRENT ignored."""
    make_build(layout, ID_A, status=None)
    make_build(layout, ID_B, status="weird")
    wal = build_path(ID_B, layout=layout).with_name(f"wh-{ID_B}.duckdb.wal")
    set_current(layout, b"../evil")
    size = build_path(ID_B, layout=layout).stat().st_size
    wal.write_bytes(b"")  # an empty WAL is replayed as a no-op
    builds = list_builds(layout=layout)
    assert [(b.build_id, b.status) for b in builds] == [(ID_B, "unreadable"), (ID_A, "unreadable")]
    assert builds[0].size_bytes >= size
    assert not any(b.is_current for b in builds)


def test_ut02_21_list_builds_locked(layout: DataLayout, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT02-21 a lock error -> locked (subprocess variant in the integration suite)."""
    make_build(layout, ID_A)

    def fail(*args: Any, **kwargs: Any) -> None:
        msg = 'IO Error: Could not set lock on file "x"'
        raise duckdb.IOException(msg)

    monkeypatch.setattr(duckdb, "connect", fail)
    [info] = list_builds(layout=layout)
    assert info.status == "locked"
    assert info.started_at is None


def test_ut02_21_list_builds_no_folder(tmp_path: Path) -> None:
    """UT02-21 no warehouse folder -> empty list."""
    assert list_builds(layout=DataLayout.from_root(tmp_path / "none")) == []


# --- UT02-22: delete_build_files --------------------------------------------------


def _spill(lay: DataLayout, build_id: str) -> Path:
    spill = lay.warehouse / "tmp" / build_id
    spill.mkdir(parents=True)
    (spill / "part.tmp").write_bytes(b"x")
    return spill


def test_ut02_22_delete_deferred_on_permission_error(
    layout: DataLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT02-22 unlink raising PermissionError -> deferred, logged."""
    make_build(layout, ID_A)

    def locked(self: Path, missing_ok: bool = False) -> None:
        msg = "sharing violation"
        raise PermissionError(msg)

    monkeypatch.setattr(Path, "unlink", locked)
    with capture_logs() as logs:
        assert delete_build_files(ID_A, layout=layout) == "deferred"
    assert [e["event"] for e in logs] == ["store.warehouse.delete_deferred"]
    assert logs[0]["build_id"] == ID_A


def test_ut02_22_delete_current_refused(layout: DataLayout) -> None:
    """UT02-22 deleting the current build -> ConfigError, nothing removed."""
    path = make_build(layout, ID_A)
    set_current(layout, f"{ID_A}\n")
    with pytest.raises(ConfigError, match="refusing to delete CURRENT build"):
        delete_build_files(ID_A, layout=layout)
    assert path.exists()


def test_ut02_22_delete_current_with_missing_file_refused(layout: DataLayout) -> None:
    """UT02-22 CURRENT naming the ID is refused even when its file is missing."""
    set_current(layout, f"{ID_A}\n")
    with pytest.raises(ConfigError):
        delete_build_files(ID_A, layout=layout)


def test_ut02_22_delete_all_three_paths(layout: DataLayout) -> None:
    """UT02-22 file, WAL and spill directory removed -> deleted; then absent."""
    path = make_build(layout, ID_A)
    wal = path.with_name(path.name + ".wal")
    wal.write_bytes(b"w")
    spill = _spill(layout, ID_A)
    set_current(layout, b"junk")
    with capture_logs() as logs:
        assert delete_build_files(ID_A, layout=layout) == "deleted"
    assert not path.exists()
    assert not wal.exists()
    assert not spill.exists()
    [event] = logs
    assert event["event"] == "store.warehouse.deleted"
    assert event["bytes"] > 0
    assert delete_build_files(ID_A, layout=layout) == "absent"


def test_ut02_22_delete_spill_only(layout: DataLayout) -> None:
    """UT02-22 only the spill directory present -> deleted."""
    spill = _spill(layout, ID_B)
    assert delete_build_files(ID_B, layout=layout) == "deleted"
    assert not spill.exists()


def test_ut02_22_delete_invalid_id(layout: DataLayout) -> None:
    """UT02-22 an invalid ID -> SchemaViolation before any path."""
    with pytest.raises(SchemaViolation):
        delete_build_files("../x", layout=layout)


def test_ut02_22_delete_other_oserror(layout: DataLayout, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT02-22 another OSError -> SchemaViolation."""
    make_build(layout, ID_A)

    def broken(self: Path, missing_ok: bool = False) -> None:
        msg = "device error"
        raise OSError(msg)

    monkeypatch.setattr(Path, "unlink", broken)
    with pytest.raises(SchemaViolation):
        delete_build_files(ID_A, layout=layout)


# --- UT02-24: warehouse_health ----------------------------------------------------


def test_ut02_24_health_down_degraded_ok(layout: DataLayout) -> None:
    """UT02-24 no CURRENT -> down; stale -> degraded; fresh -> ok."""
    status, reason = warehouse_health(now=NOW, layout=layout)
    assert status == "down"
    assert "CURRENT" in reason
    make_build(layout, ID_A, finished_at=NOW - datetime.timedelta(hours=50))
    set_current(layout, f"{ID_A}\n")
    status, reason = warehouse_health(now=NOW, layout=layout)
    assert status == "degraded"
    assert ID_A in reason
    assert "50.0 h" in reason
    status, reason = warehouse_health(now=NOW, stale_after_h=72.0, layout=layout)
    assert status == "ok"
    assert ID_A in reason
    assert "50.0 h" in reason


def test_ut02_24_health_not_promoted(layout: DataLayout) -> None:
    """UT02-24 meta.build status other than promoted -> degraded."""
    make_build(layout, ID_A, status="retired")
    set_current(layout, f"{ID_A}\n")
    status, reason = warehouse_health(now=NOW, layout=layout)
    assert status == "degraded"
    assert "retired" in reason


@pytest.mark.parametrize("case", ["invalid", "missing", "corrupt", "no_row", "no_finish"])
def test_ut02_24_health_down_cases(layout: DataLayout, case: str) -> None:
    """UT02-24 invalid pointer, missing or unopenable build -> down; no finish -> degraded."""
    expected = "down"
    set_current(layout, b"../evil" if case == "invalid" else f"{ID_A}\n".encode())
    if case == "corrupt":
        build_path(ID_A, layout=layout).write_bytes(b"junk" * 100)
    elif case == "no_row":
        make_build(layout, ID_A, status=None)
    elif case == "no_finish":
        make_build(layout, ID_A, finished_at=None)
        expected = "degraded"
    status, reason = warehouse_health(now=NOW, layout=layout)
    assert status == expected
    assert reason


def test_ut02_24_health_bad_arguments(layout: DataLayout) -> None:
    """UT02-24 stale_after_h <= 0 or a naive now -> ConfigError (caller bug)."""
    with pytest.raises(ConfigError):
        warehouse_health(now=NOW, stale_after_h=0.0, layout=layout)
    with pytest.raises(ConfigError):
        warehouse_health(now=datetime.datetime(2026, 1, 1), layout=layout)  # noqa: DTZ001
