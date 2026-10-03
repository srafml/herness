"""Shared environment for the T03-32 distillation tests (IT03-15, FT03-05).

`distill_env` builds, on the real migrated ops store (``jev_env``): a small warehouse, a
config stand-in served by ``_distill_steps.get_config`` (two questions, gold_size 100), seeded
gold reviews (two agreeing reviewers per gold hash), a stub OpenJev teacher behind
``build_decider``, a recording fake trainer behind ``select_trainer`` and the fake `laya`
module for candidate inference. `FakeCtx` is a JobContext that records GPU scopes, services,
saved states and heartbeats.
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import json
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import duckdb
import pyarrow as pa
import pytest
from tests.support.fake_laya import WEIGHT_FILES, FakeLayaAgent, fake_laya_module
from tests.unit.enrich._sampling_support import Rec, many, warehouse

from herness.core.errors import ModelUnavailable
from herness.core.jobs import JobContext
from herness.core.types import Answer, DecisionInput, DecisionOutput, Question, QuestionSet
from herness.enrich import _distill_steps as steps
from herness.enrich import distill as distill_mod
from herness.enrich.gpu import YieldRequested
from herness.enrich.labels import HUMAN_SCHEMA, LabelStore
from herness.enrich.laya_trainer import TrainHyper, TrainingSet, TrainResult
from herness.enrich.layout import EnrichPaths
from herness.enrich.questions import load_question_set
from herness.enrich.settings import DecidersSettings, DecisionsConfig

QSV = "qs-2026-10-04"
N_GOLD = 110
N_POOL = 160
REVIEWERS = ("ab" * 16, "cd" * 16)
T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


def decisions(**distill: object) -> DecisionsConfig:
    return DecisionsConfig.model_validate(
        {
            "question_set_version": QSV,
            "questions": [
                {"id": "q_c", "type": "choice", "applies_to": ["incident"],
                 "instructions": "Which root cause class is it?",
                 "options": {"a": "A.", "b": "B.", "c": "C.", "d": "D."}, "threshold": 0.7},
                {"id": "q_b", "type": "bool", "applies_to": ["incident"],
                 "instructions": "Did a change cause it?", "threshold": 0.8},
            ],
            "distill": {"gold_size": 100, **distill},
            "change_link": {"use_decider": False},
        }
    )  # fmt: skip


def stub_answer(content_hash: str, q: Question) -> tuple[str, float]:
    """The stub teacher's (answer, probability): a pure function of the hash."""
    value = int(content_hash, 16)
    prob = 0.6 if value % 3 == 0 else 0.9
    if q.type == "bool":
        return ("true" if value % 2 else "false"), prob
    options = list(q.options or {})
    return options[value % len(options)], prob


def _labels(q: Question) -> list[str]:
    return ["true", "false"] if q.type == "bool" else list(q.options or {})


def _answer(content_hash: str, q: Question) -> Answer:
    answer, prob = stub_answer(content_hash, q)
    others = [label for label in _labels(q) if label != answer]
    dist = {answer: prob, **{label: (1 - prob) / len(others) for label in others}}
    return Answer(answer=answer, probability=prob, distribution=dist)


@dataclasses.dataclass
class StubTeacher:
    """In-process OpenJev stand-in (the ``stub teacher`` of IT03-15)."""

    name: str = "openjev"
    version: str = "openjev-test"
    healthy: bool = True
    decided: list[str] = dataclasses.field(default_factory=list)

    def decide(self, items: list[DecisionInput], questions: QuestionSet) -> list[DecisionOutput]:
        outputs = []
        for item in items:
            self.decided.append(item.content_hash)
            asked = questions.for_entity(item.entity).questions
            outputs.append(DecisionOutput(
                record_id=item.record_id, content_hash=item.content_hash, decider="openjev",
                decider_version=self.version,
                answers={q.id: _answer(item.content_hash, q) for q in asked},
            ))  # fmt: skip
        return outputs

    def health(self) -> None:
        if not self.healthy:
            msg = "openjev"
            raise ModelUnavailable(msg)


@dataclasses.dataclass
class FakeTrainer:
    """Writes the fake Laya weights; records every `TrainingSet` (gold-exclusion check)."""

    name: str = "sft"
    yield_first: bool = False
    seen: list[TrainingSet] = dataclasses.field(default_factory=list)
    init_dirs: list[Path] = dataclasses.field(default_factory=list)

    def train(self, data: TrainingSet, *, init_dir: Path, out_dir: Path, hyper: TrainHyper,
              ctx: JobContext) -> TrainResult:  # fmt: skip
        self.seen.append(data)
        self.init_dirs.append(init_dir)
        (out_dir / "checkpoints" / "epoch-1").mkdir(parents=True, exist_ok=True)
        (out_dir / "checkpoints" / "epoch-1" / "state.json").write_text("{}", "utf-8")
        if self.yield_first:
            self.yield_first = False
            stage = "train"
            raise YieldRequested(stage)
        files = []
        for name, content in WEIGHT_FILES.items():
            (out_dir / name).write_bytes(content)
            files.append(out_dir / name)
        return TrainResult(epochs_run=1, best_val_nll=0.5, files=tuple(files))


@dataclasses.dataclass
class FakeServices:
    events: list[str]
    fail_start: bool = False

    def start(self, name: str, *, timeout_s: float | None = None) -> None:
        self.events.append(f"start:{name}")
        if self.fail_start:
            msg = "openjev start"
            raise ModelUnavailable(msg)

    def stop(self, name: str) -> None:
        self.events.append(f"stop:{name}")

    def healthy(self, name: str) -> bool:
        return True


@dataclasses.dataclass
class FakeCtx:
    """JobContext stand-in: scopes and services in one ordered ``events`` list."""

    payload: dict[str, Any] = dataclasses.field(default_factory=dict)
    yield_at: int | None = None  # `should_yield` is true on this check (1-based)
    events: list[str] = dataclasses.field(default_factory=list)
    saved: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    checks: int = 0
    heartbeats: list[str | None] = dataclasses.field(default_factory=list)
    fail_start: bool = False

    @property
    def job(self) -> SimpleNamespace:
        return SimpleNamespace(payload=self.payload)

    @property
    def services(self) -> FakeServices:
        services = FakeServices(self.events, self.fail_start)
        return services

    @contextlib.contextmanager
    def gpu_scope(self, cls: str) -> Iterator[None]:
        self.events.append(f"enter:{cls}")
        try:
            yield
        finally:
            self.events.append(f"exit:{cls}")

    def save_state(self, state: dict[str, Any]) -> None:
        self.saved.append(copy.deepcopy(state))

    def load_state(self) -> dict[str, Any]:
        return copy.deepcopy(self.saved[-1]) if self.saved else {}

    def should_yield(self) -> bool:
        self.checks += 1
        return self.yield_at is not None and self.checks == self.yield_at

    def heartbeat(self, note: str | None = None) -> None:
        self.heartbeats.append(note)

    def as_ctx(self) -> JobContext:
        return cast(JobContext, self)

    def steps(self) -> list[str]:
        return [state["distill"]["step"] for state in self.saved]


@dataclasses.dataclass
class DistillEnv:
    root: Path
    paths: EnrichPaths
    qs: QuestionSet
    store: LabelStore
    wh: duckdb.DuckDBPyConnection
    teacher: StubTeacher
    trainer: FakeTrainer
    agent: FakeLayaAgent
    cfg: SimpleNamespace
    gold: list[str]
    records: list[Rec]

    def set_decisions(self, cfg: DecisionsConfig) -> None:
        self.cfg.decisions = cfg

    def teacher_rows(self) -> pa.Table:
        return self.store.read("teacher")


def _gold_reviews(qs: QuestionSet, gold: list[Rec]) -> pa.Table:
    rows = [
        {"content_hash": rec.hash, "record_id": rec.record_id, "question": q.id,
         "question_fingerprint": q.fingerprint, "answer": stub_answer(rec.hash, q)[0],
         "labeled_by": user, "labeled_at": T0, "item_id": f"g-{q.id}-{n}-{i}"}
        for n, rec in enumerate(gold) for q in qs.questions
        for i, user in enumerate(REVIEWERS)
    ]  # fmt: skip
    return pa.Table.from_pylist(rows, schema=HUMAN_SCHEMA)


def build_env(root: Path, monkeypatch: pytest.MonkeyPatch, **distill: object) -> DistillEnv:
    """The distill environment under ``root`` (the ops store's data root)."""
    cfg_decisions = decisions(**distill)
    deciders = DecidersSettings.model_validate({"laya": {"device": "cpu", "dtype": "fp32"}})
    cfg = SimpleNamespace(decisions=cfg_decisions, models=SimpleNamespace(deciders=deciders),
                          paths=SimpleNamespace(data=str(root)))  # fmt: skip
    paths = EnrichPaths.from_config(cfg)  # type: ignore[arg-type]
    qs = load_question_set(cfg_decisions)
    records = many(N_GOLD + N_POOL)
    wh = warehouse(records)
    store = LabelStore(paths, QSV)
    gold = records[:N_GOLD]
    store.append("gold_reviews", _gold_reviews(qs, gold))
    teacher, trainer, agent = StubTeacher(), FakeTrainer(), FakeLayaAgent()
    base = paths.laya_root() / steps.BASE_DIR
    base.mkdir(parents=True)
    for name, content in WEIGHT_FILES.items():
        (base / name).write_bytes(content)
    monkeypatch.setattr(steps, "get_config", lambda: cfg)
    monkeypatch.setattr(steps, "open_readonly", lambda _build=None: wh.cursor())
    monkeypatch.setattr(distill_mod, "build_decider", lambda name, **_: teacher)
    monkeypatch.setattr(steps, "select_trainer", lambda: trainer)
    monkeypatch.setitem(sys.modules, "laya", fake_laya_module(agent))
    return DistillEnv(root, paths, qs, store, wh, teacher, trainer, agent, cfg,
                      [r.hash for r in gold], records)  # fmt: skip


def read_json(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text("utf-8")))
