"""Tests for herness.enrich.gold (U03-124 ... U03-126; UT03-119 ... UT03-122; T03-29).

The gold flow runs against the real migrated `ops_store` (T11-40): items are created by
`request_gold` / `consolidate_gold`, decided through impl 02's `decide_review_item` and moved
into ``gold/_reviews/`` by `sync_label_checks`, as in the distill job.
"""

from __future__ import annotations

import datetime
import json
from collections import Counter
from pathlib import Path

import pyarrow as pa
import pytest
from structlog.testing import capture_logs
from tests.support.ops_store import OpsStoreHandle
from tests.unit.enrich._sampling_support import many, no_vectors, warehouse

from herness.core.types import Question, QuestionSet
from herness.enrich import gold as gold_mod
from herness.enrich.gold import GoldStatus, consolidate_gold, fold_of, request_gold
from herness.enrich.labels import (
    GOLD_SCHEMA,
    HUMAN_SCHEMA,
    TEACHER_SCHEMA,
    LabelStore,
    gold_digest,
    sync_label_checks,
)
from herness.enrich.layout import EnrichPaths
from herness.enrich.review_items import iter_review_items
from herness.enrich.settings import DecisionsConfig
from herness.store.ops import ReviewItem, create_review_item, decide_review_item

pytestmark = pytest.mark.unit

QSV = "qs-2026-10-01"
FP = "a" * 16
USERS = ("ab" * 16, "cd" * 16, "ef" * 16, "12" * 16)
T0 = datetime.datetime(2026, 10, 1, 9, 0, tzinfo=datetime.UTC)
Q_C = Question.model_validate(
    {"id": "q_c", "type": "choice", "instructions": "Which root cause class is it?",
     "options": {"a": "A.", "b": "B.", "c": "C.", "d": "D."}, "threshold": 0.7,
     "fingerprint": FP}
)  # fmt: skip
PAIR = Question.model_validate(
    {"id": "change_caused_pair", "type": "bool", "instructions": "Did the change cause it?",
     "threshold": 0.7, "applies_to": ("incident",), "fingerprint": "b" * 16}
)  # fmt: skip
QS = QuestionSet(version=QSV, questions=(Q_C, PAIR))


def _cfg(gold_size: int = 100) -> DecisionsConfig:
    return DecisionsConfig.model_validate(
        {
            "question_set_version": QSV,
            "questions": (),
            "distill": {"gold_size": gold_size},
            "change_link": {"use_decider": False},
        }
    )


@pytest.fixture
def store(ops_store: OpsStoreHandle) -> LabelStore:
    paths = EnrichPaths(data_root=ops_store.data_root, embedding_path="data/e",
                        laya_current_file="c")  # fmt: skip
    return LabelStore(paths, QSV)


# --- UT03-119: fold_of -------------------------------------------------------------------------


def test_ut03_119_fold_by_hash_parity() -> None:
    """UT03-119: hashes ending ``a`` and ``3`` -> folds 0 and 1."""
    assert fold_of("0" * 31 + "a") == 0
    assert fold_of("0" * 31 + "3") == 1
    assert fold_of("f" * 32) == 1


# --- UT03-120: request_gold --------------------------------------------------------------------


def _teacher_rows(n: int) -> pa.Table:
    """Training rows: class a 90 %, b 8 %, c 1.5 %, d 0.5 % (d stays under 1 %)."""
    labels = ["a"] * 180 + ["b"] * 16 + ["c"] * 3 + ["d"] * 1
    rows = [
        {"content_hash": f"{i:032x}", "record_id": f"TRN-{i}", "question": "q_c",
         "question_fingerprint": FP, "answer": labels[i % len(labels)],
         "distribution": [(labels[i % len(labels)], 1.0)], "decider": "openjev",
         "decider_version": "oj-1", "round": 0, "stratum": "s", "purpose": "initial"}
        for i in range(n)
    ]  # fmt: skip
    return pa.Table.from_pylist(rows, schema=TEACHER_SCHEMA)


def _teacher_answer(content_hash: str, qid: str) -> str | None:
    """Teacher answers of the warehouse records: every 25th hash value is class c."""
    assert qid == "q_c"
    value = int(content_hash, 16)
    if value % 101 == 0:
        return None
    return "c" if value % 25 == 0 else ("b" if value % 7 == 0 else "a")


def _pending(purpose: str = "gold") -> list[ReviewItem]:
    match = {"question_set_version": QSV, "purpose": purpose}
    return list(iter_review_items("label_check", "pending", payload_match=match))


def test_ut03_120_request_gold_tops_up_rare_class_and_is_idempotent(store: LabelStore) -> None:
    """UT03-120: gold_size records plus a top-up to 30 for every >= 1 % class; idempotent."""
    records = many(1_500)
    wh = warehouse(records)
    store.append("teacher", _teacher_rows(200))
    training = {records[3].hash}  # a warehouse record in the training sample
    teacher = _teacher_rows(1).to_pylist()[0] | {"content_hash": records[3].hash}
    store.append("teacher", pa.Table.from_pylist([teacher], schema=TEACHER_SCHEMA))
    args = {"qs": QS, "store": store, "cfg": _cfg(), "teacher_answers": _teacher_answer,
            "snapshot": None, "vector_reader": no_vectors}  # fmt: skip
    with capture_logs() as logs:
        created = request_gold(wh, **args)  # type: ignore[arg-type]
    items = _pending()
    assert created == {"q_c": len(items)}
    assert "change_caused_pair" not in created
    answers = Counter(item.payload["answer"] for item in items)
    assert answers["c"] >= 30
    assert answers["d"] == 0
    assert len(items) > 100
    hashes = {item.payload["content_hash"] for item in items}
    assert not hashes & training
    payload = items[0].payload
    assert payload["purpose"] == "gold"
    assert payload["question_fingerprint"] == FP
    assert payload["text_ref"] == "enrich.text_redacted"
    assert set(payload) == {"record_id", "content_hash", "question", "question_fingerprint",
                            "question_set_version", "answer", "probability", "decider",
                            "decider_version", "purpose", "text_ref"}  # fmt: skip
    texts = {r.text for r in records}
    assert not any(isinstance(v, str) and v in texts for v in payload.values())
    assert not any(t in json.dumps(e) for e in logs for t in list(texts)[:50])
    assert request_gold(wh, **args) == {"q_c": 0}  # type: ignore[arg-type]
    assert len(_pending()) == len(items)


def test_ut03_120_request_gold_skips_frozen_and_exhausted_top_up(store: LabelStore) -> None:
    """UT03-120: a top-up that finds no record of a class stops at the pool end; frozen
    questions are skipped."""
    wh = warehouse(many(300))
    store.append("teacher", _teacher_rows(200))
    sample_args = {"qs": QS, "store": store, "cfg": _cfg(),
                   "teacher_answers": lambda h, q: "a", "snapshot": None,
                   "vector_reader": no_vectors}  # fmt: skip
    created = request_gold(wh, **sample_args)  # type: ignore[arg-type]
    assert created == {"q_c": len(_pending())}
    assert created["q_c"] >= 100  # every stratum gets min(5, N_h) when 100 < 5 x strata
    store.freeze_gold("q_c", FP, "e" * 64, 0)
    assert request_gold(wh, **sample_args) == {}  # type: ignore[arg-type]


def test_ut03_120_reviewed_hashes_not_requested_again(store: LabelStore) -> None:
    """UT03-120: after reviews are synced, a rerun creates no first-round item for them."""
    wh = warehouse(many(300))
    store.append("teacher", _teacher_rows(200))
    args = {"qs": QS, "store": store, "cfg": _cfg(), "teacher_answers": lambda h, q: "a",
            "snapshot": None, "vector_reader": no_vectors}  # fmt: skip
    total = request_gold(wh, **args)["q_c"]  # type: ignore[arg-type]
    for step, item in enumerate(_pending()[:10]):
        _decide(item, USERS[0], "a", step)
    sync_label_checks(store, qs=QS)
    assert request_gold(wh, **args) == {"q_c": 0}  # type: ignore[arg-type]
    assert len(_pending()) == total - 10


# --- UT03-121 / UT03-122: consolidate_gold -----------------------------------------------------


def _decide(item: ReviewItem, user: str, answer: str, step: int) -> None:
    decided = T0 + datetime.timedelta(minutes=step)
    decide_review_item(item.item_id, "approved", decided_by=user,
                       note=json.dumps({"answer": answer}), now=decided)  # fmt: skip


def _gold_item(content_hash: str, *, qid: str = "q_c", fp: str = FP) -> ReviewItem:
    payload = {"record_id": f"INC-{content_hash[-4:]}", "content_hash": content_hash,
               "question": qid, "question_fingerprint": fp, "question_set_version": QSV,
               "answer": "a", "probability": None, "decider": None, "decider_version": None,
               "purpose": "gold", "text_ref": "enrich.text_redacted"}  # fmt: skip
    create_review_item("label_check", payload, now=T0)
    return _pending_for(content_hash)


def _pending_for(content_hash: str) -> ReviewItem:
    found = [i for i in _pending() if i.payload["content_hash"] == content_hash]
    assert len(found) == 1
    return found[0]


def _round(store: LabelStore, cfg: DecisionsConfig | None = None) -> dict[str, GoldStatus]:
    sync_label_checks(store, qs=QS)
    return consolidate_gold(store, qs=QS, cfg=cfg or _cfg(), now=T0)


def test_ut03_121_two_agreeing_reviewers_and_repeated_reviewer(store: LabelStore) -> None:
    """UT03-121: two agreeing reviewers -> one gold row; a repeated reviewer counts once and
    a follow-up item is created."""
    h_ok, h_same = "1" * 31 + "a", "2" * 31 + "3"
    _decide(_gold_item(h_ok), USERS[0], "b", 1)
    _decide(_gold_item(h_same), USERS[0], "a", 2)
    status = _round(store)
    assert status["q_c"] == GoldStatus(n_gold=0, pending=2, frozen=False)
    _decide(_pending_for(h_ok), USERS[1], "b", 3)  # second reviewer agrees
    _decide(_pending_for(h_same), USERS[0], "a", 4)  # same reviewer again
    with capture_logs() as logs:
        status = _round(store)
    gold = store.read("gold").to_pylist()
    assert [(r["content_hash"], r["answer"], r["fold"], r["adjudicated"]) for r in gold] == [
        (h_ok, "b", 0, False)
    ]
    assert gold[0]["labeled_by"] == USERS[1]
    assert status["q_c"] == GoldStatus(n_gold=1, pending=1, frozen=False)
    assert _pending_for(h_same).payload["record_id"] == f"INC-{h_same[-4:]}"
    event = next(e for e in logs if e["event"] == "enrich.gold.consolidated")
    assert event["log_level"] == "info"
    assert (event["new"], event["follow_ups"], event["pending"]) == (1, 1, 1)
    status = _round(store)  # rerun: gold rows are not rewritten, nothing new
    assert store.read("gold").num_rows == 1
    assert status["q_c"].pending == 1


def test_ut03_122_adjudication_and_freeze_at_gold_size(store: LabelStore) -> None:
    """UT03-122: disagreement then a third reviewer -> adjudicated row; freeze at gold_size."""
    filler = [
        {"content_hash": f"{i:032x}", "record_id": f"INC-{i}", "question": "q_c",
         "question_fingerprint": FP, "answer": "a", "labeled_by": USERS[0], "labeled_at": T0,
         "item_id": f"rev_{i}", "fold": fold_of(f"{i:032x}"), "adjudicated": False}
        for i in range(1, 100)
    ]  # fmt: skip
    store.append("gold", pa.Table.from_pylist(filler, schema=GOLD_SCHEMA))
    h = "f" * 32
    _decide(_gold_item(h), USERS[0], "a", 1)
    assert _round(store)["q_c"] == GoldStatus(n_gold=99, pending=1, frozen=False)
    _decide(_pending_for(h), USERS[1], "b", 2)
    assert _round(store)["q_c"] == GoldStatus(n_gold=99, pending=1, frozen=False)
    _decide(_pending_for(h), USERS[2], "b", 3)
    status = _round(store)
    assert status["q_c"] == GoldStatus(n_gold=100, pending=0, frozen=True)
    rows = store.read("gold").to_pylist()
    new = [r for r in rows if r["content_hash"] == h]
    assert [(r["answer"], r["adjudicated"], r["fold"]) for r in new] == [("b", True, 1)]
    marker = store.paths.labels_dir(QSV, "gold") / "_frozen" / f"q_c-{FP}.json"
    body = json.loads(marker.read_text("utf-8"))
    assert body["digest"] == gold_digest(store.read("gold"))
    assert body["n"] == 100
    assert _round(store)["q_c"] == GoldStatus(n_gold=100, pending=0, frozen=True)


def test_ut03_122_third_disagreeing_then_fourth_adjudicates(store: LabelStore) -> None:
    """UT03-122: three different answers ask again; a later answer equal to one of the first
    two adjudicates; reviews of another fingerprint and pair questions are ignored."""
    h = "e" * 31 + "4"
    _decide(_gold_item(h), USERS[0], "a", 1)
    _round(store)
    _decide(_pending_for(h), USERS[1], "b", 2)
    _round(store)
    _decide(_pending_for(h), USERS[2], "c", 3)
    assert _round(store)["q_c"].pending == 1
    _decide(_pending_for(h), USERS[3], "a", 4)
    status = _round(store)
    rows = store.read("gold").to_pylist()
    assert [(r["answer"], r["adjudicated"]) for r in rows] == [("a", True)]
    assert status == {"q_c": GoldStatus(n_gold=1, pending=0, frozen=False)}


def test_ut03_122_freeze_when_exhausted_at_1000(store: LabelStore) -> None:
    """UT03-122: with gold_size above 1,000, 1,000 rows and nothing pending freeze the set."""
    rows = [
        {"content_hash": f"{i:032x}", "record_id": f"INC-{i}", "question": "q_c",
         "question_fingerprint": FP, "answer": "a", "labeled_by": USERS[0], "labeled_at": T0,
         "item_id": f"rev_{i}", "fold": fold_of(f"{i:032x}"), "adjudicated": False}
        for i in range(1_000)
    ]  # fmt: skip
    store.append("gold", pa.Table.from_pylist(rows[:999], schema=GOLD_SCHEMA))
    assert not consolidate_gold(store, qs=QS, cfg=_cfg(1_500), now=T0)["q_c"].frozen
    store.append("gold", pa.Table.from_pylist(rows[999:], schema=GOLD_SCHEMA))
    assert consolidate_gold(store, qs=QS, cfg=_cfg(1_500), now=T0)["q_c"].frozen


def test_ut03_121_follow_up_without_original_item(store: LabelStore) -> None:
    """UT03-121: a lone review whose original item is gone creates no follow-up."""
    review = {"content_hash": "9" * 32, "record_id": "INC-9", "question": "q_c",
              "question_fingerprint": FP, "answer": "a", "labeled_by": USERS[0],
              "labeled_at": T0, "item_id": "rev_x"}  # fmt: skip
    store.append("gold_reviews", pa.Table.from_pylist([review], schema=HUMAN_SCHEMA))
    status = consolidate_gold(store, qs=QS, cfg=_cfg(), now=T0)
    assert status["q_c"] == GoldStatus(n_gold=0, pending=0, frozen=False)
    assert gold_mod.PURPOSE == "gold"
    assert Path(store.paths.data_root / "locks" / "labels.lock").parent.is_dir()
