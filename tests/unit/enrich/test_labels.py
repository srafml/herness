"""Tests for herness.enrich.labels (U03-75 ... U03-77; UT03-72 ... UT03-75, PT03-09; T03-18).

UT03-74 seeds the real migrated `ops_store` (T11-40) through impl 02's `create_review_item`
and `decide_review_item`, so the keyset cursor runs against the actual `review_item` table.
"""

from __future__ import annotations

import datetime
import errno
import hashlib
import json
import random
from pathlib import Path
from typing import Any, Literal, cast

import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from structlog.testing import capture_logs
from tests.support.ops_store import OpsStoreHandle

from herness.core.errors import ConfigError, FatalError, SchemaViolation, StoreBusy
from herness.core.types import Question, QuestionSet
from herness.enrich import labels
from herness.enrich.labels import (
    GOLD_SCHEMA,
    HUMAN_SCHEMA,
    TEACHER_SCHEMA,
    LabelStore,
    gold_digest,
    sync_label_checks,
)
from herness.enrich.layout import EnrichPaths
from herness.store.ops import (
    ReviewItem,
    create_review_item,
    decide_review_item,
    list_review_items,
)
from herness.store.ops import shared as ops_shared

pytestmark = pytest.mark.unit

OLD, NEW, OTHER = "qs-2026-09-01", "qs-2026-09-02", "qs-2026-08-01"
FP_A, FP_B, FP_B2, FP_C = "a" * 16, "b" * 16, "c" * 16, "d" * 16
H1, H2, H3 = "1" * 32, "2" * 32, "3" * 32
USER = "ab" * 16
T0 = datetime.datetime(2026, 9, 26, 10, 0, tzinfo=datetime.UTC)
DIGEST = "e" * 64


def _q(qid: str, fp: str, qtype: str = "bool", **extra: Any) -> Question:
    return Question.model_validate(
        {"id": qid, "type": qtype, "instructions": "Is this a thing?", "threshold": 0.7}
        | {"fingerprint": fp}
        | extra
    )


Q_A = _q("q_a", FP_A)
Q_B = _q("q_b", FP_B, "choice", options={"db": "Database fault", "net": "Network fault"})
Q_C = _q("q_c", FP_C, "score", levels=("none", "low", "mid", "high"))
QS_OLD = QuestionSet(version=OLD, questions=(Q_A, Q_B, Q_C))
QS_NEW = QuestionSet(version=NEW, questions=(Q_A, _q("q_b", FP_B2)))


@pytest.fixture
def paths(tmp_path: Path) -> EnrichPaths:
    return EnrichPaths(data_root=tmp_path / "data", embedding_path="data/e", laya_current_file="c")


def _human(n: int = 2, *, qid: str = "q_a", fp: str = FP_A, prefix: str = "rev_") -> pa.Table:
    rows = [
        {
            "content_hash": f"{i:032x}",
            "record_id": f"INC-{i}",
            "question": qid,
            "question_fingerprint": fp,
            "answer": "true",
            "labeled_by": USER,
            "labeled_at": T0 + datetime.timedelta(minutes=i),
            "item_id": f"{prefix}{i}",
        }
        for i in range(n)
    ]
    return pa.Table.from_pylist(rows, schema=HUMAN_SCHEMA)


def _gold(rows: list[tuple[str, str, str, str, int]]) -> pa.Table:
    data = [
        {
            "content_hash": h,
            "record_id": "INC-1",
            "question": qid,
            "question_fingerprint": fp,
            "answer": answer,
            "labeled_by": USER,
            "labeled_at": T0,
            "item_id": f"rev_{h}{qid}",
            "fold": fold,
            "adjudicated": False,
        }
        for qid, fp, h, answer, fold in rows
    ]
    return pa.Table.from_pylist(data, schema=GOLD_SCHEMA)


def _teacher(qid: str = "q_a", fp: str = FP_A) -> pa.Table:
    row = {
        "content_hash": H1,
        "record_id": "INC-1",
        "question": qid,
        "question_fingerprint": fp,
        "answer": "true",
        "distribution": [("true", 0.9), ("false", 0.1)],
        "decider": "openjev",
        "decider_version": "openjev-0.4.0/qwen:7b",
        "round": 1,
        "stratum": "uniform",
        "purpose": "train",
    }
    return pa.Table.from_pylist([row], schema=TEACHER_SCHEMA)


# UT03-72 ------------------------------------------------------------------------------------


def test_ut03_72_append_read_round_trip_each_kind(paths: EnrichPaths) -> None:
    """UT03-72: each kind round-trips; gold/_reviews is invisible to gold readers."""
    store = LabelStore(paths, OLD)
    tables = {
        "teacher": _teacher(),
        "human": _human(),
        "gold": _gold([("q_a", FP_A, H1, "true", 0)]),
        "gold_reviews": _human(1, prefix="rev_g"),
    }
    for kind, table in tables.items():
        part = store.append(kind, table)  # type: ignore[arg-type]
        assert part is not None
        assert part.name.startswith("part-")
        assert part.name.endswith(".parquet")
    assert store.append("human", _human(0)) is None
    for kind, table in tables.items():
        assert store.read(kind).equals(table), kind  # type: ignore[arg-type]
    reviews_dir = paths.labels_dir(OLD, "gold") / "_reviews"
    assert len(list(reviews_dir.glob("part-*.parquet"))) == 1
    assert store.read("gold").num_rows == 1
    assert store.item_ids("human") == {"rev_0", "rev_1"}
    assert store.item_ids("gold_reviews") == {"rev_g0"}
    assert store.gold_hashes() == {H1}


def test_ut03_72_empty_reads(paths: EnrichPaths) -> None:
    """UT03-72: reads of a missing kind are empty tables with the kind's schema."""
    store = LabelStore(paths, OLD)
    assert store.read("teacher").schema.equals(TEACHER_SCHEMA)
    assert store.read("gold").schema.equals(GOLD_SCHEMA)
    assert store.read("gold_reviews").num_rows == 0
    assert store.item_ids("human") == set()
    assert store.gold_hashes() == set()
    assert store.latest_human().num_rows == 0


def test_ut03_72_wrong_schema(paths: EnrichPaths) -> None:
    """UT03-72: a table whose schema differs is refused with SchemaViolation."""
    store = LabelStore(paths, OLD)
    with pytest.raises(SchemaViolation):
        store.append("gold", _human())
    with pytest.raises(SchemaViolation):
        store.append("human", _human().drop_columns(["item_id"]))
    null_key = _human().set_column(0, "content_hash", pa.array([None, H1], type=pa.string()))
    with pytest.raises(SchemaViolation, match="null keys"):
        store.append("human", null_key)
    with pytest.raises(SchemaViolation):
        store.append("gold", _gold([(None, FP_A, H1, "true", 0)]))  # type: ignore[list-item]
    with pytest.raises(ConfigError):
        store.append("silver", _human())  # type: ignore[arg-type]
    with pytest.raises(ConfigError):
        store.read("silver")  # type: ignore[arg-type]
    with pytest.raises(ConfigError):
        LabelStore(paths, "../x")


def test_ut03_72_foreign_part_schema(paths: EnrichPaths) -> None:
    """UT03-72: a part with a foreign schema on disk fails the read with SchemaViolation."""
    store = LabelStore(paths, OLD)
    store.append("human", _human())
    folder = paths.labels_dir(OLD, "human")
    pq.write_table(pa.table({"x": [1]}), folder / "part-zzz.parquet")
    with pytest.raises(SchemaViolation):
        store.read("human")


def test_ut03_72_latest_human(paths: EnrichPaths) -> None:
    """UT03-72: latest_human keeps one row per key, the latest labeled_at."""
    store = LabelStore(paths, OLD)
    first = _human(2)
    later = first.set_column(
        first.schema.get_field_index("labeled_at"),
        "labeled_at",
        pa.array([T0 + datetime.timedelta(hours=1)] * 2, type=pa.timestamp("us", tz="UTC")),
    )
    later = later.set_column(
        later.schema.get_field_index("answer"), "answer", pa.array(["false", "false"])
    )
    store.append("human", later)
    store.append("human", first)
    latest = store.latest_human()
    assert latest.num_rows == 2
    assert set(latest.column("answer").to_pylist()) == {"false"}


def test_ut03_72_io_error_maps(paths: EnrichPaths, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT03-72: OS errors on write map as U03-38 (StoreBusy for EACCES, else FatalError)."""
    store = LabelStore(paths, OLD)

    def boom(*_a: object, **_k: object) -> None:
        raise OSError(errno.EACCES, "locked")

    monkeypatch.setattr(pq, "write_table", boom)
    with pytest.raises(StoreBusy):
        store.append("human", _human())
    assert not list(paths.labels_dir(OLD, "human").iterdir())  # no tmp file left behind
    paths.labels_dir(OLD, "teacher").mkdir(parents=True)
    monkeypatch.setattr(ds, "dataset", boom)
    with pytest.raises(StoreBusy):
        store.read("teacher")

    def eio(*_a: object, **_k: object) -> None:
        raise OSError(errno.EIO, "disk")

    monkeypatch.setattr(ds, "dataset", eio)
    with pytest.raises(FatalError):
        store.read("teacher")


# UT03-73 ------------------------------------------------------------------------------------


def test_ut03_73_frozen_gold_refuses_append(paths: EnrichPaths) -> None:
    """UT03-73: appending gold rows for a frozen (qid, fingerprint) raises ConfigError."""
    store = LabelStore(paths, OLD)
    store.append("gold", _gold([("q_a", FP_A, H1, "true", 0)]))
    assert not store.is_gold_frozen("q_a", FP_A)
    store.freeze_gold("q_a", FP_A, DIGEST, 1)
    store.freeze_gold("q_a", FP_A, DIGEST, 1)  # same freeze again is a no-op
    assert store.is_gold_frozen("q_a", FP_A)
    marker = paths.labels_dir(OLD, "gold") / "_frozen" / f"q_a-{FP_A}.json"
    body = json.loads(marker.read_text(encoding="utf-8"))
    assert body["digest"] == DIGEST
    assert body["n"] == 1
    assert "frozen_at" in body
    with pytest.raises(ConfigError, match="gold frozen for q_a"):
        store.append("gold", _gold([("q_a", FP_A, H2, "false", 1)]))
    assert store.append("gold", _gold([("q_a", FP_B, H2, "false", 1)])) is not None
    with pytest.raises(ConfigError):
        store.freeze_gold("q_a", FP_A, "f" * 64, 2)
    with pytest.raises(ConfigError):
        store.freeze_gold("q_a", FP_B, "nothex", 1)
    with pytest.raises(ConfigError):
        store.freeze_gold("q_a", FP_B, DIGEST, -1)
    with pytest.raises(ConfigError):
        store.is_gold_frozen("../q", FP_A)
    with pytest.raises(ConfigError):
        store.is_gold_frozen("q_a", "zz")


def test_ut03_73_migrate_twice(paths: EnrichPaths) -> None:
    """UT03-73: migrate_from copies unchanged questions' rows and markers once."""
    old = LabelStore(paths, OLD)
    old.append("teacher", _teacher("q_a", FP_A))
    old.append("teacher", _teacher("q_b", FP_B))  # fingerprint changed: not copied
    old.append("human", _human(3))
    old.append("gold_reviews", _human(1, prefix="rev_g"))
    old.append("gold", _gold([("q_a", FP_A, H1, "true", 0), ("q_b", FP_B, H2, "db", 1)]))
    old.freeze_gold("q_a", FP_A, DIGEST, 1)
    old.freeze_gold("q_b", FP_B, DIGEST, 1)
    old.freeze_gold("q_c", FP_C, DIGEST, 0)  # no gold rows: marker not copied
    new = LabelStore(paths, NEW)
    assert new.migrate_from(OLD, QS_NEW) == 6
    assert new.migrate_from(OLD, QS_NEW) == 6
    assert new.read("teacher").num_rows == 1
    assert new.read("human").num_rows == 3
    assert new.read("gold_reviews").num_rows == 1
    assert new.read("gold").column("question").to_pylist() == ["q_a"]
    assert new.is_gold_frozen("q_a", FP_A)
    assert not new.is_gold_frozen("q_b", FP_B)
    assert not new.is_gold_frozen("q_c", FP_C)
    assert (paths.data_root / "labels" / NEW / f"_migrated_from_{OLD}.json").is_file()


def test_ut03_73_migrate_preconditions(paths: EnrichPaths) -> None:
    """UT03-73: migrate_from refuses equal or mismatched versions and a bad marker."""
    store = LabelStore(paths, NEW)
    with pytest.raises(ConfigError):
        store.migrate_from(NEW, QS_NEW)
    with pytest.raises(ConfigError):
        store.migrate_from(OTHER, QS_OLD)
    assert store.migrate_from(OTHER, QS_NEW) == 0
    (paths.data_root / "labels" / NEW / f"_migrated_from_{OLD}.json").write_text("[]", "utf-8")
    with pytest.raises(ConfigError):
        store.migrate_from(OLD, QS_NEW)


# UT03-74 ------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    """`decide_review_item` audits under the caller's config; these tests need no audit."""

    def fake(event: str, actor: str, **fields: object) -> None:
        return None

    monkeypatch.setattr(ops_shared, "audit", fake)


def _item(
    step: int, *, status: Literal["approved", "rejected"] = "approved", **payload: object
) -> str:
    """Create and decide a `label_check` item; `note` is taken from `payload` when given."""
    note = payload.pop("note", None)
    payload = {
        "record_id": f"INC-{step}",
        "content_hash": f"{step:032x}",
        "question": "q_a",
        "question_fingerprint": FP_A,
        "question_set_version": OLD,
        "answer": "true",
        "probability": 0.8,
        "decider": "laya",
        "decider_version": "laya-20260901-1",
        "purpose": "spot_check",
        "text_ref": "enrich.text_redacted",
    } | payload
    item_id = create_review_item("label_check", payload, now=T0)
    decided = T0 + datetime.timedelta(minutes=step)
    decide_review_item(item_id, status, decided_by=USER, note=cast("str | None", note), now=decided)
    return item_id


def test_ut03_74_sync_twice(ops_store: OpsStoreHandle, paths: EnrichPaths) -> None:
    """UT03-74: approved/rejected/invalid/other-qsv/gold items sync once; rerun adds nothing."""
    store = LabelStore(paths, OLD)
    noted = _item(1, note=json.dumps({"answer": "false"}))
    _item(2)  # no note: payload answer
    _item(3, note=json.dumps({"comment": "ok"}))  # note without answer: payload answer
    _item(4, status="rejected")
    _item(5, note=json.dumps({"answer": "maybe"}))  # invalid label
    _item(6, question_set_version=OTHER)
    _item(7, purpose="gold")
    _item(8, purpose="ensemble_disagreement", question="q_b", answer="db")
    _item(9, question="q_c", answer="3")
    _item(10, question="q_c", answer="4")  # invalid score level
    _item(11, question="q_zz")  # unknown question
    _item(12, purpose="other")  # unknown purpose
    with capture_logs() as logs:
        counts = sync_label_checks(store, qs=QS_OLD)
    assert counts == {"human": 5, "gold_reviews": 1, "skipped": 5}
    invalid = [e for e in logs if e["event"] == "enrich.labels.invalid_answer"]
    assert len(invalid) == 4  # items 5, 10, 11 and the unknown purpose 12
    assert all(e["log_level"] == "warning" and "item_id" in e for e in invalid)
    assert any(e["event"] == "enrich.labels.synced" for e in logs)
    human = store.read("human")
    by_item = dict(zip(human.column("item_id").to_pylist(), human.to_pylist(), strict=True))
    assert by_item[noted]["answer"] == "false"
    assert by_item[noted]["labeled_by"] == USER
    assert by_item[noted]["labeled_at"] == T0 + datetime.timedelta(minutes=1)
    assert store.read("gold_reviews").num_rows == 1
    assert sync_label_checks(store, qs=QS_OLD) == {"human": 0, "gold_reviews": 0, "skipped": 0}
    watermark = json.loads((paths.data_root / "labels" / OLD / "_sync.json").read_text("utf-8"))
    assert watermark["last_item_id"].startswith("rev_")
    _item(13)
    assert sync_label_checks(store, qs=QS_OLD)["human"] == 1
    assert store.read("human").num_rows == 6


def test_ut03_74_dedupe_and_paging(
    ops_store: OpsStoreHandle, paths: EnrichPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-74: a lost watermark re-reads pages but known item_ids are not appended again."""
    store = LabelStore(paths, OLD)
    for step in range(1, 8):
        _item(step)
    monkeypatch.setattr(labels, "_PAGE", 3)
    calls: list[object] = []

    def spy(**kwargs: Any) -> list[ReviewItem]:
        calls.append(kwargs["decided_after"])
        return list_review_items(**kwargs)

    monkeypatch.setattr(labels, "list_review_items", spy)
    assert sync_label_checks(store, qs=QS_OLD)["human"] == 7
    assert len(calls) == 3
    (paths.data_root / "labels" / OLD / "_sync.json").unlink()
    assert sync_label_checks(store, qs=QS_OLD)["human"] == 0
    assert store.read("human").num_rows == 7


def test_ut03_74_bad_watermark_and_busy_lock(
    ops_store: OpsStoreHandle, paths: EnrichPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-74: an unreadable watermark is a ConfigError; a held lock is StoreBusy."""
    store = LabelStore(paths, OLD)
    watermark = paths.data_root / "labels" / OLD / "_sync.json"
    watermark.parent.mkdir(parents=True)
    watermark.write_text('{"last_decided_at": 3}', encoding="utf-8")
    with pytest.raises(ConfigError):
        sync_label_checks(store, qs=QS_OLD)
    watermark.unlink()
    with pytest.raises(ConfigError):
        sync_label_checks(store, qs=QS_NEW)  # question set of another version

    def busy(lock_path: Path, *, timeout_s: float = 10.0) -> Any:
        assert lock_path == paths.data_root / "locks" / "labels.lock"
        assert timeout_s == 10.0
        msg = "lock busy"
        raise StoreBusy(msg)

    monkeypatch.setattr(labels, "log_lock", busy)
    with pytest.raises(StoreBusy):
        sync_label_checks(store, qs=QS_OLD)


def test_ut03_74_last_page_ends_on_other_version(
    ops_store: OpsStoreHandle, paths: EnrichPaths
) -> None:
    """UT03-74: a last page ending on another version's item advances the watermark past it."""
    store = LabelStore(paths, OLD)
    _item(1)
    last = _item(2, question_set_version=OTHER)
    assert sync_label_checks(store, qs=QS_OLD) == {"human": 1, "gold_reviews": 0, "skipped": 0}
    watermark = json.loads((paths.data_root / "labels" / OLD / "_sync.json").read_text("utf-8"))
    assert watermark["last_item_id"] == last
    assert datetime.datetime.fromisoformat(watermark["last_decided_at"]) == T0 + datetime.timedelta(
        minutes=2
    )
    assert sync_label_checks(store, qs=QS_OLD) == {"human": 0, "gold_reviews": 0, "skipped": 0}
    assert store.read("human").num_rows == 1


def test_ut03_74_dynamic_choice_labels(ops_store: OpsStoreHandle, paths: EnrichPaths) -> None:
    """UT03-74: a dynamic choice question accepts any option-key-shaped answer."""
    team = _q("q_team", FP_A, "choice", options_source="core.team")
    qs = QuestionSet(version=OLD, questions=(team,))
    store = LabelStore(paths, OLD)
    _item(1, question="q_team", answer="team-7")
    _item(2, question="q_team", answer="")
    assert sync_label_checks(store, qs=qs) == {"human": 1, "gold_reviews": 0, "skipped": 1}


# UT03-75 / PT03-09 --------------------------------------------------------------------------

_GOLD_ROWS = [
    ("q_a", FP_A, H1, "true", 0),
    ("q_a", FP_A, H2, "false", 1),
    ("q_b", FP_B, H1, "db", 0),
    ("q_b", FP_B, H3, "net", 1),
]


def test_ut03_75_digest_independent_of_parts(paths: EnrichPaths, tmp_path: Path) -> None:
    """UT03-75: the same rows shuffled into different parts give equal digests."""
    one = LabelStore(paths, OLD)
    one.append("gold", _gold(_GOLD_ROWS))
    other_paths = EnrichPaths(data_root=tmp_path / "d2", embedding_path="e", laya_current_file="c")
    two = LabelStore(other_paths, OLD)
    shuffled = list(_GOLD_ROWS)
    random.Random(7).shuffle(shuffled)
    two.append("gold", _gold(shuffled[:1]))
    two.append("gold", _gold(shuffled[1:]))
    digest = gold_digest(one.read("gold"))
    assert digest == gold_digest(two.read("gold"))
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)
    changed = _gold([*_GOLD_ROWS[:3], ("q_b", FP_B, H3, "db", 1)])
    assert gold_digest(changed) != digest
    assert gold_digest(_gold([])) == gold_digest(GOLD_SCHEMA.empty_table())


def test_ut03_75_digest_known_answer() -> None:
    """UT03-75: the digest equals a hand-computed SHA-256 of the spec's canonical lines."""
    # sorted by (question, content_hash); canonical JSON of
    # [question, question_fingerprint, content_hash, answer, fold]; joined with a newline
    lines = [
        f'["q_a","{FP_A}","{H1}","true",0]',
        f'["q_b","{FP_B}","{H1}","db",1]',
    ]
    expected = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    # a SHA-256 known answer, not a secret
    known = (
        "d55758a759c8d4abaca339ac56e1b45d"  # pragma: allowlist secret
        "df443f18926ee566ceae159231b096e9"  # pragma: allowlist secret
    )
    assert expected == known
    table = _gold([("q_b", FP_B, H1, "db", 1), ("q_a", FP_A, H1, "true", 0)])
    assert gold_digest(table) == expected


@given(st.permutations(list(range(len(_GOLD_ROWS) + 2))))
@settings(max_examples=50, deadline=None)
def test_pt03_09_digest_permutation_invariant(order: list[int]) -> None:
    """PT03-09: the gold digest is invariant under row permutation (duplicate keys too)."""
    rows = [*_GOLD_ROWS, ("q_a", FP_A, H1, "false", 1), ("q_a", FP_A, H1, "true", 0)]
    assert gold_digest(_gold([rows[i] for i in order])) == gold_digest(_gold(rows))
