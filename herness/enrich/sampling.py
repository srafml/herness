"""Stratified sampling, prototype cap and active selection (U03-120 ... U03-123; design 03
§5.8 steps 1 and 4, §5.9 steps 2-3).

The stratum key is computed twice, by `stratum_of` in Python and by ``_STRATUM_SQL`` in the
warehouse; UT03-115 checks that both agree on band and length edges. Every query is a fixed
string: values (salt, entity list) are bound parameters and row sets (exclusions, quotas) are
registered Arrow tables under fixed names, so nothing derived from data is ever spliced into
SQL (ST03-05). Errors and logs carry counts and class names only, never ticket text (TH03-03).
Gold hashes passed in ``exclude_hashes`` never reach a sample (TH03-04).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final

import duckdb
import numpy as np
import pyarrow as pa

from herness.core.errors import SchemaViolation, StoreBusy
from herness.core.logging import get_logger
from herness.core.types import QuestionSet
from herness.enrich._cluster_io import ClusterSnapshot
from herness.enrich.cluster import project

__all__ = ["allocate", "select_active", "stratified_sample", "stratum_of"]

CANDIDATE_FACTOR: Final = 3  # candidates per stratum quota (U03-122 step 4)
VECTOR_CHUNK: Final = 8_192  # hashes per `vector_reader` call
SHORT_BELOW, LONG_ABOVE = 120, 600  # text length bands (characters)
SAMPLE_SCHEMA: Final = pa.schema(
    [(name, pa.string()) for name in ("record_id", "entity", "content_hash", "text", "stratum")]
)
_EXCLUDE, _QUOTA = "_sample_exclude", "_sample_quota"  # registered views (fixed names)
_HASHES_SCHEMA: Final = pa.schema([("content_hash", pa.string())])
_QUOTA_SCHEMA: Final = pa.schema([("stratum", pa.string()), ("k", pa.int64())])
_SCHEMA_ERRORS: Final = (duckdb.CatalogException, duckdb.BinderException)

# One row per content hash (lowest record_id) of the wanted entities with its stratum key,
# which mirrors `stratum_of` (top 50 services, quarter of `opened_at` in UTC). Views
# `_sample_exclude` and `_sample_quota` are registered under these fixed names.
_REC_SQL: Final = """
WITH opened AS (
    SELECT 'incident' AS entity, record_id, service_id, CAST(priority AS INTEGER) AS priority,
        opened_at
    FROM core.incident
    UNION ALL
    SELECT 'problem', record_id, service_id, CAST(NULL AS INTEGER), opened_at FROM core.problem
),
top AS (
    SELECT service_id FROM core.incident WHERE service_id IS NOT NULL
    GROUP BY service_id ORDER BY count(*) DESC, service_id LIMIT 50
),
rec AS (
    SELECT t.record_id, t.entity, t.content_hash, t.text,
        coalesce(tp.service_id, 'other') || '|'
        || CASE WHEN o.priority IN (1, 2) THEN 'p12' WHEN o.priority = 3 THEN 'p3' ELSE 'p45' END
        || '|' || CAST(year(timezone('UTC', o.opened_at)) AS VARCHAR)
        || 'Q' || CAST(quarter(timezone('UTC', o.opened_at)) AS VARCHAR) || '|'
        || CASE WHEN length(t.text) < 120 THEN 's' WHEN length(t.text) <= 600 THEN 'm' ELSE 'l' END
        AS stratum
    FROM enrich.text_redacted AS t
    JOIN opened AS o ON o.entity = t.entity AND o.record_id = t.record_id
    LEFT JOIN top AS tp ON tp.service_id = o.service_id
    WHERE list_contains(CAST($entities AS VARCHAR[]), t.entity) AND o.opened_at IS NOT NULL
        AND t.content_hash IS NOT NULL AND t.text IS NOT NULL
        AND t.content_hash NOT IN (SELECT content_hash FROM _sample_exclude)
    QUALIFY row_number() OVER (PARTITION BY t.content_hash ORDER BY t.record_id, t.entity) = 1
)
"""
_SIZES_TAIL: Final = "SELECT stratum, count(*) FROM rec GROUP BY stratum"
_CANDIDATES_TAIL: Final = """
SELECT r.record_id, r.entity, r.content_hash, r.text, r.stratum
FROM (
    SELECT *, row_number() OVER (
        PARTITION BY stratum ORDER BY sha256($salt || content_hash), content_hash) AS rk
    FROM rec
) AS r
JOIN _sample_quota AS q ON q.stratum = r.stratum
WHERE r.rk <= q.k
ORDER BY r.stratum, r.rk
"""
_POOL_TAIL: Final = """
SELECT record_id, content_hash FROM rec ORDER BY sha256($salt || content_hash), content_hash
"""
_SIZES_SQL: Final = _REC_SQL + _SIZES_TAIL
_CANDIDATES_SQL: Final = _REC_SQL + _CANDIDATES_TAIL
_POOL_SQL: Final = _REC_SQL + _POOL_TAIL

type _Row = tuple[str, str, str, str, str]

_log = get_logger("enrich.sampling")


def stratum_of(
    *,
    service_id: str | None,
    top_services: frozenset[str],
    priority: int | None,
    opened_at: datetime,
    text_len: int,
) -> str:
    """``"<service or 'other'>|<band>|<YYYY>Q<q>|<len>"`` (U03-120); a naive ``opened_at`` is
    read as UTC, an aware one is converted to UTC (as the SQL does)."""
    service = service_id if service_id is not None and service_id in top_services else "other"
    band = "p12" if priority in (1, 2) else "p3" if priority == 3 else "p45"  # noqa: PLR2004
    stamp = opened_at.astimezone(UTC) if opened_at.tzinfo else opened_at
    quarter = (stamp.month - 1) // 3 + 1
    size = "s" if text_len < SHORT_BELOW else "m" if text_len <= LONG_ABOVE else "l"
    return f"{service}|{band}|{stamp.year}Q{quarter}|{size}"


def _adjust(
    n: dict[str, int], order: list[str], diff: int, *, ceil: Mapping[str, int], floor_: int
) -> None:
    """Add (``diff`` > 0) or remove units one at a time over ``order``, skipping strata at
    their cap (or at ``min(floor_, N_h)`` when removing), until ``diff`` is spent."""
    step = 1 if diff > 0 else -1
    while diff:
        moved = False
        for key in order:
            room = n[key] < ceil[key] if step > 0 else n[key] > min(floor_, ceil[key])
            if room:
                n[key] += step
                diff -= step
                moved = True
                if not diff:
                    return
        if not moved:  # pragma: no cover - unreachable inside the precondition domain
            return


def allocate(sizes: Mapping[str, int], total: int, *, min_per: int = 5) -> dict[str, int]:
    """Square-root allocation with a minimum per stratum (U03-121).

    ``n_h = max(min_per, floor(total * sqrt(N_h) / sum sqrt(N_j)))`` capped at ``N_h``; the
    remainder (or excess) moves one unit at a time by largest fractional part, then key, so
    ``sum n_h = min(total, sum N_h)``. Outside the precondition (``total < min_per x strata``)
    every stratum gets ``min(min_per, N_h)``.
    """
    keys = sorted(sizes)
    if total < min_per * len(keys):
        return {key: min(min_per, sizes[key]) for key in keys}
    root_sum = sum(math.sqrt(sizes[key]) for key in keys)
    share = {key: total * math.sqrt(sizes[key]) / root_sum for key in keys}
    n = {key: min(sizes[key], max(min_per, math.floor(share[key]))) for key in keys}
    order = sorted(keys, key=lambda key: (-(share[key] - math.floor(share[key])), key))
    target = min(total, sum(sizes.values()))
    _adjust(n, order, target - sum(n.values()), ceil=sizes, floor_=min_per)
    return n


def _duck_error(exc: duckdb.Error) -> StoreBusy | SchemaViolation:
    """StoreBusy for an IO (lock) error, else SchemaViolation naming only a catalog or binder
    message or the error class (never row values)."""
    if isinstance(exc, duckdb.IOException):
        return StoreBusy("stratified_sample: warehouse busy", error_type=type(exc).__name__)
    reason = str(exc).splitlines()[0] if isinstance(exc, _SCHEMA_ERRORS) else type(exc).__name__
    return SchemaViolation(f"stratified_sample: {reason}")


def _entities(qs: QuestionSet) -> list[str]:
    problems = any("problem" in q.applies_to for q in qs.questions)
    return ["incident", "problem"] if problems else ["incident"]


def _excluded(exclude_hashes: frozenset[str]) -> pa.Table:
    return pa.table({"content_hash": sorted(exclude_hashes)}, schema=_HASHES_SCHEMA)


def _run[T](
    wh: duckdb.DuckDBPyConnection,
    sql: str,
    params: Mapping[str, object],
    views: Mapping[str, pa.Table],
    fetch: Callable[[duckdb.DuckDBPyConnection], T],
) -> T:
    """Run ``sql`` with ``views`` registered (unregistered on return) and ``fetch`` the result;
    DuckDB errors map through `_duck_error`."""
    for name, table in views.items():
        wh.register(name, table)
    try:
        return fetch(wh.execute(sql, dict(params)))
    except duckdb.Error as exc:
        raise _duck_error(exc) from exc
    finally:
        for name in views:
            wh.unregister(name)


def _all(cursor: duckdb.DuckDBPyConnection) -> list[Any]:
    return cursor.fetchall()


def _nearest(
    hashes: Sequence[str],
    snapshot: ClusterSnapshot,
    vector_reader: Callable[[Sequence[str]], np.ndarray],
) -> np.ndarray:
    """Nearest stored prototype per hash: PCA projection, then the highest cosine."""
    out = np.empty(len(hashes), dtype=np.int64)
    protos = np.asarray(snapshot.prototypes, dtype=np.float32)
    for start in range(0, len(hashes), VECTOR_CHUNK):
        chunk = hashes[start : start + VECTOR_CHUNK]
        points = project(np.asarray(vector_reader(chunk), dtype=np.float32), snapshot.pca,
                         device="cpu")  # fmt: skip
        out[start : start + len(chunk)] = np.argmax(points @ protos.T, axis=1)
    return out


def _walk(
    rows: list[_Row], quota: Mapping[str, int], protos: np.ndarray | None, cap: int
) -> list[_Row]:
    """Accept rows (stratum then hash order) while the stratum quota and prototype cap allow."""
    taken: dict[str, int] = {}
    per_proto: dict[int, int] = {}
    chosen: list[_Row] = []
    for index, row in enumerate(rows):
        stratum = row[4]
        if taken.get(stratum, 0) >= quota[stratum]:
            continue
        if protos is not None:
            proto = int(protos[index])
            if per_proto.get(proto, 0) >= cap:
                continue
            per_proto[proto] = per_proto.get(proto, 0) + 1
        taken[stratum] = taken.get(stratum, 0) + 1
        chosen.append(row)
    return chosen


def stratified_sample(  # noqa: PLR0913 - U03-122's keyword-only signature is binding
    wh: duckdb.DuckDBPyConnection,
    *,
    qs: QuestionSet,
    size: int,
    exclude_hashes: frozenset[str],
    salt: str,
    snapshot: ClusterSnapshot | None,
    vector_reader: Callable[[Sequence[str]], np.ndarray],
    max_proto_share: float = 0.02,
) -> pa.Table:
    """Draw the teacher-labeling or gold sample (U03-122); deterministic for a warehouse and
    ``salt``. Columns ``record_id, entity, content_hash, text, stratum``.

    Incidents (plus problems when a question applies to them) with a text row and an
    ``opened_at``, deduplicated by content hash (lowest record_id), minus ``exclude_hashes``;
    ``allocate`` per stratum; per stratum the first ``3 x n_h`` by ``sha256(salt || hash)``;
    with a snapshot at most ``max(1, floor(max_proto_share x size))`` rows share a nearest
    prototype. Raises SchemaViolation (missing tables or columns, other DuckDB errors) or
    StoreBusy (DuckDB IO error).
    """
    entities = _entities(qs)
    excluded = _excluded(exclude_hashes)
    counts = _run(wh, _SIZES_SQL, {"entities": entities}, {_EXCLUDE: excluded}, _all)
    sizes = {str(stratum): int(count) for stratum, count in counts}
    if not sizes or size <= 0:
        return SAMPLE_SCHEMA.empty_table()
    quota = allocate(sizes, size)
    factor = CANDIDATE_FACTOR if snapshot is not None else 1
    quota_rows = [{"stratum": key, "k": factor * n} for key, n in sorted(quota.items())]
    views = {_EXCLUDE: excluded, _QUOTA: pa.Table.from_pylist(quota_rows, schema=_QUOTA_SCHEMA)}
    params = {"entities": entities, "salt": salt}
    rows: list[_Row] = _run(wh, _CANDIDATES_SQL, params, views, _all)
    protos = None
    cap = max(1, math.floor(max_proto_share * size))
    if snapshot is not None and rows:
        protos = _nearest([row[2] for row in rows], snapshot, vector_reader)
    chosen = _walk(rows, quota, protos, cap)
    _log.info("enrich.sampling.sampled", strata=len(sizes), candidates=len(rows),
              size=len(chosen), excluded=len(exclude_hashes))  # fmt: skip
    return pa.Table.from_pylist(
        [dict(zip(SAMPLE_SCHEMA.names, row, strict=True)) for row in chosen], schema=SAMPLE_SCHEMA
    )


def _ordered_pool(
    wh: duckdb.DuckDBPyConnection, *, qs: QuestionSet, exclude_hashes: frozenset[str], salt: str
) -> pa.Table:
    """``record_id, content_hash`` of the sample pool in ``sha256(salt || hash)`` order, for
    the gold class top-up (U03-125); same pool and errors as `stratified_sample`."""
    params = {"entities": _entities(qs), "salt": salt}
    views = {_EXCLUDE: _excluded(exclude_hashes)}
    return _run(wh, _POOL_SQL, params, views, lambda cur: cur.to_arrow_table())


def select_active(
    uncertainty: np.ndarray,
    prototypes: np.ndarray,
    hashes: Sequence[str],
    *,
    candidates: int,
    per_prototype: int,
    per_round: int,
) -> list[int]:
    """Active-learning selection (U03-123): sort by (-u, hash), keep the first ``candidates``,
    then walk keeping rows while their prototype has fewer than ``per_prototype``, until
    ``per_round`` indices are chosen."""
    if len(hashes) == 0:
        return []
    order = np.lexsort((np.asarray(hashes, dtype=str), -np.asarray(uncertainty, dtype=float)))
    counts: dict[int, int] = {}
    chosen: list[int] = []
    for index in order[: max(candidates, 0)].tolist():
        if len(chosen) >= per_round:
            break
        proto = int(prototypes[index])
        if counts.get(proto, 0) < per_prototype:
            counts[proto] = counts.get(proto, 0) + 1
            chosen.append(index)
    return chosen
