"""Controller check of the eval question executor (T11-31 fold of the T11-30 r1 minors): when
a question raises, the job keeps heartbeating while the questions still in flight finish,
then the first error propagates."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest
from tests.unit.eval._runner_env import FakeCtx

from herness.eval import _runner_question as rq
from herness.eval.results import ResultsLog

pytestmark = pytest.mark.unit


class _BeatCtx(FakeCtx):
    def __init__(self) -> None:
        super().__init__({"profile": "local", "repeat": 1})
        self.beats: list[float] = []

    def heartbeat(self, note: str | None = None) -> None:
        self.beats.append(time.monotonic())


class _Q:
    def __init__(self, qid: str) -> None:
        self.id = qid


def test_cv_t11_31_heartbeat_while_draining_after_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CV-T11-31 a question that raises while another is in flight: heartbeats continue until
    the other finishes, then the error propagates."""
    finished = threading.Event()
    failed_at: list[float] = []

    def record(env: Any, question: Any, repeat: int, cache: Any, log: Any) -> None:
        if question.id == "G01":
            failed_at.append(time.monotonic())
            msg = "boom"
            raise RuntimeError(msg)
        time.sleep(0.4)
        finished.set()

    monkeypatch.setattr(rq, "_record", record)
    monkeypatch.setattr(rq, "HEARTBEAT_S", 0.05)
    ctx = _BeatCtx()
    pending: list[tuple[Any, int]] = [(_Q("G02"), 1), (_Q("G01"), 1)]
    log = ResultsLog(tmp_path / "r.jsonl")
    with pytest.raises(RuntimeError, match="boom"):
        rq.execute(Any, pending, {1: Any}, log, (ctx, 2))  # type: ignore[arg-type]
    assert finished.is_set()
    after = [b for b in ctx.beats if b > failed_at[0]]
    assert len(after) >= 3
