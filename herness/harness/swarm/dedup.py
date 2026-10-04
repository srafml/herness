"""Duplicate detection and merge after fan-out (impl 06 U06-102, U06-103; design 06 §5.6).

`dedup_components` is pure: findings are grouped per entity, at most `max_group` per group are
compared (TH06-16), and an edge needs a shared number triple plus either embedding similarity
or an equal query set. `run_dedup` reads the run's `proposed` findings, embeds their claims
(markers masked) and merges each component on the Blackboard; an unavailable embedding model
degrades to `query_set` mode.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

import numpy as np

from herness.core.errors import ConfigError, ModelUnavailable
from herness.core.ids import canonical_json
from herness.core.logging import get_logger
from herness.core.numbers import parse_markers
from herness.enrich.embed import embed_query
from herness.harness.swarm._dedup_read import read_proposed

if TYPE_CHECKING:
    from herness.core.types import Finding
    from herness.harness.swarm.routing import RunEnv

__all__ = ["MAX_GROUP", "dedup_components", "run_dedup"]

type DedupMode = Literal["embedding", "query_set"]

MAX_GROUP: Final = 200
_NUM_MASK: Final = "<num>"
_log = get_logger("harness.swarm")


@dataclass(frozen=True, slots=True)
class _Link:
    """How two compared findings are linked: the mode, its vectors and the cosine floor."""

    mode: DedupMode
    vectors: Mapping[str, np.ndarray]
    cosine: float


@dataclass(frozen=True, slots=True)
class _Item:
    """One compared finding with its number triples and query set (U06-102 step 3)."""

    finding: Finding
    triples: frozenset[tuple[str, str, str]]
    queries: frozenset[str]


def _score_order(f: Finding) -> tuple[float, object, str]:
    return (-f.confidence * len(f.numbers), f.created_at, f.finding_id)


def _item(f: Finding) -> _Item:
    triples = frozenset((n.query_id, n.column, canonical_json(n.row_key)) for n in f.numbers)
    return _Item(f, triples, frozenset(f.query_ids))


def _linked(a: _Item, b: _Item, link: _Link) -> bool:
    """Edge rule of U06-102 step 4 (one pair comparison)."""
    if a.triples.isdisjoint(b.triples):
        return False
    if link.mode == "query_set":
        return a.queries == b.queries
    va = link.vectors.get(a.finding.finding_id)
    vb = link.vectors.get(b.finding.finding_id)
    if va is None or vb is None:
        return False
    return float(np.dot(va, vb)) >= link.cosine


def _find(parent: list[int], i: int) -> int:
    while parent[i] != i:
        parent[i] = parent[parent[i]]
        i = parent[i]
    return i


def _group_components(items: Sequence[_Item], link: _Link) -> list[tuple[str, list[str]]]:
    """Union-find over one ordered entity group; keep = the first member in group order."""
    parent = list(range(len(items)))
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if _linked(items[i], items[j], link):
                ri, rj = _find(parent, i), _find(parent, j)
                parent[max(ri, rj)] = min(ri, rj)  # the root stays the earliest member
    members: dict[int, list[int]] = {}
    for i in range(len(items)):
        members.setdefault(_find(parent, i), []).append(i)
    out: list[tuple[str, list[str]]] = []
    for root, idx in members.items():
        if len(idx) > 1:
            dups = sorted(items[i].finding.finding_id for i in idx if i != root)
            out.append((items[root].finding.finding_id, dups))
    return out


def dedup_components(
    findings: Sequence[Finding],
    *,
    mode: DedupMode,
    vectors: Mapping[str, np.ndarray] | None,
    cosine: float,
    max_group: int = MAX_GROUP,
) -> list[tuple[str, list[str]]]:
    """Duplicate components `(keep id, sorted duplicate ids)` among `findings` (U06-102); pure.

    Groups are visited in `(entity_type, entity_id)` order and only the first `max_group`
    findings of each group (by `(-confidence * len(numbers), created_at, finding_id)`) are
    compared (TH06-16). Embedding mode needs `vectors` (`ConfigError` otherwise); a finding
    without a vector has no embedding edge.
    """
    if mode == "embedding" and vectors is None:
        msg = "embedding mode needs vectors"
        raise ConfigError(msg)
    link = _Link(mode, vectors or {}, cosine)
    groups: dict[tuple[str, str], list[Finding]] = {}
    for f in findings:
        groups.setdefault((f.entity_type, f.entity_id), []).append(f)
    out: list[tuple[str, list[str]]] = []
    for key in sorted(groups):
        ordered = sorted(groups[key], key=_score_order)[:max_group]
        out.extend(_group_components([_item(f) for f in ordered], link))
    return out


def _masked_claim(claim: str) -> str:
    """The claim with every valid marker replaced by `<num>` (U06-103 step 3)."""
    parts: list[str] = []
    pos = 0
    for marker in parse_markers(claim).markers:
        parts += [claim[pos : marker.start], _NUM_MASK]
        pos = marker.end
    return "".join([*parts, claim[pos:]])


def _embed_all(findings: Sequence[Finding]) -> dict[str, np.ndarray]:
    return {f.finding_id: embed_query(_masked_claim(f.claim)) for f in findings}


async def _vectors(env: RunEnv, findings: Sequence[Finding]) -> dict[str, np.ndarray] | None:
    """Claim embeddings, or None (after `harness.dedup.degraded`) when the model is unusable."""
    try:
        return await asyncio.to_thread(_embed_all, findings)
    except (ModelUnavailable, ConfigError) as exc:
        _log.warning(
            "harness.dedup.degraded",
            run_id=env.run.run_id,
            mode="query_set",
            error_type=type(exc).__name__,
        )
        return None


async def run_dedup(env: RunEnv) -> int:
    """Merge duplicate `proposed` findings after fan-out (U06-103); returns the merged count.

    Crosscheck-authored findings are left out. Fast runs use `query_set` mode, others embed
    the claims. Idempotent: merged findings are no longer `proposed`. Every `proposed` finding
    is read, in task-id chunks below the page limit (`_dedup_read`). The returned count is the
    number of duplicate ids passed to `bb.merge`; it may include an id the compare-and-set
    skipped because it had already left `proposed`.
    """
    run = env.run
    findings = await read_proposed(env.bb, run.run_id)
    mode: DedupMode = "query_set" if run.depth == "fast" else "embedding"
    vectors = await _vectors(env, findings) if mode == "embedding" else None
    if vectors is None:
        mode = "query_set"
    components = dedup_components(
        findings, mode=mode, vectors=vectors, cosine=env.cfg.swarm.dedup.cosine
    )
    merged = 0
    for keep, dups in components:
        await env.bb.merge(keep, dups)
        merged += len(dups)
    _log.info("harness.dedup.completed", run_id=run.run_id, merged=merged, mode=mode)
    return merged
