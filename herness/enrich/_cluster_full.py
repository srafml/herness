"""Full recluster of the `cluster` stage (impl 03 U03-105 steps 3-4; design 03 §5.3): a
private sibling of `cluster_stage`, split off for its 390-line budget (T03-25 spec note).

PCA (reused while the algorithm is current), projection, prototype k-means, HDBSCAN,
assignment, pruning, streamed centroids and stable ids; the `assigned` snapshot and
`members.parquet` are saved before the caller writes the warehouse. Step 4 builds the
c-TF-IDF terms and the naming candidates. Logs carry counts only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final, NamedTuple

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

from herness.core.errors import ConfigError, FatalError
from herness.core.ids import new_ulid
from herness.core.jobs import JobContext
from herness.core.logging import get_logger
from herness.enrich import cluster as num
from herness.enrich._cluster_io import (
    ClusterSnapshot,
    SnapshotMeta,
    VectorSource,
    centroid_dim,
    empty_centroids,
    fixed_list,
    matrix,
)
from herness.enrich.cluster_describe import (
    NamingCandidate,
    describe_clusters,
    needs_naming,
    representative_texts,
    top_terms_ctfidf,
)
from herness.enrich.cluster_ids import IdMatch, compute_centroids, match_cluster_ids
from herness.enrich.gpu import YieldRequested, release_cuda
from herness.enrich.layout import EnrichPaths
from herness.enrich.settings import ClusteringSettings

__all__ = ["ALGORITHM_BASE", "CLUSTER_SEED", "Full", "Run", "cluster_rows", "full_recluster"]

ALGORITHM_BASE: Final = "proto-hdbscan-v1-pca64"
CLUSTER_SEED: Final = 1729
STAGE: Final = "cluster"
ALL: Final = ("entity = 'incident'",)
TEXT_SAMPLE: Final = 2_000
_CARRIED: Final = ("named_centroid", "named_size", "label", "root_cause_category")
_SAMPLED: Final = "_cluster_full_members"
_SAMPLE_SQL: Final = f"""SELECT m.cluster_id, m.record_id, t.text, t.content_hash
    FROM {_SAMPLED} AS m
    JOIN enrich.text_redacted AS t ON t.record_id = m.record_id AND t.entity = 'incident'
    QUALIFY row_number() OVER (
        PARTITION BY m.cluster_id ORDER BY sha256(m.cluster_id || m.record_id)) <= ?
    ORDER BY m.cluster_id, sha256(m.cluster_id || m.record_id)"""  # noqa: S608 - fixed name

_log = get_logger("enrich.cluster")


class Run(NamedTuple):
    """The fixed parts of one stage run."""

    wh: duckdb.DuckDBPyConnection
    source: VectorSource
    paths: EnrichPaths
    cl: ClusteringSettings
    build_id: str
    device: str
    ctx: JobContext
    now: datetime
    win: pa.Table  # in-window incidents by record_id: record_id, content_hash, sample_rank


class Full(NamedTuple):
    """A full run's snapshot and members, and the projections step 4 ranks examples with."""

    snap: ClusterSnapshot
    members: pa.Table
    keep: np.ndarray  # window positions of the projected rows (ascending)
    x: np.ndarray  # their projections


def _new_cluster_id() -> str:
    return "cl_" + new_ulid()


def full_recluster(run: Run, prev: ClusterSnapshot | None, reason: str) -> Full:
    """Steps 3a-3i: saves members.parquet and the `assigned` snapshot, then honours a
    yield request. FatalError on CUDA OOM; ConfigError when too few vectors to cluster."""
    import torch  # noqa: PLC0415 - lazy: importing this module must never load torch

    cl, heartbeat = run.cl, run.ctx.heartbeat
    try:
        pca = _pca(run, prev)
        heartbeat(STAGE)
        keep, x = run.source.project(pca, ALL, run.device)
        k = min(max(len(keep) // cl.proto_per, cl.k_min), cl.k_max, len(keep))
        if k < max(cl.min_cluster_size, cl.min_samples + 1):
            msg = f"cluster stage: {len(keep)} incident vectors in the window, too few"
            raise ConfigError(msg)
        heartbeat(STAGE)
        km = num.spherical_kmeans(x, k=k, iters=cl.iters, seed=CLUSTER_SEED, device=run.device)
    except torch.cuda.OutOfMemoryError as exc:
        release_cuda()
        msg = "cluster stage: cuda out of memory"
        raise FatalError(msg) from exc
    heartbeat(STAGE)
    labels, probs = num.hdbscan_prototypes(
        km.prototypes, min_cluster_size=cl.min_cluster_size, min_samples=cl.min_samples
    )
    sims = {"assign_min_sim": cl.assign_min_sim, "full_sim": cl.full_sim}
    idx, prob = num.assign_members(km.assign, km.sim, labels, probs, **sims)
    pruned = num.prune_clusters(idx, min_incidents=cl.min_incidents)
    heartbeat(STAGE)
    point = np.full(run.win.num_rows, -1, dtype=np.int64)
    point[keep] = pruned
    batches = ((vectors, point[pos]) for pos, vectors in run.source.batches(ALL))
    centroids, sizes = compute_centroids(batches, n_clusters=int(pruned.max(initial=-1)) + 1)
    heartbeat(STAGE)
    match = _match(run, prev, centroids)
    member = pruned >= 0
    members = pa.table({
        "record_id": run.win.column("record_id").take(keep[member]),
        "cluster_id": pa.array([match.ids[i] for i in pruned[member]], pa.string()),
        "membership_prob": pa.array(prob[member], pa.float64()),
    })  # fmt: skip
    of_proto = _proto_clusters(labels, idx, pruned)
    proto = pa.table({
        "proto_idx": pa.array(np.arange(k), pa.int32()),
        "cluster_id": pa.array([match.ids[i] if i >= 0 else None for i in of_proto], pa.string()),
        "hdbscan_prob": pa.array(probs, pa.float64()),
        "weight": pa.array(km.counts, pa.int64()),
    })  # fmt: skip
    meta = SnapshotMeta("full", "assigned", run.now, run.now, len(keep), k)
    snap = ClusterSnapshot(
        f"{ALGORITHM_BASE}-{pca.fit_id}", run.build_id, meta, pca, km.prototypes, proto,
        _centroid_table(prev, match, centroids, sizes, run.now),
    )  # fmt: skip
    snap.save_members(run.paths, members)
    snap.save(run.paths)
    _log.info("enrich.cluster.full_completed", n=len(keep), k=k, clusters=len(match.ids),
              inherited=match.inherited, revived=match.revived, created=match.created,
              retired=len(match.retired), reason=reason)  # fmt: skip
    heartbeat(STAGE)
    if run.ctx.should_yield():
        raise YieldRequested(STAGE)
    return Full(snap, members, keep, x)


def _pca(run: Run, prev: ClusterSnapshot | None) -> num.PcaModel:
    """The previous fit while its algorithm is current, else a fit on the lowest
    `sha256(record_id)` sample of `pca_sample` in-window vectors."""
    if prev is not None and prev.algorithm_version.startswith(ALGORITHM_BASE):
        return prev.pca
    rank = run.win.column("sample_rank").to_numpy()
    sample = run.source.sample(rank, min(run.cl.pca_sample, len(rank)), ALL)
    return num.fit_pca(sample, dims=run.cl.pca_dims, seed=CLUSTER_SEED)


def _proto_clusters(labels: np.ndarray, idx: np.ndarray, pruned: np.ndarray) -> np.ndarray:
    """Pruned cluster index per prototype (-1: noise or pruned). The lookup has one spare
    slot at the end so that label -1 reads -1."""
    lookup = np.full(int(labels.max(initial=-1)) + 2, -1, dtype=np.int64)
    member = idx >= 0
    lookup[idx[member]] = pruned[member]
    out: np.ndarray = lookup[labels]
    return out


def _match(run: Run, prev: ClusterSnapshot | None, centroids: np.ndarray) -> IdMatch:
    table = empty_centroids() if prev is None else prev.centroids
    dim = centroid_dim(table)
    live = table.column("retired_at").is_null()
    act, ret = table.filter(live), table.filter(pc.invert(live))
    cl = run.cl
    return match_cluster_ids(
        centroids,
        prev_active=(act.column("cluster_id").to_pylist(), matrix(act.column("centroid"), dim)),
        prev_retired=(ret.column("cluster_id").to_pylist(), matrix(ret.column("centroid"), dim),
                      ret.column("retired_at").to_pylist()),
        now=run.now, match_cos=cl.match_cos, revive_cos=cl.revive_cos,
        revive_days=cl.revive_days, new_id=_new_cluster_id,
    )  # fmt: skip


def _centroid_table(
    prev: ClusterSnapshot | None, match: IdMatch, centroids: np.ndarray, sizes: np.ndarray,
    now: datetime,
) -> pa.Table:  # fmt: skip
    """New clusters (names carried for inherited and revived ids) then the retired ones
    (newly retired get `retired_at = now`)."""
    base = empty_centroids() if prev is None else prev.centroids
    index = {cid: i for i, cid in enumerate(base.column("cluster_id").to_pylist())}
    at = pa.array([index.get(c) for c in match.ids], pa.int64())
    carried = base.select(list(_CARRIED)).take(at)
    ts = base.schema.field("retired_at").type
    new = pa.table({
        "cluster_id": pa.array(match.ids, pa.string()),
        "centroid": fixed_list(centroids),
        "size": pa.array(sizes, pa.int64()),
        **{name: carried.column(name) for name in _CARRIED},
        "retired_at": pa.nulls(len(match.ids), ts),
    })  # fmt: skip
    taken = set(match.ids)
    old = base.take(pa.array([i for cid, i in index.items() if cid not in taken], pa.int64()))
    stamp = pc.coalesce(old.column("retired_at"), pa.scalar(now, ts))
    old = old.set_column(old.schema.get_field_index("retired_at"), "retired_at", stamp)
    return pa.concat_tables([new, old.select(new.column_names).cast(new.schema)])


def cluster_rows(run: Run, full: Full) -> tuple[pa.Table, list[NamingCandidate]]:
    """Step 4: `enrich.cluster` rows (descriptors, top terms, inherited label and category;
    texts sampled <= 2,000 per cluster by lowest `sha256(cluster_id + record_id)`) and the
    naming candidates."""
    snap = full.snap
    run.wh.register(_SAMPLED, full.members)
    try:
        desc = describe_clusters(run.wh, members_view=_SAMPLED)
        sample = run.wh.execute(_SAMPLE_SQL, [TEXT_SAMPLE]).to_arrow_table()
    finally:
        run.wh.unregister(_SAMPLED)
    terms, naming = _naming(run, full, desc, sample)
    ids = desc.column("cluster_id").to_pylist()
    known = {cid: i for i, cid in enumerate(snap.centroids.column("cluster_id").to_pylist())}
    carry = snap.centroids.take(pa.array([known[c] for c in ids], pa.int64()))
    rows = pa.table({
        "cluster_id": desc.column("cluster_id"),
        "label": carry.column("label"),
        "root_cause_category": carry.column("root_cause_category"),
        **{c: desc.column(c) for c in ("size", "first_seen", "last_seen")},
        "top_terms": pa.array([terms.get(c, []) for c in ids], pa.list_(pa.string())),
        "service_ids": desc.column("service_ids"),
        "algorithm_version": pa.array([snap.algorithm_version] * len(ids), pa.string()),
    })  # fmt: skip
    return rows, naming


def _naming(
    run: Run, full: Full, desc: pa.Table, sample: pa.Table
) -> tuple[dict[str, list[str]], list[NamingCandidate]]:
    """Step 4: top terms per cluster (c-TF-IDF over the text sample) and candidates for the
    clusters `needs_naming` flags; examples are ranked in projection space against the
    mean of the sampled members' projections."""
    groups: dict[str, list[int]] = {}
    for i, cid in enumerate(sample.column("cluster_id").to_pylist()):
        groups.setdefault(cid, []).append(i)
    texts, hashes = sample.column("text").to_pylist(), sample.column("content_hash").to_pylist()
    terms = top_terms_ctfidf({cid: [texts[i] for i in rows] for cid, rows in groups.items()})
    cent, how = full.snap.centroids, run.cl.naming
    known = {cid: i for i, cid in enumerate(cent.column("cluster_id").to_pylist())}
    vectors = matrix(cent.column("centroid"), centroid_dim(cent))
    named_sizes, named_vecs = cent.column("named_size").to_pylist(), cent.column("named_centroid")
    at = _projection_rows(run, full, sample)
    out: list[NamingCandidate] = []
    columns = (desc.column(c).to_pylist() for c in ("cluster_id", "size", "service_ids"))
    for cid, size, services in zip(*columns, strict=True):
        i = known[cid]
        named = None
        if named_sizes[i] is not None:
            named = (named_vecs[i].values.to_numpy(zero_copy_only=False), named_sizes[i])
        if not needs_naming(vectors[i], size, named, rename_cos=run.cl.rename_cos):
            continue
        picked = [r for r in groups.get(cid, []) if at[r] >= 0]
        x = full.x[at[picked]]
        examples = representative_texts(
            x, [hashes[r] for r in picked], [texts[r] for r in picked], x.sum(0),
            n=how.examples, max_chars=how.example_chars,
        )  # fmt: skip
        out.append(NamingCandidate(cid, size, terms.get(cid, []), services, examples))
    return terms, out


def _projection_rows(run: Run, full: Full, sample: pa.Table) -> np.ndarray:
    """Row of `full.x` per sample row; -1 when the record has no projected vector."""
    ids = run.win.column("record_id").combine_chunks()
    pos = pc.fill_null(pc.index_in(sample.column("record_id"), value_set=ids), -1)
    wanted = pos.to_numpy(zero_copy_only=False).astype(np.int64)
    row = np.searchsorted(full.keep, wanted)
    found = row < len(full.keep)
    found[found] = full.keep[row[found]] == wanted[found]
    out: np.ndarray = np.where(found & (wanted >= 0), row, -1)
    return out
