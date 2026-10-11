"""Tests for herness._cli._inline_boot: the CLI process bootstrap of ``--inline`` (T09-29)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from structlog.testing import capture_logs

import herness.store.ops.resilience
from herness._cli import _inline_boot, _worker_boot
from herness.core.errors import ConfigError
from herness.core.jobs.handlers import register_handler, resolve_handler
from herness.core.jobs.ports import JobContext
from herness.core.resilience import process_state
from herness.core.types import JobOutcome

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_handlers() -> Iterator[None]:
    """An empty handler table per test; the process's own table is restored after."""
    state = process_state()
    saved = dict(state.handlers)
    state.handlers.clear()
    yield
    state.handlers.clear()
    state.handlers.update(saved)


def _make(name: str) -> Callable[[JobContext], JobOutcome]:
    def handler(ctx: JobContext) -> JobOutcome:
        raise AssertionError(name)

    return handler


def _steps(
    monkeypatch: pytest.MonkeyPatch, *steps: tuple[str, tuple[str, ...], Callable[[], None]]
) -> list[str]:
    """Owner steps replaced by ``steps``; the ports bind is recorded."""
    events: list[str] = []
    monkeypatch.setattr(_inline_boot._Booted, "state", None)
    monkeypatch.setattr(_worker_boot, "owner_steps", lambda _dir: steps)
    monkeypatch.setattr(
        herness.store.ops.resilience, "bind_core_backends", lambda: events.append("ports")
    )
    return events


def test_ut09_115_ports_then_owners_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT09-115 boot_inline binds the ports, registers each owner, and a second call in the
    same process does nothing."""
    sync, review = _make("sync"), _make("review")
    events = _steps(
        monkeypatch,
        ("connectors", ("sync",), lambda: register_handler("sync", sync)),
        ("swarm", ("review",), lambda: register_handler("review", review)),
    )
    _inline_boot.boot_inline(Path("config"))
    assert events == ["ports"]
    assert resolve_handler("sync") is sync
    assert resolve_handler("review") is review
    _inline_boot.boot_inline(Path("config"))
    assert events == ["ports"]


def test_ut09_115_prebound_kind_is_kept_not_a_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT09-115 a kind already bound in this process keeps its handler, the rest of the same
    owner still registers, and nothing raises "handler already registered"."""
    mine, theirs, other = _make("mine"), _make("theirs"), _make("other")
    register_handler("sync", mine)

    def owner() -> None:
        register_handler("sync", theirs)
        register_handler("reconcile", other)

    _steps(monkeypatch, ("connectors", ("sync", "reconcile"), owner))
    _inline_boot.boot_inline(Path("config"))
    assert resolve_handler("sync") is mine
    assert resolve_handler("reconcile") is other


def test_ut09_115_config_error_skips_owner_and_logs_class_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT09-115 an owner raising ConfigError stays unbound (even its partial work), is logged
    with owner, kinds and the error class only, and the other owners proceed."""
    chat, kept = _make("chat"), _make("kept")
    register_handler("review", kept)

    def failing() -> None:
        register_handler("outcome_measure", _make("partial"))
        msg = "api key sk-SECRET missing"
        raise ConfigError(msg)

    _steps(
        monkeypatch,
        ("memory", ("outcome_measure", "memory_maintenance"), failing),
        ("chat", ("chat",), lambda: register_handler("chat", chat)),
    )
    with capture_logs() as logs:
        _inline_boot.boot_inline(Path("config"))
    assert resolve_handler("chat") is chat
    assert resolve_handler("review") is kept
    with pytest.raises(ConfigError, match="no handler for outcome_measure"):
        resolve_handler("outcome_measure")
    warned = [x for x in logs if x["event"] == "cli.inline.handler_unavailable"]
    assert len(warned) == 1
    assert warned[0]["log_level"] == "warning"
    assert warned[0]["owner"] == "memory"
    assert warned[0]["kinds"] == ["outcome_measure", "memory_maintenance"]
    assert warned[0]["error"] == "ConfigError"
    assert "SECRET" not in repr(logs)


def test_ut09_115_other_errors_are_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT09-115 only ConfigError is tolerated; anything else propagates."""

    def broken() -> None:
        msg = "boom"
        raise RuntimeError(msg)

    _steps(monkeypatch, ("chat", ("chat",), broken))
    with pytest.raises(RuntimeError, match="boom"):
        _inline_boot.boot_inline(Path("config"))
