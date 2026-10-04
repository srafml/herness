"""Spawn-pool run of tools.synth.shards (U11-19), T11-13 acceptance.

A tiny generation through `run_all_shards` (2 spawn workers) into `tmp_path` leaves no
dot-prefixed file under `data/raw` and writes `servicenow/cmn_department` and
`servicenow/task_sla`; a failing shard terminates the pool with a `FatalError` naming it.
"""

import dataclasses
import hashlib
import logging
from datetime import date
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from herness.core import config as config_module
from herness.core.errors import FatalError
from herness.core.logging import configure_logging, reset_logging
from tools.synth import shards as shards_module
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
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(shards_module, "_ROWS_PER_WORKER", 1)  # tiny would run in process
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


def test_rf_in_process_failure_raises_fatal_naming_shard(
    tmp_path: Path, cat: Catalog, params: SynthParams
) -> None:
    """T11-14b the in-process run (one worker fits the plan) raises the same `FatalError`
    naming the shard, and leaves the root logger level as it found it."""
    good = plan_shards(cat, params)[0]
    bad = dataclasses.replace(good, entity="bogus_entity", index=1)
    before = logging.getLogger().level
    with pytest.raises(FatalError, match="servicenow/bogus_entity/2026-06"):
        run_all_shards([good, bad], cat, params, seed=_SEED, root=tmp_path, workers=1)
    assert logging.getLogger().level == before


def _lake_fingerprint(root: Path) -> dict[str, list[str]]:
    """Per lake directory, the sorted sha256 of its parquet parts (part names are ULIDs)."""
    raw = root / "data" / "raw"
    out: dict[str, list[str]] = {}
    for f in sorted(raw.rglob("*.parquet")):
        out.setdefault(f.parent.parent.relative_to(raw).as_posix(), []).append(
            hashlib.sha256(f.read_bytes()).hexdigest()
        )
    return {k: sorted(v) for k, v in out.items()}


def test_rf_in_process_matches_pool_under_non_default_config(
    tmp_path: Path,
    cat: Catalog,
    params: SynthParams,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """T11-14b in-process and pool runs initialise the same way (`init_config` for profile
    `synth`) and write the same bytes under a non-default config (`HERNESS_PATHS__DATA`
    pointing elsewhere; the run's `paths.data` override wins in both). The in-process run
    prints nothing to stdout and restores the config cache and the root logger level."""
    decoy = tmp_path / "decoy"
    monkeypatch.setenv("HERNESS_PATHS__DATA", decoy.as_posix())
    shards = plan_shards(cat, params)
    cached = (config_module._Cache.config, config_module._Cache.root)
    inproc, pooled = tmp_path / "inproc", tmp_path / "pool"
    inproc.mkdir()
    pooled.mkdir()
    configure_logging("INFO")  # as the CLI does; stderr only
    capfd.readouterr()
    try:
        one = run_all_shards(shards, cat, params, seed=_SEED, root=inproc, workers=1)
        captured = capfd.readouterr()
        assert captured.out == ""
        assert "config.validate.issue" not in captured.err  # ERROR level inside the run
        level = logging.getLogger().level
    finally:
        reset_logging()
    assert level == logging.INFO  # restored after the run
    assert (config_module._Cache.config, config_module._Cache.root) == cached
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(shards_module, "_ROWS_PER_WORKER", 1)
        many = run_all_shards(shards, cat, params, seed=_SEED, root=pooled, workers=4)
    assert one.row_counts == many.row_counts
    assert _lake_fingerprint(inproc) == _lake_fingerprint(pooled)
    assert _lake_fingerprint(inproc)
    assert not decoy.exists()
