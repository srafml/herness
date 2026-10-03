"""F03-01 step bodies of `run_enrichment` (impl 03 §5; private sibling, T03-28 spec note).

Private sibling of `herness.enrich.pipeline`, split off for its 390-line budget: `prepare`
runs steps 1-2 (config, question set, cache/label migration, Laya state, primaries and
versions); one function per stage runs steps 3-13. The pipeline holds the GPU scopes and the
stage wrapper; these functions take no GPU lock. A rerun rebuilds the warehouse tables of
`text`, `link` and `resolve` (impl 03 §4.1). Logs and notes carry codes and counts only.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Final

import duckdb
import numpy as np

from herness.core import redact
from herness.core import time as clock
from herness.core.config import get_config
from herness.core.errors import (
    CircuitOpen,
    ConfigError,
    HernessError,
    ModelUnavailable,
    SchemaViolation,
    StoreBusy,
)
from herness.core.logging import get_logger
from herness.core.resilience import guard
from herness.core.types import DecisionInput
from herness.enrich import cache_maint
from herness.enrich.cache import DecisionCache
from herness.enrich.calibrate import CalibrationStore
from herness.enrich.cluster_stage import finalize_clusters, run_cluster_stage
from herness.enrich.decide import Decider, chain_after, primary_decider_for
from herness.enrich.decide_stage import ResolveArgs, run_decide_escalate, run_decide_primary
from herness.enrich.deciders import build_decider, register_deciders
from herness.enrich.deciders.laya import LayaDecider
from herness.enrich.deciders.llm import LlmDecider
from herness.enrich.embed import Device, embed_texts, get_encoder
from herness.enrich.embed_stage import run_embed_stage
from herness.enrich.ensemble_stage import ensemble_band
from herness.enrich.gpu import YieldRequested, release_cuda
from herness.enrich.labels import LabelStore
from herness.enrich.laya_models import verify_model_dir
from herness.enrich.layout import EnrichPaths
from herness.enrich.link_changes import link_candidates, pair_inputs, run_link_stage
from herness.enrich.mapping_suggest import prepare_mapping_vectors, run_suggest_stage
from herness.enrich.questions import (
    PAIR_QUESTIONS,
    check_fingerprint_registry,
    load_question_set,
    resolve_dynamic_options,
)
from herness.enrich.resolve import QueueItem, escalation_queue, resolve_frame, run_resolve
from herness.enrich.settings import check_decider_refs
from herness.enrich.text import build_text_redacted

if TYPE_CHECKING:
    from herness.enrich.pipeline import Run, StageReport

CHUNK: Final = 2_000  # inputs per member `decide` call and cache flush (U03-86, U03-89)
_ACCEPTED: Final = frozenset({"accepted"})
_PREV: Final = "enrich_prev_qsv"
_WIDE: Final = 20  # choice questions with more options need Laya's `embed_fn` (U03-58)
_WINDOW_SQL: Final = """SELECT count(*) FROM core.incident AS i JOIN enrich.text_redacted AS t
    ON t.record_id = i.record_id AND t.entity = 'incident' WHERE i.opened_at >= ?"""
_COVERAGE_SQL: Final = """SELECT entity, count(*) FILTER (WHERE status = 'final'),
    count(*) FILTER (WHERE status <> 'out_of_scope') FROM enrich_resolved GROUP BY entity"""

_log = get_logger("enrich.pipeline")


def _sql(
    wh: duckdb.DuckDBPyConnection, sql: str, params: Sequence[object] = ()
) -> list[tuple[Any, ...]]:
    try:
        return wh.execute(sql, list(params)).fetchall()
    except duckdb.Error as exc:
        msg = f"enrichment pipeline: {type(exc).__name__}"
        raise SchemaViolation(msg) from None


def degrade(report: StageReport, note: str, exc: BaseException | None = None) -> None:
    """Mark `report` degraded with the code `note`; log the error class only."""
    report.status, report.note = "degraded", note
    error_class = None if exc is None else type(exc).__name__
    _log.warning("enrich.stage.degraded", note=note, error_class=error_class)


def device() -> Device:
    import torch  # noqa: PLC0415 - lazy: importing the pipeline never loads torch

    return "cuda" if torch.cuda.is_available() else "cpu"


# --- steps 1-2 ------------------------------------------------------------------------------


def prepare(run: Run) -> None:
    """F03-01 steps 1-2. ConfigError (config, decider refs, fingerprint drift) before GPU work."""
    run.cfg = cfg = get_config()
    dcfg, dset = cfg.decisions, cfg.models.deciders
    if any(issue["severity"] == "error" for issue in check_decider_refs(dcfg, dset)):
        _log.error("enrich.config.invalid", error_class="ConfigError", question=None)
        msg = "decision config names a disabled decider"
        raise ConfigError(msg)
    register_deciders()  # once, at the composition root (idempotent)
    run.paths = EnrichPaths.from_config(cfg)
    qs = load_question_set(dcfg)
    check_fingerprint_registry(qs, paths=run.paths)
    run.qs = resolve_dynamic_options(qs, wh=run.wh, redact=lambda s: redact.redact_text(s) or "")
    run.cache, run.labels = DecisionCache(run.paths, qs.version), LabelStore(run.paths, qs.version)
    run.calibration = CalibrationStore(run.paths)
    _migrate(run)
    accepted = _laya(run)
    run.primaries = {
        q.id: primary_decider_for(q, cfg=dcfg, laya_accepted=accepted, deciders=dset)
        for q in run.qs.questions
    }
    _teachers(run)


def _migrate(run: Run) -> None:
    """F03-16: migrate cache and labels when the previous build used another version."""
    if run.prev is None or not run.prev.is_file():
        return
    path = str(run.prev).replace("'", "''")
    try:
        run.wh.execute(f"ATTACH '{path}' AS {_PREV} (READ_ONLY)")
    except duckdb.Error:
        return
    try:
        row = run.wh.execute(
            f"SELECT DISTINCT question_set_version FROM {_PREV}.enrich.decision LIMIT 1"  # noqa: S608
        ).fetchone()
    except duckdb.Error:
        row = None
    finally:
        run.wh.execute(f"DETACH {_PREV}")
    old = None if row is None else row[0]
    if isinstance(old, str) and old != run.qs.version:
        rows = cache_maint.migrate(run.paths, old, run.qs)
        labels = run.labels.migrate_from(old, run.qs)
        _log.info("enrich.pipeline.migrated", cache_rows=rows, label_rows=labels)


def _laya(run: Run) -> frozenset[str] | None:
    """Laya state (U03-59): accepted question ids, or None and `laya_degraded` (FT03-04)."""
    try:
        laya = build_decider("laya", cfg=run.cfg, depth=run.depth, paths=run.paths, llm=None,
                             embed_fn=_embed_fn(run))  # fmt: skip
        manifest = verify_model_dir(run.paths, laya.version, require_status=_ACCEPTED)
    except (ConfigError, ModelUnavailable) as exc:
        _log.warning("enrich.laya.degraded", reason=type(exc).__name__)
        run.warnings.append("laya_degraded")
        return None
    assert isinstance(laya, LayaDecider)  # noqa: S101 - build_decider("laya") contract
    run.laya = laya
    run.versions["laya"] = laya.version
    return frozenset(manifest.accepted_questions)


def _embed_fn(run: Run) -> Callable[[Sequence[str]], np.ndarray] | None:
    """OI-06 fallback: CPU encoding for wide choice questions; None when there are none."""
    if not any(len(q.options or {}) > _WIDE for q in run.qs.questions):
        return None
    batch = run.cfg.decisions.embedding.batch_size

    def encode(texts: Sequence[str]) -> np.ndarray:
        encoder = get_encoder()
        encoder.load("cpu")
        return embed_texts(encoder, texts, batch_size=batch)

    return encode


def _teachers(run: Run) -> None:
    """Build the enabled escalation deciders now, so `versions` is complete for resolution."""
    for name in ("openjev", "jev"):
        if getattr(run.cfg.models.deciders, name).enabled:
            try:
                built = build_decider(name, cfg=run.cfg, depth=run.depth, paths=run.paths, llm=None)
            except HernessError as exc:  # e.g. jev without a key: the chain moves on
                _log.warning("enrich.decider.unavailable", decider=name,
                             error_class=type(exc).__name__, deferred=0)  # fmt: skip
                run.warnings.append(f"{name}_unavailable")
                continue
            run.deciders[name], run.versions[name] = built, built.version
    if run.llm_factory is None:
        return
    try:
        llm = build_decider("llm", cfg=run.cfg, depth=run.depth, paths=run.paths,
                            llm=run.llm_factory("enrich_decider"))  # fmt: skip
    except (ModelUnavailable, CircuitOpen) as exc:
        _log.warning("enrich.decider.unavailable", decider="llm",
                     error_class=type(exc).__name__, deferred=0)  # fmt: skip
        return
    run.deciders["llm"], run.versions["llm"] = llm, llm.version


def _teacher_name(run: Run) -> str | None:
    chain = chain_after("laya", cfg=run.cfg.decisions, deciders=run.cfg.models.deciders)
    return next((name for name in chain if name != "llm"), None)


def _resolve_args(run: Run) -> ResolveArgs:
    return ResolveArgs(cfg=run.cfg.decisions, deciders=run.cfg.models.deciders,
                       labels=run.labels, calibration=run.calibration, primaries=run.primaries,
                       versions=dict(run.versions), now=run.started_at)  # fmt: skip


def frame(run: Run) -> None:
    """`resolve_frame` (U03-79) into the temp table `enrich_resolved` of `run.wh`."""
    a = _resolve_args(run)
    resolve_frame(run.wh, qs=run.qs, cfg=a.cfg, deciders=a.deciders, cache=run.cache,
                  labels=a.labels, calibration=a.calibration, primaries=a.primaries,
                  versions=a.versions, now=a.now)  # fmt: skip


# --- steps 3-8: text, embed, decide-primary, link candidates, decide-escalate, cluster --------


def text(run: Run, report: StageReport) -> None:
    _sql(run.wh, "DELETE FROM enrich.text_redacted")  # a rerun rebuilds the table (§4.1)
    build_text_redacted(run.wh, prev_warehouse=run.prev, report=report)


def embed(run: Run, report: StageReport) -> None:
    """Step 4: embeddings, then mapping and option vectors while the encoder is loaded."""
    encoder = get_encoder()
    encoder.load(device())
    try:
        run_embed_stage(run.wh, encoder=encoder, ctx=run.ctx, report=report)
        run.vectors = prepare_mapping_vectors(run.wh, encoder=encoder, qs=run.qs)
    finally:
        encoder.unload()


def decide_primary(run: Run, report: StageReport) -> None:
    run_decide_primary(run.wh, laya=run.laya, qs=run.qs, primaries=run.primaries,
                       cache=run.cache, ctx=run.ctx, report=report)  # fmt: skip


def _link_candidates(run: Run) -> None:
    if not run.linked:
        link_candidates(run.wh, cfg=run.cfg.decisions)
        run.linked = True


def decide_escalate(run: Run, report: StageReport) -> None:
    """Steps 6-7: change-link pairs, then the teacher (OpenJev started and stopped here)."""
    _link_candidates(run)
    pairs = pair_inputs(run.wh, cfg=run.cfg.decisions, qs=run.qs, paths=run.paths,
                        build_id=run.build_id)  # fmt: skip
    name = _teacher_name(run)
    teacher = run.deciders.get(name) if name else None
    started, cause = False, None
    if teacher is not None and name == "openjev":
        try:
            guard("decider:openjev")
            release_cuda()
            run.ctx.services.start("openjev")
            started = run.teacher_up = True
        except (ModelUnavailable, CircuitOpen) as exc:
            teacher, cause = None, exc
    try:
        run.deferred = run_decide_escalate(run.wh, teacher=teacher, qs=run.qs,
            resolve_args=_resolve_args(run), pairs=pairs, cache=run.cache,
            cfg=run.cfg.decisions, ctx=run.ctx, report=report)  # fmt: skip
        if name is not None and teacher is None:
            degrade(report, f"{name}_unavailable", cause)
        elif teacher is not None:  # T03-21 note: the teacher's below-gate primary answers
            _requeue(run)  # go to the LLM phase tonight, not only on the next run
        if run.depth == "deep":
            run.band = ensemble_band(run.wh, cfg=run.cfg.decisions, qs=run.qs)
            if teacher is not None and started:
                run.teacher_up = decide_members(run, teacher, run.band, "decide-escalate")
    finally:
        if started:
            run.ctx.services.stop("openjev")


def _requeue(run: Run) -> None:
    """Deferred = every record still queued once the teacher answered, then the pair items."""
    pairs = [i for i in run.deferred if set(i.question_ids) <= PAIR_QUESTIONS]
    frame(run)
    cap = run.cfg.decisions.escalation.max_rows_per_night
    run.deferred = escalation_queue(run.wh, max_records=cap) + pairs


def decide_members(run: Run, decider: Decider, items: Sequence[QueueItem], stage: str) -> bool:
    """Deep ensemble member over band items without its rows; False when it became unavailable."""
    have = run.cache.existing_keys(decider.name, decider.version, run.qs)
    todo = [DecisionInput.model_validate({"record_id": i.record_id, "entity": i.entity,
                "content_hash": i.content_hash, "text": i.text, "question_ids": qids})
            for i in items if (qids := tuple(
                q for q in i.question_ids if (i.content_hash, q) not in have))]  # fmt: skip
    samples = decider.samples if isinstance(decider, LlmDecider) else None
    writer = run.cache.writer(decider.name, decider.version, questions=run.qs, flush_rows=CHUNK)
    try:
        for start in range(0, len(todo), CHUNK):
            try:
                writer.add(decider.decide(todo[start : start + CHUNK], run.qs), samples=samples)
            except (ModelUnavailable, CircuitOpen) as exc:
                left, error_class = len(todo) - start, type(exc).__name__
                _log.warning("enrich.decider.unavailable", decider=decider.name,
                             error_class=error_class, deferred=left)  # fmt: skip
                return False
            run.ctx.heartbeat(stage)
            if run.ctx.should_yield():
                writer.flush()
                raise YieldRequested(stage)
    finally:
        writer.flush()
    return True


def _window_size(run: Run) -> int:
    since = clock.now() - timedelta(days=run.cfg.decisions.clustering.window_days)
    return int(_sql(run.wh, _WINDOW_SQL, [since])[0][0])


def cluster(run: Run, report: StageReport) -> None:
    """Step 8. Too few in-window vectors to cluster (a first small lake) -> degraded, skipped."""
    cl = run.cfg.decisions.clustering
    try:
        run.cluster = run_cluster_stage(run.wh, prev_warehouse=run.prev, paths=run.paths,
            cfg=run.cfg.decisions, build_id=run.build_id, force_full=run.force_full,
            device=device(), ctx=run.ctx, report=report)  # fmt: skip
    except ConfigError as exc:
        if _window_size(run) >= max(cl.pca_dims, cl.min_cluster_size, cl.min_samples + 1):
            raise
        degrade(report, "too_few_vectors", exc)


# --- steps 11-13: link, suggest, resolve -----------------------------------------------------


def link(run: Run, report: StageReport) -> None:
    _link_candidates(run)
    _sql(run.wh, "DELETE FROM enrich.incident_change_link")  # a rerun rebuilds the table
    name = _teacher_name(run)
    pair = (name, run.versions[name]) if name and name in run.versions else None
    run_link_stage(run.wh, cfg=run.cfg.decisions, qs=run.qs, cache=run.cache,
                   calibration=run.calibration, pair_decider=pair, report=report)  # fmt: skip


def suggest(run: Run, report: StageReport) -> None:
    try:
        run_suggest_stage(run.wh, vectors=run.vectors, cfg=run.cfg.decisions, report=report)
    except StoreBusy as exc:  # F03-01 step 12, after impl 02's retries
        degrade(report, "ops_busy", exc)


def resolve(run: Run, report: StageReport) -> None:
    """Step 13: `enrich.decision` (rebuilt on a rerun), cluster names and snapshot, compaction."""
    _sql(run.wh, "DELETE FROM enrich.decision")
    a = _resolve_args(run)
    run_resolve(run.wh, qs=run.qs, cfg=a.cfg, build_id=run.build_id,
        run_started_at=run.started_at, report=report, deciders=a.deciders, cache=run.cache,
        labels=a.labels, calibration=a.calibration, primaries=a.primaries,
        versions=a.versions, now=a.now)  # fmt: skip
    stats = _sql(run.wh, _COVERAGE_SQL)
    run.coverage = {str(e): final / scope for e, final, scope in stats if scope}
    run.escalation_share = report.escalated / report.decided if report.decided else None
    if run.cluster is not None:
        finalize_clusters(run.wh, result=run.cluster, names=run.names, qs=run.qs, paths=run.paths)
    cache_maint.compact(run.paths, run.qs.version)
