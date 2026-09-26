"""Writable build connection and ``CURRENT`` writer (impl 02 U02-34, U02-35).

The only writable warehouse connection (design 02 §10: only the build pipeline writes);
import restricted to ``herness.model.build`` and ``herness.model.promote`` by the
import-linter contract ``store-rw-restricted`` (TH02-05, checked by ST02-05).
"""

from __future__ import annotations

import os
from typing import Final, Protocol

import duckdb

from herness.core.errors import ConfigError, SchemaViolation, StoreBusy
from herness.core.ids import new_ulid
from herness.core.logging import get_logger
from herness.store import warehouse
from herness.store.lake import _fsync
from herness.store.layout import DataLayout, data_layout
from herness.store.warehouse import build_path

type _Config = dict[str, str | bool | int | float | list[str]]

_CURRENT: Final = "CURRENT"
# GLOBAL, not a session SET: a session-level SET would not reach cursors or later
# connections to the shared instance (T02-09 lesson; a wording deviation from the design).
_SET_TZ: Final = "SET GLOBAL TimeZone = 'UTC'"
_log = get_logger("store.warehouse")


class _BuildSettings(Protocol):
    """Structural view of ``herness.model.settings.BuildSettings`` (U02-75).

    ``herness.store`` never imports ``herness.model`` (contract ``store-no-upward``).
    """

    @property
    def threads(self) -> int | None: ...
    @property
    def memory_limit(self) -> str: ...


def _resolve(layout: DataLayout | None) -> DataLayout:
    return layout if layout is not None else data_layout()


def open_for_build(
    build_id: str,
    *,
    create: bool,
    cfg: _BuildSettings,
    layout: DataLayout | None = None,
) -> duckdb.DuckDBPyConnection:
    """Open the single writable connection for ``build_id`` (U02-34).

    ``create=True`` requires the file to not exist yet; ``create=False`` requires it to
    already exist; either violation raises ``ConfigError``. Extension autoinstall and
    autoload stay off; external access stays on (staging reads the lake with
    ``read_parquet``).
    """
    lay = _resolve(layout)
    path = build_path(build_id, layout=lay)  # SchemaViolation for an invalid ID
    exists = path.is_file()
    if create and exists:
        msg = f"build {build_id} already exists"
        raise ConfigError(msg, build_id=build_id)
    if not create and not exists:
        msg = f"build {build_id} does not exist"
        raise ConfigError(msg, build_id=build_id)
    lay.warehouse.mkdir(parents=True, exist_ok=True)
    spill = lay.warehouse / "tmp" / build_id
    spill.mkdir(parents=True, exist_ok=True)
    config: _Config = {
        "autoinstall_known_extensions": False,
        "autoload_known_extensions": False,
        "threads": cfg.threads if cfg.threads is not None else (os.cpu_count() or 1),
        "memory_limit": cfg.memory_limit,
        "temp_directory": str(spill),
        "preserve_insertion_order": False,
    }
    try:
        con = duckdb.connect(str(path), read_only=False, config=config)
    except duckdb.Error as exc:
        raise warehouse._open_error(exc, build_id) from exc
    try:
        con.execute(_SET_TZ)
    except duckdb.Error as exc:
        con.close()
        raise warehouse._open_error(exc, build_id) from exc
    _log.info("store.warehouse.opened_for_build", build_id=build_id, create=create)
    return con


def write_current(build_id: str, *, layout: DataLayout | None = None) -> str | None:
    """Atomically switch ``CURRENT`` to ``build_id`` (U02-35); return the previous value."""
    lay = _resolve(layout)
    warehouse._existing_path(build_id, lay)  # validates the ID; NotFoundError if missing
    try:
        previous = warehouse.read_current(layout=lay)
    except Exception:  # noqa: BLE001 - any read failure means "no known previous" (step 2)
        previous = None
    current = lay.warehouse / _CURRENT
    tmp = lay.warehouse / f".{_CURRENT}.tmp-{new_ulid()}"
    try:
        tmp.write_bytes(f"{build_id}\n".encode("ascii"))
        _fsync(tmp, os.O_RDWR | getattr(os, "O_BINARY", 0))
        os.replace(tmp, current)
        if os.name != "nt":  # directory fsync is POSIX-only
            _fsync(lay.warehouse, os.O_RDONLY)
    except PermissionError as exc:
        tmp.unlink(missing_ok=True)
        msg = "CURRENT is being replaced"
        raise StoreBusy(msg, build_id=build_id) from exc
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        msg = "cannot write CURRENT"
        raise SchemaViolation(msg, build_id=build_id, error_type=type(exc).__name__) from exc
    _log.info("store.warehouse.current_switched", previous=previous, build_id=build_id)
    return previous
