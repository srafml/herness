"""Enrichment composition root: stage order, GPU switching, checkpoints, report (U03-141 ...
U03-144; design 03 §5.1, flow F03-01).

`stages` is validated before any work (R-48) and config errors surface before GPU work. GPU
stages run inside `ctx.gpu_scope("decider")`, the reasoning phase in a nested
`gpu_scope("reasoning")` (R-43); no scope when no GPU stage runs. Each stage is logged,
measured and checkpointed (`ctx.save_state`, key `enrich`, merged into the entry state); a
rerun of the build skips done stages unless a pending stage needs their in-memory results.
`YieldRequested` (U03-152, re-exported) propagates after the stage flushed. Steps 1-8 and
11-13 live in the private sibling `_pipeline_stages` (T03-28 spec note). No text in logs,
notes or the report (TH03-03).
"""

from __future__ import annotations

import contextlib
import dataclasses
import functools
from collections.abc import Callable, Iterator, Sequence
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, get_args

import duckdb
import pyarrow.dataset as ds
from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from herness.core import time as clock
from herness.core.errors import CircuitOpen, ConfigError, ModelUnavailable
from herness.core.jobs import JobContext
from herness.core.logging import get_logger
from herness.core.resilience.metrics import record_counter, record_histogram
from herness.core.types import Depth, Entity, QuestionSet
from herness.enrich import _pipeline_stages as steps
from herness.enrich.cluster_describe import name_clusters
from herness.enrich.decide_stage import run_llm_escalation
from herness.enrich.deciders.llm import LlmDecider
from herness.enrich.ensemble_stage import ensemble_band, run_ensemble_pool
from herness.enrich.gpu import YieldRequested
from herness.enrich.resolve import QueueItem

if TYPE_CHECKING:
    from herness.core.config import HernessConfig
    from herness.enrich.cache import DecisionCache
    from herness.enrich.calibrate import CalibrationStore
    from herness.enrich.cluster_describe import NameResult
    from herness.enrich.cluster_stage import ClusterStageResult
    from herness.enrich.decide import Decider
    from herness.enrich.deciders.laya import LayaDecider
    from herness.enrich.deciders.llm import CompletionClient
    from herness.enrich.labels import LabelStore
    from herness.enrich.layout import EnrichPaths
    from herness.enrich.mapping_suggest import MappingVectors

__all__ = ["STAGE_ORDER", "EnrichReport", "LlmFactory", "StageName", "StageReport",
           "StageStatus", "YieldRequested", "run_enrichment"]  # fmt: skip

StageName = Literal[
    "text", "embed", "decide-primary", "decide-escalate", "ensemble", "cluster", "reasoning",
    "link", "suggest", "resolve",
]  # fmt: skip
STAGE_ORDER: Final[tuple[StageName, ...]] = get_args(StageName)
StageStatus = Literal["done", "skipped", "degraded", "failed"]
type LlmFactory = Callable[
    [Literal["enrich_decider", "cluster_namer"]], tuple[CompletionClient, str, int]
]
type _Body = Callable[[Run, StageReport], None]

_GPU_STAGES: Final[frozenset[StageName]] = frozenset(
    {"embed", "decide-primary", "decide-escalate", "ensemble", "cluster", "reasoning"}
)
# In-memory results a later stage needs: on a resumed run a done producer runs again (its
# work is idempotent) while one of its consumers still has to run.
_CONSUMERS: Final[dict[StageName, tuple[StageName, ...]]] = {
    "embed": ("suggest",), "decide-escalate": ("reasoning", "ensemble"),
    "cluster": ("reasoning", "resolve"), "reasoning": ("resolve",),
}  # fmt: skip
_NOTE_RE: Final = r"^[a-z][a-z0-9_]*$"  # fixed vocabulary codes, never record text
_PROMPT: Final = Path(str(resources.files("herness.enrich").joinpath("prompts/cluster_namer.md")))
_MEMBERS: Final = ("laya", "openjev", "jev", "llm")

_log = get_logger("enrich.pipeline")


class StageReport(BaseModel):
    """Per-stage counts (U03-142): mutated by the stage, a frozen copy goes in the report."""

    model_config = ConfigDict(validate_assignment=True, extra="forbid")

    status: StageStatus = "done"
    rows: int = 0
    cache_hits: int = 0
    embedded: int = 0
    decided: int = 0
    escalated: int = 0
    failed: int = 0
    duration_s: float = 0.0
    note: str | None = Field(default=None, max_length=200, pattern=_NOTE_RE)


class EnrichReport(BaseModel):
    """`run_enrichment` result (U03-143), stored by spec 08 in `job.result`; no text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    build_id: str
    depth: Depth
    started_at: datetime
    finished_at: datetime
    stages: dict[StageName, StageReport]
    decider_versions: dict[str, str]
    question_primary: dict[str, str]
    coverage: dict[Entity, float]
    escalation_share: float | None
    cluster_run: Literal["incremental", "full", "skipped"]
    warnings: list[str]

    @field_validator("stages")
    @classmethod
    def _in_stage_order(cls, stages: dict[StageName, StageReport]) -> dict[StageName, StageReport]:
        return {name: stages[name] for name in STAGE_ORDER if name in stages}


@dataclasses.dataclass(eq=False)
class Run:
    """State of one `run_enrichment` call; the fields after `started_at` are set by `prepare`."""

    wh: duckdb.DuckDBPyConnection
    build_id: str
    depth: Depth
    ctx: JobContext
    prev: Path | None
    llm_factory: LlmFactory | None
    force_full: bool
    started_at: datetime
    cfg: HernessConfig = dataclasses.field(init=False)
    paths: EnrichPaths = dataclasses.field(init=False)
    qs: QuestionSet = dataclasses.field(init=False)
    cache: DecisionCache = dataclasses.field(init=False)
    labels: LabelStore = dataclasses.field(init=False)
    calibration: CalibrationStore = dataclasses.field(init=False)
    laya: LayaDecider | None = None
    deciders: dict[str, Decider] = dataclasses.field(default_factory=dict)
    primaries: dict[str, str] = dataclasses.field(default_factory=dict)
    versions: dict[str, str] = dataclasses.field(default_factory=dict)
    warnings: list[str] = dataclasses.field(default_factory=list)
    vectors: MappingVectors | None = None
    deferred: list[QueueItem] = dataclasses.field(default_factory=list)
    band: list[QueueItem] | None = None
    teacher_up: bool = False  # OpenJev started for this run (deep: LLM labels the band if not)
    cluster: ClusterStageResult | None = None
    names: dict[str, NameResult] = dataclasses.field(default_factory=dict)
    linked: bool = False  # temp table `link_cand` built on `wh`
    coverage: dict[str, float] = dataclasses.field(default_factory=dict)
    escalation_share: float | None = None


def _selected(stages: Sequence[str] | None) -> frozenset[StageName]:
    """R-48: every value must be a stage name (ConfigError before any work); None = all."""
    for name in stages or ():
        if name not in STAGE_ORDER:
            msg = f"unknown enrichment stage {name}"
            raise ConfigError(msg)
    return frozenset(n for n in STAGE_ORDER if stages is None or n in stages)


def _resumed(state: dict[str, JsonValue], build_id: str) -> tuple[list[StageName], datetime | None]:
    """Done stages and first start time saved by an earlier attempt on the same build."""
    saved = state.get("enrich")
    if not isinstance(saved, dict) or saved.get("build_id") != build_id:
        return [], None
    done, started = saved.get("stages_done"), saved.get("started_at")
    names = [n for n in STAGE_ORDER if isinstance(done, list) and n in done]
    try:
        return names, datetime.fromisoformat(started) if isinstance(started, str) else None
    except ValueError:  # an unreadable time: the spot-check window starts now
        return names, None


def _skipped(selected: frozenset[StageName], done: Sequence[StageName]) -> frozenset[StageName]:
    """Done stages a resumed run skips: all but the producers a pending consumer needs."""
    skip = set(done) & selected
    changed = True
    while changed:
        changed = False
        for producer, consumers in _CONSUMERS.items():
            if producer in skip and any(c in selected and c not in skip for c in consumers):
                skip.discard(producer)
                changed = True
    return frozenset(skip)


class _Driver:
    """Runs the selected stages through one wrapper; keeps the reports and the checkpoint."""

    def __init__(
        self, run: Run, selected: frozenset[StageName], done: list[StageName],
        base: dict[str, JsonValue],
    ) -> None:  # fmt: skip
        self.run, self.selected, self.done, self.base = run, selected, done, base
        self.skip = _skipped(selected, done)
        self.reports = {name: StageReport() for name in STAGE_ORDER}

    def runs(self, name: StageName) -> bool:
        return name in self.selected and name not in self.skip

    def stage(self, name: StageName, body: _Body) -> None:
        report = self.reports[name]
        if not self.runs(name):
            report.status = "skipped"
            report.note = "resumed" if name in self.skip else "not_selected"
            return
        with self._wrapped(name, report):
            body(self.run, report)

    @contextlib.contextmanager
    def _wrapped(self, name: StageName, report: StageReport) -> Iterator[None]:
        fields = {"stage": name, "build_id": self.run.build_id, "job_id": self.run.ctx.job_id}
        _log.info("enrich.stage.started", **fields)
        started = clock.monotonic()
        try:
            yield
        except YieldRequested:
            _log.info("enrich.stage.yielded", **fields)
            raise
        except Exception as exc:  # logged, then re-raised unchanged: never swallowed
            report.status = "failed"
            _log.error("enrich.stage.failed", error_class=type(exc).__name__, **fields)
            raise
        finally:
            report.duration_s = round(clock.monotonic() - started, 3)
        self._completed(name, report, fields)

    def _completed(self, name: StageName, report: StageReport, fields: dict[str, str]) -> None:
        if report.status == "degraded" and report.note not in self.run.warnings:
            self.run.warnings.append(report.note or "degraded")
        _log.info("enrich.stage.completed", **report.model_dump(exclude={"note"}), **fields)
        labels = {"stage": name}
        record_histogram("herness_enrich_stage_duration_seconds", report.duration_s,
                         component="enrich", labels=labels)  # fmt: skip
        record_counter("herness_enrich_records_total", report.rows, component="enrich",
                       labels=labels)  # fmt: skip
        self.done.append(name)
        run = self.run
        done: list[JsonValue] = [n for n in STAGE_ORDER if n in self.done]
        checkpoint: dict[str, JsonValue] = {"build_id": run.build_id, "stages_done": done,
                                            "started_at": run.started_at.isoformat()}  # fmt: skip
        run.ctx.save_state({**self.base, "enrich": checkpoint})

    def report(self, started_at: datetime) -> EnrichReport:
        run = self.run
        return EnrichReport(build_id=run.build_id, depth=run.depth, started_at=started_at,
            finished_at=clock.now(), stages={n: r.model_copy() for n, r in self.reports.items()},
            decider_versions=dict(run.versions), question_primary=dict(run.primaries),
            coverage=dict(run.coverage),  # type: ignore[arg-type]  # entity names from SQL
            escalation_share=run.escalation_share, warnings=list(run.warnings),
            cluster_run=run.cluster.kind if run.cluster is not None else "skipped")  # fmt: skip


# --- F03-01 steps 9-10: reasoning phase and ensemble pooling ----------------------------------


def _reasoning_due(run: Run) -> bool:
    """Naming candidates, deferred items or (deep) ensemble band rows for the LLM."""
    naming = run.cluster is not None and bool(run.cluster.naming)
    return naming or bool(run.deferred) or (run.depth == "deep" and bool(run.band))


def _reasoning_phase(drv: _Driver) -> None:
    """Step 9: nested `gpu_scope("reasoning")` only when there is LLM work and an LLM."""
    run = drv.run
    if not drv.runs("reasoning") or not _reasoning_due(run):
        drv.stage("reasoning", _no_work)
        return
    with contextlib.ExitStack() as scope:
        available = "llm" in run.deciders
        if available:
            try:
                scope.enter_context(run.ctx.gpu_scope("reasoning"))
            except ModelUnavailable:
                available = False
        drv.stage("reasoning", functools.partial(_reasoning, available=available))


def _no_work(run: Run, report: StageReport) -> None:
    report.status, report.note = "skipped", "no_work"


def _reasoning(run: Run, report: StageReport, *, available: bool) -> None:
    """Names, LLM escalation of deferred items, deep LLM band rows (F03-06); without an LLM
    or the class switch: degraded `reasoning_unavailable`, auto labels, no LLM calls."""
    llm = run.deciders.get("llm") if available else None
    naming = run.cluster.naming if run.cluster is not None else []
    client = None
    if llm is None:
        steps.degrade(report, "reasoning_unavailable")
    elif naming and run.llm_factory is not None:
        try:
            client = run.llm_factory("cluster_namer")[0]
        except (ModelUnavailable, CircuitOpen) as exc:
            steps.degrade(report, "reasoning_unavailable", exc)
    if naming:
        root = next((q for q in run.qs.questions if q.id == "root_cause"), None)
        labels = list(root.options or {}) if root is not None else None
        calls = run.cfg.decisions.clustering.naming.max_llm_calls
        run.names = name_clusters(naming, client=client, root_cause_labels=labels,
                                  max_calls=calls, prompt_path=_PROMPT)  # fmt: skip
    assert llm is None or isinstance(llm, LlmDecider)  # noqa: S101 - build_decider("llm") contract
    cap = run.cfg.decisions.escalation.llm_max_rows_per_night
    run_llm_escalation(run.deferred, llm=llm, qs=run.qs, cache=run.cache, cap=cap, ctx=run.ctx,
                       report=report)  # fmt: skip
    if llm is not None and run.depth == "deep" and run.band:
        band = _llm_band(run)[: run.cfg.decisions.ensemble.llm_max_rows]
        steps.decide_members(run, llm, band, "reasoning")


def _llm_band(run: Run) -> list[QueueItem]:
    """F03-08 step 3: the whole band without OpenJev, else the items Laya and OpenJev differ on."""
    band = run.band or []
    dataset = run.cache.dataset()
    if not run.teacher_up or dataset is None or not {"laya", "openjev"} <= run.versions.keys():
        return band
    where = ds.field("content_hash").isin(sorted({i.content_hash for i in band}))
    columns = ["content_hash", "question", "decider", "decider_version", "answer"]
    rows = dataset.to_table(columns=columns, filter=where).to_pylist()
    answer = {(r["decider"], r["content_hash"], r["question"]): r["answer"] for r in rows
              if run.versions.get(r["decider"]) == r["decider_version"]}  # fmt: skip
    return [i for i in band if any(answer.get(("laya", i.content_hash, q))
            != answer.get(("openjev", i.content_hash, q)) for q in i.question_ids)]  # fmt: skip


def _ensemble(run: Run, report: StageReport) -> None:
    """Step 10, deep only (U03-141 invariant): pool the members' cached band rows (U03-89)."""
    if run.depth != "deep":
        report.status, report.note = "skipped", "not_deep"
        return
    if run.band is None:  # decide-escalate did not run in this call
        steps.frame(run)
        run.band = ensemble_band(run.wh, cfg=run.cfg.decisions, qs=run.qs)
    members = [(d, run.versions[d]) for d in _MEMBERS if d in run.versions]
    gold: dict[tuple[str, str], float] = {(d, qid): result.accuracy for d, v in members
        for qid, result in run.calibration.load(d, v, run.qs.version).items()}  # fmt: skip
    run.versions["ensemble"] = run_ensemble_pool(run.wh, band=run.band, qs=run.qs,
        cfg=run.cfg.decisions, cache=run.cache, calibration=run.calibration, members=members,
        gold_accuracy=gold, build_id=run.build_id, report=report)  # fmt: skip


def run_enrichment(  # noqa: PLR0913 - U03-144's signature is binding
    wh: duckdb.DuckDBPyConnection,
    build_id: str,
    *,
    depth: Depth,
    ctx: JobContext,
    prev_warehouse: Path | None,
    stages: Sequence[str] | None = None,
    llm_factory: LlmFactory | None = None,
    force_full_recluster: bool = False,
) -> EnrichReport:
    """Run the enrichment stages of flow F03-01 on the build connection `wh` (U03-144).

    Raises ConfigError (unknown stage, config, fingerprint drift), FatalError (OOM at batch
    1) and SchemaViolation (SQL); `YieldRequested` propagates to impl 02's `_stage_enrich`.
    Never touches the warehouse `CURRENT`; the job's GPU class on return equals its entry one.
    """
    selected = _selected(stages)
    started_at = clock.now()
    base = ctx.load_state()
    done, first_start = _resumed(base, build_id)
    run = Run(wh, build_id, depth, ctx, prev_warehouse, llm_factory,
                    force_full_recluster, first_start or started_at)  # fmt: skip
    steps.prepare(run)
    drv = _Driver(run, selected, done, base)
    drv.stage("text", steps.text)
    gpu = any(drv.runs(name) for name in _GPU_STAGES)
    with ctx.gpu_scope("decider") if gpu else contextlib.nullcontext():
        drv.stage("embed", steps.embed)
        drv.stage("decide-primary", steps.decide_primary)
        drv.stage("decide-escalate", steps.decide_escalate)
        drv.stage("cluster", steps.cluster)
        _reasoning_phase(drv)
        drv.stage("ensemble", _ensemble)
    drv.stage("link", steps.link)
    drv.stage("suggest", steps.suggest)
    drv.stage("resolve", steps.resolve)
    return drv.report(started_at)
