"""T11-14c reproduction: concurrent spawn workers creating sibling lake partitions.

The probe (`tests.support.lake_race`) has 32 spawn workers resolve and create one `dt=`
partition in each of 300 fresh entity directories they share, through the real
`herness.store.lake.partition_dir`. Without the entity directories in place some of those
checks fail on Windows (the CPython `ntpath.realpath` race described in
`tests/unit/store/test_store_lake_paths.py`); after `tools.synth.shards.prepare_lake` none
do. The end-to-end run forces the synth pool to 32 workers on the tiny plan.
"""

import sys
from datetime import date
from pathlib import Path

import pytest
from tests.support import lake_race

from tools.synth import shards as shards_module
from tools.synth.catalog import build_catalog
from tools.synth.params import load_params
from tools.synth.shards import plan_shards, prepare_lake, run_all_shards

pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.skipif(sys.platform != "win32", reason="the race is in Windows path resolution"),
]

_WORKERS = 32
_ENTITIES = 300


def _raw(tmp_path: Path, name: str) -> Path:
    raw = (tmp_path / name / "data" / "raw").resolve()
    raw.mkdir(parents=True)
    return raw


def test_rf_t11_14c_unprepared_entities_reproduce_the_race(tmp_path: Path) -> None:
    """T11-14c control: without the entity directories, concurrent workers make
    `partition_dir` report sibling partitions outside the raw root (this keeps the probe
    honest: the prepared run below would pass vacuously if the race never fired)."""
    failures = lake_race.run(_raw(tmp_path, "bare"), _WORKERS, _ENTITIES)
    assert failures > 0


def test_rf_t11_14c_prepared_entities_never_fail_containment(tmp_path: Path) -> None:
    """T11-14c fix: with `prepare_lake` run first, no worker fails the containment check,
    and every partition was created under the raw root."""
    raw = _raw(tmp_path, "prepared")
    created = prepare_lake(raw, lake_race.entity_names(_ENTITIES))
    assert len(created) == _ENTITIES
    assert lake_race.run(raw, _WORKERS, _ENTITIES) == 0
    assert len(list(raw.glob("*/*/dt=*"))) == _WORKERS * _ENTITIES


def test_rf_t11_14c_tiny_plan_in_32_worker_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T11-14c the tiny plan through a forced 32-worker spawn pool completes, and no empty
    entity directory is left behind by `prepare_lake`."""
    params = load_params(
        "tiny",
        start=date(2026, 6, 1),
        end=date(2026, 8, 31),
        sources=("servicenow", "jira", "monitoring", "files"),
        dirty="default",
        fetch_mode="initial",
        params_file=None,
    )
    cat = build_catalog(7, params)
    monkeypatch.setattr(shards_module, "_ROWS_PER_WORKER", 1)
    agg = run_all_shards(plan_shards(cat, params), cat, params, seed=7, root=tmp_path, workers=32)
    raw = tmp_path / "data" / "raw"
    assert agg.rows_written > 0
    assert all(any(d.iterdir()) for d in raw.glob("*/*"))
