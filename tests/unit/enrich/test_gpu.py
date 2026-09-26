"""Tests for herness.enrich.gpu (U03-21 ... U03-23, U03-152; T03-04).

torch is real (CUDA is available on the test machine) but every test monkeypatches the
CUDA-facing calls so nothing here depends on a real GPU; it must pass on CPU-only CI.
"""

from __future__ import annotations

import gc
from collections.abc import Sequence

import pytest
import torch
from structlog.testing import capture_logs

from herness.core.errors import ConfigError, FatalError, HernessError
from herness.enrich import gpu
from herness.enrich.gpu import YieldRequested, run_batches_with_oom_backoff

pytestmark = pytest.mark.unit

_OOM_MSG = "simulated oom"


def _noop_release(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Replace `gpu.release_cuda` with a no-op that records how many times it ran."""
    calls: list[int] = []
    monkeypatch.setattr(gpu, "release_cuda", lambda: calls.append(1))
    return calls


def test_ut03_19_one_retry_at_same_size_no_halving(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT03-19 fn raising OOM once at 128: 300 items -> results in order, one retry, no halving."""
    release_calls = _noop_release(monkeypatch)
    call_sizes: list[int] = []

    def fn(chunk: Sequence[int]) -> list[int]:
        call_sizes.append(len(chunk))
        if len(call_sizes) == 1:
            raise torch.cuda.OutOfMemoryError(_OOM_MSG)
        return list(chunk)

    items = list(range(300))
    result = run_batches_with_oom_backoff(items, fn, start_batch=128, fault_name="embed.batch")

    assert result == items
    assert call_sizes == [128, 128, 128, 44]  # first attempt at 128 failed, then one retry
    assert len(release_calls) == 1


def test_ut03_20_halves_to_fatal_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT03-20 fn raising OOM twice at every size: sizes 128,128,...,1,1 then FatalError."""
    release_calls = _noop_release(monkeypatch)
    call_sizes: list[int] = []

    def fn(chunk: Sequence[int]) -> list[int]:
        call_sizes.append(len(chunk))
        raise torch.cuda.OutOfMemoryError(_OOM_MSG)

    with pytest.raises(FatalError, match="cuda oom at batch 1"):
        run_batches_with_oom_backoff(
            list(range(200)), fn, start_batch=128, fault_name="embed.batch"
        )

    assert call_sizes == [128, 128, 64, 64, 32, 32, 16, 16, 8, 8, 4, 4, 2, 2, 1, 1]
    assert len(release_calls) == len(call_sizes)


def test_ut03_21_release_cuda_no_gpu_no_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT03-21 CUDA unavailable: release_cuda raises nothing and gc.collect ran."""
    collected: list[int] = []
    monkeypatch.setattr(gc, "collect", lambda: collected.append(1))
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)

    gpu.release_cuda()

    assert collected == [1]


def test_ut03_21_release_cuda_available_runs_sync_empty_ipc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT03-21 CUDA available: synchronize, empty_cache and ipc_collect all run."""
    collected: list[int] = []
    calls: list[str] = []
    monkeypatch.setattr(gc, "collect", lambda: collected.append(1))
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(torch.cuda, "synchronize", lambda: calls.append("synchronize"))
    monkeypatch.setattr(torch.cuda, "empty_cache", lambda: calls.append("empty_cache"))
    monkeypatch.setattr(torch.cuda, "ipc_collect", lambda: calls.append("ipc_collect"))

    gpu.release_cuda()

    assert collected == [1]
    assert calls == ["synchronize", "empty_cache", "ipc_collect"]


def test_ut03_140_yield_requested_is_public_and_not_a_herness_error() -> None:
    """UT03-140 YieldRequested: not a HernessError, stage=='embed', no record text in str()."""
    assert gpu.YieldRequested is YieldRequested
    exc = YieldRequested("embed")

    assert not isinstance(exc, HernessError)
    assert exc.stage == "embed"
    assert str(exc) == "yield requested at embed"


def test_ut03_19_fault_point_called_with_fault_name_per_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT03-19 (extra) fault_point(fault_name) runs once per chunk attempt, including retries."""
    _noop_release(monkeypatch)
    fault_calls: list[str] = []
    monkeypatch.setattr(gpu, "fault_point", fault_calls.append)
    attempts = 0

    def fn(chunk: Sequence[int]) -> list[int]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise torch.cuda.OutOfMemoryError(_OOM_MSG)
        return list(chunk)

    run_batches_with_oom_backoff(list(range(3)), fn, start_batch=3, fault_name="decider.batch")

    assert fault_calls == ["decider.batch", "decider.batch"]


def test_ut03_19_on_batch_called_with_batch_index(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT03-19 (extra) on_batch is invoked once per successful chunk, with a 0-based batch index."""
    _noop_release(monkeypatch)
    seen: list[tuple[int, list[int]]] = []

    def fn(chunk: Sequence[int]) -> list[int]:
        return list(chunk)

    run_batches_with_oom_backoff(
        list(range(5)),
        fn,
        start_batch=2,
        fault_name="embed.batch",
        on_batch=lambda idx, res: seen.append((idx, res)),
    )

    assert seen == [(0, [0, 1]), (1, [2, 3]), (2, [4])]


def test_ut03_20_non_oom_exception_propagates_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT03-20 (extra) a non-OOM exception from fn propagates unchanged, no backoff or release."""
    release_calls = _noop_release(monkeypatch)

    def fn(_chunk: Sequence[int]) -> list[int]:
        msg = "boom"
        raise ValueError(msg)

    with pytest.raises(ValueError, match="boom"):
        run_batches_with_oom_backoff(list(range(3)), fn, start_batch=3, fault_name="embed.batch")

    assert release_calls == []


def test_ut03_20_precondition_start_batch_ge_min_batch_ge_1() -> None:
    """UT03-20 (extra) start_batch >= min_batch >= 1 is enforced; a violation raises ConfigError."""

    def fn(chunk: Sequence[int]) -> list[int]:
        return list(chunk)

    with pytest.raises(ConfigError):
        run_batches_with_oom_backoff([1], fn, start_batch=1, min_batch=0, fault_name="embed.batch")
    with pytest.raises(ConfigError):
        run_batches_with_oom_backoff([1], fn, start_batch=1, min_batch=2, fault_name="embed.batch")


def test_ut03_21_release_failed_is_logged_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT03-21 a RuntimeError from CUDA during release is logged (WARNING) and not re-raised."""
    collected: list[int] = []
    monkeypatch.setattr(gc, "collect", lambda: collected.append(1))
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)

    def _boom() -> None:
        msg = "cuda driver error"
        raise RuntimeError(msg)

    monkeypatch.setattr(torch.cuda, "synchronize", _boom)
    monkeypatch.setattr(torch.cuda, "empty_cache", lambda: None)
    monkeypatch.setattr(torch.cuda, "ipc_collect", lambda: None)

    with capture_logs() as logs:
        gpu.release_cuda()  # must not raise

    assert collected == [1]
    events = [e for e in logs if e["event"] == "enrich.gpu.release_failed"]
    assert len(events) == 1
    assert events[0]["log_level"] == "warning"
