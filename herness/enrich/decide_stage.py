"""Decide stages: `build_inputs`, `decide-primary`, `decide-escalate`, LLM escalation.

Impl 03 §3.13 (U03-84 ... U03-87; design 03 §5.1, §5.7; F03-04 ... F03-06). The caller
(`run_enrichment`, T03-28) holds the GPU class (R-43), starts services and builds deciders;
these stages take no GPU lock. A yield request flushes the cache writer, then raises
`YieldRequested(<stage>)` (U03-152). Logs, errors and notes carry counts, ids and codes only.
`report` is the pipeline's `StageReport` (U03-142, imported for typing only, as `_Report`).
`ResolveArgs` bundles `resolve_frame`'s arguments but `wh`, `qs` and `cache`.
Spec note (T03-21): the teacher is sent only queued questions it has no current row for
(`exclude_deciders`); those it answered below the gate go straight to the LLM phase.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Final, Literal

import duckdb
import pyarrow as pa
import pyarrow.dataset as ds

from herness.core.config import get_config
from herness.core.errors import (
    AuthError,
    CircuitOpen,
    ConfigError,
    EgressBlocked,
    ModelUnavailable,
    RecoverableError,
    RetryableError,
    SchemaViolation,
)
from herness.core.jobs import JobContext
from herness.core.jobs.gpu import gpu_state
from herness.core.logging import get_logger
from herness.core.resilience import DeciderChain, GpuStateReader
from herness.core.resilience.metrics import record_counter
from herness.core.types import DecisionInput, DecisionOutput, QuestionSet
from herness.enrich.cache import CacheWriter, DecisionCache
from herness.enrich.calibrate import CalibrationStore
from herness.enrich.decide import Decider
from herness.enrich.deciders.laya import LayaDecider
from herness.enrich.deciders.llm import LlmDecider
from herness.enrich.gpu import YieldRequested
from herness.enrich.labels import LabelStore
from herness.enrich.questions import PAIR_QUESTIONS, question_fingerprint
from herness.enrich.resolve import QueueItem, escalation_queue, resolve_frame
from herness.enrich.settings import DecidersSettings, DecisionsConfig

if TYPE_CHECKING:
    from herness.enrich.pipeline import StageReport as _Report
    from herness.enrich.pipeline import StageStatus

__all__ = [
    "ResolveArgs", "build_inputs", "run_decide_escalate", "run_decide_primary",
    "run_llm_escalation",
]  # fmt: skip

DECIDE_CHUNK: Final = 2_000  # inputs per `decide` chunk (U03-84, U03-86)
LLM_CHUNK: Final = 500  # inputs per LLM chunk (U03-87)
WRITE_ROWS: Final = 2_000  # cache writes every 2,000 answers (U03-86, U03-87)
CHECKPOINT_CALLS: Final = 20  # Laya checkpoint every 20 calls (U03-85)
_HAVE, _ASKED = "_bi_have", "_bi_questions"  # registered Arrow views of `build_inputs`
_QUESTIONS_SCHEMA: Final = pa.schema(
    [("question", pa.string()), ("fingerprint", pa.string()), ("applies_to", pa.list_(pa.string()))]
)
_HAVE_COLUMNS: Final = ["content_hash", "question", "question_fingerprint"]
_ASKS_CTE: Final = f"""WITH asks AS (
    SELECT t.record_id, t.entity, t.content_hash, t.text, q.question,
        EXISTS (SELECT 1 FROM {_HAVE} AS h WHERE h.content_hash = t.content_hash
            AND h.question = q.question AND h.question_fingerprint = q.fingerprint) AS cached
    FROM enrich.text_redacted AS t
    JOIN {_ASKED} AS q ON list_contains(q.applies_to, t.entity){{boot}}
)"""  # noqa: S608 - fixed view names
_BOOT_JOIN: Final = """
    JOIN (SELECT 'incident' AS entity, record_id, opened_at FROM core.incident
        UNION ALL SELECT 'change', record_id, opened_at FROM core.change
        UNION ALL SELECT 'problem', record_id, opened_at FROM core.problem) AS o
        ON o.entity = t.entity AND o.record_id = t.record_id AND o.opened_at >= $since"""
_HITS_SQL: Final = f"{_ASKS_CTE} SELECT count(*) FROM asks WHERE cached"  # noqa: S608
_MISS_SQL: Final = f"""{_ASKS_CTE}
SELECT record_id, entity, content_hash, text, list_sort(list(question)) FROM asks
WHERE NOT cached GROUP BY record_id, entity, content_hash, text
ORDER BY content_hash, record_id, entity"""  # noqa: S608 - fixed view names

_log = get_logger("enrich.decide")


@dataclass(frozen=True, slots=True)
class ResolveArgs:
    """`resolve_frame` arguments except `wh`, `qs` and `cache` (U03-86)."""

    cfg: DecisionsConfig
    deciders: DecidersSettings
    labels: LabelStore
    calibration: CalibrationStore
    primaries: Mapping[str, str]
    versions: Mapping[str, str]
    now: datetime


def _mark(report: _Report, status: StageStatus, note: str) -> None:
    report.status, report.note = status, note


def _checkpoint(ctx: JobContext, writer: CacheWriter, stage: str) -> None:
    """Heartbeat; on a yield request flush the writer, then raise `YieldRequested(stage)`."""
    ctx.heartbeat(stage)
    if ctx.should_yield():
        writer.flush()
        raise YieldRequested(stage)


def _input(rid: str, entity: str, digest: str, text: str, qids: Sequence[str]) -> DecisionInput:
    return DecisionInput.model_validate({"record_id": rid, "entity": entity,
        "content_hash": digest, "text": text, "question_ids": tuple(qids)})  # fmt: skip


def _have_table(cache: DecisionCache, decider: str, version: str) -> pa.Table:
    dataset = cache.dataset()
    if dataset is None:
        return pa.table({c: pa.array([], pa.string()) for c in _HAVE_COLUMNS})
    where = (ds.field("decider") == decider) & (ds.field("decider_version") == version)
    return dataset.to_table(columns=_HAVE_COLUMNS, filter=where)


def _asked_table(qs: QuestionSet, question_ids: frozenset[str]) -> pa.Table:
    rows = [
        {"question": q.id, "fingerprint": q.fingerprint or question_fingerprint(q),
         "applies_to": list(q.applies_to)}
        for q in qs.questions if q.id in question_ids and q.id not in PAIR_QUESTIONS
    ]  # fmt: skip
    return pa.Table.from_pylist(rows, schema=_QUESTIONS_SCHEMA)


def build_inputs(  # noqa: PLR0913 - U03-84's keyword-only signature is binding
    wh: duckdb.DuckDBPyConnection, *, decider: str, version: str, qs: QuestionSet,
    question_ids: frozenset[str], cache: DecisionCache, chunk: int = DECIDE_CHUNK,
    record_filter_sql: Literal["all", "bootstrap"] = "all", since: datetime | None = None,
) -> Iterator[list[DecisionInput]]:  # fmt: skip
    """Stream `DecisionInput` chunks of records missing rows of (`decider`, `version`) (U03-84).

    Each input asks only its applicable, uncached non-pair questions of `question_ids`; hash
    order. Do not use `wh` while iterating. ConfigError (bad `chunk`, `bootstrap` without
    `since`); SchemaViolation (DuckDB or cache schema error)."""
    boot = record_filter_sql == "bootstrap"
    if chunk < 1 or (boot and since is None):
        msg = "build_inputs: chunk must be >= 1 and bootstrap needs since"
        raise ConfigError(msg, chunk=chunk)
    asked = _asked_table(qs, question_ids)
    if asked.num_rows == 0:
        return
    params: dict[str, object] = {"since": since} if boot else {}
    boot_sql = _BOOT_JOIN if boot else ""
    wh.register(_HAVE, _have_table(cache, decider, version))
    wh.register(_ASKED, asked)
    try:
        hits = wh.execute(_HITS_SQL.format(boot=boot_sql), params).fetchone()
        record_counter("herness_enrich_cache_hits_total", hits[0] if hits else 0,
                       component="enrich", labels={"decider": decider})  # fmt: skip
        result = wh.execute(_MISS_SQL.format(boot=boot_sql), params)
        while rows := result.fetchmany(chunk):
            yield [_input(*row) for row in rows]
    except duckdb.Error as exc:
        msg = f"build_inputs: {type(exc).__name__}"  # the class only: never row values
        raise SchemaViolation(msg) from None
    finally:
        wh.unregister(_HAVE)
        wh.unregister(_ASKED)


def _laya_questions(qs: QuestionSet, primaries: Mapping[str, str]) -> frozenset[str]:
    return frozenset(
        q.id for q in qs.questions if primaries.get(q.id) == "laya" and q.id not in PAIR_QUESTIONS
    )


def _count(outputs: Sequence[DecisionOutput], report: _Report) -> int:
    """Add answered and failed items to `report`; return the answered count."""
    failed = sum(1 for o in outputs if o.error is not None)
    report.decided += len(outputs) - failed
    report.failed += failed
    return len(outputs) - failed


def run_decide_primary(  # noqa: PLR0913 - U03-85's keyword-only signature is binding
    wh: duckdb.DuckDBPyConnection, *, laya: LayaDecider | None, qs: QuestionSet,
    primaries: Mapping[str, str], cache: DecisionCache, ctx: JobContext, report: _Report,
) -> None:  # fmt: skip
    """Stage `decide-primary`: Laya over cache misses of Laya-primary questions (U03-85).

    Caller inside `ctx.gpu_scope("decider")`. Skipped without Laya or Laya-primary questions;
    Laya `ConfigError`/`ModelUnavailable` -> `degraded` (§6); `FatalError` propagates."""
    stage = "decide-primary"
    asked = _laya_questions(qs, primaries)
    if laya is None or not asked:
        _mark(report, "skipped", "laya_degraded" if laya is None else "no_work")
        return
    call_batch = get_config().models.deciders.laya.call_batch
    flush_rows = CHECKPOINT_CALLS * call_batch * len(asked)
    writer = cache.writer(laya.name, laya.version, questions=qs, flush_rows=flush_rows)
    try:
        laya.load()
        inputs = build_inputs(wh, decider=laya.name, version=laya.version, qs=qs,
                              question_ids=asked, cache=cache, chunk=DECIDE_CHUNK)  # fmt: skip
        for chunk in inputs:
            outputs = laya.decide(chunk, qs)
            writer.add(outputs, samples=None)
            _count(outputs, report)
            _checkpoint(ctx, writer, stage)
        writer.flush()
    except (ConfigError, ModelUnavailable) as exc:
        writer.flush()
        _mark(report, "degraded", "laya_degraded")
        _log.warning("enrich.stage.degraded", stage=stage, error_class=type(exc).__name__)
    finally:
        laya.unload()
    _log.info("enrich.decide.primary_done", decided=report.decided, failed=report.failed)


def _as_input(item: QueueItem) -> DecisionInput:
    return _input(item.record_id, item.entity, item.content_hash, item.text, item.question_ids)


def _as_item(pair: DecisionInput) -> QueueItem:
    qids = pair.question_ids or tuple(sorted(PAIR_QUESTIONS))
    return QueueItem(pair.record_id, pair.entity, pair.content_hash, pair.text, qids)


def _split(queue: list[QueueItem], fresh: list[QueueItem]) -> tuple[list[QueueItem], ...]:
    """(to send, deferred now): questions the teacher has a row for skip the teacher."""
    new = {(i.record_id, i.entity): set(i.question_ids) for i in fresh}
    send: list[QueueItem] = []
    defer: list[QueueItem] = []
    for item in queue:
        ask = new.get((item.record_id, item.entity), set())
        mine = tuple(q for q in item.question_ids if q in ask)
        rest = tuple(q for q in item.question_ids if q not in ask)
        if mine:
            send.append(replace(item, question_ids=mine))
        if rest:
            defer.append(replace(item, question_ids=rest))
    return send, defer


def _merge(queue: list[QueueItem], parts: list[QueueItem]) -> list[QueueItem]:
    """Deferred record parts merged per record, in queue (priority) order."""
    rank = {(q.record_id, q.entity): n for n, q in enumerate(queue)}
    out: dict[int, QueueItem] = {}
    for part in parts:
        n = rank[(part.record_id, part.entity)]
        prev = out.get(n)
        qids = part.question_ids if prev is None else (*prev.question_ids, *part.question_ids)
        out[n] = replace(part, question_ids=tuple(sorted(set(qids))))
    return [out[n] for n in sorted(out)]


@dataclass(slots=True)
class _Sender:
    """Teacher chunks through a one-member `DeciderChain` (U03-86 steps 4-6)."""

    chain: DeciderChain
    name: str
    writer: CacheWriter
    ctx: JobContext
    report: _Report

    def run(self, inputs: list[DecisionInput], qs: QuestionSet, records: int) -> set[int]:
        """Send `inputs` (the first `records` are queue records); return deferred indices."""
        fresh = list(range(len(inputs)))
        retry: list[int] = []
        tried: set[int] = set()
        deferred: set[int] = set()
        while fresh or retry:
            batch, retry = retry, []  # retries of one chunk: never more than a chunk
            take = DECIDE_CHUNK - len(batch)
            batch, fresh = batch + fresh[:take], fresh[take:]
            error_class = None  # the chain logs which of ModelUnavailable/CircuitOpen it was
            try:
                decided, _left = self.chain.decide([inputs[i] for i in batch], qs)
            except (AuthError, EgressBlocked) as exc:
                self._stopped(exc)
                return deferred | set(batch) | set(retry) | set(fresh)
            except (RetryableError, RecoverableError) as exc:  # e.g. RateLimited past its cap
                decided, error_class = [], type(exc).__name__
            if decided:
                self.report.escalated += sum(1 for i in batch if i < records and i not in tried)
                self.writer.add(decided, samples=None)
                errors = [i for i, o in zip(batch, decided, strict=True) if o.error is not None]
                retry = [i for i in errors if i not in tried]
                deferred.update(set(errors) - set(retry))
                self.report.failed += len(errors) - len(retry)
                tried.update(retry)
            else:  # chunk lost: deferred to the LLM phase
                deferred.update(batch)
                _mark(self.report, "degraded", f"{self.name}_unavailable")
                _log.warning("enrich.decider.unavailable", decider=self.name,
                             error_class=error_class, deferred=len(batch))  # fmt: skip
            _checkpoint(self.ctx, self.writer, "decide-escalate")
        return deferred

    def _stopped(self, exc: AuthError | EgressBlocked) -> None:
        auth = isinstance(exc, AuthError)
        _mark(self.report, "degraded", f"{self.name}_{'auth' if auth else 'blocked'}")
        event = "enrich.decider.auth_failed" if auth else "enrich.decider.egress_blocked"
        _log.error(event, decider=self.name)


def run_decide_escalate(  # noqa: PLR0913 - U03-86's keyword-only signature is binding (+ gpu)
    wh: duckdb.DuckDBPyConnection, *, teacher: Decider | None, qs: QuestionSet,
    resolve_args: ResolveArgs, pairs: Sequence[DecisionInput], cache: DecisionCache,
    cfg: DecisionsConfig, ctx: JobContext, report: _Report, gpu: GpuStateReader | None = None,
) -> list[QueueItem]:  # fmt: skip
    """Stage `decide-escalate`: queue and change-link pairs to the teacher (U03-86).

    Returns the deferred items (queue records in priority order, then pairs); sends at most
    `escalation.max_rows_per_night` records and `change_link.decider_max_pairs` pairs. `gpu`
    defaults to `gpu_state()`. Only `FatalError` (and the yield) propagate."""
    a = resolve_args
    resolve_frame(wh, qs=qs, cfg=a.cfg, deciders=a.deciders, cache=cache, labels=a.labels,
                  calibration=a.calibration, primaries=a.primaries, versions=a.versions,
                  now=a.now)  # fmt: skip
    cap = cfg.escalation.max_rows_per_night
    queue = escalation_queue(wh, max_records=cap)
    if queue and len(queue) >= cap:
        _log.info("enrich.decide.escalation_capped", queued=len(queue), cap=cap)
    pair_cap = cfg.change_link.decider_max_pairs
    if len(pairs) > pair_cap:
        _log.info("enrich.decide.pairs_capped", pairs=len(pairs), cap=pair_cap)
    pair_items = [_as_item(p) for p in pairs[:pair_cap]]
    if teacher is None:
        _mark(report, "skipped", "teacher_unavailable")
        return queue + pair_items
    fresh = escalation_queue(wh, max_records=cap, exclude_deciders=frozenset({teacher.name}))
    send, parts = _split(queue, fresh)
    items = send + pair_items
    writer = cache.writer(teacher.name, teacher.version, questions=qs, flush_rows=WRITE_ROWS)
    chain = DeciderChain([teacher.name], gpu=gpu or gpu_state(), resolve=lambda _n: teacher)
    sender = _Sender(chain, teacher.name, writer, ctx, report)
    try:
        deferred = sorted(sender.run([_as_input(i) for i in items], qs, len(send)))
    finally:
        writer.flush()
    parts += [items[i] for i in deferred if i < len(send)]
    out = _merge(queue, parts) + [items[i] for i in deferred if i >= len(send)]
    _log.info("enrich.decide.escalated", sent=len(items), escalated=report.escalated,
              deferred=len(out), failed=report.failed)  # fmt: skip
    return out


def run_llm_escalation(  # noqa: PLR0913 - U03-87's keyword-only signature is binding
    deferred: Sequence[QueueItem], *, llm: LlmDecider | None, qs: QuestionSet,
    cache: DecisionCache, cap: int, ctx: JobContext, report: _Report,
) -> int:  # fmt: skip
    """Answer the first `cap` deferred items with the LLM decider; return records answered.

    Part of stage `reasoning` (U03-87): chunks of 500 in `deferred` order; an unavailable
    LLM or open breaker stops the phase and leaves the rest as cache misses."""
    if llm is None:
        return 0
    todo = list(deferred[: max(cap, 0)])
    writer = cache.writer(llm.name, llm.version, questions=qs, flush_rows=WRITE_ROWS)
    answered = 0
    try:
        for start in range(0, len(todo), LLM_CHUNK):
            chunk = todo[start : start + LLM_CHUNK]
            try:
                outputs = llm.decide([_as_input(item) for item in chunk], qs)
            except (ModelUnavailable, CircuitOpen) as exc:
                _mark(report, "degraded", "llm_unavailable")
                left = len(todo) - start
                _log.warning("enrich.decider.unavailable", decider=llm.name,
                             error_class=type(exc).__name__, deferred=left)  # fmt: skip
                break
            writer.add(outputs, samples=llm.samples)
            answered += _count(outputs, report)
            _checkpoint(ctx, writer, "reasoning")
    finally:
        writer.flush()
    _log.info("enrich.decide.llm_escalated", answered=answered, taken=len(todo))
    return answered
