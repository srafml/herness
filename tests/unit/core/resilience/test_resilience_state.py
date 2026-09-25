"""Tests for herness.core.resilience._state: ProcessState and port binding (T08-03)."""

import random
import secrets
import sys
import threading
import types
from collections.abc import Callable

import pytest

from herness.core import resilience
from herness.core.errors import ConfigError
from herness.core.resilience import _state

pytestmark = pytest.mark.unit


class _Backend:
    """Stand-in for a ResilienceBackend; binding never calls it."""


class _Registry:
    """Stand-in for a ChainRegistry; binding never calls it."""


def test_ut08_29_process_state_defaults() -> None:
    """UT08-29 a fresh ProcessState has no bound ports, empty registries and real clocks."""
    state = _state.reset_process_state()
    assert state is _state.process_state()
    assert (state.ops, state.jobs, state.chains) == (None, None, None)
    assert state.breakers == {}
    assert state.probes == {}
    assert state.handlers == {}
    assert state.fault_plan is _state.NOT_LOADED
    assert state.faults_enabled is False
    assert state.auth_dropped == set()
    assert state.policies_cache == {}
    assert state.policies_hash is None
    assert state.kill_service_hook is None
    assert isinstance(state.rng, secrets.SystemRandom)
    assert isinstance(state.lock, type(threading.Lock()))
    _state.reset_process_state()


def test_ut08_29_reset_replaces_singleton(reset_process_state: _state.ProcessState) -> None:
    """UT08-29 reset_process_state swaps in a new instance; the fixture seeds rng."""
    assert reset_process_state is _state.process_state()
    assert isinstance(reset_process_state.rng, random.Random)
    assert reset_process_state.rng.random() == random.Random(0).random()
    reset_process_state.sleep(5.0)
    fresh = _state.reset_process_state()
    assert fresh is not reset_process_state
    assert _state.process_state() is fresh


def test_ut08_29_unbound_ports_raise_config_error(
    reset_process_state: _state.ProcessState,
) -> None:
    """UT08-29 unbound case: using an unbound port raises ConfigError naming the fix."""
    with pytest.raises(ConfigError) as ops_err:
        _state.require_ops_backend()
    assert str(ops_err.value) == (
        "resilience backend not bound; call herness.store.ops.resilience.bind_core_backends()"
    )
    with pytest.raises(ConfigError, match="chain registry not bound"):
        _state.require_chain_registry()


def test_ut08_29_bind_sets_and_rebinding_replaces(
    reset_process_state: _state.ProcessState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT08-29 bind functions set the ports; a rebind replaces and logs rebound=True."""
    logged: list[tuple[str, dict[str, object]]] = []

    def debug(event: str, **fields: object) -> None:
        logged.append((event, fields))

    monkeypatch.setattr(_state._log, "debug", debug)
    monkeypatch.setattr(_state._holder, "atexit_registered", True)
    first, second, registry = _Backend(), _Backend(), _Registry()
    resilience.bind_ops_backend(first)  # type: ignore[arg-type]
    assert _state.require_ops_backend() is first
    resilience.bind_ops_backend(second)  # type: ignore[arg-type]
    assert reset_process_state.ops is second
    resilience.bind_chain_registry(registry)  # type: ignore[arg-type]
    assert _state.require_chain_registry() is registry
    assert logged == [
        ("resilience.backend.bound", {"port": "ops", "rebound": False}),
        ("resilience.backend.bound", {"port": "ops", "rebound": True}),
        ("resilience.backend.bound", {"port": "chains", "rebound": False}),
    ]


def test_ut08_29_bind_rejects_none(reset_process_state: _state.ProcessState) -> None:
    """UT08-29 binding None raises ConfigError and leaves the port unbound."""
    with pytest.raises(ConfigError):
        _state.bind_ops_backend(None)  # type: ignore[arg-type]
    with pytest.raises(ConfigError):
        _state.bind_chain_registry(None)  # type: ignore[arg-type]
    assert reset_process_state.ops is None
    assert reset_process_state.chains is None


def test_ut08_29_atexit_flush_registered_once(
    reset_process_state: _state.ProcessState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT08-29 bind_ops_backend registers the atexit metric flush exactly once."""
    registered: list[Callable[[], None]] = []
    monkeypatch.setattr(_state.atexit, "register", registered.append)
    monkeypatch.setattr(_state._holder, "atexit_registered", False)
    _state.bind_ops_backend(_Backend())  # type: ignore[arg-type]
    _state.reset_process_state()
    _state.bind_ops_backend(_Backend())  # type: ignore[arg-type]
    assert registered == [_state._flush_metrics_at_exit]


def test_ut08_29_atexit_flush_calls_metrics_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT08-29 the exit hook calls metrics.flush_metrics when that module exists."""
    calls: list[str] = []
    fake = types.ModuleType("herness.core.resilience.metrics")
    fake.flush_metrics = lambda: calls.append("flush")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "herness.core.resilience.metrics", fake)
    _state._flush_metrics_at_exit()
    assert calls == ["flush"]


def test_ut08_29_atexit_flush_tolerates_missing_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT08-29 the exit hook is a no-op while the metrics module does not exist."""

    def missing(name: str) -> types.ModuleType:
        raise ModuleNotFoundError(name)

    monkeypatch.setattr(_state.importlib, "import_module", missing)
    _state._flush_metrics_at_exit()


def test_ut08_29_lazy_exports_resolve() -> None:
    """UT08-29 the package re-exports its names lazily; unknown names raise AttributeError."""
    assert resilience.__all__ == tuple(sorted(resilience._EXPORTS))
    for name in resilience.__all__:
        assert getattr(resilience, name) is not None
    with pytest.raises(AttributeError):
        _ = resilience.not_a_name
