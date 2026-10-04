"""Private sibling of `tools.synth.shards` (U11-19, T11-14c): the lake directories of a run,
created before any shard worker starts and pruned of unused ones after the run.

Split off for the 350-line budget of `shards.py`, which imports it at module level (this
module needs `Shard` only for type checking); imported only by `shards`.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Final

from herness.store.lake import validate_name

if TYPE_CHECKING:
    from tools.synth.shards import Shard

# Entities a shard writes besides its own: incident shards add their task SLAs (U11-77).
_ALSO_WRITES: Final = {("servicenow", "incident"): ("servicenow", "task_sla")}


def lake_entities(shards: Sequence[Shard]) -> list[tuple[str, str]]:
    """Every `(source, entity)` the shards write to the lake, once each, in plan order."""
    out: dict[tuple[str, str], None] = {}
    for shard in shards:
        key = (shard.source, shard.entity)
        out[key] = None
        if key in _ALSO_WRITES:
            out[_ALSO_WRITES[key]] = None
    return list(out)


def prepare_lake(raw_root: Path, entities: Iterable[tuple[str, str]]) -> list[Path]:
    """Create `<raw_root>/<source>/<entity>/` for each pair before any worker starts and
    return the directories created (T11-14c). On Windows `Path.resolve(strict=False)` of a
    missing path keeps its `\\\\?\\` prefix when its parent appears between the two
    final-path lookups of CPython's `ntpath.realpath`, so workers creating one entity's
    directories made `partition_dir` place their partitions outside the raw root. With the
    raw root and every entity directory in place only a `dt=` leaf can still appear, and
    that resolves the same either way."""
    created: list[Path] = []
    for source, entity in entities:
        path = raw_root / validate_name("source", source) / validate_name("entity", entity)
        if not path.is_dir():
            path.mkdir(parents=True, exist_ok=True)
            created.append(path)
    return created


def prune_unused(created: Sequence[Path]) -> None:
    """Remove the directories `prepare_lake` created that got no rows (`--verify` reads every
    entity directory), and their source directory when that is left empty too."""
    for path in created:
        for directory in (path, path.parent):
            with contextlib.suppress(OSError):  # not empty: kept
                directory.rmdir()


__all__ = ["lake_entities", "prepare_lake", "prune_unused"]
