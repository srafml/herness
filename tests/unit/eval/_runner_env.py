"""Shared environment of the eval runner tests (T11-30: UT11-116, ST11-15, CV checks).

A tmp data root whose `warehouse/CURRENT` names `BUILD_ID` (the build file is an empty
placeholder: `warehouse_for` hands out the in-memory fixture warehouse of `_golden_fixtures`
plus a `meta.build` row), the seed-42 truth manifest beside it, one ops `evidence` row for
`NUMERIC_SQL` so `count_unsupported` finds every cited number supported, a scripted chat
service (`AnswerChat`, optionally counting concurrent calls), a fake `JobContext` and the
`config/eval.yaml` default settings. Golden content belongs to T11-32, so suites are
written per test with `question`.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import duckdb
import pytest
from pydantic import JsonValue
from tests.support.ops_store import OpsStoreHandle
from tests.unit.eval._golden_fixtures import BUILD_ID, NUMERIC_SQL, make_con, write_suite
from tests.unit.eval.test_truth import _truth_payload
from tests.unit.harness._blackboard_env import test_redactor

from herness.core import time as clock
from herness.core.ids import IdKind, new_id
from herness.core.ids import query_id as compute_query_id
from herness.core.jobs.ports import JobRow
from herness.core.types import (
    ChatAnswer,
    ChatEvent,
    ChatMode,
    ErrorEvent,
    Evidence,
    FinalEvent,
    ModeEvent,
    NumberRef,
)
from herness.eval.runner import EvalDeps
from herness.eval.settings import EvalConfig
from herness.store.ops import record_evidence

__all__ = ["ANSWER", "BUILD_ID", "NUMERIC_QID", "AnswerChat", "FakeCtx", "chat_q", "make_deps",
           "make_root", "runner_store", "settings", "suite_file", "test_redactor"]  # fmt: skip

NUMERIC_QID = compute_query_id(NUMERIC_SQL, {}, BUILD_ID)
ANSWER = "Platform Ops had [[n1]] priority one incidents."
T0 = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)

SETTINGS: dict[str, Any] = {
    "suite": "tests/eval/golden.yaml",
    "judge": {"profile": "local-judge", "temperature": 0.0, "cache_dir": "data/cache/judge"},
    "repeat": {"fast": 1, "standard": 1, "deep": 3},
    "thresholds": {
        "unsupported_number_rate_max": 0.0, "correctness_drop_max_pp": 3.0,
        "correctness_floor": {"local-standard": 0.80, "local-deep": 0.85, "hybrid": 0.88,
                              "premium": 0.90},
        "tool_success_min": {"local": 0.90, "hybrid": 0.95, "premium": 0.95},
        "latency_p95_ratio_max": 1.25, "cost_ratio_max": 1.20, "bench_regression_max": 0.20,
    },
    "classifier": {"gate_recompute_tolerance": 0.005, "synthetic_sample": 5000,
                   "bootstrap": 1000},
    "baseline": "synthetic-42-small",
}  # fmt: skip


@pytest.fixture
def runner_store(ops_store: OpsStoreHandle, test_redactor: object) -> OpsStoreHandle:
    """`ops_store` with a fixed-key process redactor (the eval chat session stores the
    redacted question)."""
    del test_redactor
    return ops_store


def settings() -> EvalConfig:
    """The `config/eval.yaml` defaults (design 11 §7)."""
    return EvalConfig.model_validate(SETTINGS)


def make_root(data_root: Path, *, kind: str = "synthetic") -> duckdb.DuckDBPyConnection:
    """`CURRENT` + placeholder build file under `data_root`, truth beside it, the evidence
    row of `NUMERIC_SQL`; returns the fixture warehouse with a `meta.build` row."""
    wh = data_root / "warehouse"
    wh.mkdir(parents=True, exist_ok=True)
    (wh / f"wh-{BUILD_ID}.duckdb").write_bytes(b"")
    (wh / "CURRENT").write_text(BUILD_ID, encoding="ascii")
    truth_dir = data_root.parent / "truth"
    truth_dir.mkdir(exist_ok=True)
    (truth_dir / "truth.json").write_text(json.dumps(_truth_payload()), encoding="utf-8")
    con = make_con()
    con.execute("CREATE SCHEMA meta")
    con.execute("CREATE TABLE meta.build (build_id VARCHAR, dataset_kind VARCHAR)")
    con.execute("INSERT INTO meta.build VALUES (?, ?)", [BUILD_ID, kind])
    assert record_evidence(Evidence(
        query_id=NUMERIC_QID, run_id=None, build_id=BUILD_ID, sql=NUMERIC_SQL, params={},
        result_hash="0" * 64, row_count=1, result_sample=[{"value": 2}], executed_at=T0,
        duration_ms=1,
    ))  # fmt: skip
    return con


def chat_q(qid: str, **fields: Any) -> dict[str, Any]:
    """A chat question graded by `NUMERIC_SQL` (value 2, exact)."""
    raw: dict[str, Any] = {
        "id": qid, "pipeline": "chat", "question": "How many priority one incidents?",
        "expected": {"numeric": {"reference_sql": NUMERIC_SQL, "unit": "count",
                                 "tolerance": {"abs": 0}}},
    }  # fmt: skip
    raw.update(fields)
    return raw


def suite_file(path: Path, questions: list[dict[str, Any]]) -> Path:
    return write_suite(path, questions)


def final(
    value: int = 2, *, text: str = ANSWER, query_id: str = NUMERIC_QID, column: str = "value",
    run_id: str | None = None,
) -> FinalEvent:  # fmt: skip
    """A final answer citing `[[n1]]` = `value` of `query_id`'s `column`."""
    ref = NumberRef(id="n1", value=value, unit="count", query_id=query_id, column=column,
                    row_key=None)  # fmt: skip
    answer = ChatAnswer(text=text, numbers=[ref], query_ids=[query_id])
    return FinalEvent(answer=answer, run_id=run_id or new_id(IdKind.RUN))


@dataclass
class AnswerChat:
    """Chat stand-in: per question text, `errors` answer with an `ErrorEvent`, every other
    call with `final(value)`; counts the calls running at once (`delay_s` each)."""

    value: int = 2
    errors: frozenset[str] = frozenset()
    query_id: str = NUMERIC_QID
    column: str = "value"
    text: str = ANSWER
    run_id: str | None = None
    delay_s: float = 0.0
    calls: list[tuple[str, str]] = field(default_factory=list)
    modes: list[ChatMode] = field(default_factory=list)
    running: int = 0
    max_running: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def answer(
        self, session_id: str, text: str, user_ref: str, mode: ChatMode
    ) -> Iterator[ChatEvent]:
        del user_ref
        with self._lock:
            self.calls.append((session_id, text))
            self.modes.append(mode)
            self.running += 1
            self.max_running = max(self.max_running, self.running)
        try:
            time.sleep(self.delay_s)
            yield ModeEvent(mode=mode, message=f"mode {mode}")
            if text in self.errors:
                yield ErrorEvent(error_type="ModelUnavailable", message="down", hint=None)
            else:
                yield final(
                    self.value,
                    text=self.text,
                    query_id=self.query_id,
                    column=self.column,
                    run_id=self.run_id,
                )
        finally:
            with self._lock:
                self.running -= 1


class FakeCtx:
    """A `JobContext` for `eval` jobs: `yield_after` questions polled, then `should_yield`."""

    def __init__(self, payload: dict[str, JsonValue], *, yield_after: int | None = None) -> None:
        self.job_id = new_id(IdKind.JOB)
        self.job = JobRow.model_validate({
            "job_id": self.job_id, "kind": "eval", "payload": payload, "gpu_class": "none",
            "priority": 20, "status": "running", "attempts": 1, "max_attempts": 3,
        })  # fmt: skip
        self.kind, self.attempt, self.stop_reason = "eval", 1, None
        self.polls = 0
        self.yield_after = yield_after
        self.state: dict[str, JsonValue] = {}

    def should_yield(self) -> bool:
        self.polls += 1
        return self.yield_after is not None and self.polls > self.yield_after

    def heartbeat(self, note: str | None = None) -> None:
        del note

    def save_state(self, state: dict[str, JsonValue]) -> None:
        self.state = dict(state)

    def load_state(self) -> dict[str, JsonValue]:
        return dict(self.state)

    @contextlib.contextmanager
    def gpu_scope(self, cls: str) -> Iterator[None]:
        del cls
        yield


def make_deps(
    data_root: Path, con: duckdb.DuckDBPyConnection, *, chat: object | None = None,
    swarm: object | None = None, job: object | None = None,
    judge: Callable[[], Any] | None = None,
) -> EvalDeps:  # fmt: skip
    """`EvalDeps` over the fixture warehouse and the given fakes; baselines live under the
    tmp `data_root.parent` (never the checkout's `tests/eval/baselines/`)."""
    return EvalDeps(
        warehouse_for=lambda build_id: con, swarm_for=lambda profile: cast("Any", swarm),
        chat_for=lambda profile: cast("Any", chat or AnswerChat()),
        judge_for=judge or (lambda: None), memory=None, job=cast("Any", job), clock=clock.now,
        data_root=data_root, settings=settings(), repo_root=data_root.parent,
    )  # fmt: skip


def lines(path: Path) -> Sequence[dict[str, Any]]:
    """The JSON lines of `path`."""
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
