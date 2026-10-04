"""Spawn-pool probe of the lake partition containment check (T11-14c).

Every worker resolves and creates one `dt=` partition in each of `entities` fresh entity
directories that all workers share, the way concurrent synth shards of one entity do. On
Windows, `Path.resolve(strict=False)` of a missing path keeps its `\\\\?\\` prefix when the
path's parent appears between the two final-path lookups of CPython's `ntpath.realpath`, so
`partition_dir` then reports the partition outside the raw root. Creating the entity
directories first (`tools.synth.shards.prepare_lake`) leaves only the `dt=` leaf to appear,
which resolves stably.
"""

from __future__ import annotations

import multiprocessing
from datetime import date, timedelta
from pathlib import Path

from herness.core.errors import ConfigError
from herness.store.lake import partition_dir

SOURCE = "race"


def entity_names(entities: int) -> list[tuple[str, str]]:
    """The `(source, entity)` pairs the probe writes."""
    return [(SOURCE, f"e{n}") for n in range(entities)]


def create_partitions(job: tuple[str, int, int]) -> int:  # pragma: no cover - child
    """Worker: `partition_dir` and `mkdir` of day `slot` in every entity; returns the number
    of containment failures (`ConfigError`)."""
    raw, slot, entities = job
    root = Path(raw)
    day = date(2026, 6, 1) + timedelta(days=slot)
    failures = 0
    for source, entity in entity_names(entities):
        try:
            path = partition_dir(root, source, entity, day)
        except ConfigError:
            failures += 1
            continue
        path.mkdir(parents=True, exist_ok=True)
    return failures


def run(raw: Path, workers: int, entities: int) -> int:
    """Run the probe in a spawn pool of `workers`; total containment failures."""
    jobs = [(str(raw), slot, entities) for slot in range(workers)]
    with multiprocessing.get_context("spawn").Pool(workers) as pool:
        return sum(pool.map(create_partitions, jobs))
