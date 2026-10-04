"""Tests for tools.synth.shards (U11-19) in-process: plan, run_shard, abort, path guard.

The spawn-pool run (acceptance: tiny generation) is in
`tests/integration/tools/synth/test_synth_shards_pool.py`; IT11-01/02 belong to T11-14.
"""

import dataclasses
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from tools.synth import _shard_io
from tools.synth.catalog import Catalog, build_catalog
from tools.synth.catalog_plan import background_count
from tools.synth.params import SynthParams, SynthUsageError, load_params
from tools.synth.shards import (
    DIMENSIONS,
    AggregateResult,
    Shard,
    plan_shards,
    require_under,
    run_all_shards,
    run_shard,
    worker_context,
)

pytestmark = pytest.mark.unit

_SEED = 7
_ORDER = ["incident", "change_request", "problem", "issue", "event", "metric_daily"]


@pytest.fixture(scope="module")
def params() -> SynthParams:
    return load_params(
        "tiny",
        start=date(2026, 6, 1),
        end=date(2026, 8, 31),
        sources=("servicenow", "jira", "monitoring"),
        dirty="default",
        fetch_mode="initial",
        params_file=None,
    )


@pytest.fixture(scope="module")
def cat(params: SynthParams) -> Catalog:
    return build_catalog(_SEED, params)


def _lake_files(root: Path) -> list[Path]:
    raw = root / "data" / "raw"
    return [p for p in raw.rglob("*") if p.is_file()] if raw.exists() else []


def test_rf_plan_shards_order_counts_and_dimensions(cat: Catalog, params: SynthParams) -> None:
    """U11-19 plan: 5 dimension shards at the first month (R-60), then entities in order by
    month; background counts and seq starts from the catalog; indexes 0..n-1."""
    shards = plan_shards(cat, params)
    assert [s.index for s in shards] == list(range(len(shards)))
    dims = shards[: len(DIMENSIONS)]
    assert [s.entity for s in dims] == list(DIMENSIONS)
    assert {s.month for s in dims} == {params.start.replace(day=1)}
    assert dims[0].n_records == len(cat.orgs) + len(cat.teams)
    rest = shards[len(DIMENSIONS) :]
    entities = list(dict.fromkeys(s.entity for s in rest))
    assert entities == _ORDER
    for shard in rest:
        key = (shard.source, shard.entity, shard.month)
        assert shard.n_records == background_count(cat, key)
        assert shard.seq_start == cat.seq_start[key]
        assert shard.month.day == 1


def test_rf_plan_shards_without_servicenow(cat: Catalog, params: SynthParams) -> None:
    """U11-19 plan: no ServiceNow source -> no dimension or ServiceNow shards."""
    jira_only = params.model_copy(update={"sources": ("jira",)})
    shards = plan_shards(dataclasses.replace(cat, month_counts={
        k: v for k, v in cat.month_counts.items() if k[0] == "jira"}), jira_only)  # fmt: skip
    assert shards
    assert {s.source for s in shards} == {"jira"}


def test_rf_run_shard_incident_writes_lake_index_and_parts(
    tmp_path: Path, cat: Catalog, params: SynthParams
) -> None:
    """U11-19 an incident shard commits incident and `task_sla` files (no dot files), writes
    the month's incident index and its label, text, PII, member and pair parts."""
    ctx = worker_context(tmp_path, _SEED, params, cat)
    shard = next(s for s in plan_shards(cat, params) if s.entity == "incident")
    result = run_shard(shard, ctx)
    assert set(result.row_counts) == {"servicenow/incident", "servicenow/task_sla"}
    assert result.rows_written == sum(result.row_counts.values())
    assert sum(pq.read_metadata(f).num_rows for f in result.files) == result.rows_written
    assert not [p for p in _lake_files(tmp_path) if p.name.startswith(".")]
    assert result.labels_path is not None
    assert result.labels_path.parent == ctx.parts_dir
    assert set(result.plant_members) == {"T2", "T2c", "T3"}
    assert len(result.plant_pairs) == len(result.plant_members["T3"])
    assert result.dirty.missing_service > 0
    index, _ = _shard_io.load_index(ctx.parts_dir, *_bounds(shard))
    assert sum(len(v) for v in index.opened_at.values()) > shard.n_records * 0.8
    for svc, times in index.opened_at.items():
        assert list(times) == sorted(times)
        assert len(index.numbers[svc]) == len(times)


def _bounds(shard: Shard) -> tuple[datetime, datetime]:
    start = datetime.combine(shard.month, time(), UTC)
    nxt = (shard.month.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start, datetime.combine(nxt, time(), UTC)


def test_rf_run_shard_dimensions_and_departments(
    tmp_path: Path, cat: Catalog, params: SynthParams
) -> None:
    """U11-19 dimension shards write every dimension entity, `cmn_department` included (R-60)."""
    ctx = worker_context(tmp_path, _SEED, params, cat)
    for shard in plan_shards(cat, params)[: len(DIMENSIONS)]:
        result = run_shard(shard, ctx)
        key = f"servicenow/{shard.entity}"
        assert result.row_counts[key] >= shard.n_records
        assert result.labels_path is None
    raw = tmp_path / "data" / "raw" / "servicenow"
    assert sorted(p.name for p in raw.iterdir()) == sorted(DIMENSIONS)


def test_rf_run_shard_failure_aborts_writers(
    tmp_path: Path, cat: Catalog, params: SynthParams, monkeypatch: pytest.MonkeyPatch
) -> None:
    """U11-19 an exception after lake writes aborts every open writer: no lake file, no temp
    file remains, and the error propagates."""
    ctx = worker_context(tmp_path, _SEED, params, cat)
    shard = next(s for s in plan_shards(cat, params) if s.entity == "incident")
    from tools.synth import _shard_run  # noqa: PLC0415 - patched module

    def boom(*_args: object, **_kwargs: object) -> list[object]:
        msg = "injected"
        raise RuntimeError(msg)

    monkeypatch.setattr(_shard_run, "gen_task_slas", boom)
    with pytest.raises(RuntimeError, match="injected"):
        run_shard(shard, ctx)
    assert _lake_files(tmp_path) == []


def test_rf_require_under_rejects_escape(tmp_path: Path, cat: Catalog, params: SynthParams) -> None:
    """U11-19 TH11-07: paths are resolved and must stay under the root."""
    assert require_under(tmp_path, tmp_path / "a" / "b") == (tmp_path / "a" / "b").resolve()
    with pytest.raises(SynthUsageError):
        require_under(tmp_path / "root", tmp_path / "root" / ".." / "other")
    with pytest.raises(SynthUsageError):
        worker_context(tmp_path / "root", _SEED, params, cat, raw_root=tmp_path / "lake")


def test_rf_run_all_shards_argument_checks(
    tmp_path: Path, cat: Catalog, params: SynthParams
) -> None:
    """U11-19 `workers < 1` and a missing root are usage errors; no shards -> empty result."""
    with pytest.raises(SynthUsageError):
        run_all_shards([], cat, params, seed=_SEED, root=tmp_path, workers=0)
    with pytest.raises(SynthUsageError):
        run_all_shards([], cat, params, seed=_SEED, root=tmp_path / "missing", workers=1)
    out = run_all_shards([], cat, params, seed=_SEED, root=tmp_path, workers=1)
    assert isinstance(out, AggregateResult)
    assert out.rows_written == 0
    assert out.generated_noise_ratio == 0.0
    assert (tmp_path / "truth" / ".parts" / "idx").is_dir()
