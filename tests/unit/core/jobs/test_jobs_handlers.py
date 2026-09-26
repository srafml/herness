"""Tests for herness.core.jobs.handlers: registry and wrapper (impl 08 U08-55, U08-56; T08-12)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import pytest
import structlog
from pydantic import BaseModel

from herness.core.errors import ConfigError, FatalError, SchemaViolation, SourceUnavailable
from herness.core.jobs.handlers import register_handler, resolve_handler, run_handler
from herness.core.jobs.ports import JobContext
from herness.core.resilience import ProcessState
from herness.core.types import JobOutcome

pytestmark = pytest.mark.unit


@dataclass
class _Ctx:
    job_id: str = "job_01J8Z6Q4V5W6X7Y8Z9A0B1C2D3"
    kind: str = "sync"


CTX = cast("JobContext", _Ctx())


def _ok(ctx: JobContext) -> JobOutcome:
    del ctx
    return JobOutcome(status="done", result={"n": 1})


def _other(ctx: JobContext) -> JobOutcome:
    del ctx
    return JobOutcome(status="yield")


def test_ut08_64_register_resolve_and_duplicate(reset_process_state: ProcessState) -> None:
    """UT08-64 register then resolve; the same object again is a no-op; a different one for
    the same kind → ConfigError; an unregistered kind → ConfigError."""
    register_handler("sync", _ok)
    register_handler("sync", _ok)
    assert resolve_handler("sync") is _ok
    assert reset_process_state.handlers == {"sync": _ok}
    with pytest.raises(ConfigError, match="handler already registered for sync"):
        register_handler("sync", _other)
    assert resolve_handler("sync") is _ok
    with pytest.raises(ConfigError, match="no handler for review"):
        resolve_handler("review")


@pytest.mark.usefixtures("reset_process_state")
def test_ut08_64_register_refuses_unknown_kind_and_non_callable() -> None:
    """UT08-64 only JobKind names and callables are registered (static registry)."""
    with pytest.raises(ConfigError, match="unknown job kind bogus"):
        register_handler(cast("Any", "bogus"), _ok)
    with pytest.raises(ConfigError, match="not callable"):
        register_handler("sync", cast("Any", "herness.module.fn"))


def _raises(exc: BaseException) -> Any:
    def handler(ctx: JobContext) -> JobOutcome:
        del ctx
        raise exc

    return handler


def test_ut08_64_run_handler_wraps_errors() -> None:
    """UT08-64 `ValueError` → FatalError naming it (logged); `None` → FatalError;
    `SourceUnavailable` returned as is; a JobOutcome passes through."""
    with structlog.testing.capture_logs() as logs:
        crashed = run_handler(CTX, _raises(ValueError("secret detail")))
    assert isinstance(crashed, FatalError)
    assert crashed.message == "handler raised ValueError"
    assert [(e["event"], e["log_level"], e["kind"], e["error_type"]) for e in logs] == [
        ("jobs.handler.crashed", "error", "sync", "ValueError")
    ]
    assert "secret detail" not in str(logs)
    none_result = run_handler(CTX, lambda ctx: cast("JobOutcome", None))
    assert isinstance(none_result, FatalError)
    assert none_result.message == "handler returned NoneType"
    unavailable = SourceUnavailable("jira down")
    assert run_handler(CTX, _raises(unavailable)) is unavailable
    assert run_handler(CTX, _ok) == JobOutcome(status="done", result={"n": 1})


class _Strict(BaseModel):
    n: int


def test_ut08_64_validation_error_is_schema_violation() -> None:
    """UT08-64 a pydantic ValidationError raised by a handler → SchemaViolation."""

    def handler(ctx: JobContext) -> JobOutcome:
        del ctx
        _Strict.model_validate({"n": "x"})
        return JobOutcome(status="done")

    result = run_handler(CTX, handler)
    assert isinstance(result, SchemaViolation)
    assert result.message == "handler output invalid"


def test_ut08_64_base_exception_propagates() -> None:
    """UT08-64 a BaseException that is not an Exception propagates."""
    with pytest.raises(KeyboardInterrupt):
        run_handler(CTX, _raises(KeyboardInterrupt()))
