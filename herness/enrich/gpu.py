"""GPU helpers: CUDA OOM backoff, CUDA release and the stage yield signal (impl 03 §3.4).

`run_batches_with_oom_backoff` halves the batch size on a CUDA OOM (design 03 §5.2, §6;
TH03-09 bounds GPU memory use). `release_cuda` frees CUDA memory before a GPU class or
service switch (precondition of T08-03 `JobContext.gpu_scope`). `YieldRequested` is public
so impl 02's `build_pipeline` handler and `make_distill_handler` (U03-137) can catch it by
name; re-exported as `herness.enrich.pipeline.YieldRequested` and `.distill.YieldRequested`.

`torch` is imported lazily inside functions so importing this module never loads it.
"""

from __future__ import annotations

import gc
from collections.abc import Callable, Sequence
from typing import ClassVar, Literal

from herness.core.errors import ConfigError, FatalError, RecoverableError
from herness.core.logging import get_logger
from herness.core.resilience import fault_point

_log = get_logger("enrich.gpu")


class CudaOutOfMemory(RecoverableError):  # noqa: N818 - name fixed by spec 03 U03-21
    """CUDA ran out of memory for an in-process model (U03-21); raised and caught inside
    `run_batches_with_oom_backoff` — it never crosses this module's boundary."""

    _extra_attrs: ClassVar[tuple[str, ...]] = ("batch_size",)

    def __init__(self, batch_size: int, /) -> None:
        msg = f"CUDA out of memory at batch size {batch_size}"
        super().__init__(msg, batch_size=batch_size)
        self.batch_size = batch_size


class YieldRequested(Exception):  # noqa: N818 - name fixed by spec 03 U03-152
    """A stage stopped at a chunk boundary because `ctx.should_yield()` fired (U03-152).

    Not a `HernessError`, so impl 08 never classifies it as a failure. Raised only after
    the stage flushed its buffers and saved its checkpoint; never carries record text.
    """

    def __init__(self, stage: str) -> None:
        super().__init__(f"yield requested at {stage}")
        self.stage = stage


def release_cuda() -> None:
    """Free CUDA memory before a GPU class or service switch (U03-23).

    `gc.collect()` always runs; when CUDA is available its sync/empty/ipc calls also run.
    A `RuntimeError` from any of them is logged (`enrich.gpu.release_failed`, WARNING) and
    not re-raised: spec 08's VRAM check after this call raises `ModelUnavailable` instead.
    """
    gc.collect()
    import torch  # noqa: PLC0415 - lazy: importing this module must never load torch (U03-23)

    try:
        if torch.cuda.is_available():
            torch.cuda.synchronize()
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()  # type: ignore[no-untyped-call]  # untyped in the torch stubs
    except RuntimeError:
        _log.warning("enrich.gpu.release_failed")


def run_batches_with_oom_backoff[T, R](
    items: Sequence[T],
    fn: Callable[[Sequence[T]], list[R]],
    *,
    start_batch: int,
    min_batch: int = 1,
    fault_name: Literal["embed.batch", "decider.batch"],
    on_batch: Callable[[int, list[R]], None] | None = None,
) -> list[R]:
    """Run `fn` over `items` in chunks, halving the batch size on CUDA OOM (U03-22).

    Returns `fn` results in input order; the batch size never grows within one call.
    Raises `ConfigError` unless `start_batch >= min_batch >= 1`, `FatalError` when the
    size would drop below `min_batch`; any other exception from `fn` propagates unchanged.
    """
    if min_batch < 1 or start_batch < min_batch:
        msg = f"start_batch={start_batch} must be >= min_batch={min_batch} >= 1"
        raise ConfigError(msg)
    import torch  # noqa: PLC0415 - lazy: importing this module must never load torch (U03-22)

    size = start_batch
    index = 0
    batch_index = 0
    retried = False
    results: list[R] = []
    while index < len(items):
        chunk = items[index : index + size]
        try:
            fault_point(fault_name)
            try:
                chunk_results = fn(chunk)
            except torch.cuda.OutOfMemoryError as exc:
                raise CudaOutOfMemory(size) from exc
        except CudaOutOfMemory as oom:
            release_cuda()
            _log.warning("enrich.gpu.oom_retried", batch_size=size, retried=retried)
            if not retried:
                retried = True
                continue
            size //= 2
            retried = False
            if size < min_batch:
                msg = f"cuda oom at batch {min_batch}"
                raise FatalError(msg) from oom
            continue
        results.extend(chunk_results)
        if on_batch is not None:
            on_batch(batch_index, chunk_results)
        index += len(chunk)
        batch_index += 1
        retried = False
    return results
