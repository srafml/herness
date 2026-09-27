"""Unit tests of U08-88 (`child_main`, T08-21) run in this process on a real pipe: bootstrap,
lease check, handler run, outcome message (redacted, cut, error attributes) and shutdown."""

from __future__ import annotations

import multiprocessing
import signal
from datetime import UTC, datetime
from typing import Any

import pytest
from tests.support.worker_bootstrap import BOOTSTRAP
from tests.unit.core.jobs._supervisor_env import SupEnv

from herness.core import errors
from herness.core.jobs import child
from herness.core.jobs.child import MESSAGE_WITHHELD, call_bootstrap, child_main, outcome_message
from herness.core.jobs.pipe import OutcomeMsg, decode_message
from herness.core.jobs.queue import claim
from herness.core.types import JobOutcome

pytestmark = pytest.mark.unit

OWNER = "h:1:cpu0"
CALLS: list[str] = []


def noop_bootstrap() -> None:
    """A bootstrap that registers nothing (the test's process state stays as it is)."""
    CALLS.append("noop")


NOT_CALLABLE = 3


@pytest.fixture
def signals(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, Any]]:
    """`signal.signal` calls of `child_main`, recorded instead of applied."""
    calls: list[tuple[int, Any]] = []
    monkeypatch.setattr(child.signal, "signal", lambda s, h: calls.append((s, h)))
    return calls


def _claimed(env: SupEnv, mode: str = "done") -> str:
    job_id = env.enqueue(mode)
    row = claim(owner=OWNER, allowed_classes=["none"])
    assert row is not None
    assert row.job_id == job_id
    return job_id


def test_cv_t08_21_bootstrap_spec_errors() -> None:
    """CV-T08-21 a bootstrap must be `module:function` naming an importable callable."""
    for spec in (
        "x",
        ":f",
        "m:",
        "no.such.module:f",
        f"{__name__}:missing",
        f"{__name__}:NOT_CALLABLE",
    ):
        with pytest.raises(errors.ConfigError):
            call_bootstrap(spec)
    call_bootstrap(f"{__name__}:noop_bootstrap")
    assert CALLS[-1] == "noop"


def test_cv_t08_21_outcome_message_redacts_cuts_and_keeps_attributes(
    sup_env: SupEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CV-T08-21 U08-88 step 7: `done`/`yield` carry the result; an error carries its class
    name and the redacted message cut to 2 KB (withheld when redaction fails), plus what
    `CircuitOpen` and `RateLimited` need to be rebuilt."""
    del sup_env
    assert outcome_message(JobOutcome(status="yield")) == OutcomeMsg(status="yield", result={})
    long = outcome_message(errors.SourceUnavailable("x" * 3000))
    assert long.error_class == "SourceUnavailable"
    assert long.error_message is not None
    assert len(long.error_message) <= 2048
    at = datetime(2026, 9, 1, tzinfo=UTC)
    circuit = outcome_message(errors.CircuitOpen("open", key="model:x", retry_at=at))
    assert circuit.result == {"key": "model:x", "retry_at": "2026-09-01T00:00:00.000000Z"}
    assert outcome_message(errors.RateLimited("slow", retry_after=5)).result == {"retry_after": 5.0}
    assert outcome_message(errors.RateLimited("slow")).result == {}
    monkeypatch.setattr(child, "redact_text", lambda text: None)
    assert outcome_message(errors.FatalError("secret")).error_message == MESSAGE_WITHHELD


def test_cv_t08_21_child_main_runs_the_handler(
    sup_env: SupEnv, signals: list[tuple[int, Any]]
) -> None:
    """CV-T08-21 U08-88: interrupts are ignored, the bootstrap runs, the handler's `done`
    outcome is sent and the pipe is closed."""
    job_id = _claimed(sup_env)
    parent, end = multiprocessing.Pipe()
    child_main(job_id, OWNER, "cpu", BOOTSTRAP, end)
    ignored = {s for s, h in signals if h is signal.SIG_IGN}
    assert signal.SIGINT in ignored
    assert getattr(signal, "SIGBREAK", signal.SIGINT) in ignored
    msg = decode_message(parent.recv_bytes())
    assert isinstance(msg, OutcomeMsg)
    assert msg.status == "done"
    assert msg.result["attempt"] == 1
    assert end.closed


def test_cv_t08_21_child_main_lease_not_ours_exits_1(
    sup_env: SupEnv, signals: list[tuple[int, Any]]
) -> None:
    """CV-T08-21 U08-88 step 3: a row that is not running under this owner → exit 1 (R-46),
    no outcome sent."""
    del signals
    job_id = _claimed(sup_env)
    parent, end = multiprocessing.Pipe()
    with pytest.raises(SystemExit) as exc:
        child_main(job_id, "h:2:cpu0", "cpu", BOOTSTRAP, end)
    assert exc.value.code == 1
    assert end.closed
    with pytest.raises((EOFError, BrokenPipeError)):  # closed with nothing sent
        parent.recv_bytes()


def test_cv_t08_21_child_main_no_handler_is_the_outcome(
    sup_env: SupEnv, signals: list[tuple[int, Any]]
) -> None:
    """CV-T08-21 U08-88 step 5: a kind without a handler sends a `ConfigError` outcome."""
    del signals
    job_id = _claimed(sup_env)
    sup_env.state.handlers.clear()
    parent, end = multiprocessing.Pipe()
    child_main(job_id, OWNER, "cpu", f"{__name__}:noop_bootstrap", end)
    msg = decode_message(parent.recv_bytes())
    assert isinstance(msg, OutcomeMsg)
    assert (msg.status, msg.error_class) == ("error", "ConfigError")


def test_cv_t08_21_child_main_unencodable_outcome(
    sup_env: SupEnv, signals: list[tuple[int, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """CV-T08-21 an outcome that cannot be encoded is replaced by a `SchemaViolation` one;
    a supervisor that is gone (closed pipe) is not an error for the child."""
    del signals
    job_id = _claimed(sup_env)
    real = child.encode_message
    calls: list[int] = []

    def once_too_big(msg: Any) -> bytes:
        calls.append(1)
        if len(calls) == 1:
            text = "pipe message exceeds 8 MiB"
            raise errors.SchemaViolation(text)
        return real(msg)

    monkeypatch.setattr(child, "encode_message", once_too_big)
    parent, end = multiprocessing.Pipe()
    child_main(job_id, OWNER, "cpu", BOOTSTRAP, end)
    msg = decode_message(parent.recv_bytes())
    assert isinstance(msg, OutcomeMsg)
    assert msg.error_class == "SchemaViolation"
    second = _claimed(sup_env)
    parent, end = multiprocessing.Pipe()
    parent.close()
    child_main(second, OWNER, "cpu", BOOTSTRAP, end)  # OSError on send is swallowed


def test_cv_t08_21_child_main_closes_context_before_pipe(
    sup_env: SupEnv, signals: list[tuple[int, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """CV-T08-21 U08-88 step 8 (review M3): `ctx.close()` stops the pipe reader before the
    connection is closed."""
    del signals
    from herness.core.jobs.context import ChildJobContext  # noqa: PLC0415

    order: list[str] = []
    real_close = ChildJobContext.close

    def ctx_close(self: ChildJobContext, timeout_s: float = 1.0) -> None:
        order.append("ctx")
        real_close(self, timeout_s)

    monkeypatch.setattr(ChildJobContext, "close", ctx_close)

    class Conn:
        def __init__(self, inner: Any) -> None:
            self.inner = inner

        def __getattr__(self, name: str) -> Any:
            return getattr(self.inner, name)

        def close(self) -> None:
            order.append("conn")
            self.inner.close()

    job_id = _claimed(sup_env)
    parent, end = multiprocessing.Pipe()
    child_main(job_id, OWNER, "cpu", BOOTSTRAP, Conn(end))  # type: ignore[arg-type]
    assert order == ["ctx", "conn"]
    assert isinstance(decode_message(parent.recv_bytes()), OutcomeMsg)
