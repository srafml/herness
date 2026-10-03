"""Spawn-pool run of tools.synth.shards (U11-19), T11-13 acceptance.

A tiny generation through `run_all_shards` (2 spawn workers) into `tmp_path` leaves no
dot-prefixed file under `data/raw` and writes `servicenow/cmn_department` and
`servicenow/task_sla`; a failing shard terminates the pool with a `FatalError` naming it.
"""

import dataclasses
from datetime import date
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from herness.core.errors import FatalError
from tools.synth.catalog import Catalog, build_catalog
from tools.synth.params import SynthParams, load_params
from tools.synth.shards import AggregateResult, plan_shards, run_all_shards

pytestmark = pytest.mark.integration

_SEED = 7
_WORKERS = 2


@pytest.fixture(scope="module")
def params() -> SynthParams:
    return load_params(
        "tiny",
        start=date(2026, 6, 1),
        end=date(2026, 8, 31),
        sources=("servicenow", "jira", "monitoring", "files"),
        dirty="default",
        fetch_mode="initial",
        params_file=None,
    )


@pytest.fixture(scope="module")
def cat(params: SynthParams) -> Catalog:
    return build_catalog(_SEED, params)


@pytest.fixture(scope="module")
def generated(
    tmp_path_factory: pytest.TempPathFactory, cat: Catalog, params: SynthParams
) -> tuple[Path, AggregateResult]:
    root = tmp_path_factory.mktemp("tiny_root")
    shards = plan_shards(cat, params)
    return root, run_all_shards(shards, cat, params, seed=_SEED, root=root, workers=_WORKERS)


def test_rf_tiny_generation_lake_has_no_dot_files(generated: tuple[Path, AggregateResult]) -> None:
    """T11-13 acceptance: no dot-prefixed file under `data/raw` after a tiny generation."""
    root, _ = generated
    raw = root / "data" / "raw"
    assert raw.is_dir()
    assert not [p for p in raw.rglob("*") if p.name.startswith(".")]


def test_rf_tiny_generation_writes_departments_and_task_slas(
    generated: tuple[Path, AggregateResult],
) -> None:
    """T11-13 acceptance (R-60): `servicenow/cmn_department` and `servicenow/task_sla` exist."""
    root, agg = generated
    raw = root / "data" / "raw" / "servicenow"
    for entity in ("cmn_department", "task_sla"):
        files = list((raw / entity).rglob("*.parquet"))
        assert files
        rows = sum(pq.read_metadata(f).num_rows for f in files)
        assert rows == agg.row_counts[f"servicenow/{entity}"] > 0


def test_rf_tiny_generation_counts_match_lake(generated: tuple[Path, AggregateResult]) -> None:
    """U11-19 every `source/entity` count equals the rows committed to the lake; the truth
    parts and the incident index sit under `<root>/truth/.parts/`, outside `data/`."""
    root, agg = generated
    raw = root / "data" / "raw"
    for key, rows in agg.row_counts.items():
        files = list((raw / key).rglob("*.parquet"))
        assert sum(pq.read_metadata(f).num_rows for f in files) == rows, key
    assert sorted({str(p.relative_to(raw).parent.parent).replace("\\", "/") for p in agg.files})
    assert set(agg.plant_members) == {"T2", "T2c", "T3"}
    assert agg.plant_pairs
    assert 0.0 < agg.generated_noise_ratio <= 1.0
    parts = root / "truth" / ".parts"
    assert list(parts.glob("labels-*.parquet"))
    assert list((parts / "idx").glob("*.opened.npy"))
    assert not (root / "data" / "truth").exists()


def test_rf_worker_failure_raises_fatal_naming_shard(
    tmp_path: Path, cat: Catalog, params: SynthParams
) -> None:
    """U11-19 a worker failure terminates the pool: `FatalError` naming the shard."""
    good = plan_shards(cat, params)[0]
    bad = dataclasses.replace(good, entity="bogus_entity", index=1)
    with pytest.raises(FatalError, match="servicenow/bogus_entity/2026-06"):
        run_all_shards([good, bad], cat, params, seed=_SEED, root=tmp_path, workers=_WORKERS)
