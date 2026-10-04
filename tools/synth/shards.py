"""Generation shards of the synthetic lake (U11-19, design §5.1.8).

`plan_shards` splits the work into `(source, entity, month)` shards; `run_all_shards` runs
them in a spawn pool in two phases (A: incident shards, which write the per-month incident
time index under `<root>/truth/.parts/idx/`; B: everything else) and sums their
`ShardResult`s. `run_shard` generates one shard, applies plants, dirty defects and fetch
placement, and commits it through `herness.store.lake.LakeWriter`; the work itself lives in
the private siblings `tools.synth._shard_run` and `tools.synth._shard_io` (line budget).
The `Shard`, `IncidentTimeIndex` and `PlantOutput` value types predate this card (they are
shared with the generators, which import them from here, so the generator modules are
imported lazily below).
"""

from __future__ import annotations

import dataclasses
import multiprocessing
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import numpy as np
import numpy.typing as npt

from herness.core.errors import FatalError
from tools.synth.catalog_plan import background_count
from tools.synth.catalog_rows import Catalog, MonthKey
from tools.synth.params import SynthParams, SynthUsageError

if TYPE_CHECKING:
    from tools.synth.dirty import DirtyCounters
    from tools.synth.text import TemplateBank

TARGET_BYTES: Final = 128 * 2**20  # LakeWriter target file size (U11-19)
DIMENSIONS: Final = (
    "sys_user_group",
    "cmn_department",
    "cmdb_ci",
    "cmdb_ci_service",
    "cmdb_rel_ci",
)
_ORDER: Final = (
    ("servicenow", "incident"),
    ("servicenow", "change_request"),
    ("servicenow", "problem"),
    ("jira", "issue"),
    ("monitoring", "event"),
    ("monitoring", "metric_daily"),
)
PARTS: Final = Path("truth") / ".parts"


@dataclasses.dataclass(frozen=True, slots=True)
class Shard:
    """One `(source, entity, month)` unit of work; `month` is the first day of the month.

    `n_records` background records are numbered from `seq_start`; `index` is the shard's
    position in the plan.
    """

    source: str
    entity: str
    month: date
    n_records: int
    seq_start: int
    index: int


@dataclasses.dataclass(frozen=True, slots=True, eq=False)
class IncidentTimeIndex:
    """Incident open times per service (keyed by the incident's `business_service` sys_id).

    For each service `opened_at[s]` is a sorted `int64` array of `opened_at` epoch seconds
    (UTC) for the shard month +/- 1 day; `sys_ids[s]` and `numbers[s]` hold the matching
    incident sys_ids and numbers in the same order. Data only; U11-19 builds it.
    """

    opened_at: Mapping[str, npt.NDArray[np.int64]]
    sys_ids: Mapping[str, tuple[str, ...]]
    numbers: Mapping[str, tuple[str, ...]]


@dataclasses.dataclass(frozen=True, slots=True)
class PlantOutput:
    """What one plant adds to a shard (U11-11): source-shaped `records`, truth label rows
    `{record_id, question, answer}`, `members` (incident record ids of the plant) and
    `links` (pair rows such as T3 `{incident_record_id, change_record_id}`); `replaced`
    lists the ids of background records a plant replaced in place (T6), whose background
    label and PII rows U11-19 drops. Data only."""

    records: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    labels: list[dict[str, str]] = dataclasses.field(default_factory=list)
    members: list[str] = dataclasses.field(default_factory=list)
    links: list[dict[str, str]] = dataclasses.field(default_factory=list)
    replaced: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass(frozen=True, slots=True)
class ShardResult:
    """What one shard wrote (U11-19). `row_counts` (rows per `source/entity`, the incident
    shards' `servicenow/task_sla` rows included) and `s4_noise` (S4 events with a noise
    severity and NULL `incident_ref`, all S4 events with a noise severity; U11-13) are
    additive fields the truth manifest needs."""

    rows_written: int
    dirty: DirtyCounters
    labels_path: Path | None
    pii_rows: int
    plant_members: dict[str, list[str]]
    plant_pairs: list[dict[str, str]]
    files: tuple[Path, ...]
    row_counts: dict[str, int] = dataclasses.field(default_factory=dict)
    s4_noise: tuple[int, int] = (0, 0)


@dataclasses.dataclass(frozen=True, slots=True, eq=False)
class WorkerContext:
    """Per-process, read-only inputs of `run_shard`; build it with `worker_context`."""

    root: Path
    seed: int
    params: SynthParams
    cat: Catalog
    raw_root: Path
    parts_dir: Path
    bank: TemplateBank
    names: tuple[tuple[str, str], ...]


@dataclasses.dataclass(slots=True)
class AggregateResult:
    """Sum of every `ShardResult` of a run, in plan order (built by the parent, U11-19)."""

    parts_dir: Path
    dirty: DirtyCounters
    row_counts: dict[str, int] = dataclasses.field(default_factory=dict)
    pii_rows: int = 0
    plant_members: dict[str, list[str]] = dataclasses.field(default_factory=dict)
    plant_pairs: list[dict[str, str]] = dataclasses.field(default_factory=list)
    files: list[Path] = dataclasses.field(default_factory=list)
    s4_noise: tuple[int, int] = (0, 0)

    @property
    def rows_written(self) -> int:
        return sum(self.row_counts.values())

    @property
    def generated_noise_ratio(self) -> float:
        """U11-13: S4 noise events with NULL `incident_ref` / all S4 noise events (0 if none)."""
        null_ref, total = self.s4_noise
        return null_ref / total if total else 0.0

    def add(self, result: ShardResult) -> None:
        self.dirty.add(result.dirty)
        for key, rows in result.row_counts.items():
            self.row_counts[key] = self.row_counts.get(key, 0) + rows
        self.pii_rows += result.pii_rows
        for plant, members in result.plant_members.items():
            self.plant_members.setdefault(plant, []).extend(members)
        self.plant_pairs.extend(result.plant_pairs)
        self.files.extend(result.files)
        null_ref, total = result.s4_noise
        self.s4_noise = (self.s4_noise[0] + null_ref, self.s4_noise[1] + total)


def _dimension_sizes(cat: Catalog) -> dict[str, int]:
    return {
        "sys_user_group": len(cat.orgs) + len(cat.teams),
        "cmn_department": len(cat.orgs),
        "cmdb_ci": len(cat.cis),
        "cmdb_ci_service": len(cat.services),
        "cmdb_rel_ci": len(cat.rels),
    }


def plan_shards(cat: Catalog, params: SynthParams) -> list[Shard]:
    """Dimension shards at the first month (R-60), then per month incidents, changes,
    problems, Jira issues, events and `metric_daily`; `n_records` is the month's background
    count (plants add theirs) and `seq_start` its first sequence number from the catalog."""
    plan: list[tuple[str, str, date, int, int]] = []
    if "servicenow" in params.sources:
        sizes = _dimension_sizes(cat)
        first = params.start.replace(day=1)
        plan += [("servicenow", entity, first, sizes[entity], 1) for entity in DIMENSIONS]
    for source, entity in _ORDER:
        keys: list[MonthKey] = sorted(k for k in cat.month_counts if k[:2] == (source, entity))
        plan += [(source, entity, k[2], background_count(cat, k), cat.seq_start[k]) for k in keys]
    return [Shard(s, e, m, n, q, i) for i, (s, e, m, n, q) in enumerate(plan)]


def label(shard: Shard) -> str:
    """`source/entity/YYYY-MM` of a shard (error messages and logs)."""
    return f"{shard.source}/{shard.entity}/{shard.month:%Y-%m}"


def require_under(root: Path, path: Path) -> Path:
    """`path` resolved; `SynthUsageError` unless it lies under the resolved `root` (TH11-07)."""
    resolved = path.resolve(strict=False)
    if not resolved.is_relative_to(root.resolve(strict=False)):
        msg = "path escapes the synthetic root"
        raise SynthUsageError(msg, key="root")
    return resolved


def worker_context(
    root: Path, seed: int, params: SynthParams, cat: Catalog, *, raw_root: Path | None = None
) -> WorkerContext:
    """The context of `run_shard`: lake under `raw_root` (default `<root>/data/raw`), temp
    parts under `<root>/truth/.parts`, the template bank and the made-up name list."""
    from tools.synth.pii import build_name_list  # noqa: PLC0415 - generators import Shard
    from tools.synth.text import TemplateBank  # noqa: PLC0415 - kept lazy with the above

    base = root.resolve(strict=False)
    raw = require_under(base, base / "data" / "raw" if raw_root is None else raw_root)
    parts = require_under(base, base / PARTS)
    return WorkerContext(base, seed, params, cat, raw, parts, TemplateBank(), build_name_list(seed))


def run_shard(shard: Shard, ctx: WorkerContext) -> ShardResult:
    """Generate, plant, dirty, place and commit one shard; write its truth parts. On any
    error every open lake writer is aborted and the error re-raised."""
    from tools.synth._shard_run import execute  # noqa: PLC0415 - generators import Shard

    return execute(shard, ctx)


CONFIG_DIR: Final = Path(__file__).resolve().parents[2] / "config"
_STATE: dict[str, Any] = {}


def _init_worker(root: Path, seed: int, params: SynthParams, cat: Catalog) -> None:
    """Pool initializer: config profile `synth` with `paths.data = <root>/data` (T10-03).
    A failure is kept for `_pool_task` to raise (a raising initializer makes the pool
    respawn workers forever)."""
    try:
        from herness.core.config import init_config  # noqa: PLC0415 - worker process only
        from herness.store.layout import data_layout  # noqa: PLC0415 - worker process only

        data = (root / "data").as_posix()
        cfg = init_config("synth", overrides=(f"paths.data={data}",), config_dir=CONFIG_DIR)
        _STATE["ctx"] = worker_context(root, seed, params, cat, raw_root=data_layout(cfg=cfg).raw)
    except Exception as exc:  # noqa: BLE001 - reported by _pool_task as FatalError
        _STATE["error"] = type(exc).__name__


def _pool_task(shard: Shard) -> ShardResult:
    ctx = _STATE.get("ctx")
    if ctx is None:
        msg = f"synth worker initialisation failed ({_STATE.get('error', 'unknown')})"
        raise FatalError(msg, shard=label(shard))
    try:
        return run_shard(shard, ctx)
    except Exception as exc:
        msg = f"synth shard {label(shard)} failed"
        raise FatalError(msg, shard=label(shard), cause=type(exc).__name__) from exc


def run_all_shards(
    shards: Sequence[Shard],
    cat: Catalog,
    params: SynthParams,
    *,
    seed: int,
    root: Path,
    workers: int,
) -> AggregateResult:
    """Run phase A (incident shards) then phase B (the rest) in a spawn pool of `workers`
    processes and sum the results in plan order. A worker failure terminates the pool and
    raises `FatalError` naming the shard; the root is left as is for inspection."""
    from tools.synth.dirty import DirtyCounters  # noqa: PLC0415 - generators import Shard

    if workers < 1:
        msg = "workers must be at least 1"
        raise SynthUsageError(msg, key="workers")
    base = root.resolve(strict=False)
    if not base.is_dir():
        msg = "the synthetic root does not exist"
        raise SynthUsageError(msg, key="root")
    out = AggregateResult(require_under(base, base / PARTS), DirtyCounters())
    (out.parts_dir / "idx").mkdir(parents=True, exist_ok=True)
    if not shards:
        return out
    incident = ("servicenow", "incident")
    phases = (
        [s for s in shards if (s.source, s.entity) == incident],
        [s for s in shards if (s.source, s.entity) != incident],
    )
    spawn = multiprocessing.get_context("spawn")
    size = min(workers, len(shards))
    with spawn.Pool(size, initializer=_init_worker, initargs=(base, seed, params, cat)) as pool:
        try:
            for phase in phases:
                for result in pool.imap(_pool_task, phase):
                    out.add(result)
        except FatalError:
            pool.terminate()
            raise
        except Exception as exc:
            pool.terminate()
            msg = "synth shard pool failed"
            raise FatalError(msg, cause=type(exc).__name__) from exc
    return out


__all__ = [
    "CONFIG_DIR",
    "DIMENSIONS",
    "PARTS",
    "TARGET_BYTES",
    "AggregateResult",
    "IncidentTimeIndex",
    "PlantOutput",
    "Shard",
    "ShardResult",
    "WorkerContext",
    "label",
    "plan_shards",
    "require_under",
    "run_all_shards",
    "run_shard",
    "worker_context",
]
