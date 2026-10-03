"""Cluster stage: snapshots, cadence, incremental and full runs, finalize (impl 03 U03-103 ...
U03-106; design 03 §5.3, §5.4; F03-09, F03-10).

Vectors stream from LanceDB in chunks of `CHUNK` rows; only their 64-d projections stay in
memory. A full run (private sibling `_cluster_full`) saves `members.parquet` and an
`assigned` snapshot before any warehouse write, so a crash or yield rerun of the same build
resumes from them; only `finalize_clusters` moves `CURRENT`. Cluster ids come only from
`match_cluster_ids`; an incremental run changes none. Logs carry counts only. The caller
holds the GPU class (R-43) and injects `device`. Snapshot IO lives in `_cluster_io`.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final, Literal, Protocol

import duckdb
import numpy as np
import pyarrow as pa

from herness.core import time as clock
from herness.core.errors import ConfigError, SchemaViolation
from herness.core.jobs import JobContext
from herness.core.logging import get_logger
from herness.core.resilience.metrics import record_gauge
from herness.core.types import QuestionSet
from herness.enrich._cluster_full import (
    ALGORITHM_BASE,
    ALL,
    CLUSTER_SEED,
    STAGE,
    Full,
    Run,
    cluster_rows,
    full_recluster,
)
from herness.enrich._cluster_io import ClusterSnapshot, SnapshotMeta, VectorSource, named_centroids
from herness.enrich.cluster import assign_members
from herness.enrich.cluster_describe import NameResult, NamingCandidate, describe_clusters
from herness.enrich.embed_stage import FILTER_MAX, lance_filter_in
from herness.enrich.layout import EnrichPaths
from herness.enrich.settings import ClusteringSettings
from herness.store.vectors import VectorStore

__all__ = [
    "ALGORITHM_BASE",
    "CLUSTER_SEED",
    "DRIFT_MIN_NEW",
    "ClusterSnapshot",
    "ClusterStageResult",
    "SnapshotMeta",
    "finalize_clusters",
    "is_full_recluster_due",
    "run_cluster_stage",
]

DRIFT_MIN_NEW: Final = 500  # open item OI-09
CHUNK = 262_144  # vectors per streamed chunk (U03-105); a module value tests lower
_PREV: Final = "cluster_prev"
_WIN: Final = "_cluster_window"
_MEMBERS: Final = "_cluster_members"
_ROWS: Final = "_cluster_rows"
_NAMES: Final = "_cluster_names"
_FIRST_PRINTABLE: Final = 0x20
_NAMES_SCHEMA: Final = pa.schema(
    [(c, pa.string()) for c in ("cluster_id", "label", "root_cause_category")]
    + [("auto", pa.bool_())]
)

_WINDOW_SQL: Final = """SELECT i.record_id, t.content_hash,
        row_number() OVER (ORDER BY sha256(i.record_id)) - 1 AS sample_rank
    FROM core.incident AS i
    JOIN enrich.text_redacted AS t ON t.record_id = i.record_id AND t.entity = 'incident'
    WHERE i.opened_at >= ? ORDER BY i.record_id"""
_SAME: Final = f"""EXISTS (SELECT 1 FROM {_PREV}.enrich.text_redacted AS p WHERE p.entity =
    'incident' AND p.record_id = w.record_id AND p.content_hash = w.content_hash)"""  # noqa: S608
_PROBES: Final = [f"SELECT {c} FROM {_PREV}.enrich.{t} LIMIT 0" for t, c in (  # noqa: S608
    ("text_redacted", "record_id, entity, content_hash"),
    ("cluster_member", "record_id, cluster_id, membership_prob"),
    ("cluster", "cluster_id, label, root_cause_category, top_terms, algorithm_version"),
)]  # fmt: skip
_CHANGED_SQL: Final = f"SELECT w.record_id FROM {_WIN} AS w WHERE NOT {_SAME} ORDER BY 1"  # noqa: S608
_KEPT_SQL: Final = f"""SELECT m.record_id, m.cluster_id, m.membership_prob
    FROM {_PREV}.enrich.cluster_member AS m JOIN {_WIN} AS w ON w.record_id = m.record_id
    WHERE {_SAME}"""  # noqa: S608 - fixed names
_INC_ROWS_SQL: Final = f"""SELECT p.cluster_id, p.label, p.root_cause_category,
        coalesce(d.size, 0) AS size, d.first_seen, d.last_seen, p.top_terms,
        coalesce(d.service_ids, []::VARCHAR[]) AS service_ids, p.algorithm_version
    FROM {_PREV}.enrich.cluster AS p LEFT JOIN {_ROWS} AS d ON d.cluster_id = p.cluster_id
    ORDER BY p.cluster_id"""  # noqa: S608 - fixed names
# Auto-named: a NameResult with source "auto", or (not renamed now) an "auto: " label; a
# row with no label at all gets the auto label from its stored top terms.
_FINAL_SQL: Final = f"""UPDATE enrich.cluster AS c SET label = v.label,
        root_cause_category = v.root_cause_category
    FROM (SELECT e.cluster_id,
            coalesce(n.label, e.label,
                left('auto: ' || array_to_string(e.top_terms[1:3], ' / '), 60)) AS label,
            CASE WHEN coalesce(n.auto, e.label IS NULL OR starts_with(e.label, 'auto: '))
                THEN CASE WHEN ? THEN a.answer END
                WHEN n.cluster_id IS NULL THEN e.root_cause_category
                ELSE n.root_cause_category END AS root_cause_category
        FROM enrich.cluster AS e LEFT JOIN {_NAMES} AS n ON n.cluster_id = e.cluster_id
        LEFT JOIN (SELECT m.cluster_id, d.answer FROM enrich.cluster_member AS m
            JOIN enrich.decision AS d ON d.record_id = m.record_id
            WHERE d.question = 'root_cause' AND d.answer IS NOT NULL
            GROUP BY m.cluster_id, d.answer QUALIFY row_number() OVER (
                PARTITION BY m.cluster_id ORDER BY count(*) DESC, d.answer) = 1
        ) AS a ON a.cluster_id = e.cluster_id) AS v
    WHERE c.cluster_id = v.cluster_id"""  # noqa: S608 - fixed names

_log = get_logger("enrich.cluster")


class _Report(Protocol):
    """Counters the stage mutates. T03-28: retype to StageReport (U03-142)."""

    rows: int


class _Config(Protocol):  # `DecisionsConfig` satisfies it
    @property
    def clustering(self) -> ClusteringSettings: ...


@dataclasses.dataclass(frozen=True, eq=False)
class ClusterStageResult:
    """What `finalize_clusters` needs: the run's snapshot, naming candidates and kind."""

    snapshot: ClusterSnapshot
    naming: list[NamingCandidate]
    kind: Literal["incremental", "full"]


def is_full_recluster_due(  # noqa: PLR0913 - U03-104's keyword-only signature is binding
    prev: SnapshotMeta | None, *, prev_algorithm_version: str | None, algorithm_base: str,
    now: datetime, full_every_days: int, forced: bool, drift_share: float | None,
    drift_threshold: float, n_new: int,
) -> tuple[bool, str]:  # fmt: skip
    """Cadence rule (U03-104): the first matching reason of forced, no_snapshot,
    algorithm_changed, age, drift; `(False, "none")` otherwise."""
    if forced:
        return True, "forced"
    if prev is None:
        return True, "no_snapshot"
    if prev_algorithm_version is None or not prev_algorithm_version.startswith(algorithm_base):
        return True, "algorithm_changed"
    if now - prev.full_at >= timedelta(days=full_every_days):
        return True, "age"
    drifted = drift_share is not None and drift_share > drift_threshold
    if drifted and n_new >= DRIFT_MIN_NEW:
        return True, "drift"
    return False, "none"


def run_cluster_stage(  # noqa: PLR0913 - U03-105's keyword-only signature is binding
    wh: duckdb.DuckDBPyConnection, *, prev_warehouse: Path | None, paths: EnrichPaths,
    cfg: _Config, build_id: str, force_full: bool, device: str, ctx: JobContext,
    report: _Report,
) -> ClusterStageResult:  # fmt: skip
    """Nightly incremental assignment or full recluster (U03-105); writes `enrich.cluster`
    and `enrich.cluster_member`. A full run leaves an `assigned` snapshot for `build_id`.

    FatalError on CUDA OOM at the minimum chunk; StoreBusy on a LanceDB conflict; ConfigError
    on too few window vectors; YieldRequested after the assigned snapshot is saved.
    """
    rerun = _usable(lambda: _assigned(paths, build_id))
    now = clock.now() if rerun is None else rerun[0].meta.created_at  # a rerun keeps its window
    since = now - timedelta(days=cfg.clustering.window_days)
    win = wh.execute(_WINDOW_SQL, [since]).to_arrow_table()
    source = VectorSource(VectorStore(paths.vectors_dir()).table("ticket_embedding"), win, CHUNK)
    run = Run(wh, source, paths, cfg.clustering, build_id, device, ctx, now, win)
    if rerun is not None:  # crash or yield rerun: resume from members.parquet
        snap, members = rerun
        keep, x = source.project(snap.pca, ALL, device)
        _log.info("enrich.cluster.resumed", n=snap.meta.n, k=snap.meta.k)
        return _finish_full(run, Full(snap, members, keep, x), report)
    prev = _usable(lambda: ClusterSnapshot.load_current(paths))
    if force_full or prev is None:
        reason = "forced" if force_full else "no_snapshot"
    elif not _attach(wh, prev_warehouse):
        reason = "no_prev_warehouse"
    else:
        try:
            outcome = _try_incremental(run, prev, report)
        finally:
            wh.execute(f"DETACH {_PREV}")
        if isinstance(outcome, ClusterStageResult):
            return outcome
        reason = outcome
    return _finish_full(run, full_recluster(run, prev, reason), report)


def _assigned(paths: EnrichPaths, build_id: str) -> tuple[ClusterSnapshot, pa.Table] | None:
    snap = ClusterSnapshot.load_assigned(paths, build_id)
    return None if snap is None else (snap, snap.load_members(paths))


def _usable[T](load: Callable[[], T | None]) -> T | None:
    """The loaded snapshot, or None (so a full recluster follows) when it is corrupt."""
    try:
        return load()
    except ConfigError as exc:
        _log.warning("enrich.cluster.snapshot_unusable", file=str(exc.context.get("file", "")))
        return None


def _attach(wh: duckdb.DuckDBPyConnection, path: Path | None) -> bool:
    """`ATTACH <path> AS cluster_prev (READ_ONLY)` holding the enrich tables the run reads;
    False (a full recluster) when it cannot attach or lacks them (an older or foreign file)."""
    value = "" if path is None else str(path)
    if not value or any(ord(ch) < _FIRST_PRINTABLE for ch in value):
        return False
    try:  # ATTACH takes no bound parameter: the path is quoted as a string literal
        wh.execute(f"ATTACH '{value.replace("'", "''")}' AS {_PREV} (READ_ONLY)")
        for probe in _PROBES:
            wh.execute(probe)
    except duckdb.Error as exc:
        wh.execute(f"DETACH DATABASE IF EXISTS {_PREV}")
        _log.warning("enrich.cluster.prev_unavailable", error_type=type(exc).__name__)
        return False
    return True


def _with_window[T](run: Run, sql: str, read: Callable[[duckdb.DuckDBPyConnection], T]) -> T:
    run.wh.register(_WIN, run.win)
    try:
        return read(run.wh.execute(sql))
    finally:
        run.wh.unregister(_WIN)


# --- incremental (U03-105 step 2) ------------------------------------------------------------


def _try_incremental(run: Run, prev: ClusterSnapshot, report: _Report) -> ClusterStageResult | str:
    """The incremental result, or the reason a full recluster is due instead."""
    due, reason = _due(run, prev, None, 0)
    if due:
        return reason
    new, sim = _assign_new(run, prev)
    drift = float(np.mean(sim < run.cl.assign_min_sim)) if sim.size else 0.0
    record_gauge("herness_enrich_cluster_drift_ratio", drift, component="enrich")
    due, reason = _due(run, prev, drift, int(sim.size))
    if not due:
        return _finish_incremental(run, prev, new, drift, report)
    _log.warning("enrich.cluster.drift_detected", drift_share=drift, n_new=int(sim.size))
    return reason


def _due(run: Run, prev: ClusterSnapshot, drift: float | None, n_new: int) -> tuple[bool, str]:
    return is_full_recluster_due(
        prev.meta, prev_algorithm_version=prev.algorithm_version, algorithm_base=ALGORITHM_BASE,
        now=run.now, full_every_days=run.cl.full_every_days, forced=False, drift_share=drift,
        drift_threshold=run.cl.drift_share, n_new=n_new,
    )  # fmt: skip


def _lance_ok(record_id: str) -> bool:
    try:
        lance_filter_in("record_id", [record_id])
    except SchemaViolation:  # never embedded: the embed stage skips such ids too
        return False
    return True


def _assign_new(run: Run, prev: ClusterSnapshot) -> tuple[pa.Table, np.ndarray]:
    """Changed in-window incidents assigned to the nearest stored prototype (chunked
    `x @ prototypes.T`): (member rows, similarity of every changed vector)."""
    rows = _with_window(run, _CHANGED_SQL, lambda cur: cur.fetchall())
    changed = [r for (r,) in rows if _lance_ok(r)]
    where = [lance_filter_in("record_id", changed[i : i + FILTER_MAX])
             for i in range(0, len(changed), FILTER_MAX)]  # fmt: skip
    keep, x = run.source.project(prev.pca, where, run.device)
    assign, sim = np.zeros(len(x), np.int64), np.zeros(len(x), np.float32)
    for start in range(0, len(x), CHUNK):
        s = x[start : start + CHUNK] @ prev.prototypes.T
        assign[start : start + CHUNK], sim[start : start + CHUNK] = s.argmax(1), s.max(1)
    proto = prev.proto_cluster.sort_by("proto_idx")
    of_proto = proto.column("cluster_id").to_pylist()
    names = sorted({c for c in of_proto if c is not None})
    code = {c: i for i, c in enumerate(names)}
    labels = np.array([-1 if c is None else code[c] for c in of_proto], dtype=np.int64)
    idx, prob = assign_members(
        assign, sim, labels, proto.column("hdbscan_prob").to_numpy(),
        assign_min_sim=run.cl.assign_min_sim, full_sim=run.cl.full_sim,
    )  # fmt: skip
    member = idx >= 0
    new = pa.table({
        "record_id": run.win.column("record_id").take(keep[member]),
        "cluster_id": pa.array([names[i] for i in idx[member]], pa.string()),
        "membership_prob": pa.array(prob[member], pa.float64()),
    })  # fmt: skip
    return new, sim


def _finish_incremental(
    run: Run, prev: ClusterSnapshot, new: pa.Table, drift: float, report: _Report
) -> ClusterStageResult:
    """Copy unchanged memberships and prev cluster rows, add the new assignments, recompute
    the descriptors (U03-105 step 2e); ids and labels are the previous build's."""
    kept = _with_window(run, _KEPT_SQL, lambda cur: cur.to_arrow_table())
    members = pa.concat_tables([kept, new.cast(kept.schema)]).sort_by("record_id")
    run.wh.register(_ROWS, _describe(run.wh, members))
    try:
        rows = run.wh.execute(_INC_ROWS_SQL).to_arrow_table()
    finally:
        run.wh.unregister(_ROWS)
    _write(run.wh, rows, members)
    _log.info("enrich.cluster.incremental_completed", assigned=new.num_rows,
              members=members.num_rows, clusters=rows.num_rows, drift_share=drift)  # fmt: skip
    _done(run, report, rows.num_rows, members.num_rows)
    return ClusterStageResult(snapshot=prev, naming=[], kind="incremental")


# --- full run: descriptors, naming and writes (U03-105 steps 4-5) ----------------------------


def _finish_full(run: Run, full: Full, report: _Report) -> ClusterStageResult:
    """Steps 4-5 of a full run: cluster rows and naming candidates, then the writes."""
    rows, naming = cluster_rows(run, full)
    _write(run.wh, rows, full.members)
    _done(run, report, rows.num_rows, full.members.num_rows)
    return ClusterStageResult(snapshot=full.snap, naming=naming, kind="full")


def _describe(wh: duckdb.DuckDBPyConnection, members: pa.Table) -> pa.Table:
    """Descriptors of `members` (U03-98) through a registered view."""
    wh.register(_MEMBERS, members)
    try:
        return describe_clusters(wh, members_view=_MEMBERS)
    finally:
        wh.unregister(_MEMBERS)


def _write(wh: duckdb.DuckDBPyConnection, clusters: pa.Table, members: pa.Table) -> None:
    """Replace `enrich.cluster` and `enrich.cluster_member` in one transaction."""
    wh.register(_ROWS, clusters)
    wh.register(_MEMBERS, members)
    try:
        wh.begin()
        for table, view in (("cluster", _ROWS), ("cluster_member", _MEMBERS)):
            wh.execute(f"DELETE FROM enrich.{table}")  # noqa: S608 - fixed names
            wh.execute(f"INSERT INTO enrich.{table} BY NAME SELECT * FROM {view}")  # noqa: S608
        wh.commit()
    except duckdb.Error as exc:
        wh.rollback()
        msg = f"cluster stage write: {type(exc).__name__}"
        raise SchemaViolation(msg) from None
    finally:
        wh.unregister(_ROWS)
        wh.unregister(_MEMBERS)


def _done(run: Run, report: _Report, clusters: int, members: int) -> None:
    report.rows += members
    record_gauge("herness_enrich_clusters_count", clusters, component="enrich")
    run.ctx.heartbeat(STAGE)


# --- U03-106 ---------------------------------------------------------------------------------


def finalize_clusters(
    wh: duckdb.DuckDBPyConnection, *, result: ClusterStageResult,
    names: Mapping[str, NameResult], qs: QuestionSet, paths: EnrichPaths,
) -> None:  # fmt: skip
    """Apply names, set auto-named clusters' root cause to their members' majority `root_cause`
    answer (ties: label ascending), then for a full run save the named centroids and publish
    the snapshot (`CURRENT` last). SchemaViolation on a warehouse error."""
    rows = [{"cluster_id": cid, "label": n.label, "root_cause_category": n.root_cause_category,
             "auto": n.source == "auto"} for cid, n in sorted(names.items())]  # fmt: skip
    wh.register(_NAMES, pa.Table.from_pylist(rows, schema=_NAMES_SCHEMA))
    try:
        wh.execute(_FINAL_SQL, [any(q.id == "root_cause" for q in qs.questions)])
        final = wh.execute("SELECT cluster_id, label, root_cause_category FROM enrich.cluster")
        labels = {cid: (label, category) for cid, label, category in final.fetchall()}
    except duckdb.Error as exc:
        msg = f"cluster finalize: {type(exc).__name__}"
        raise SchemaViolation(msg) from None
    finally:
        wh.unregister(_NAMES)
    if result.kind == "full":
        snap = result.snapshot
        snap.save_centroids(paths, named_centroids(snap.centroids, set(names), labels))
        snap.mark_final(paths)
