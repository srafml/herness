"""Unit tests for herness.store.ops.resilience.bind_core_backends (impl 08 U08-98, U08-10;
T08-23): both ports bound, rebinding replaces, the atexit metric flush registered once."""

from __future__ import annotations

import pytest

from herness.core.jobs.ports import require_jobs_backend
from herness.core.resilience import ProcessState, _state
from herness.store import ops
from herness.store.ops import resilience as area
from herness.store.ops.jobs import SqliteJobsBackend
from herness.store.ops.resilience import SqliteResilienceBackend

pytestmark = pytest.mark.unit


def test_cv_t08_23_binds_both_ports(reset_process_state: ProcessState) -> None:
    """IT08-04 (cv) bind_core_backends binds the SQLite resilience and jobs backends."""
    assert reset_process_state.ops is None
    assert reset_process_state.jobs is None
    area.bind_core_backends()
    assert isinstance(reset_process_state.ops, SqliteResilienceBackend)
    assert isinstance(reset_process_state.jobs, SqliteJobsBackend)
    assert require_jobs_backend() is reset_process_state.jobs
    assert _state.require_ops_backend() is reset_process_state.ops


def test_cv_t08_23_rebind_replaces_and_registers_atexit_once(
    reset_process_state: ProcessState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT08-04 (cv) a second call replaces both ports without error; atexit flush once (U08-10)."""
    registered: list[object] = []
    monkeypatch.setattr(_state.atexit, "register", registered.append)
    monkeypatch.setattr(_state._holder, "atexit_registered", False)
    area.bind_core_backends()
    first = (reset_process_state.ops, reset_process_state.jobs)
    area.bind_core_backends()
    assert reset_process_state.ops is not first[0]
    assert reset_process_state.jobs is not first[1]
    assert isinstance(reset_process_state.ops, SqliteResilienceBackend)
    assert isinstance(reset_process_state.jobs, SqliteJobsBackend)
    assert registered == [_state._flush_metrics_at_exit]


def test_cv_t08_23_package_reexports() -> None:
    """IT08-04 (cv) the 08 block of herness.store.ops re-exports the module-level functions."""
    for name in ("record_metric_samples", "purge_metric_samples", "purge_events"):
        assert name in ops.__all__
    assert "bind_core_backends" in ops.__all__
    assert ops.bind_core_backends is area.bind_core_backends
    assert ops.purge_events is area.purge_events
