"""Nightly delta scoping of the resolve frame (T03-36 carry-over w50-s11; impl 03 §3.12).

Private sibling of `resolve`. A night with a previous warehouse resolves only the text rows it
cannot carry over: every other text row keeps its `enrich.decision` rows from the previous
warehouse, so `enrich.decision` ends up identical to a full resolve (U03-83 postcondition) at the
cost of the night's delta. A text row (record, entity, content hash) is settled, and carried,
when ALL of the following hold:

* the previous warehouse holds a decision row for each of its applicable questions, for the same
  record and hash, none of them with a review status (human labels and pending reviews re-resolve);
* its hash has no cache row in a part written since the previous night's resolve (the state file
  `_resolve_state.json` in the cache folder lists the parts that existed then, with size and
  mtime; any of them missing or changed makes the whole night a full resolve) and is not a hash
  of tonight's human labels or pending reviews;
* the digest of the resolve inputs (question set, `qmeta`, decider versions, calibration, this
  SQL and the state version) equals the digest the previous night stored, and the previous
  warehouse is the build that stored it.

Anything else (first build, deep depth, resumed run, a stage subset, a changed input, an
unreadable previous warehouse) is a full resolve with `enrich_scope` = every text row. The
state file is written after a successful resolve of any night. Logs carry counts and codes only.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from importlib import resources
from pathlib import Path
from typing import Any, Final

import duckdb
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.dataset as ds
import pyarrow.parquet as pq

from herness.core.errors import HernessError
from herness.core.logging import get_logger
from herness.enrich.cache import CACHE_SCHEMA, DecisionCache, replace_atomic

__all__ = ["DELTA_STAGES", "FULL_SCOPE_SQL", "STATE_NAME", "ResolveDelta"]

STATE_NAME: Final = "_resolve_state.json"
DELTA_STAGES: Final = frozenset(
    {"text", "decide-primary", "decide-escalate", "reasoning", "ensemble"}
)
_CACHE_COLUMNS: Final = [  # the columns resolve_decisions.sql reads from `cache_rows`
    "content_hash", "question", "question_fingerprint", "answer", "distribution",
    "backend_confidence", "decided_at", "decider", "decider_version",
]  # fmt: skip
_LABEL_VIEWS: Final = ("human_latest", "pending_items")
_VERSION: Final = 1
_PREV: Final = "enrich_prev_res"
_COLUMNS: Final = (
    "record_id, question, answer, probability, agreement, decider, decider_version,"
    " question_set_version, content_hash, decided_at, escalated, review_status"
)
FULL_SCOPE_SQL: Final = """CREATE OR REPLACE TEMP TABLE enrich_scope AS
    SELECT record_id, entity, content_hash FROM enrich.text_redacted"""
_PREV_OK_SQL: Final = f"""CREATE OR REPLACE TEMP TABLE enrich_prev_ok AS
SELECT t.record_id, t.entity, t.content_hash FROM enrich.text_redacted AS t
JOIN (SELECT record_id, content_hash, count(*) AS n
      FROM {_PREV}.enrich.decision
      WHERE question_set_version = $qsv AND review_status = 'none'
      GROUP BY record_id, content_hash) AS d
    ON d.record_id = t.record_id AND d.content_hash = t.content_hash
JOIN enrich_expected AS e ON e.entity = t.entity AND e.n = d.n"""  # noqa: S608 - fixed alias
_SETTLED_SQL: Final = """CREATE OR REPLACE TEMP TABLE enrich_settled AS
SELECT p.* FROM enrich_prev_ok AS p
WHERE NOT EXISTS (SELECT 1 FROM enrich_touched AS x WHERE x.content_hash = p.content_hash)"""
_SCOPE_SQL: Final = """CREATE OR REPLACE TEMP TABLE enrich_scope AS
SELECT t.record_id, t.entity, t.content_hash FROM enrich.text_redacted AS t
WHERE NOT EXISTS (SELECT 1 FROM enrich_settled AS s WHERE s.record_id = t.record_id
    AND s.entity = t.entity AND s.content_hash = t.content_hash)"""
_CARRY_FROM: Final = f"""FROM {_PREV}.enrich.decision AS d
JOIN enrich_settled AS s ON s.record_id = d.record_id AND s.content_hash = d.content_hash
WHERE d.question_set_version = $qsv"""
_D_COLUMNS: Final = ", ".join(f"d.{c.strip()}" for c in _COLUMNS.split(","))
_CARRY_SQL: Final = f"INSERT INTO enrich.decision ({_COLUMNS}) SELECT {_D_COLUMNS} {_CARRY_FROM}"
_CARRY_STATS_SQL: Final = f"""SELECT s.entity, count(*), count(*) FILTER (WHERE d.escalated)
{_CARRY_FROM} GROUP BY s.entity"""

_log = get_logger("enrich.resolve")

type Signature = dict[str, list[int]]


def _signature(cache: DecisionCache) -> Signature:
    """`{part path: [size, mtime_ns]}` of every visible cache part."""
    out: Signature = {}
    for part in cache.part_files():
        stat = part.stat()
        out[part.relative_to(cache.directory).as_posix()] = [stat.st_size, stat.st_mtime_ns]
    return out


def _digest(qsv: str, relations: Mapping[str, pa.Table]) -> str:
    """Digest of the resolve inputs that apply to every text row (see the module docstring)."""
    sql = resources.files("herness.enrich").joinpath("sql/resolve_decisions.sql").read_text("utf-8")
    h = hashlib.sha256(f"{_VERSION}|{qsv}|{sql}".encode())
    for name in ("qmeta", "versions", "calib"):
        h.update(name.encode())
        for row in sorted(repr(r) for r in relations[name].to_pylist()):
            h.update(row.encode())
    return h.hexdigest()


def _scan(cache: DecisionCache, files: list[Path], hashes: pa.Array | None) -> pa.Table | None:
    """The resolve columns of the cache rows of `files` (of `hashes` when given)."""
    dataset = cache.dataset(files)
    if dataset is None:
        return None
    keep = None if hashes is None else ds.field("content_hash").isin(hashes)
    return dataset.to_table(columns=_CACHE_COLUMNS, filter=keep)


def _concat(tables: list[pa.Table | None]) -> pa.Table | None:
    held = [t for t in tables if t is not None]
    return pa.concat_tables(held) if held else None


def _read_state(cache: DecisionCache) -> dict[str, Any] | None:
    try:
        doc = json.loads((cache.directory / STATE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


class ResolveDelta:
    """Delta state of one `run_enrichment` call, shared by every `resolve_frame` of the run.

    `eligible` is the caller's verdict that this call is a complete forward run (fresh, every
    cache-writing stage selected, standard depth). The first frame decides `active`; a frame
    whose input digest differs from the first one falls back to a full frame.
    """

    def __init__(self, prev: Path | None = None, *, eligible: bool = False) -> None:
        self.prev = prev
        self.eligible = eligible and prev is not None
        self.digest: str | None = None
        self.active: bool | None = None
        self.reason = "not_eligible"
        self._first: str | None = None
        self._old_parts: set[str] = set()
        self._old_rows: pa.Table | None = None
        self._have: pa.Array | None = None
        self._carried: list[tuple[str, int, int]] = []

    @classmethod
    def for_call(
        cls, prev: Path | None, depth: str, selected: frozenset[str], *, fresh: bool
    ) -> ResolveDelta:
        """Delta state of a `run_enrichment` call: eligible when it is a fresh, standard-depth
        run of every stage that writes the texts or the cache (a resumed run, a stage subset
        or the deep band, which reads the whole frame, resolve in full)."""
        return cls(prev, eligible=fresh and depth != "deep" and selected >= DELTA_STAGES)

    # --- frame ---------------------------------------------------------------------------

    def begin(
        self, wh: duckdb.DuckDBPyConnection, cache: DecisionCache,
        relations: Mapping[str, pa.Table], qsv: str,
    ) -> pa.Table | None:  # fmt: skip
        """Create `enrich_scope`; return the cache rows to register as `cache_rows`, or None
        for the whole cache (a full frame)."""
        self.digest = digest = _digest(qsv, relations)
        if self.active is None:
            self._first = digest
            self.active = self._prepare(wh, cache, relations, qsv)
            _log.info("enrich.resolve.delta", active=self.active, reason=self.reason)
        elif self.active and digest != self._first:
            self.active, self.reason = False, "inputs_changed"
            _log.info("enrich.resolve.delta", active=False, reason=self.reason)
        if not self.active:
            return None
        wh.register("enrich_touched", self._touched(cache, relations))
        try:
            wh.execute(_SETTLED_SQL)
            wh.execute(_SCOPE_SQL)
        finally:
            wh.unregister("enrich_touched")
        hashes = wh.execute("SELECT DISTINCT content_hash FROM enrich_scope").to_arrow_table()
        return self._cache_rows(cache, hashes.column(0).combine_chunks())

    def _cache_rows(self, cache: DecisionCache, hashes: pa.Array) -> pa.Table | None:
        """Cache rows of `hashes`: the old parts are scanned once per run (the scope only grows,
        so a later frame scans them again only for hashes it adds); the parts written tonight
        are read whole."""
        parts = cache.part_files()
        old = [p for p in parts if p.relative_to(cache.directory).as_posix() in self._old_parts]
        new = [p for p in parts if p not in old]
        if self._have is None:
            self._old_rows, self._have = _scan(cache, old, hashes), hashes
        else:
            missing = hashes.filter(pc.invert(pc.is_in(hashes, value_set=self._have)))
            if len(missing):
                extra = _scan(cache, old, missing)
                self._old_rows = _concat([self._old_rows, extra])
                self._have = pa.concat_arrays([self._have, missing])
        return _concat([self._old_rows, _scan(cache, new, None)])

    def _fail(self, reason: str) -> bool:
        self.reason = reason
        return False

    def _state_problem(self, cache: DecisionCache, state: dict[str, Any] | None) -> str | None:
        """The reason the previous night's state cannot be used, or None when it can."""
        if not self.eligible or self.prev is None:
            return "not_eligible"
        if state is None or state.get("version") != _VERSION:
            return "no_state"
        if state.get("digest") != self.digest:
            return "inputs_changed"
        if not self.prev.is_file() or self.prev.name != f"wh-{state.get('build_id')}.duckdb":
            return "prev_mismatch"
        old, current = state.get("parts"), _signature(cache)
        if not isinstance(old, dict) or any(current.get(k) != v for k, v in old.items()):
            return "cache_changed"
        self._old_parts = set(old)
        return None

    def _prepare(
        self, wh: duckdb.DuckDBPyConnection, cache: DecisionCache,
        relations: Mapping[str, pa.Table], qsv: str,
    ) -> bool:  # fmt: skip
        state = _read_state(cache)
        problem = self._state_problem(cache, state)
        if problem is not None or state is None:
            return self._fail(problem or "no_state")
        try:
            return self._build_prev_ok(wh, relations, qsv, rows=state.get("rows"))
        except duckdb.Error as exc:
            _log.warning("enrich.resolve.delta_prev_unavailable", error_class=type(exc).__name__)
            return self._fail("prev_unreadable")

    def _build_prev_ok(
        self, wh: duckdb.DuckDBPyConnection, relations: Mapping[str, pa.Table], qsv: str,
        *, rows: object,
    ) -> bool:  # fmt: skip
        expected: dict[str, int] = {}
        for meta in relations["qmeta"].to_pylist():
            for entity in meta["applies_to"]:
                expected[entity] = expected.get(entity, 0) + 1
        wh.register(
            "enrich_expected", pa.table({"entity": list(expected), "n": list(expected.values())})
        )
        self._attach(wh)
        try:
            row = wh.execute(
                f"SELECT count(*), count(DISTINCT question_set_version),"  # noqa: S608
                f" min(question_set_version) FROM {_PREV}.enrich.decision"
            ).fetchone()
            if row is None or tuple(row) != (rows, 1, qsv):
                return self._fail("prev_rows")
            wh.execute(_PREV_OK_SQL, {"qsv": qsv})
        finally:
            wh.execute(f"DETACH {_PREV}")
            wh.unregister("enrich_expected")
        self.reason = "delta"
        return True

    def _attach(self, wh: duckdb.DuckDBPyConnection) -> None:
        path = str(self.prev).replace("'", "''")
        wh.execute(f"ATTACH '{path}' AS {_PREV} (READ_ONLY)")

    def _touched(self, cache: DecisionCache, relations: Mapping[str, pa.Table]) -> pa.Table:
        """Hashes with a cache row written since the previous night, a human label or a
        pending review (tonight's)."""
        string = CACHE_SCHEMA.field("content_hash").type
        chunks = [
            pq.ParquetFile(part).read(columns=["content_hash"]).column(0)
            for part in cache.part_files()
            if part.relative_to(cache.directory).as_posix() not in self._old_parts
        ]
        chunks += [relations[n].column("content_hash").cast(string) for n in _LABEL_VIEWS]
        return pa.table(
            {
                "content_hash": pc.unique(
                    pa.chunked_array([c for k in chunks for c in k.chunks], type=pa.string())
                )
            }
        )

    # --- resolve -------------------------------------------------------------------------

    def carry(self, wh: duckdb.DuckDBPyConnection, qsv: str) -> int:
        """Insert the settled rows of the previous warehouse into `enrich.decision`; keep
        their per-entity counts for `merge_stats`. Returns the rows carried (0 on a full frame)."""
        self._carried = []
        if not self.active:
            return 0
        self._attach(wh)
        try:
            self._carried = [
                (str(e), int(n), int(x))
                for e, n, x in wh.execute(_CARRY_STATS_SQL, {"qsv": qsv}).fetchall()
            ]
            wh.execute(_CARRY_SQL, {"qsv": qsv})
        finally:
            wh.execute(f"DETACH {_PREV}")
        return sum(n for _, n, _ in self._carried)

    def merge_stats(
        self, stats: list[tuple[str, int, int, int]]
    ) -> list[tuple[str, int, int, int]]:
        """Add the carried rows (all final, all in scope) to `(entity, final, escalated, scope)`."""
        merged = {e: (f, x, s) for e, f, x, s in stats}
        for entity, n, esc in self._carried:
            f, x, s = merged.get(entity, (0, 0, 0))
            merged[entity] = (f + n, x + esc, s + n)
        return [(e, *merged[e]) for e in sorted(merged)]

    def merge_coverage(self, rows: list[tuple[Any, ...]]) -> list[tuple[str, int, int]]:
        """Add the carried rows to `(entity, final, in scope)` rows."""
        merged = {str(e): (f, s) for e, f, s in rows}
        for entity, n, _ in self._carried:
            f, s = merged.get(entity, (0, 0))
            merged[entity] = (f + n, s + n)
        return [(e, *merged[e]) for e in sorted(merged)]

    def commit(self, wh: duckdb.DuckDBPyConnection, cache: DecisionCache, build_id: str) -> None:
        """Write the state file after a successful resolve (call after the cache compaction)."""
        if self.digest is None:
            return
        row = wh.execute("SELECT count(*) FROM enrich.decision").fetchone()
        doc = {
            "version": _VERSION,
            "build_id": build_id,
            "digest": self.digest,
            "rows": int(row[0]) if row else 0,
            "parts": _signature(cache),
        }
        try:
            replace_atomic(cache.directory / STATE_NAME,
                           lambda tmp: tmp.write_text(json.dumps(doc), encoding="utf-8"),
                           kind="resolve state")  # fmt: skip
        except HernessError as exc:  # the next night then resolves in full
            _log.warning("enrich.resolve.state_unwritten", error_class=type(exc).__name__)
