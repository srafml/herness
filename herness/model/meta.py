"""The ``meta.build`` row, row counts, ``dataset_kind`` and ``git_sha`` (impl 02 U02-88 … U02-92).

Every statement is parameterised; the only identifiers put into SQL text are fixed column
names and ``schema.table`` names read from ``duckdb_tables()`` that pass the identifier
pattern (TH02-10). Messages name columns and rules, never row values (TH02-14).
"""

from __future__ import annotations

import datetime
import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final, Literal, get_args

import duckdb

from herness.core.errors import ConfigError, SchemaViolation
from herness.core.logging import get_logger
from herness.model.lakeinfo import LakeInventory

type DatasetKind = Literal["synthetic", "real"]
type BuildRowStatus = Literal["building", "failed", "promoted", "retired"]
type CountSchema = Literal["core", "enrich", "metrics", "score"]

_ENV_SHA_RE: Final = re.compile(r"^[0-9a-f]{7,40}$")
_GIT_SHA_RE: Final = re.compile(r"^[0-9a-f]{40}\s*$")
_CONFIG_HASH_RE: Final = re.compile(r"^cfg_[0-9a-f]{16}$")
_IDENT_RE: Final = re.compile(r"^[a-z_][a-z0-9_]{0,127}$")
_SHA_CHARS: Final = 12
_GIT_TIMEOUT_S: Final = 5
_STATUSES: Final = frozenset(get_args(BuildRowStatus.__value__))
_KINDS: Final = frozenset(get_args(DatasetKind.__value__))
_COUNT_SCHEMAS: Final = frozenset(get_args(CountSchema.__value__))
_INSERT: Final = (
    "INSERT INTO meta.build (build_id, started_at, finished_at, git_sha, config_hash,"
    " dataset_kind, source_watermarks, row_counts, status)"
    " VALUES (?, ?, NULL, ?, ?, ?, ?, '{}', 'building')"
)
_TABLES: Final = (
    "SELECT schema_name, table_name FROM duckdb_tables()"
    " WHERE database_name = current_database() AND schema_name IN (SELECT unnest(?))"
    " ORDER BY schema_name, table_name"
)

_log = get_logger("model.build")


def dataset_kind(profile: str, inventory: LakeInventory) -> DatasetKind:
    """``synthetic`` for the ``synth`` profile or a synthetic lake, else ``real`` (U02-88)."""
    return "synthetic" if profile == "synth" or inventory.from_synth else "real"


def git_sha(*, env: Mapping[str, str], repo_dir: Path) -> str:
    """12 hex characters of ``HERNESS_GIT_SHA`` or ``git rev-parse HEAD``, else ``unknown``."""
    from_env = env.get("HERNESS_GIT_SHA", "")
    if _ENV_SHA_RE.fullmatch(from_env):
        return from_env[:_SHA_CHARS]
    try:
        done = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607 - fixed argv, PATH lookup of git is intended
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        done = None
    if done is not None and done.returncode == 0 and _GIT_SHA_RE.fullmatch(done.stdout):
        return done.stdout[:_SHA_CHARS]
    _log.warning("model.build.git_sha_unknown")
    return "unknown"


def _require_utc(value: datetime.datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != datetime.timedelta(0):
        msg = f"meta.build {name} must be an aware UTC datetime"
        raise SchemaViolation(msg)


def _row_count(con: duckdb.DuckDBPyConnection) -> int:
    row = con.execute("SELECT count(*) FROM meta.build").fetchone()
    return int(row[0]) if row is not None else 0


def insert_build_row(  # noqa: PLR0913 - U02-90 signature
    con: duckdb.DuckDBPyConnection,
    *,
    build_id: str,
    started_at: datetime.datetime,
    git_sha: str,
    config_hash: str,
    dataset_kind: DatasetKind,
    source_watermarks: Mapping[str, str],
) -> None:
    """Write the single ``meta.build`` row with status ``building`` (U02-90)."""
    if _CONFIG_HASH_RE.fullmatch(config_hash) is None:
        msg = "meta.build config_hash does not match cfg_<16 hex>"
        raise SchemaViolation(msg)
    if dataset_kind not in _KINDS:
        msg = "meta.build dataset_kind is invalid"
        raise SchemaViolation(msg)
    _require_utc(started_at, "started_at")
    watermarks = json.dumps(dict(source_watermarks), sort_keys=True)
    params = [build_id, started_at, git_sha, config_hash, dataset_kind, watermarks]
    try:
        if _row_count(con):
            msg = "meta.build already has a row"
            raise SchemaViolation(msg, build_id=build_id)
        con.execute(_INSERT, params)
    except duckdb.Error as exc:
        msg = "meta.build insert failed"
        raise SchemaViolation(msg, build_id=build_id, error_type=type(exc).__name__) from exc


def update_build_row(
    con: duckdb.DuckDBPyConnection,
    *,
    status: BuildRowStatus | None = None,
    finished_at: datetime.datetime | None = None,
    clear_finished: bool = False,
    row_counts: Mapping[str, int] | None = None,
) -> None:
    """Update the requested columns of the one ``meta.build`` row (U02-91)."""
    if finished_at is not None and clear_finished:
        msg = "update_build_row: finished_at and clear_finished are exclusive"
        raise ConfigError(msg)
    if status is not None and status not in _STATUSES:
        msg = "update_build_row: unknown status"
        raise ConfigError(msg)
    sets: list[str] = []
    params: list[object] = []
    if status is not None:
        sets.append("status = ?")
        params.append(status)
    if finished_at is not None:
        _require_utc(finished_at, "finished_at")
        sets.append("finished_at = ?")
        params.append(finished_at)
    if clear_finished:
        sets.append("finished_at = NULL")
    if row_counts is not None:
        sets.append("row_counts = ?")
        params.append(json.dumps(dict(row_counts), sort_keys=True))
    if not sets:
        msg = "update_build_row: no change requested"
        raise ConfigError(msg)
    try:
        if _row_count(con) != 1:
            msg = "no meta.build row to update"
            raise SchemaViolation(msg)
        con.execute(f"UPDATE meta.build SET {', '.join(sets)}", params)  # noqa: S608 - fixed column fragments
    except duckdb.Error as exc:
        msg = "meta.build update failed"
        raise SchemaViolation(msg, error_type=type(exc).__name__) from exc


def collect_row_counts(
    con: duckdb.DuckDBPyConnection, schemas: Sequence[CountSchema]
) -> dict[str, int]:
    """Rows per base table of ``schemas`` as ``{"schema.table": n}``, sorted by key (U02-92).

    Tables whose schema or table name is not a plain lower-case identifier are skipped and
    counted in one ``model.build.row_count_skipped`` WARNING (schemas and count only).
    """
    if not set(schemas) <= _COUNT_SCHEMAS:
        msg = "collect_row_counts: unknown schema"
        raise ConfigError(msg)
    counts: dict[str, int] = {}
    skipped = 0
    try:
        tables = con.execute(_TABLES, [list(schemas)]).fetchall()
        for schema, table in tables:
            if not (_IDENT_RE.fullmatch(str(schema)) and _IDENT_RE.fullmatch(str(table))):
                skipped += 1
                continue
            row = con.execute(f'SELECT count(*) FROM "{schema}"."{table}"').fetchone()  # noqa: S608 - checked identifiers
            counts[f"{schema}.{table}"] = int(row[0]) if row is not None else 0
    except duckdb.Error as exc:
        msg = "row count collection failed"
        raise SchemaViolation(msg, error_type=type(exc).__name__) from exc
    if skipped:  # names are not logged: they failed the identifier check
        _log.warning("model.build.row_count_skipped", schemas=sorted(schemas), count=skipped)
    return dict(sorted(counts.items()))
