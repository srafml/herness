"""Warehouse read side (impl 02 U02-24 … U02-33, U02-133): build IDs, ``CURRENT``, readers.

Every path comes from an ID that passed ``BUILD_ID_RE`` (TH02-03); every reader connection
is read-only, external access off, configuration locked (TH02-04). Writes: ``_warehouse_rw``.
"""

from __future__ import annotations

import dataclasses
import datetime
import re
import shutil
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Final, Literal, cast

import duckdb

from herness.core import ids
from herness.core import time as clock
from herness.core.errors import ConfigError, HernessError, SchemaViolation, StoreBusy
from herness.core.logging import get_logger
from herness.store.errors import NotFoundError
from herness.store.layout import DataLayout, data_layout

type BuildStatus = Literal["building", "failed", "promoted", "retired", "unreadable", "locked"]
type HealthResult = tuple[Literal["ok", "degraded", "down"], str]
type _Config = dict[str, str | bool | int | float | list[str]]

# re.ASCII keeps non-ASCII digits out of \d, so only plain ASCII IDs ever reach a path.
BUILD_ID_RE: Final = re.compile(r"^\d{8}-\d{6}-[0-9A-HJKMNP-TV-Z]{6}$", re.ASCII)
"""``YYYYMMDD-HHMMSS-<ulid6>`` (Crockford base32, upper case); gates every build path."""

_MEMORY_LIMIT_RE: Final = re.compile(r"^([1-9][0-9]?%|100%|[0-9]+(\.[0-9]+)?\s?(GB|MB|GiB|MiB))$")
_MAX_THREADS: Final = 256
_CURRENT: Final = "CURRENT"
_CURRENT_MAX_BYTES: Final = 64
# POSIX DuckDB names the file lock; on Windows the OS reports a sharing violation instead.
_LOCK_RE: Final = re.compile(r"\block\b|being used by another process|already open", re.I)
# Setting names verified on the pinned DuckDB by UT02-20 (open-questions (b) item 4).
_HARDENING: Final = (
    "SET TimeZone = 'UTC'",
    "SET enable_external_access = false",
    "SET lock_configuration = true",
)
_META_SQL: Final = "SELECT status, epoch_us(started_at), epoch_us(finished_at) FROM meta.build"
_META_STATUSES: Final = frozenset({"building", "failed", "promoted", "retired"})
_MONOTONIC: Final = clock.monotonic
_EPOCH: Final = datetime.datetime(1970, 1, 1, tzinfo=datetime.UTC)

_log = get_logger("store.warehouse")


def _extensions_off() -> _Config:
    return {"autoinstall_known_extensions": False, "autoload_known_extensions": False}


def _resolve(layout: DataLayout | None) -> DataLayout:
    return layout if layout is not None else data_layout()


def _valid_id(build_id: object) -> bool:
    return isinstance(build_id, str) and BUILD_ID_RE.fullmatch(build_id) is not None


def new_build_id(now: datetime.datetime) -> str:
    """Return a new build ID for the UTC time ``now``. Raises ConfigError for a non-UTC time."""
    if now.tzinfo is None or now.utcoffset() != datetime.timedelta(0):
        msg = "new_build_id needs a UTC time"
        raise ConfigError(msg)
    return ids.new_build_id(now)


def build_path(build_id: str, *, layout: DataLayout | None = None) -> Path:
    """Return ``<warehouse>/wh-<build_id>.duckdb``. Raises SchemaViolation for an invalid ID."""
    if not _valid_id(build_id):
        msg = "invalid build_id"
        raise SchemaViolation(msg)
    return _resolve(layout).warehouse / f"wh-{build_id}.duckdb"


def build_exists(build_id: str, *, layout: DataLayout | None = None) -> bool:
    """True exactly when ``build_id`` is valid and its build file is a regular file."""
    if not _valid_id(build_id):
        return False  # no path is built from an invalid ID (TH02-03)
    try:
        return build_path(build_id, layout=layout).is_file()
    except OSError:
        return False


def _parse_current(raw: bytes) -> str:
    try:
        text = raw.decode("ascii").strip()
    except UnicodeDecodeError:
        text = ""
    if len(raw) > _CURRENT_MAX_BYTES or not _valid_id(text):
        msg = "CURRENT content invalid"
        raise SchemaViolation(msg)
    return text


def read_current(*, layout: DataLayout | None = None) -> str | None:
    """Return the promoted build ID, or None when ``CURRENT`` does not exist.

    Raises SchemaViolation (bad content), NotFoundError (build file missing), StoreBusy.
    """
    lay = _resolve(layout)
    try:
        with (lay.warehouse / _CURRENT).open("rb") as fh:
            raw = fh.read(_CURRENT_MAX_BYTES + 1)
    except FileNotFoundError:
        return None
    except PermissionError as exc:  # Windows sharing violation while the file is replaced
        msg = "CURRENT is being replaced"
        raise StoreBusy(msg) from exc
    except OSError as exc:
        msg = "cannot read CURRENT"
        raise SchemaViolation(msg, error_type=type(exc).__name__) from exc
    build_id = _parse_current(raw)
    if not build_path(build_id, layout=lay).is_file():
        msg = f"warehouse build {build_id} not found"
        raise NotFoundError(msg, kind="build", key=build_id)
    return build_id


class CurrentPointer:
    """Cached ``read_current`` that re-reads at most once per ``recheck_s`` seconds."""

    def __init__(
        self,
        *,
        layout: DataLayout | None = None,
        recheck_s: float = 60.0,
        clock: Callable[[], float] = _MONOTONIC,
    ) -> None:
        if not 1.0 <= recheck_s <= 3600.0:  # noqa: PLR2004 - range of U02-28
            msg = "recheck_s must be between 1 and 3600 seconds"
            raise ConfigError(msg)
        self._layout, self._recheck_s, self._clock = layout, recheck_s, clock
        self._lock = threading.Lock()
        self._value: str | None = None
        self._read_at: float | None = None
        self._changed_at: float | None = None

    @property
    def changed_at(self) -> float | None:
        """Clock value of the last observed change, None before any change."""
        return self._changed_at

    def get(self) -> str | None:
        """Return the cached pointer, re-reading it once the interval has passed."""
        with self._lock:
            now = self._clock()
            if self._read_at is None or now - self._read_at >= self._recheck_s:
                value = read_current(layout=self._layout)  # errors leave the cache unchanged
                if self._read_at is not None and value != self._value:
                    self._changed_at = now
                    _log.info("store.warehouse.current_changed", old=self._value, new=value)
                self._value, self._read_at = value, now
            return self._value


def _is_lock_error(exc: duckdb.Error) -> bool:
    return isinstance(exc, duckdb.IOException) and _LOCK_RE.search(str(exc)) is not None


def _open_error(exc: duckdb.Error, build_id: str) -> HernessError:
    error_type = type(exc).__name__
    if _is_lock_error(exc):
        msg = f"warehouse {build_id} is locked"
        return StoreBusy(msg, build_id=build_id, error_type=error_type)
    msg = f"cannot open warehouse {build_id}"
    return SchemaViolation(msg, build_id=build_id, error_type=error_type)


def _reader_config(threads: int | None, memory_limit: str | None) -> _Config:
    config = _extensions_off()
    if threads is not None:
        if not 1 <= threads <= _MAX_THREADS:
            msg = f"threads must be between 1 and {_MAX_THREADS}"
            raise ConfigError(msg)
        config["threads"] = threads
    if memory_limit is not None:
        if _MEMORY_LIMIT_RE.fullmatch(memory_limit) is None:
            msg = "invalid memory_limit"
            raise ConfigError(msg)
        config["memory_limit"] = memory_limit
    return config


def open_readonly(
    build_id: str | None = None,
    *,
    threads: int | None = None,
    memory_limit: str | None = None,
    layout: DataLayout | None = None,
) -> duckdb.DuckDBPyConnection:
    """Open the hardened read-only connection every reader uses (TH02-04).

    Raises NotFoundError, ConfigError (limits), StoreBusy (lock) and SchemaViolation.
    """
    config = _reader_config(threads, memory_limit)
    lay = _resolve(layout)
    if build_id is None:
        build_id = read_current(layout=lay)
        if build_id is None:
            msg = "no promoted warehouse build"
            raise NotFoundError(msg, kind="current", key=_CURRENT)
    path = build_path(build_id, layout=lay)
    if not path.is_file():
        msg = f"warehouse build {build_id} not found"
        raise NotFoundError(msg, kind="build", key=build_id)
    try:
        con = duckdb.connect(str(path), read_only=True, config=config)
    except duckdb.Error as exc:
        raise _open_error(exc, build_id) from exc
    try:
        for statement in _HARDENING:
            con.execute(statement)
    except duckdb.Error as exc:
        con.close()
        raise _open_error(exc, build_id) from exc
    return con


@dataclasses.dataclass(frozen=True, slots=True)
class BuildInfo:
    """One warehouse file as seen by ``list_builds``; ``size_bytes`` includes the WAL."""

    build_id: str
    path: Path
    size_bytes: int
    status: BuildStatus
    started_at: datetime.datetime | None
    finished_at: datetime.datetime | None
    is_current: bool


def _from_epoch_us(value: object) -> datetime.datetime | None:
    return _EPOCH + datetime.timedelta(microseconds=value) if isinstance(value, int) else None


def _meta_row(con: duckdb.DuckDBPyConnection) -> tuple[str, object, object] | None:
    """The single ``meta.build`` row as (status, started_us, finished_us), else None."""
    rows = con.execute(_META_SQL).fetchmany(2)
    if len(rows) != 1:
        return None
    status, started, finished = rows[0]
    return str(status), started, finished


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _inspect(path: Path, build_id: str, *, is_current: bool) -> BuildInfo:
    size = _file_size(path) + _file_size(path.with_name(path.name + ".wal"))
    status: BuildStatus = "unreadable"
    started = finished = None
    try:
        con = duckdb.connect(str(path), read_only=True, config=_extensions_off())
    except duckdb.Error as exc:
        status = "locked" if _is_lock_error(exc) else "unreadable"
    else:
        try:
            row = _meta_row(con)
        except duckdb.Error:
            row = None
        finally:
            con.close()
        if row is not None and row[0] in _META_STATUSES:
            status = cast("BuildStatus", row[0])
            started, finished = _from_epoch_us(row[1]), _from_epoch_us(row[2])
    return BuildInfo(build_id, path, size, status, started, finished, is_current)


def list_builds(*, layout: DataLayout | None = None) -> list[BuildInfo]:
    """Return one ``BuildInfo`` per ``wh-<build_id>.duckdb`` file, newest build ID first."""
    lay = _resolve(layout)
    try:
        current = read_current(layout=lay)
    except (SchemaViolation, NotFoundError):
        current = None
    if not lay.warehouse.is_dir():
        return []
    infos: list[BuildInfo] = []
    for path in lay.warehouse.glob("wh-*.duckdb"):
        build_id = path.name[len("wh-") : -len(".duckdb")]
        if not _valid_id(build_id):
            _log.warning("store.warehouse.foreign_file", file=path.name)
        elif path.is_file():
            infos.append(_inspect(path, build_id, is_current=build_id == current))
    return sorted(infos, key=lambda info: info.build_id, reverse=True)


def _is_current(build_id: str, layout: DataLayout) -> bool:
    try:
        return read_current(layout=layout) == build_id
    except NotFoundError as exc:  # CURRENT names this ID although its file is gone
        return exc.key == build_id
    except SchemaViolation:
        return False


def _remove(build_id: str, lay: DataLayout) -> tuple[bool, int]:
    """Remove WAL, file and spill directory; return (anything existed, bytes freed)."""
    path = build_path(build_id, layout=lay)
    existed, freed = False, 0
    for target in (path.with_name(path.name + ".wal"), path):
        if target.exists():
            existed, freed = True, freed + _file_size(target)
            target.unlink(missing_ok=True)
    spill = lay.warehouse / "tmp" / build_id
    if spill.is_dir():
        existed = True
        shutil.rmtree(spill, ignore_errors=False)
    return existed, freed


def delete_build_files(
    build_id: str, *, layout: DataLayout | None = None
) -> Literal["deleted", "deferred", "absent"]:
    """Delete a build's file, WAL and spill directory, tolerating Windows file locks.

    Raises ConfigError for the current build; SchemaViolation for an invalid ID or an
    OSError other than a sharing violation.
    """
    lay = _resolve(layout)
    build_path(build_id, layout=lay)  # validates the ID before anything else
    if _is_current(build_id, lay):
        msg = f"refusing to delete CURRENT build {build_id}"
        raise ConfigError(msg)
    try:
        existed, freed = _remove(build_id, lay)
    except PermissionError:
        _log.warning("store.warehouse.delete_deferred", build_id=build_id)
        return "deferred"
    except OSError as exc:
        msg = f"cannot delete warehouse build {build_id}"
        raise SchemaViolation(msg, build_id=build_id, error_type=type(exc).__name__) from exc
    if not existed:
        return "absent"
    _log.info("store.warehouse.deleted", build_id=build_id, bytes=freed)
    return "deleted"


def _current_meta(layout: DataLayout | None) -> tuple[str | None, tuple[str, object] | None]:
    """(current build ID, (status, finished_us) of its ``meta.build`` row or None)."""
    build_id = read_current(layout=layout)
    if build_id is None:
        return None, None
    con = open_readonly(build_id, layout=layout)
    try:
        row = _meta_row(con)
    finally:
        con.close()
    return build_id, None if row is None else (row[0], row[2])


def warehouse_health(
    *, now: datetime.datetime, stale_after_h: float = 48.0, layout: DataLayout | None = None
) -> HealthResult:
    """Classify the current build as ok, degraded or down (ENG §4 health).

    Raises ConfigError only for a caller bug (``stale_after_h`` <= 0 or a naive ``now``).
    """
    if not stale_after_h > 0 or now.tzinfo is None or now.utcoffset() is None:
        msg = "warehouse_health needs stale_after_h > 0 and an aware UTC now"
        raise ConfigError(msg)
    try:
        build_id, meta = _current_meta(layout)
    except (HernessError, duckdb.Error, OSError, ImportError) as exc:
        return "down", f"current build cannot be opened ({type(exc).__name__})"
    if build_id is None:
        return "down", "no CURRENT build pointer"
    if meta is None:
        return "down", f"build {build_id} has no meta.build row"
    status, finished = meta[0], _from_epoch_us(meta[1])
    if finished is None:
        return "degraded", f"build {build_id} is {status} with no finished_at"
    age_h = (now - finished).total_seconds() / 3600
    reason = f"build {build_id} is {status}, age {age_h:.1f} h"
    return ("degraded" if status != "promoted" or age_h > stale_after_h else "ok"), reason
