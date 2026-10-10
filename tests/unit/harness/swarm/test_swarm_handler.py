"""UT06-56: the `review` job handler over a fake Swarm (impl 06 U06-85); package re-exports."""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pytest

from herness.core.errors import ConfigError, RetryableError
from herness.core.jobs.ports import JobRow
from herness.core.types import Coverage, JobOutcome
from herness.harness import swarm as swarm_pkg
from herness.harness.swarm import handler
from herness.harness.swarm.lifecycle import RunRequest, RunResult

pytestmark = pytest.mark.unit

RUN_ID = "run_" + "0" * 26
_COV = Coverage(
    planned_tasks=1, done_tasks=1, dead_tasks=0, must_cover_total=0, must_cover_done=0,
    verified_findings=0, rejected_findings=0, publishable=True,
)  # fmt: skip


def _result(status: str) -> RunResult:
    return RunResult(
        run_id=RUN_ID, status=status, draft_path=None, coverage=_COV, dead_task_ids=[],
        token_usage={}, cost_usd=Decimal(0),
    )  # fmt: skip


@dataclass
class FakeSwarm:
    status: str = "done"
    calls: list[tuple[str, Any, Any]] = field(default_factory=list)
    closed: int = 0
    error: Exception | None = None

    async def resume(self, run_id: str, job: Any = None, *, force: bool = False) -> RunResult:
        self.calls.append(("resume", run_id, force))
        if self.error is not None:
            raise self.error
        return _result(self.status)

    async def start(self, req: RunRequest, job: Any = None) -> RunResult:
        self.calls.append(("start", req, job))
        return _result(self.status)

    def close(self) -> None:
        self.closed += 1


class Ctx:
    def __init__(self, payload: dict[str, Any], *, yielding: bool = False) -> None:
        self.job_id = "job_" + "1" * 26
        self.job = JobRow.model_validate(
            {"job_id": self.job_id, "kind": "review", "gpu_class": "reasoning",
             "status": "running", "priority": 40, "attempts": 1, "max_attempts": 3,
             "payload": payload}
        )  # fmt: skip
        self._yielding = yielding

    def should_yield(self) -> bool:
        return self._yielding


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeSwarm:
    swarm = FakeSwarm()
    monkeypatch.setattr(handler, "get_config", object)
    monkeypatch.setattr(handler, "build_swarm_from_config", lambda cfg: swarm)
    return swarm


def _run(payload: dict[str, Any], *, yielding: bool = False) -> JobOutcome:
    return handler.review_job_handler(Ctx(payload, yielding=yielding))  # type: ignore[arg-type]


def test_ut06_56_resume_payload(fake: FakeSwarm) -> None:
    """UT06-56 `{"run_id", "resume": true, "force"}` resumes the run; done outcome; closed."""
    out = _run({"run_id": RUN_ID, "resume": True, "force": True})
    assert fake.calls == [("resume", RUN_ID, True)]
    assert out == JobOutcome(
        status="done", result={"run_id": RUN_ID, "status": "done", "partial": False}
    )
    assert fake.closed == 1


def test_ut06_56_resume_payload_from_enqueue_resume(fake: FakeSwarm) -> None:
    """UT06-56 the spec 08 `enqueue_resume` payload (`retry_dead`) is accepted; force False."""
    _run({"run_id": RUN_ID, "resume": True, "retry_dead": True})
    assert fake.calls == [("resume", RUN_ID, False)]


def test_ut06_56_request_payload_starts(fake: FakeSwarm) -> None:
    """UT06-56 `{"request": {...}}` validates a `RunRequest` and starts it with the job."""
    fake.status = "partial"
    out = _run({"request": {"kind": "org_review", "depth": "fast"}})
    kind, req, job = fake.calls[0]
    assert (kind, req.kind, req.depth) == ("start", "org_review", "fast")
    assert job is not None
    assert out.result == {"run_id": RUN_ID, "status": "partial", "partial": True}


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"run_id": RUN_ID},
        {"run_id": RUN_ID, "resume": False},
        {"run_id": 7, "resume": True},
        {"run_id": RUN_ID, "resume": True, "force": "yes"},
        {"run_id": RUN_ID, "resume": True, "extra": 1},
        {"request": {"kind": "funding_review", "depth": "huge"}},
        {"request": {"kind": "funding_review"}, "run_id": RUN_ID},
    ],
)
def test_ut06_56_invalid_payload_config_error(fake: FakeSwarm, payload: dict[str, Any]) -> None:
    """UT06-56 any other payload → `ConfigError("review payload invalid: job_id=<id>")`."""
    with pytest.raises(ConfigError, match=r"^review payload invalid: job_id=job_1{26}$"):
        _run(payload)
    assert fake.closed == 1


def test_ut06_56_yield_when_open_and_should_yield(fake: FakeSwarm) -> None:
    """UT06-56 a non-terminal result while the job should yield → `yield` outcome."""
    fake.status = "running"
    out = _run({"run_id": RUN_ID, "resume": True}, yielding=True)
    assert out == JobOutcome(status="yield", result={"run_id": RUN_ID, "status": "running"})


def test_ut06_56_terminal_result_never_yields(fake: FakeSwarm) -> None:
    """UT06-56 a terminal run answers `done` even when the job should yield."""
    fake.status = "canceled"
    out = _run({"run_id": RUN_ID, "resume": True}, yielding=True)
    assert out.status == "done"


def test_ut06_56_open_result_without_yield_is_done(fake: FakeSwarm) -> None:
    """UT06-56 a non-terminal result with no yield request answers `done` (U06-85 step 3)."""
    fake.status = "running"
    assert _run({"run_id": RUN_ID, "resume": True}).status == "done"


def test_ut06_56_errors_propagate_and_close(fake: FakeSwarm) -> None:
    """UT06-56 errors from the Swarm propagate (spec 08 requeues or fails); `close` runs."""
    fake.error = RetryableError("busy")
    with pytest.raises(RetryableError):
        _run({"run_id": RUN_ID, "resume": True})
    assert fake.closed == 1


def test_ut06_56_register_handlers_idempotent(reset_process_state: object) -> None:
    """UT06-56 `register_handlers` registers `review` once; calling it again is a no-op."""
    from herness.core.jobs import handlers as jh  # noqa: PLC0415

    del reset_process_state
    handler.register_handlers()
    handler.register_handlers()
    assert jh.resolve_handler("review") is handler.review_job_handler


def test_ut06_56_package_reexports() -> None:
    """UT06-56 `herness.harness.swarm` re-exports the §2 names lazily."""
    module = importlib.reload(swarm_pkg)
    assert module.review_job_handler is handler.review_job_handler
    assert module.RunRequest is RunRequest
    assert module.Swarm.__name__ == "Swarm"
    assert sorted(module.__all__) == ["RunRequest", "RunResult", "Swarm", "review_job_handler"]
    with pytest.raises(AttributeError):
        _ = module.not_there
