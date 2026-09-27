"""Hybrid memory recall (impl 07 §3.11, U07-58 … U07-62; design 07 §5.6).

Visibility is decided on the hydrated SQLite rows (TH07-06, TH07-13). Only the redacted query is
embedded or turned into a quoted FTS5 expression (TH07-08). Vector-path failures degrade recall
to keyword and entity scoring. Logs carry no memory text, query text or memory ids."""

import math
import re
import sqlite3
import threading
import unicodedata
from collections import OrderedDict
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import zip_longest
from typing import Final, get_args

import duckdb
import numpy as np
from pydantic import ValidationError

from herness.core import time as clock
from herness.core.errors import ToolInputError
from herness.core.ids import IdKind, is_valid_id
from herness.core.logging import get_logger
from herness.core.redact import Redactor
from herness.core.resilience.metrics import record_histogram
from herness.core.types import Layer, MemoryItem, MemoryRunContext, RecallHit, Status
from herness.harness.memory.settings import MemoryConfig, RecallWeights
from herness.harness.memory.store import Embedder, VectorIndex
from herness.harness.memory.types import RecallFilters
from herness.store import ops, warehouse

__all__ = [
    "MemoryRecaller", "MmrCandidate", "RecallResult", "RelatednessCache", "fts_query_string",
    "mmr_select", "score_candidate",
]  # fmt: skip

_TOKEN_RE: Final = re.compile(r"\w+")
_MAX_TOKENS: Final = 32
_MIN_TOKEN_LEN: Final = 2
_MAX_CANDIDATES: Final = 150  # impl 07 §10 enforced limit: candidates hydrated per recall
_QUERY_MAX: Final = 2000
_K_MAX: Final = 50
_ALL_LAYERS: Final[tuple[Layer, ...]] = get_args(Layer)
_EMPTY_MAP_TTL_S: Final = 60.0
_LATENCY: Final = "herness_memory_recall_latency_seconds"
_SERVICE_MAP_SQL: Final = "SELECT service_id, team_id, org_id FROM core.service_map"

type _Groups = dict[str, frozenset[str]]

_log = get_logger("harness.memory")


def fts_query_string(query: str) -> str | None:
    """Quoted ``\\w`` tokens (length ≥ 2, first 32 distinct) joined by `` OR ``; None if none."""
    text = unicodedata.normalize("NFKC", query).casefold()
    tokens = list(dict.fromkeys(t for t in _TOKEN_RE.findall(text) if len(t) >= _MIN_TOKEN_LEN))
    return " OR ".join(f'"{token}"' for token in tokens[:_MAX_TOKENS]) or None


def _unit(value: float) -> float:
    return min(1.0, max(0.0, value))


def score_candidate(  # noqa: PLR0913 - keyword-only signature fixed by U07-59
    *, sim: float, kw_raw: float | None, max_kw: float, ent: float, age_days: float,
    half_life_days: float, confidence: float, is_pending: bool, weights: RecallWeights,
    conf_floor: float, rec_floor: float, degraded: bool,
) -> dict[str, float]:  # fmt: skip
    """The design 07 §5.6 score: keys ``sim``, ``kw``, ``ent``, ``rec``, ``conf``, ``final``."""
    kw = _unit(-kw_raw / max_kw) if kw_raw is not None and max_kw > 0 else 0.0
    sim, ent = _unit(sim), _unit(ent)
    if degraded:
        denominator = weights.kw + weights.ent
        rel = (weights.kw * kw + weights.ent * ent) / denominator if denominator > 0 else 0.0
    else:
        rel = weights.sim * sim + weights.kw * kw + weights.ent * ent
    rec = math.exp(-math.log(2) * max(age_days, 0.0) / half_life_days)
    conf = _unit(confidence) * (0.5 if is_pending else 1.0)
    final = _unit(rel) * (conf_floor + (1 - conf_floor) * conf)
    final *= rec_floor + (1 - rec_floor) * rec
    return {"sim": sim, "kw": kw, "ent": ent, "rec": rec, "conf": conf, "final": _unit(final)}


@dataclass(frozen=True, slots=True)
class MmrCandidate:
    """One MMR input: an id, its final score and its unit vector (None when it has none)."""

    memory_id: str
    score: float
    vector: np.ndarray | None


def _cosine(a: np.ndarray | None, b: np.ndarray | None) -> float:
    if a is None or b is None:
        return 0.0
    norm = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(np.dot(a, b)) / norm if norm > 0 else 0.0


def mmr_select(cands: Sequence[MmrCandidate], k: int, lam: float) -> list[str]:
    """Greedy maximal marginal relevance: ≤ k distinct ids, the first has the highest score."""
    pool = {cand.memory_id: cand for cand in reversed(cands)}  # the first entry per id wins
    penalty: dict[str, float | None] = dict.fromkeys(pool)  # max cosine to the selected ids
    chosen: list[str] = []
    while len(chosen) < k and pool:
        best = min(pool.values(), key=lambda c: (
            (1 - lam) * (penalty[c.memory_id] or 0.0) - lam * c.score, -c.score, c.memory_id
        ))  # fmt: skip
        del pool[best.memory_id]
        chosen.append(best.memory_id)
        for cand in pool.values():
            sim, old = _cosine(cand.vector, best.vector), penalty[cand.memory_id]
            penalty[cand.memory_id] = sim if old is None else max(old, sim)
    return chosen


def _group_map(rows: Iterable[Sequence[object]]) -> _Groups:
    groups: dict[str, set[str]] = {}
    for row in rows:
        members = {str(value) for value in row if value is not None}
        for member in members:
            groups.setdefault(member, set()).update(members)
    return {member: frozenset(group) for member, group in groups.items()}


class RelatednessCache:
    """``core.service_map`` relatedness per pinned build, LRU over ``max_builds`` builds."""

    def __init__(self, connect_build: Callable[[str], duckdb.DuckDBPyConnection]
                 = warehouse.open_readonly, max_builds: int = 3) -> None:  # fmt: skip
        self._connect, self._max_builds = connect_build, max_builds
        self._cache: OrderedDict[str, tuple[_Groups, float | None]] = OrderedDict()
        self._lock = threading.Lock()

    def related(self, build_id: str, a: str, b: str) -> bool:
        """True when ``b`` shares a service map or team row with ``a`` (never for ``a == b``)."""
        return a != b and b in self._groups(build_id).get(a, frozenset())

    def related_any(self, build_id: str, ids_a: Collection[str], ids_b: Collection[str]) -> bool:
        """True when any id of ``ids_a`` is related to any id of ``ids_b``."""
        groups = self._groups(build_id)
        return any(a != b and b in groups.get(a, frozenset()) for a in ids_a for b in ids_b)

    def _groups(self, build_id: str) -> _Groups:
        with self._lock:
            entry = self._cache.get(build_id)
            if entry is not None and (entry[1] is None or entry[1] > clock.monotonic()):
                self._cache.move_to_end(build_id)
                return entry[0]
        built = self._build(build_id)  # outside the lock; a map is immutable once built
        expires = None if built is not None else clock.monotonic() + _EMPTY_MAP_TTL_S
        with self._lock:
            self._cache[build_id] = (built or {}, expires)
            self._cache.move_to_end(build_id)
            while len(self._cache) > self._max_builds:
                self._cache.popitem(last=False)
        return built or {}

    def _build(self, build_id: str) -> _Groups | None:
        try:
            con = self._connect(build_id)  # read-only warehouse handle, closed after two reads
            try:
                rows = con.execute(_SERVICE_MAP_SQL).fetchall()
                rows += con.execute("SELECT team_id, org_id FROM core.team").fetchall()
            finally:
                con.close()
        except Exception as exc:  # noqa: BLE001 - U07-61 never raises; empty map for 60 s
            _log.warning("memory.recall.relatedness_unavailable", build_id=build_id,
                         error_type=type(exc).__name__)  # fmt: skip
            return None
        return _group_map(rows)


@dataclass(frozen=True, slots=True)
class RecallResult:
    """Hits in MMR order, whether the vector path was down, and the candidate count."""

    hits: list[RecallHit]
    degraded: bool
    n_candidates: int


@dataclass(slots=True)
class _Scan:  # per-call state: validated arguments and what the candidate searches found
    layers: list[Layer]
    filters: RecallFilters
    now: datetime
    run_ctx: MemoryRunContext | None
    run_id: str | None = None
    query_vector: np.ndarray | None = None
    ann: dict[str, float] | None = None
    keyword: dict[str, float] | None = None
    vectors: dict[str, np.ndarray] | None = None
    degraded: bool = False


def _checked_layers(query: object, layers: Sequence[Layer] | None, k: object) -> list[Layer]:
    if not isinstance(query, str) or not 1 <= len(query) <= _QUERY_MAX or type(k) is not int:
        msg = f"query must be 1 to {_QUERY_MAX} characters and k an integer"
        raise ToolInputError(msg)
    if not 1 <= k <= _K_MAX:
        msg = f"k must be from 1 to {_K_MAX}"
        raise ToolInputError(msg)
    chosen = list(_ALL_LAYERS if layers is None else layers)
    if not chosen or len(set(chosen)) != len(chosen) or not set(chosen) <= set(_ALL_LAYERS):
        msg = "layers must be distinct memory layers"
        raise ToolInputError(msg)
    return chosen


def _visible(item: MemoryItem, scan: _Scan) -> bool:  # step 6 on SQLite rows (TH07-06, TH07-13)
    flt, owner = scan.filters, scan.filters.include_pending_for
    status_ok = item.status == "active" or (item.status == "pending_approval"
                                            and owner is not None
                                            and item.provenance.author_ref == owner)  # fmt: skip
    return (status_ok and (item.expires_at is None or item.expires_at > scan.now)
            and item.layer in scan.layers and (flt.kinds is None or item.kind in flt.kinds)
            and item.confidence >= flt.min_confidence
            and (flt.created_after is None or item.created_at >= flt.created_after))  # fmt: skip


def _entity_ids(item: MemoryItem, entity_type: str | None) -> list[str]:
    raw = item.data.get("entities")
    found = [e for e in raw if isinstance(e, dict)] if isinstance(raw, list) else []
    typed = [e for e in found if entity_type in (None, e.get("type"))]
    return [i for e in typed if isinstance(i := e.get("id"), str)]


def _current_build(run_ctx: MemoryRunContext | None) -> str | None:
    try:
        return run_ctx.build_id if run_ctx is not None else warehouse.read_current()
    except Exception:  # noqa: BLE001 - relatedness is optional; ent falls back to 0
        return None


class MemoryRecaller:
    """Hybrid recall over the ops store, the vector index and the service map (U07-62)."""

    def __init__(
        self, cfg: MemoryConfig, *, conn_factory: Callable[[], sqlite3.Connection],
        vectors: VectorIndex, embedder: Embedder, redactor: Redactor,
        relatedness: RelatednessCache | None = None,
    ) -> None:  # fmt: skip
        self._rc, self._conn_factory = cfg.recall, conn_factory
        self._vectors, self._embedder = vectors, embedder
        self._redactor, self._relatedness = redactor, relatedness

    def recall(
        self, query: str, layers: Sequence[Layer] | None = None,
        filters: RecallFilters | None = None, k: int = 10,
        run_ctx: MemoryRunContext | None = None, *, now: datetime | None = None,
    ) -> RecallResult:  # fmt: skip
        """Hits in MMR order; vector-path failures degrade. Raises ToolInputError, StoreBusy,
        SchemaViolation for a naive ``now`` and RedactionFailed (fail closed, deliberate)."""
        started = clock.monotonic()
        at = clock.ensure_utc(now) if now is not None else clock.now()
        scan = _Scan(_checked_layers(query, layers, k), filters or RecallFilters(), at, run_ctx,
                     run_ctx.run_id if run_ctx is not None else None)  # fmt: skip
        redacted = self._redactor.redact(query)  # step 2: only redacted text goes further
        conn = self._conn_factory()
        ids = self._candidates(redacted.text if redacted is not None else "", scan, conn)
        items = [item for item in self._hydrate(ids, conn) if _visible(item, scan)]
        self._fetch_vectors(items, scan)
        hits = self._rank(items, scan, k)
        elapsed = clock.monotonic() - started
        _log.debug("memory.recall.completed", n_candidates=len(ids), n_hits=len(hits),
                   degraded=scan.degraded, duration_ms=round(elapsed * 1000, 3),
                   run_id=scan.run_id)  # fmt: skip
        labels = {"degraded": "true" if scan.degraded else "false"}
        record_histogram(_LATENCY, elapsed, component="memory", labels=labels)
        return RecallResult(hits=hits, degraded=scan.degraded, n_candidates=len(ids))

    def _degrade(self, scan: _Scan, exc: Exception) -> None:
        scan.degraded, scan.query_vector, scan.vectors = True, None, None
        _log.warning("memory.recall.degraded", reason=type(exc).__name__, run_id=scan.run_id)

    def _candidates(self, text: str, scan: _Scan, conn: sqlite3.Connection) -> list[str]:
        statuses: list[Status] = ["active"]
        if scan.filters.include_pending_for is not None:
            statuses.append("pending_approval")
        limits, layers = self._rc.candidates, scan.layers
        scope: dict[str, Sequence[str]] = {"layers": layers, "statuses": statuses}
        try:
            scan.query_vector = self._embedder.embed(text)
            found_ann = self._vectors.search(scan.query_vector, layers, statuses, limits.vector)
            scan.ann = {i: d for i, d in found_ann if is_valid_id(IdKind.MEMORY, i)}
            if dropped := len({i for i, _ in found_ann} - scan.ann.keys()):  # foreign/corrupt rows
                _log.warning("memory.recall.invalid_vector_ids", count=dropped)
        except Exception as exc:  # noqa: BLE001 - any vector-path failure degrades (U07-62)
            self._degrade(scan, exc)
        if (match := fts_query_string(text)) is not None:
            found = ops.fts_candidates(match, **scope, limit=limits.keyword, conn=conn)
            scan.keyword = dict(found)
        by_entity: list[str] = []
        if entity_ids := scan.filters.entity_ids:
            by_entity = ops.entity_candidates(entity_ids, **scope, limit=limits.entity, conn=conn)
        merged = zip_longest(scan.ann or {}, scan.keyword or {}, by_entity)  # round robin
        union = dict.fromkeys(i for group in merged for i in group if i is not None)
        return list(union)[:_MAX_CANDIDATES]

    @staticmethod
    def _hydrate(ids: list[str], conn: sqlite3.Connection) -> list[MemoryItem]:
        items: list[MemoryItem] = []
        for row in ops.get_memory_items(ids, conn=conn) if ids else []:
            try:
                items.append(MemoryItem.model_validate(row))
            except ValidationError:
                _log.warning("memory.recall.invalid_row", error_type="ValidationError")
        return items

    def _fetch_vectors(self, items: list[MemoryItem], scan: _Scan) -> None:
        if scan.degraded or not items:  # all kept items: sim of non-ANN ones (step 7) and MMR
            return
        try:
            scan.vectors = self._vectors.vectors([item.memory_id for item in items])
        except Exception as exc:  # noqa: BLE001 - any vector-path failure degrades (U07-62)
            self._degrade(scan, exc)

    def _sim(self, memory_id: str, scan: _Scan) -> float:
        if scan.degraded or scan.query_vector is None:
            return 0.0
        if (distance := (scan.ann or {}).get(memory_id)) is not None:
            return max(0.0, 1.0 - distance)
        vector = (scan.vectors or {}).get(memory_id)
        return 0.0 if vector is None else max(0.0, float(np.dot(scan.query_vector, vector)))

    def _ent(self, item: MemoryItem, scan: _Scan, build: str | None) -> float:
        wanted, own = scan.filters.entity_ids, _entity_ids(item, scan.filters.entity_type)
        if not wanted or not own:
            return 0.0
        if not set(own).isdisjoint(wanted):
            return 1.0
        rel = self._relatedness
        return 0.5 if rel and build and rel.related_any(build, own, wanted) else 0.0

    def _rank(self, items: list[MemoryItem], scan: _Scan, k: int) -> list[RecallHit]:
        rc, keyword, flt = self._rc, scan.keyword or {}, scan.filters
        build = _current_build(scan.run_ctx) if flt.entity_ids and self._relatedness else None
        max_kw = max((-raw for raw in keyword.values()), default=0.0)
        scored: dict[str, RecallHit] = {}
        for item in items:
            touched = max(item.created_at, item.last_used_at or item.created_at)
            parts = score_candidate(
                sim=self._sim(item.memory_id, scan), kw_raw=keyword.get(item.memory_id),
                max_kw=max_kw, ent=self._ent(item, scan, build),
                age_days=(scan.now - touched) / timedelta(days=1),
                half_life_days=rc.half_life_days[item.kind], confidence=item.confidence,
                is_pending=item.status == "pending_approval", weights=rc.weights,
                conf_floor=rc.conf_floor, rec_floor=rc.rec_floor, degraded=scan.degraded,
            )  # fmt: skip
            if parts["final"] >= rc.min_score:
                scored[item.memory_id] = RecallHit(
                    item=item, score=parts["final"], unconfirmed=item.status == "pending_approval",
                    components=parts)  # type: ignore[arg-type]  # fmt: skip
        vectors = scan.vectors or {}
        cands = [MmrCandidate(i, h.score, vectors.get(i)) for i, h in scored.items()]
        return [scored[i] for i in mmr_select(cands, k, rc.mmr_lambda)]
