"""Decider comparison and the synthetic truth check of the classifier eval (U11-67; T11-35).

Private sibling of `herness.eval.classifier` (§2 module map): flow F11-09 step 5 (every
decider against frozen gold: cache deciders from their latest cache rows at the current
fingerprints, `human` from its latest labels on overlapping hashes, fewer than 30 overlaps
`insufficient`, no rows `absent`) and step 6 (a stratified sample of the build's
`enrich.decision` rows scored against the planted answers). The cache reader is a thin
eval-side twin of impl 03's private one (carry-over T03-36). The planted answers are only
compared with decisions already stored; nothing here produces a decision.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.dataset as ds

from herness.core.types import Question, QuestionSet
from herness.enrich.cache import DecisionCache
from herness.enrich.labels import LabelStore
from herness.enrich.questions import question_fingerprint
from herness.eval._classifier_metrics import (
    DeciderMetrics,
    hard_metrics,
    label_space,
    matrix,
    not_scored,
    scored_metrics,
)

__all__ = ["Bootstrap", "GoldRow", "compare_deciders", "fingerprint", "latest_dists",
           "synthetic_metrics"]  # fmt: skip

HUMAN: Final = "human"
MIN_HUMAN: Final = 30
MIN_STRATUM: Final = 20
_DECISION_SQL: Final = (
    "SELECT record_id, question, answer, probability FROM enrich.decision "
    "WHERE question_set_version = ? AND answer IS NOT NULL"
)

type GoldRow = Mapping[str, object]  # content_hash, answer, fold
type Dists = dict[tuple[str, str], dict[str, float]]


@dataclass(frozen=True)
class Bootstrap:
    """The run's random generator (seeded once) and the resample count."""

    rng: np.random.Generator
    resamples: int


def fingerprint(q: Question) -> str:
    return q.fingerprint or question_fingerprint(q)


def _filter(decider: str, version: str | None, fingerprints: Sequence[str]) -> ds.Expression:
    condition = (ds.field("decider") == decider) & ds.field("question_fingerprint").isin(
        list(fingerprints)
    )
    return condition if version is None else condition & (ds.field("decider_version") == version)


def latest_dists(
    cache: DecisionCache, decider: str, version: str, fingerprints: Mapping[str, str]
) -> Dists:
    """Latest distribution per (question, content_hash) of one partition at the given
    fingerprints (qid -> fingerprint), as impl 03's `evaluate_candidate` reads it."""
    dataset = cache.dataset()
    if dataset is None or not fingerprints:
        return {}
    columns = ["question", "question_fingerprint", "content_hash", "distribution", "decided_at"]
    table = dataset.to_table(
        columns=columns, filter=_filter(decider, version, list(fingerprints.values()))
    )
    latest: dict[tuple[str, str], tuple[datetime, dict[str, float]]] = {}
    for row in table.sort_by([("decided_at", "ascending")]).to_pylist():
        if fingerprints.get(row["question"]) == row["question_fingerprint"]:
            latest[(row["question"], row["content_hash"])] = (
                row["decided_at"],
                dict(row["distribution"] or []),
            )
    return {key: dist for key, (_, dist) in latest.items()}


def _version(
    cache: DecisionCache, decider: str, gold: Mapping[str, Sequence[GoldRow]],
    fingerprints: Mapping[str, str],
) -> str | None:  # fmt: skip
    """The `decider_version` with the most gold rows (ties: lexicographically greatest)."""
    dataset = cache.dataset()
    if dataset is None or not fingerprints:
        return None
    columns = ["decider_version", "question", "question_fingerprint", "content_hash"]
    table = dataset.to_table(
        columns=columns, filter=_filter(decider, None, list(fingerprints.values()))
    )
    keys = {(qid, str(row["content_hash"])) for qid, rows in gold.items() for row in rows}
    counts: Counter[str] = Counter()
    seen: set[tuple[str, str, str]] = set()
    for row in table.to_pylist():
        key = (row["question"], row["content_hash"])
        current = fingerprints.get(row["question"]) == row["question_fingerprint"]
        if current and key in keys and (row["decider_version"], *key) not in seen:
            seen.add((row["decider_version"], *key))
            counts[row["decider_version"]] += 1
    if not counts:
        return None
    return max(counts, key=lambda version: (counts[version], version))


def _cache_decider(
    q: Question, gold: Sequence[GoldRow], dists: Dists, version: str, boot: Bootstrap
) -> DeciderMetrics:
    present = [row for row in gold if (q.id, str(row["content_hash"])) in dists]
    rows = [dists[(q.id, str(row["content_hash"]))] for row in present]
    seen = [str(r["answer"]) for r in gold] + [k for (qid, _), d in dists.items() if qid == q.id
                                                for k in d]  # fmt: skip
    space = label_space(q, seen)
    labels = np.array([space.index(str(row["answer"])) for row in present], dtype=np.int64)
    folds = np.array([row["fold"] for row in present], dtype=np.int64)
    probs = matrix(rows, space)
    return scored_metrics(q, space, probs, labels, folds, version=version, rng=boot.rng,
                          resamples=boot.resamples)  # fmt: skip


def _human(
    qs: QuestionSet, gold: Mapping[str, Sequence[GoldRow]], store: LabelStore, boot: Bootstrap
) -> dict[str, DeciderMetrics]:
    """`human/` labels (latest per key) against gold on overlapping hashes."""
    human = store.latest_human().to_pylist()
    if not human:
        return {qid: not_scored("absent") for qid in gold}
    answers = {(r["question"], r["question_fingerprint"], r["content_hash"]): str(r["answer"])
               for r in human}  # fmt: skip
    out: dict[str, DeciderMetrics] = {}
    for qid, rows in gold.items():
        q = qs.get(qid)
        fp = fingerprint(q)
        keyed = [((qid, fp, str(row["content_hash"])), str(row["answer"])) for row in rows]
        pairs = [(answers[key], gold_answer) for key, gold_answer in keyed if key in answers]
        if len(pairs) < MIN_HUMAN:
            out[qid] = not_scored("insufficient", n=len(pairs))
            continue
        space = label_space(q, [a for pair in pairs for a in pair])
        pred = np.array([space.index(h) for h, _ in pairs], dtype=np.int64)
        labels = np.array([space.index(g) for _, g in pairs], dtype=np.int64)
        out[qid] = hard_metrics(q, space, pred, labels, None, version=None, rng=boot.rng,
                                resamples=boot.resamples)  # fmt: skip
    return out


def compare_deciders(  # noqa: PLR0913 - the step-5 inputs: deciders, gold, stores, versions
    deciders: Sequence[str], qs: QuestionSet, gold: Mapping[str, Sequence[GoldRow]], *,
    cache: DecisionCache, store: LabelStore, fixed: Mapping[str, str], boot: Bootstrap,
) -> dict[str, dict[str, DeciderMetrics]]:  # fmt: skip
    """F11-09 step 5: per decider, per gold question. `fixed` pins a decider's version
    (Laya: `CURRENT`; OpenJev: the manifest's teacher version); every other decider uses the
    version with the most gold rows; a decider without cache rows is `absent`."""
    fingerprints = {qid: fingerprint(qs.get(qid)) for qid in gold}
    out: dict[str, dict[str, DeciderMetrics]] = {}
    for decider in deciders:
        if decider == HUMAN:
            out[decider] = _human(qs, gold, store, boot)
            continue
        version = fixed.get(decider) or _version(cache, decider, gold, fingerprints)
        dists = {} if version is None else latest_dists(cache, decider, version, fingerprints)
        out[decider] = {
            qid: _cache_decider(qs.get(qid), rows, dists, version, boot)
            if version is not None and dists
            else not_scored("absent", version=version)
            for qid, rows in gold.items()
        }
    return out


def _stratified(strata: Sequence[str], total: int, rng: np.random.Generator) -> list[int]:
    """Row indices: every row when there are at most `total`; else per stratum a share
    proportional to its size, at least `MIN_STRATUM` (or the whole stratum)."""
    if len(strata) <= total:
        return list(range(len(strata)))
    groups: dict[str, list[int]] = defaultdict(list)
    for index, stratum in enumerate(strata):
        groups[stratum].append(index)
    chosen: list[int] = []
    for stratum in sorted(groups):
        members = groups[stratum]
        share = max(MIN_STRATUM, round(total * len(members) / len(strata)))
        take = min(len(members), share)
        chosen += sorted(int(i) for i in rng.choice(members, size=take, replace=False))
    return chosen


def _decisions(
    warehouse: duckdb.DuckDBPyConnection, qsv: str
) -> dict[tuple[str, str], tuple[str, float]]:
    """(record_id, question) -> (answer, probability) of the stored decisions."""
    cursor = warehouse.cursor()
    try:
        rows = cursor.execute(_DECISION_SQL, [qsv]).fetchall()
    except duckdb.CatalogException:  # a build without enrichment output
        return {}
    finally:
        cursor.close()
    return {(str(r[0]), str(r[1])): (str(r[2]), float(r[3] or 0.0)) for r in rows}


def synthetic_metrics(
    qs: QuestionSet, qsv: str, warehouse: duckdb.DuckDBPyConnection, truth: pa.Table, *,
    sample: int, boot: Bootstrap,
) -> dict[str, DeciderMetrics]:  # fmt: skip
    """F11-09 step 6: per question, a stratified sample (strata = truth answer, total
    `sample`) of the decisions joined to the planted answers; informational metrics."""
    decided = _decisions(warehouse, qsv)
    planted: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for row in truth.to_pylist():
        planted[str(row["question"])].append((str(row["record_id"]), str(row["answer"])))
    out: dict[str, DeciderMetrics] = {}
    for q in qs.questions:
        if q.id not in planted:
            continue
        joined = [(answer, decided[(rid, q.id)]) for rid, answer in planted[q.id]
                  if (rid, q.id) in decided]  # fmt: skip
        picked = [joined[i] for i in _stratified([t for t, _ in joined], sample, boot.rng)]
        space = label_space(q, [a for t, (d, _) in picked for a in (t, d)])
        labels = np.array([space.index(t) for t, _ in picked], dtype=np.int64)
        pred = np.array([space.index(d) for _, (d, _) in picked], dtype=np.int64)
        conf = np.array([p for _, (_, p) in picked], dtype=np.float64)
        out[q.id] = hard_metrics(q, space, pred, labels, conf, version=None, rng=boot.rng,
                                 resamples=boot.resamples)  # fmt: skip
    return out
