"""Gold set requests and consolidation (U03-124 ... U03-126; design 03 §5.8 step 4, TH03-04).

`request_gold` sends the first-round ``label_check`` items (``purpose = "gold"``) of every
unfrozen question; `sync_label_checks` (U03-76) moves decided items into ``gold/_reviews/``;
`consolidate_gold` turns two agreeing reviewers (or a third that adjudicates) into gold rows,
asks for another review where needed and freezes complete sets with their digest. Items are
matched on (purpose, question, content_hash) plus the ``question_set_version`` scope (the
T03-20 spec note on U03-148). Payloads, logs and errors carry ids, hashes and counts only,
never ticket text (TH03-03); gold rows are only appended, never rewritten.
"""

from __future__ import annotations

import dataclasses
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Final

import duckdb
import numpy as np
import pyarrow as pa

from herness.core import time as clock
from herness.core.audit import log_lock
from herness.core.logging import get_logger
from herness.core.types import Question, QuestionSet
from herness.enrich._cluster_io import ClusterSnapshot
from herness.enrich.labels import GOLD_SCHEMA, LabelStore, gold_digest
from herness.enrich.questions import PAIR_QUESTIONS, question_fingerprint
from herness.enrich.review_items import create_if_absent, iter_review_items, open_label_counts
from herness.enrich.sampling import _ordered_pool, stratified_sample
from herness.enrich.settings import DecisionsConfig

__all__ = ["GoldStatus", "consolidate_gold", "fold_of", "request_gold"]

PURPOSE: Final = "gold"
MIN_CLASS: Final = 30  # examples per class with >= 1 % teacher prevalence
MIN_PREVALENCE: Final = 0.01
EXHAUSTED_MIN: Final = 1_000  # freeze floor once nothing is pending (spec 11's gate)
_KEYS: Final = ("purpose", "question", "content_hash")  # + qsv scope (T03-20 note, U03-148)
_BLOCKING: Final = ("pending",)
_LOCK_TIMEOUT_S: Final = 10.0

_log = get_logger("enrich.gold")


@dataclasses.dataclass(frozen=True)
class GoldStatus:
    """Per-question gold state after `consolidate_gold` (U03-126)."""

    n_gold: int
    pending: int
    frozen: bool


def fold_of(content_hash: str) -> int:
    """Fold by hash parity (U03-124): ``int(content_hash[-1], 16) % 2``."""
    return int(content_hash[-1], 16) % 2


def _read_gold(store: LabelStore) -> pa.Table:
    """``store.read("gold")``, or an empty table while ``gold/`` holds no part yet: a dataset
    of only ``_reviews/`` and ``_frozen/`` has no columns, which `LabelStore.read` cannot
    project (T03-29 note on U03-75)."""
    if not any(store.paths.labels_dir(store.qsv, "gold").glob("part-*.parquet")):
        return GOLD_SCHEMA.empty_table()
    return store.read("gold")


def _fingerprint(q: Question) -> str:
    return q.fingerprint or question_fingerprint(q)


def _questions(qs: QuestionSet) -> list[Question]:
    return [q for q in qs.questions if q.id not in PAIR_QUESTIONS]


def _payload(qs: QuestionSet, q: Question, record_id: str, content_hash: str,
             answer: str | None) -> dict[str, object]:  # fmt: skip
    """A design 03 §4.6 ``label_check`` payload; the teacher's probability and versions are
    not known here (only its answer through ``teacher_answers``), so they are null."""
    return {
        "record_id": record_id, "content_hash": content_hash, "question": q.id,
        "question_fingerprint": _fingerprint(q), "question_set_version": qs.version,
        "answer": answer, "probability": None, "decider": None, "decider_version": None,
        "purpose": PURPOSE, "text_ref": "enrich.text_redacted",
    }  # fmt: skip


def _keys_of(table: pa.Table, qid: str, fingerprint: str) -> set[str]:
    """Content hashes of the rows of (qid, fingerprint) in a label table."""
    names = ("question", "question_fingerprint", "content_hash")
    rows = zip(*(table.column(name).to_pylist() for name in names), strict=True)
    return {h for q, fp, h in rows if q == qid and fp == fingerprint}


def _top_up(
    pool: pa.Table,
    need: dict[str, int],
    skip: set[str],
    teacher_answers: Callable[[str, str], str | None],
    qid: str,
) -> list[tuple[str, str, str]]:
    """``(record_id, content_hash, answer)`` drawn in pool (hash) order for classes in need."""
    picks: list[tuple[str, str, str]] = []
    ids, hashes = pool.column("record_id").to_pylist(), pool.column("content_hash").to_pylist()
    for record_id, content_hash in zip(ids, hashes, strict=True):
        if not any(need.values()):
            break
        if content_hash in skip:
            continue
        answer = teacher_answers(content_hash, qid)
        if answer is not None and need.get(answer, 0) > 0:
            need[answer] -= 1
            picks.append((record_id, content_hash, answer))
    return picks


def _class_needs(teacher: pa.Table, q: Question, answers: Sequence[str | None]) -> dict[str, int]:
    """Missing examples per class with >= 1 % teacher-predicted prevalence on the training
    rows of the question's fingerprint."""
    names = ("question", "question_fingerprint", "answer")
    rows = zip(*(teacher.column(name).to_pylist() for name in names), strict=True)
    prevalence = Counter(a for qid, fp, a in rows if qid == q.id and fp == _fingerprint(q))
    total = sum(prevalence.values())
    have = Counter(answers)
    return {
        label: MIN_CLASS - have[label]
        for label, count in sorted(prevalence.items())
        if count >= MIN_PREVALENCE * total and have[label] < MIN_CLASS
    }


def request_gold(  # noqa: PLR0913 - U03-125's keyword-only signature is binding
    wh: duckdb.DuckDBPyConnection,
    *,
    qs: QuestionSet,
    store: LabelStore,
    cfg: DecisionsConfig,
    teacher_answers: Callable[[str, str], str | None],
    snapshot: ClusterSnapshot | None,
    vector_reader: Callable[[Sequence[str]], np.ndarray],
) -> dict[str, int]:
    """Create the first-round gold ``label_check`` items per unfrozen question (U03-125);
    returns the items created per question.

    Per question: ``gold_size`` records of an independent `stratified_sample` (salt
    ``gold:<qid>:<fingerprint>``, excluding the training (teacher) hashes) plus a top-up, in
    the same hash order, so every class with >= 1 % teacher prevalence has >= 30 records.
    Records already reviewed or gold for the question get no new item (follow-ups belong to
    `consolidate_gold`); pending items suppress duplicates, so a rerun creates nothing.
    Raises as `stratified_sample` and `create_if_absent`.
    """
    teacher = store.read("teacher", columns=["content_hash", "question",
                                              "question_fingerprint", "answer"])  # fmt: skip
    training = frozenset(teacher.column("content_hash").to_pylist())
    gold, reviews = _read_gold(store), store.read("gold_reviews")
    created: dict[str, int] = {}
    for q in _questions(qs):
        fingerprint = _fingerprint(q)
        if store.is_gold_frozen(q.id, fingerprint):
            continue
        one = QuestionSet(version=qs.version, questions=(q,))
        salt = f"gold:{q.id}:{fingerprint}"
        sample = stratified_sample(wh, qs=one, size=cfg.distill.gold_size,
                                   exclude_hashes=training, salt=salt, snapshot=snapshot,
                                   vector_reader=vector_reader)  # fmt: skip
        hashes = sample.column("content_hash").to_pylist()
        answers = [teacher_answers(h, q.id) for h in hashes]
        picks = list(zip(sample.column("record_id").to_pylist(), hashes, answers, strict=True))
        need = _class_needs(teacher, q, answers)
        if need:
            pool = _ordered_pool(wh, qs=one, exclude_hashes=training, salt=salt)
            picks += _top_up(pool, need, set(hashes), teacher_answers, q.id)
        done = _keys_of(gold, q.id, fingerprint) | _keys_of(reviews, q.id, fingerprint)
        payloads = [_payload(qs, q, rid, h, a) for rid, h, a in picks if h not in done]
        made = 0
        if payloads:
            made, _ = create_if_absent("label_check", payloads, match_keys=_KEYS,
                                       blocking_statuses=_BLOCKING,
                                       scope={"question_set_version": qs.version},
                                       now=clock.now())  # fmt: skip
        created[q.id] = made
        _log.info("enrich.gold.requested", question=q.id, created=made, sampled=len(hashes),
                  top_up=len(picks) - len(hashes))  # fmt: skip
    return created


def _verdict(reviews: list[dict[str, object]]) -> tuple[dict[str, object], bool] | None:
    """The deciding review and ``adjudicated`` of one (question, hash), or None when more
    reviews are needed. ``reviews`` are in (labeled_at, item_id) order; a reviewer counts once
    (their first answer)."""
    firsts: dict[object, dict[str, object]] = {}
    for review in reviews:
        firsts.setdefault(review["labeled_by"], review)
    distinct = list(firsts.values())
    if len(distinct) < 2:  # noqa: PLR2004 - two reviewers
        return None
    first, second = distinct[0]["answer"], distinct[1]["answer"]
    if first == second:
        return distinct[1], False
    for later in distinct[2:]:
        if later["answer"] in (first, second):
            return later, True
    return None


def _follow_up_payloads(qsv: str, wanted: set[tuple[str, str]]) -> list[dict[str, object]]:
    """The original payload of each (question, hash) in ``wanted`` (first approved item)."""
    found: dict[tuple[str, str], dict[str, object]] = {}
    match = {"question_set_version": qsv, "purpose": PURPOSE}
    for item in iter_review_items("label_check", "approved", payload_match=match):
        key = (str(item.payload.get("question")), str(item.payload.get("content_hash")))
        if key in wanted and key not in found:
            found[key] = dict(item.payload)
    return [found[key] for key in sorted(found)]


def _consolidate_question(
    q: Question, gold: pa.Table, reviews: pa.Table
) -> tuple[pa.Table, set[tuple[str, str]]]:
    """New gold rows of ``q`` and the (question, hash) keys that need another review."""
    fingerprint = _fingerprint(q)
    known = _keys_of(gold, q.id, fingerprint)
    grouped: dict[str, list[dict[str, object]]] = {}
    ordered = reviews.sort_by([("labeled_at", "ascending"), ("item_id", "ascending")])
    for row in ordered.to_pylist():
        if row["question"] == q.id and row["question_fingerprint"] == fingerprint:
            grouped.setdefault(str(row["content_hash"]), []).append(row)
    rows: list[dict[str, object]] = []
    again: set[tuple[str, str]] = set()
    for content_hash in sorted(set(grouped) - known):
        verdict = _verdict(grouped[content_hash])
        if verdict is None:
            again.add((q.id, content_hash))
            continue
        row, adjudicated = verdict
        rows.append({**row, "fold": fold_of(content_hash), "adjudicated": adjudicated})
    return pa.Table.from_pylist(rows, schema=GOLD_SCHEMA), again


def consolidate_gold(
    store: LabelStore, *, qs: QuestionSet, cfg: DecisionsConfig, now: datetime
) -> dict[str, GoldStatus]:
    """Turn gold reviews into gold rows, ask for further reviews and freeze complete sets
    (U03-126); returns the `GoldStatus` per (non-pair) question.

    Two distinct reviewers agreeing on their first answers make a gold row (``adjudicated``
    false; labeled_by/at and item_id of the second); after a disagreement the first later
    distinct reviewer whose answer equals one of the two adjudicates (true). Otherwise a new
    item with the original payload is created unless one is pending. A question is frozen
    (``freeze_gold`` with `gold_digest`) once nothing is pending and it has >= ``gold_size``
    rows, or >= 1,000 (top-up exhausted). Runs under ``data/locks/labels.lock``; raises
    ops, IO and lock (StoreBusy) errors.
    """
    lock = store.paths.data_root / "locks" / "labels.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with log_lock(lock, timeout_s=_LOCK_TIMEOUT_S):
        gold, reviews = _read_gold(store), store.read("gold_reviews")
        fresh: dict[str, pa.Table] = {}
        again: set[tuple[str, str]] = set()
        for q in _questions(qs):
            if not store.is_gold_frozen(q.id, _fingerprint(q)):
                fresh[q.id], wanted = _consolidate_question(q, gold, reviews)
                store.append("gold", fresh[q.id])
                again |= wanted
        follow_ups = _follow_up_payloads(qs.version, again) if again else []
        asked = Counter[str]()
        for payload in follow_ups:
            made, _ = create_if_absent("label_check", [payload], match_keys=_KEYS,
                                       blocking_statuses=_BLOCKING,
                                       scope={"question_set_version": qs.version},
                                       now=now)  # fmt: skip
            asked[str(payload["question"])] += made
        pending = open_label_counts(qsv=qs.version, purposes=frozenset({PURPOSE}))
        gold = _read_gold(store)
        status: dict[str, GoldStatus] = {}
        for q in _questions(qs):
            new = fresh[q.id].num_rows if q.id in fresh else 0
            status[q.id] = _status(store, q, gold, pending.get(q.id, 0), cfg, (new, asked[q.id]))
        return status


def _status(store: LabelStore, q: Question, gold: pa.Table, pending: int,
            cfg: DecisionsConfig, counts: tuple[int, int]) -> GoldStatus:  # fmt: skip
    """Freeze ``q`` when complete and log ``enrich.gold.consolidated``."""
    fingerprint = _fingerprint(q)
    mask = [
        qid == q.id and fp == fingerprint
        for qid, fp in zip(gold.column("question").to_pylist(),
                           gold.column("question_fingerprint").to_pylist(), strict=True)
    ]  # fmt: skip
    rows = gold.filter(pa.array(mask, type=pa.bool_()))
    n_gold = rows.num_rows
    frozen = store.is_gold_frozen(q.id, fingerprint)
    complete = n_gold >= min(cfg.distill.gold_size, EXHAUSTED_MIN)
    if not frozen and pending == 0 and complete:
        store.freeze_gold(q.id, fingerprint, gold_digest(rows), n_gold)
        frozen = True
    _log.info("enrich.gold.consolidated", question=q.id, n_gold=n_gold, new=counts[0],
              follow_ups=counts[1], pending=pending, frozen=frozen)  # fmt: skip
    return GoldStatus(n_gold=n_gold, pending=pending, frozen=frozen)
