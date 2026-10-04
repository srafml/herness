"""Private sibling of `tools.synth.shards` (U11-19, T11-14b): the pool worker and the
in-process run of `run_all_shards`.

Split off for the 350-line budget of `shards.py`, which imports it lazily because this module
imports `Shard` from there. A spawn worker costs about a second on Windows (interpreter start
and the pandas/pyarrow/duckdb/lancedb imports), so a plan that fits one worker runs in the
calling process instead; the written bytes are the same either way.
"""

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from herness.core.errors import FatalError
from tools.synth.catalog_rows import Catalog
from tools.synth.params import SynthParams
from tools.synth.shards import (
    CONFIG_DIR,
    AggregateResult,
    Shard,
    ShardResult,
    WorkerContext,
    label,
    run_shard,
    worker_context,
)

_STATE: dict[str, Any] = {}


def _context(
    root: Path, seed: int, params: SynthParams, cat: Catalog, *, cache: bool = False
) -> WorkerContext:
    """Config profile `synth` with `paths.data = <root>/data` (T10-03), then `worker_context`.
    `cache` fills the process-wide config cache with `init_config`, as a pool worker does."""
    from herness.core.config import init_config, load_config  # noqa: PLC0415 - off import path
    from herness.store.layout import data_layout  # noqa: PLC0415 - kept off the import path

    load = init_config if cache else load_config
    data = (root / "data").as_posix()
    cfg = load("synth", overrides=(f"paths.data={data}",), config_dir=CONFIG_DIR)
    return worker_context(root, seed, params, cat, raw_root=data_layout(cfg=cfg).raw)


def init_worker(root: Path, seed: int, params: SynthParams, cat: Catalog) -> None:
    """Pool initializer. Worker logs go to stderr at ERROR (the parent logs the same config
    warnings once; the CLI's stdout carries only its JSON summary, T11-14). A failure is
    kept for `pool_task` to raise (a raising initializer makes the pool respawn forever)."""
    try:
        from herness.core.logging import configure_logging  # noqa: PLC0415 - worker only

        configure_logging("ERROR")
        _STATE["ctx"] = _context(root, seed, params, cat, cache=True)
    except Exception as exc:  # noqa: BLE001 - reported by pool_task as FatalError
        _STATE["error"] = type(exc).__name__


def _run_labelled(shard: Shard, ctx: WorkerContext) -> ShardResult:
    try:
        return run_shard(shard, ctx)
    except Exception as exc:
        msg = f"synth shard {label(shard)} failed"
        raise FatalError(msg, shard=label(shard), cause=type(exc).__name__) from exc


def pool_task(shard: Shard) -> ShardResult:
    ctx = _STATE.get("ctx")
    if ctx is None:
        msg = f"synth worker initialisation failed ({_STATE.get('error', 'unknown')})"
        raise FatalError(msg, shard=label(shard))
    return _run_labelled(shard, ctx)


def run_in_process(
    phases: Sequence[Sequence[Shard]], out: AggregateResult, args: tuple[Any, ...]
) -> None:
    """The one-worker plan without a pool, initialised as a worker is: `init_config` for the
    `synth` profile and logs at ERROR for the run. The process-wide state this touches (the
    config cache, the root logger level) is restored in `finally`. Failures are `FatalError`
    naming the shard, as from the pool."""
    from herness.core import config as config_module  # noqa: PLC0415 - off import path

    cache = config_module._Cache
    saved = (cache.config, cache.root)
    root_logger = logging.getLogger()
    previous = root_logger.level
    root_logger.setLevel(logging.ERROR)
    try:
        ctx = _context(*args, cache=True)
        for phase in phases:
            for shard in phase:
                out.add(_run_labelled(shard, ctx))
    finally:
        root_logger.setLevel(previous)
        cache.config, cache.root = saved
