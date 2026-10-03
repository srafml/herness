"""ST03-05 (TH03-04): gold hashes seeded into the sample pool never reach the sample, the
teacher parts or the training set; sample SQL is fixed text with bound values only, and no
sample text reaches a log event (T03-29)."""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pytest
from structlog.testing import capture_logs
from tests.unit.enrich._sampling_support import many, proto_reader, snapshot, warehouse

from herness.core.types import Question, QuestionSet
from herness.enrich import sampling
from herness.enrich.labels import GOLD_SCHEMA, HUMAN_SCHEMA, TEACHER_SCHEMA, LabelStore
from herness.enrich.laya_trainer import build_training_set
from herness.enrich.layout import EnrichPaths
from herness.enrich.sampling import stratified_sample

pytestmark = pytest.mark.unit

QSV = "qs-2026-10-01"
FP = "a" * 16
T0 = datetime.datetime(2026, 10, 1, tzinfo=datetime.UTC)
Q = Question.model_validate(
    {"id": "q_a", "type": "bool", "instructions": "Is this a thing?", "threshold": 0.7,
     "fingerprint": FP}
)  # fmt: skip
QS = QuestionSet(version=QSV, questions=(Q,))
SALT = "train:v1'; DROP TABLE core.incident; --"


class _Spy:
    """Records every statement and its parameters, delegating to a real connection."""

    def __init__(self, wh: duckdb.DuckDBPyConnection) -> None:
        self.wh = wh
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def register(self, name: str, table: pa.Table) -> None:
        self.wh.register(name, table)

    def unregister(self, name: str) -> None:
        self.wh.unregister(name)

    def execute(self, sql: str, params: dict[str, Any]) -> duckdb.DuckDBPyConnection:
        self.calls.append((sql, params))
        return self.wh.execute(sql, params)


def _gold_rows(hashes: list[str]) -> pa.Table:
    rows = [
        {"content_hash": h, "record_id": f"INC-{i}", "question": "q_a",
         "question_fingerprint": FP, "answer": "true", "labeled_by": "ab" * 16,
         "labeled_at": T0, "item_id": f"rev_{i}", "fold": 0, "adjudicated": False}
        for i, h in enumerate(hashes)
    ]  # fmt: skip
    return pa.Table.from_pylist(rows, schema=GOLD_SCHEMA)


def _teacher_rows(hashes: list[str]) -> pa.Table:
    rows = [
        {"content_hash": h, "record_id": f"INC-{i}", "question": "q_a",
         "question_fingerprint": FP, "answer": "true",
         "distribution": [("true", 0.8), ("false", 0.2)], "decider": "openjev",
         "decider_version": "oj-1", "round": 0, "stratum": "s", "purpose": "initial"}
        for i, h in enumerate(hashes)
    ]  # fmt: skip
    return pa.Table.from_pylist(rows, schema=TEACHER_SCHEMA)


def test_st03_05_gold_hashes_never_sampled_taught_or_trained(tmp_path: Path) -> None:
    """ST03-05: gold hashes seeded into the pool are not in the sample, the teacher parts or
    the training set; no sample text is logged; the SQL binds every value."""
    records = many(300)
    texts = {r.hash: r.text for r in records}
    store = LabelStore(EnrichPaths(data_root=tmp_path / "data", embedding_path="data/e",
                                   laya_current_file="c"), QSV)  # fmt: skip
    seeded = [r.hash for r in records[::3]]  # a third of the pool is gold
    store.append("gold", _gold_rows(seeded))
    gold = frozenset(store.gold_hashes())
    assert gold == set(seeded)
    spy = _Spy(warehouse(records))
    with capture_logs() as logs:
        sample = stratified_sample(
            spy,  # type: ignore[arg-type]
            qs=QS, size=150, exclude_hashes=gold, salt=SALT, snapshot=snapshot(),
            vector_reader=proto_reader(lambda h: int(h, 16)),
        )  # fmt: skip
    sampled = sample.column("content_hash").to_pylist()
    assert sampled
    assert not set(sampled) & gold

    # teacher parts: the distill job appends the sample's teacher rows (step 5)
    store.append("teacher", _teacher_rows(sampled))
    assert not set(store.read("teacher").column("content_hash").to_pylist()) & gold

    # training set: even teacher rows that name gold hashes are dropped
    teacher = pa.concat_tables([store.read("teacher"), _teacher_rows(seeded[:20])])
    training = build_training_set(teacher, HUMAN_SCHEMA.empty_table(), texts=texts,
                                  questions=QS, gold=gold)  # fmt: skip
    trained = training.train.column("content_hash").to_pylist()
    trained += training.val.column("content_hash").to_pylist()
    assert trained
    assert not set(trained) & gold

    # logs: counts only, never sample text
    dumped = json.dumps(logs, default=str)
    assert not any(text in dumped for text in sample.column("text").to_pylist())

    # SQL: fixed statements, values bound (the hostile salt is a parameter, not SQL text)
    fixed = {sampling._SIZES_SQL, sampling._CANDIDATES_SQL, sampling._POOL_SQL}
    assert spy.calls
    for sql, params in spy.calls:
        assert sql in fixed
        assert set(params) <= {"entities", "salt"}
        assert not any(h in sql for h in gold)
    assert any(params.get("salt") == SALT for _, params in spy.calls)
    assert spy.wh.execute("SELECT count(*) FROM core.incident").fetchone() == (300,)
