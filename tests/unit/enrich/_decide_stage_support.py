"""Shared fakes and fixtures for the decide-stage tests (UT03-79 ... UT03-82, FT03-01, FT03-03).

`ScriptedDecider` stands in for Laya, the teacher and the LLM decider: it answers every asked
question with probability 0.95 (bool `true`, first option, score `2`), records the record ids
of each call, and can raise, return item errors or be killed (the stub OpenJev service).
`FakeGpu` is a `GpuStateReader` whose `openjev` health follows the stub's liveness.
`decide_env` loads the full test config (Laya `call_batch` 1), binds a migrated ops store as
the resilience backend and installs a test redactor, as the resilience unit tests do.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pytest
from tests.support.config_tree import write_full_config
from tests.support.fake_keyring import MemoryKeyring
from tests.support.ops_store import OpsStoreHandle

from herness.core import config as c
from herness.core import redact as r
from herness.core.redact_directory import NameDirectory
from herness.core.resilience import ProcessState, bind_ops_backend
from herness.core.settings import RedactionConfig
from herness.core.types import (
    Answer,
    DecisionInput,
    DecisionOutput,
    GpuClass,
    Question,
    QuestionSet,
    ServiceName,
)
from herness.enrich.cache import CACHE_SCHEMA, DecisionCache
from herness.enrich.calibrate import CalibrationStore
from herness.enrich.decide_stage import ResolveArgs
from herness.enrich.labels import LabelStore
from herness.enrich.layout import EnrichPaths
from herness.enrich.questions import question_fingerprint
from herness.enrich.settings import DecidersSettings, DecisionsConfig
from herness.store.ops.resilience import SqliteResilienceBackend

SETTINGS_SQL = Path(__file__).resolve().parents[3] / "herness/model/sql/000_settings.sql"
QSV = "qs-2026-10-01.1"
NOW = datetime(2026, 9, 26, tzinfo=UTC)
LAYA_V = "laya-20260901-1"
VERSIONS = {"laya": LAYA_V, "openjev": "v1", "llm": "v1"}


def question(qid: str, qtype: str, **kw: Any) -> Question:
    extra: dict[str, Any] = {}
    if qtype == "choice":
        extra["options"] = {"a": "Option A.", "b": "Option B."}
    if qtype == "score":
        extra["levels"] = ("None at all.", "Low level.", "Medium level.", "High level.")
    base = {"id": qid, "type": qtype, "instructions": "Classify this ticket with care, please."}
    return Question.model_validate({**base, "threshold": 0.7, **extra, **kw})


QS = QuestionSet(
    version=QSV,
    questions=(
        question("q_bool", "bool"),
        question("q_choice", "choice", scoring_use=False, applies_to=("incident", "change")),
        question("q_score", "score"),
        question("change_caused_pair", "bool", scoring_use=False),
    ),
)
TEACHER_PRIMARIES = {q.id: "openjev" for q in QS.questions}
FPS = {q.id: question_fingerprint(q) for q in QS.questions}


def digest(n: int) -> str:
    return f"{n:032x}"


def rid(n: int) -> str:
    return f"inc_{n:03d}"


def warehouse(
    records: Sequence[tuple[str, str, str, datetime | None]],
) -> duckdb.DuckDBPyConnection:
    """In-memory warehouse: settings DDL, `core.<entity>(record_id, opened_at)`, text rows."""
    wh = duckdb.connect()
    wh.execute(SETTINGS_SQL.read_text("utf-8"))
    for entity in ("incident", "change", "problem"):
        wh.execute(f"CREATE TABLE core.{entity} (record_id VARCHAR, opened_at TIMESTAMPTZ)")
    for record_id, entity, content_hash, opened in records:
        wh.execute(
            "INSERT INTO enrich.text_redacted VALUES (?, ?, ?, ?)",
            [record_id, entity, f"ticket text of {record_id}", content_hash],
        )
        wh.execute(f"INSERT INTO core.{entity} VALUES (?, ?)", [record_id, opened])  # noqa: S608
    return wh


def incidents(count: int, *, start: int = 1) -> list[tuple[str, str, str, datetime | None]]:
    """`count` incidents opened in consecutive recent hours (newest has the highest number)."""
    return [
        (rid(n), "incident", digest(n), NOW - timedelta(hours=200 - n))
        for n in range(start, start + count)
    ]


def _answer(q: Question, probability: float) -> Answer:
    if q.type == "bool":
        return Answer(
            answer="true",
            probability=probability,
            distribution={"true": probability, "false": 1 - probability},
        )
    labels = ["0", "1", "2", "3"] if q.type == "score" else sorted(q.options or {})
    top = labels[2] if q.type == "score" else labels[0]
    rest = (1 - probability) / (len(labels) - 1)
    dist = {label: probability if label == top else rest for label in labels}
    return Answer(answer=top, probability=probability, distribution=dist)


@dataclass
class ScriptedDecider:
    """Deterministic decider; `fail(items)` may return an exception to raise for a call."""

    name: str
    version: str = "v1"
    probability: float = 0.95
    fail: Callable[[Sequence[DecisionInput]], BaseException | None] | None = None
    item_errors: dict[str, int] = field(default_factory=dict)  # record_id -> errors left
    samples: int = 3
    alive: bool = True
    calls: list[list[str]] = field(default_factory=list)
    loaded: int = 0
    unloaded: int = 0

    def load(self) -> None:
        self.loaded += 1

    def unload(self) -> None:
        self.unloaded += 1

    def health(self) -> None:
        return None

    def kill(self, service: str) -> None:
        assert service == "openjev"
        self.alive = False

    def decide(self, items: Sequence[DecisionInput], qs: QuestionSet) -> list[DecisionOutput]:
        self.calls.append([item.record_id for item in items])
        error = self.fail(items) if self.fail is not None else None
        if not self.alive:
            from herness.core.errors import ModelUnavailable  # noqa: PLC0415

            error = ModelUnavailable("stub openjev is down")
        if error is not None:
            raise error
        return [self._output(item, qs) for item in items]

    def _output(self, item: DecisionInput, qs: QuestionSet) -> DecisionOutput:
        base = {"record_id": item.record_id, "content_hash": item.content_hash,
                "decider": self.name, "decider_version": self.version}  # fmt: skip
        left = self.item_errors.get(item.record_id, 0)
        if left:
            self.item_errors[item.record_id] = left - 1
            return DecisionOutput.model_validate({**base, "answers": {}, "error": "ModelRefused"})
        qids = item.question_ids or tuple(q.id for q in qs.questions)
        answers = {qid: _answer(qs.get(qid), self.probability) for qid in qids}
        return DecisionOutput.model_validate({**base, "answers": answers})

    @property
    def decided(self) -> int:
        return sum(len(call) for call in self.calls)


@dataclass
class FakeGpu:
    """`GpuStateReader`: the decider class loaded; `openjev` healthy while the stub lives."""

    teacher: ScriptedDecider | None = None
    loaded: GpuClass = "decider"

    def loaded_class(self) -> GpuClass:
        return self.loaded

    def service_healthy(self, name: ServiceName) -> bool:
        return name == "openjev" and self.teacher is not None and self.teacher.alive


@dataclass
class Report:
    """A stand-in for `StageReport` (U03-142) with the fields the stages touch."""

    status: str = "done"
    note: str | None = None
    decided: int = 0
    escalated: int = 0
    failed: int = 0


def decisions_cfg(**escalation: int) -> DecisionsConfig:
    return DecisionsConfig.model_validate(
        {
            "question_set_version": QSV,
            "escalation_chain": ("openjev", "llm"),
            "questions": (),
            "change_link": {"use_decider": False, "decider_max_pairs": 2},
            "escalation": escalation,
        }
    )


@dataclass
class DecideEnv:
    """Stores under `tmp_path` and a `ResolveArgs` factory."""

    paths: EnrichPaths
    cache: DecisionCache
    labels: LabelStore
    calibration: CalibrationStore

    def resolve_args(
        self, cfg: DecisionsConfig, primaries: dict[str, str] | None = None
    ) -> ResolveArgs:
        return ResolveArgs(
            cfg=cfg,
            deciders=DecidersSettings(),
            labels=self.labels,
            calibration=self.calibration,
            primaries=primaries or TEACHER_PRIMARIES,
            versions=VERSIONS,
            now=NOW,
        )

    def rows(self) -> list[dict[str, Any]]:
        dataset = self.cache.dataset()
        return [] if dataset is None else dataset.to_table().to_pylist()

    def write_rows(self, decider: str, version: str, rows: list[tuple[str, str, str, float]]):
        """Cache rows (content_hash, question, answer, p of `true`/answer) for one partition."""
        from herness.enrich.cache import write_part  # noqa: PLC0415

        table = pa.Table.from_pylist(
            [
                {"content_hash": ch, "question": qid, "question_fingerprint": FPS[qid],
                 "answer": ans, "probability": p, "distribution": [("true", p), ("false", 1 - p)],
                 "backend_confidence": None, "samples": None, "decided_at": NOW}
                for ch, qid, ans, p in rows
            ],
            schema=CACHE_SCHEMA,
        )  # fmt: skip
        write_part(self.paths.cache_partition(QSV, decider, version), table)


@pytest.fixture
def decide_env(
    tmp_path: Path,
    fake_keyring: MemoryKeyring,
    ops_store: OpsStoreHandle,
    reset_process_state: ProcessState,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[DecideEnv]:
    """Full test config (Laya call_batch 1), ops backend bound, test redactor, enrich stores."""
    del fake_keyring, ops_store
    c.reset_config()
    c.init_config(
        "local",
        overrides=["models.deciders.laya.call_batch=1"],
        config_dir=write_full_config(tmp_path),
        env={},
    )
    bind_ops_backend(SqliteResilienceBackend())
    directory = NameDirectory.from_files(None, (), None)
    redactor = r.Redactor(RedactionConfig(directory_file=None), bytes(range(32)), directory)
    monkeypatch.setattr(r._State, "redactor", redactor)
    del reset_process_state
    paths = EnrichPaths(
        data_root=tmp_path / "enrich", embedding_path="data/e", laya_current_file="data/c"
    )
    yield DecideEnv(
        paths, DecisionCache(paths, QSV), LabelStore(paths, QSV), CalibrationStore(paths)
    )
    c.reset_config()
