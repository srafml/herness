"""`--verify` of a synthetic root: lake contract and truth counts, determinism hashes (U11-22).

`verify_root` re-reads `<root>/data/raw` with an in-memory DuckDB whose file access is
limited to that directory (`allowed_directories`, then `enable_external_access = false` and
`lock_configuration = true`) and checks, per `source/entity`, the 8 metadata columns and
their spec 02 §3.1 types, non-NULL key columns, `_record_id = _source:_entity:_source_key`,
NULL `_payload` exactly on tombstones, and that no dot-prefixed file remains. Row counts
must equal `truth.row_counts`; `duplicate_rows` (rows minus distinct `(_record_id,
_source_updated_at, _payload)`), `later_versions` (distinct live versions minus distinct
live record ids) and `tombstones`, recomputed from the lake, must equal `truth.dirty`.
`content_hashes` is design §5.1.8's per-entity md5 (timestamps rendered in UTC).
Problems name keys, columns and paths only, never a value (TH11-07). Read-only.
"""

import dataclasses
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final

import duckdb

from herness.core.errors import ConfigError
from herness.eval.truth import load_truth

__all__ = ["DIRTY_KEYS", "METADATA_TYPES", "VerifyReport", "content_hashes", "verify_root"]

# spec 02 §3.1 required metadata columns and their DuckDB types; nullable only `_payload`
METADATA_TYPES: Final = {
    "_record_id": "VARCHAR",
    "_source": "VARCHAR",
    "_entity": "VARCHAR",
    "_source_key": "VARCHAR",
    "_source_updated_at": "TIMESTAMP WITH TIME ZONE",
    "_fetched_at": "TIMESTAMP WITH TIME ZONE",
    "_deleted": "BOOLEAN",
    "_payload": "VARCHAR",
}
DIRTY_KEYS: Final = ("duplicate_rows", "later_versions", "tombstones")
_NAME_RE: Final = re.compile(r"[a-z][a-z0-9_]*")
_READ: Final = "read_parquet($glob, hive_partitioning = true, union_by_name = true)"
_VERSION: Final = "(_record_id, _source_updated_at, _payload)"
_CHECKS: Final = f"""
SELECT
    count(*) FILTER (WHERE _record_id IS NULL OR _source IS NULL OR _entity IS NULL
        OR _source_key IS NULL OR _source_updated_at IS NULL OR _fetched_at IS NULL
        OR _deleted IS NULL),
    count(*) FILTER (WHERE _record_id <> _source || ':' || _entity || ':' || _source_key
        OR _source <> $source OR _entity <> $entity),
    count(*) FILTER (WHERE _deleted <> (_payload IS NULL)),
    count(*) - count(DISTINCT {_VERSION}),
    count(DISTINCT {_VERSION}) FILTER (WHERE NOT _deleted)
        - count(DISTINCT _record_id) FILTER (WHERE NOT _deleted),
    count(*) FILTER (WHERE _deleted)
FROM {_READ}
"""  # noqa: S608 - constant SQL; the glob and the names are bound parameters
_CHECK_NAMES: Final = (
    "NULL in a metadata key column",
    "_record_id differs from _source:_entity:_source_key or the path",
    "_payload NULL on a live row or set on a tombstone",
)
_HASH: Final = f"""
SELECT md5(string_agg(v, ',' ORDER BY v)) FROM (
    SELECT _record_id || CAST(_source_updated_at AS VARCHAR) || md5(coalesce(_payload, '')) AS v
    FROM {_READ}
)
"""  # noqa: S608 - constant SQL; the glob is a bound parameter


@dataclasses.dataclass(frozen=True, slots=True)
class VerifyReport:
    """Outcome of `verify_root`: `ok` when `problems` is empty; lake row counts per
    `source/entity` and the recomputed `DIRTY_KEYS` counters."""

    ok: bool
    problems: tuple[str, ...]
    row_counts: dict[str, int]
    dirty: dict[str, int]


def _raw(root: Path) -> Path:
    return root.resolve(strict=False) / "data" / "raw"


@contextmanager
def _connect(raw: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    """In-memory DuckDB that can read files under `raw` only, configuration locked."""
    con = duckdb.connect(
        ":memory:",
        config={"autoinstall_known_extensions": False, "autoload_known_extensions": False},
    )
    try:
        con.execute("SET TimeZone = 'UTC'")
        con.execute("SET allowed_directories = $dirs", {"dirs": [raw.as_posix() + "/"]})
        con.execute("SET enable_external_access = false")
        con.execute("SET lock_configuration = true")
        yield con
    finally:
        con.close()


def _entities(raw: Path) -> list[str]:
    """`source/entity` keys of the lake directories (non-conforming names are skipped and
    reported by `_dot_files` / the truth comparison)."""
    if not raw.is_dir():
        return []
    return [
        f"{s.name}/{e.name}"
        for s in sorted(raw.iterdir())
        if s.is_dir() and _NAME_RE.fullmatch(s.name)
        for e in sorted(s.iterdir())
        if e.is_dir() and _NAME_RE.fullmatch(e.name)
    ]


def _glob(raw: Path, key: str) -> str:
    return f"{(raw / key).as_posix()}/**/[!.]*.parquet"


def _dot_files(raw: Path) -> list[str]:
    if not raw.is_dir():
        return []
    found = [p.relative_to(raw).as_posix() for p in sorted(raw.rglob(".*"))]
    return [f"data/raw/{rel}: dot-prefixed file or directory remains" for rel in found]


def _column_problems(con: duckdb.DuckDBPyConnection, raw: Path, key: str) -> list[str]:
    sql = f"DESCRIBE SELECT * FROM {_READ}"  # noqa: S608 - constant SQL, bound glob
    rows = con.execute(sql, {"glob": _glob(raw, key)}).fetchall()
    types = {str(r[0]): str(r[1]) for r in rows}
    out = []
    for name, expected in METADATA_TYPES.items():
        if name not in types:
            out.append(f"{key}: metadata column {name} missing")
        elif types[name] != expected:
            out.append(f"{key}: metadata column {name} has type {types[name]}, not {expected}")
    return out


def _one(con: duckdb.DuckDBPyConnection, sql: str, params: dict[str, str]) -> tuple[Any, ...]:
    """The single row of an aggregate query."""
    row = con.execute(sql, params).fetchone()
    return () if row is None else tuple(row)


def _check_entity(
    con: duckdb.DuckDBPyConnection, raw: Path, key: str, dirty: dict[str, int]
) -> tuple[int, list[str]]:
    """(rows, problems) of one entity; adds its recomputed dirty counters to `dirty`."""
    params = {"glob": _glob(raw, key)}
    try:
        (rows,) = _one(con, f"SELECT count(*) FROM {_READ}", params)  # noqa: S608 - constant
        problems = _column_problems(con, raw, key)
        if problems:
            return int(rows), problems
        source, entity = key.split("/")
        found = [int(v) for v in _one(con, _CHECKS, {**params, "source": source, "entity": entity})]
    except duckdb.Error as exc:
        return 0, [f"{key}: lake files unreadable ({type(exc).__name__})"]
    checks = zip(found[:3], _CHECK_NAMES, strict=True)
    problems = [f"{key}: {n} rows with {what}" for n, what in checks if n]
    for name, value in zip(DIRTY_KEYS, found[3:], strict=True):
        dirty[name] += value
    return int(rows), problems


def _truth_problems(
    truth_rows: dict[str, int], truth_dirty: dict[str, int], report: VerifyReport
) -> list[str]:
    out = []
    for key in sorted(set(truth_rows) | set(report.row_counts)):
        lake, expected = report.row_counts.get(key, 0), truth_rows.get(key, 0)
        if lake != expected:
            out.append(f"{key}: {lake} rows in the lake, truth.row_counts says {expected}")
    out += [
        f"dirty.{name}: lake {report.dirty[name]}, truth {truth_dirty.get(name)}"
        for name in DIRTY_KEYS
        if report.dirty[name] != truth_dirty.get(name)
    ]
    return out


def verify_root(root: Path) -> VerifyReport:
    """Check the lake contract under `<root>/data/raw` and the counts in `<root>/truth`."""
    raw = _raw(root)
    problems = _dot_files(raw)
    counts: dict[str, int] = {}
    dirty = dict.fromkeys(DIRTY_KEYS, 0)
    with _connect(raw) as con:
        for key in _entities(raw):
            counts[key], found = _check_entity(con, raw, key, dirty)
            problems += found
    lake = VerifyReport(ok=False, problems=(), row_counts=counts, dirty=dirty)
    try:
        truth = load_truth(root.resolve(strict=False) / "truth")
    except ConfigError as exc:
        problems.append(f"truth/truth.json: {exc.message}")
    else:
        problems += _truth_problems(truth.row_counts, truth.dirty, lake)
    return VerifyReport(not problems, tuple(problems), counts, dirty)


def content_hashes(root: Path) -> dict[str, str]:
    """Per `source/entity`: `md5(string_agg(_record_id || _source_updated_at ||
    md5(coalesce(_payload, '')) ORDER BY 1))` over the lake files (design §5.1.8)."""
    raw = _raw(root)
    with _connect(raw) as con:
        out = {}
        for key in _entities(raw):
            out[key] = str(_one(con, _HASH, {"glob": _glob(raw, key)})[0])
        return out
