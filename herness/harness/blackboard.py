"""Blackboard API over the ops ``finding`` table (impl 06 U06-51 … U06-59, design 06 §3.5, §6.5).

All writes run on one writer thread; reads use ``herness.store.ops`` on the caller's thread
(R-10). ``post`` commits a checked finding with the task checkpoint ``state`` key (R-21); status
changes are compare-and-set (TH06-10). Agent text is only a SQL parameter; errors name fields,
rules, ids and entity types, never claim text (TH06-05).
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from collections.abc import Callable, Collection, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime
from typing import Any, Final, NoReturn, Protocol, cast, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from herness.core import time as clock
from herness.core.errors import ConfigError, StoreBusy, ToolInputError
from herness.core.ids import IdKind, canonical_json, new_id
from herness.core.jobs.tasks import save_checkpoint
from herness.core.logging import get_logger
from herness.core.numbers import find_uncited
from herness.core.redact import get_redactor
from herness.core.resilience import fault_point
from herness.core.types import (
    Challenge,
    Finding,
    FindingStatus,
    NumberRef,
    RejectReason,
    Role,
    ScopeEntityType,
    SwarmTaskState,
    TaskSpec,
    ToolContext,
    VerificationRecord,
)
from herness.harness.findings import EntityCatalog, validate_markers
from herness.harness.tracing import Tracer
from herness.store.ops import (
    get_evidence,
    get_run,
    get_task,
    insert_finding,
    insert_tasks,
    list_task_findings,
    load_json,
    query_findings,
    run_write,
    transition_finding,
)

__all__ = ["Blackboard", "FindingFilter"]

WRITER_TIMEOUT_S: Final = 30.0
WRITE_METRIC: Final = "herness_harness_blackboard_write_seconds"
_ARGS: Final = {"claim", "entity_type", "entity_id", "numbers", "query_ids", "confidence"}
_FILTER_FIELDS: Final = {"entity_type", "entity_ids", "task_ids", "author_roles", "min_confidence"}
_ALL_STATUSES: Final[frozenset[FindingStatus]] = frozenset(get_args(FindingStatus.__value__))
_MARKER_HINT: Final = "use [[nX]] markers for every number and cite each NumberRef"
_QUERY_HINT: Final = "cite query_ids returned by tools in this task"
_META_SQL: Final = "SELECT query_id FROM meta.evidence WHERE query_id IN (SELECT unnest(?))"
_CHALLENGE_SQL: Final = "SELECT challenge FROM finding WHERE finding_id = ?"
_OPEN: Final[frozenset[FindingStatus]] = frozenset(("proposed", "challenged"))
type _Conn = sqlite3.Connection

_log = get_logger("harness.blackboard")


class _MetricSink(Protocol):
    """Stand-in for spec 08 ``MetricSink`` (T08-05 not built yet); histogram observations."""

    def record_histogram(self, name: str, value: float, *, component: str) -> None: ...


class FindingFilter(BaseModel):
    """Query filter for ``list_findings`` (U06-51, design 06 §3.5)."""

    model_config = ConfigDict(extra="forbid", strict=False)

    run_id: str
    status: set[FindingStatus] | None = None
    entity_type: ScopeEntityType | None = None
    entity_ids: set[str] | None = Field(default=None, max_length=50)
    task_ids: set[str] | None = Field(default=None, max_length=200)
    author_roles: set[Role] | None = None
    min_confidence: float | None = Field(default=None, ge=0, le=1)
    include_superseded: bool = False
    limit: int = Field(default=500, ge=1, le=500)

    def effective_statuses(self) -> frozenset[FindingStatus]:
        """``status`` when given; else all but ``revised``/``merged`` unless superseded."""
        if self.status is not None:
            return frozenset(self.status)
        return _ALL_STATUSES if self.include_superseded else _ALL_STATUSES - {"revised", "merged"}


def _content_key(f: Finding) -> str:
    """Identity of a post for the duplicate check (R-14 canonical JSON)."""
    numbers = [n.model_dump(mode="json") for n in f.numbers]
    return canonical_json([f.claim, f.entity_type, f.entity_id, numbers])


def _query_ids(args: Mapping[str, object]) -> list[object]:
    """Sorted ``query_ids`` plus every number's ``query_id``; non-strings kept for validation."""
    given, numbers = args.get("query_ids"), args.get("numbers")
    ids: list[object] = [*(given if isinstance(given, list | tuple) else ())]
    nums = numbers if isinstance(numbers, list | tuple) else ()
    ids += [n.query_id for n in nums if isinstance(n, NumberRef)]
    ids += [n.get("query_id") for n in nums if isinstance(n, Mapping)]
    strings = {q for q in ids if isinstance(q, str)}
    return [*sorted(strings), *(q for q in ids if not isinstance(q, str))]


def _transitioned(ok: bool, finding_id: str, to: str, reason: str | None = None) -> bool:
    if ok:
        _log.debug("harness.finding.transitioned", finding_id=finding_id, to=to, reason=reason)
    return ok


class Blackboard:
    """Blackboard API over ops ``finding`` with one writer thread per swarm process (U06-52)."""

    def __init__(  # noqa: PLR0913 - U06-52 signature
        self,
        run_id: str,
        *,
        build_id: str,
        catalog: EntityCatalog,
        allowed_numerals: Sequence[re.Pattern[str]],
        tracer: Tracer | None = None,
        writer: ThreadPoolExecutor | None = None,
        metrics: _MetricSink | None = None,
    ) -> None:
        self.run_id = run_id
        self.build_id = build_id
        self._catalog = catalog
        self._allowed = tuple(allowed_numerals)
        self._tracer = tracer
        self._metrics = metrics
        self._owns_writer = writer is None
        self._writer = writer or ThreadPoolExecutor(1, "herness-bb-writer")

    def close(self) -> None:
        """Shut down the writer when this Blackboard created it."""
        if self._owns_writer:
            self._writer.shutdown(wait=True)

    def _observe(self, started: float) -> None:
        if self._metrics is not None:  # T08-05: herness_harness_blackboard_write_seconds
            elapsed = clock.monotonic() - started
            self._metrics.record_histogram(WRITE_METRIC, elapsed, component="harness")

    async def run_on_writer[T](self, fn: Callable[[], T]) -> T:
        """Run ``fn`` on the writer thread and await its result."""
        started = clock.monotonic()
        try:
            return await asyncio.get_running_loop().run_in_executor(self._writer, fn)
        finally:
            self._observe(started)

    def run_on_writer_sync[T](self, fn: Callable[[], T], *, timeout_s: float = 30.0) -> T:
        """Run ``fn`` on the writer thread; a writer busy past ``timeout_s`` → StoreBusy."""
        return self._sync(fn, timeout_s, "blackboard writer timeout")

    def _sync[T](self, fn: Callable[[], T], timeout_s: float, busy: str) -> T:
        started = clock.monotonic()
        try:
            future = self._writer.submit(fn)
            if not wait([future], timeout=timeout_s).done:
                future.cancel()
                raise StoreBusy(busy)
            return future.result()  # errors of fn (StoreBusy included) propagate unchanged
        finally:
            self._observe(started)

    def _tx[T](self, fn: Callable[[sqlite3.Connection], T], op: str) -> Callable[[], T]:
        return lambda: run_write(fn, op=op)

    def _reject(self, ctx: ToolContext, rule: str, msg: str, hint: str | None = None) -> NoReturn:
        _log.info(
            "harness.finding.post_rejected", run_id=ctx.run_id, task_id=ctx.task_id, rule=rule
        )
        raise ToolInputError(msg, hint=hint)

    def _build(self, ctx: ToolContext, args: Mapping[str, object]) -> Finding:
        """Step 1: the Finding; argument errors name field paths only."""
        if set(args) - _ARGS:
            self._reject(ctx, "arguments", "unexpected post_finding arguments")
        fixed: dict[str, object] = {"finding_id": new_id(IdKind.FINDING), "run_id": ctx.run_id}
        fixed |= {"task_id": ctx.task_id, "author_role": ctx.role, "created_at": clock.now()}
        fixed["query_ids"] = _query_ids(args)
        try:
            return Finding.model_validate({**args, **fixed})
        except ValidationError as exc:
            paths = sorted({".".join(str(p) for p in e["loc"]) or "input" for e in exc.errors()})
            self._reject(ctx, "schema", f"invalid post_finding arguments: {', '.join(paths)}")

    def _unknown_queries(self, ctx: ToolContext, ids: Sequence[str]) -> list[str]:
        """Step 4: ids neither in ops evidence of this build nor in ``meta.evidence``."""
        rest = [q for q in ids if (e := get_evidence(q)) is None or e.build_id != self.build_id]
        if rest:
            cursor: Any = ctx.warehouse.cursor()
            found = {str(row[0]) for row in cursor.execute(_META_SQL, [rest]).fetchall()}
            rest = [qid for qid in rest if qid not in found]
        return rest

    def _validate(self, ctx: ToolContext, f: Finding) -> None:
        """Steps 6, 2, 3, 4, 5: PII runs before any claim text reaches a message (ENG §3.4)."""
        if spans := get_redactor().scan(f.claim):
            types = ", ".join(sorted({span.type for span in spans}))
            self._reject(ctx, "pii", f"claim contains personal data ({types})")
        if errors := validate_markers(f.claim, f.numbers):
            self._reject(ctx, "markers", "; ".join(errors), _MARKER_HINT)
        if hits := find_uncited(f.claim, self._allowed):
            tokens = ", ".join(hit.text for hit in hits)
            self._reject(ctx, "numerals", f"numerals outside markers: {tokens}")
        if missing := self._unknown_queries(ctx, f.query_ids):
            self._reject(ctx, "evidence", f"unknown query_id {missing[0]}", _QUERY_HINT)
        if self._catalog.missing(f.entity_type, [f.entity_id]):
            self._reject(ctx, "entity", f"unknown {f.entity_type} {f.entity_id}")

    def _commit(self, ctx: ToolContext, f: Finding) -> tuple[str, bool]:
        """Step 7 on the writer: duplicate check, revision rule, finding + checkpoint in one tx."""
        task = get_task(ctx.task_id)
        if task is None or task.run_id != self.run_id:
            self._reject(ctx, "task", "task not found")
        existing = list_task_findings(ctx.task_id)
        dup = next((p.finding_id for p in existing if _content_key(p) == _content_key(f)), None)
        if dup is not None:
            return dup, False
        revision_of = task.spec.revision_of
        if revision_of is not None and existing:
            self._reject(ctx, "revision", "revision tasks post exactly one finding")
        state = SwarmTaskState.from_envelope(task.checkpoint or {}, task_id=ctx.task_id)
        if state is None:
            run = get_run(self.run_id)
            state = SwarmTaskState(phase=run.status if run is not None else "running")
        data = state.model_dump(mode="json")
        data["pending_findings"] = [*state.pending_findings, f.finding_id]
        value = SwarmTaskState.model_validate(data).model_dump(mode="json")

        def writes(conn: _Conn) -> None:
            if revision_of is None:
                insert_finding(conn, f)
            else:
                Blackboard.tx_supersede(conn, revision_of, f)

        save_checkpoint(ctx.task_id, "state", value, writes=writes)
        return f.finding_id, True

    def post(self, ctx: ToolContext, **args: object) -> str:
        """Validate and commit one finding with the task checkpoint; return its id (U06-53)."""
        if ctx.run_id != self.run_id or ctx.role != "analyst":
            self._reject(ctx, "role", "post_finding is only available to analyst tasks")
        finding = self._build(ctx, args)
        self._validate(ctx, finding)
        busy = f"blackboard writer timeout: task_id={ctx.task_id}"
        fid, written = self._sync(lambda: self._commit(ctx, finding), WRITER_TIMEOUT_S, busy)
        if written:
            fault_point("swarm.after_finding_write", role="analyst")
            _log.debug(
                "harness.finding.posted", run_id=self.run_id, task_id=ctx.task_id, finding_id=fid
            )
        return fid

    async def list_findings(self, flt: FindingFilter) -> list[Finding]:
        """Committed findings of this run matching ``flt`` (U06-54)."""
        if flt.run_id != self.run_id:
            msg = "run_id mismatch"
            raise ToolInputError(msg)
        filters = flt.model_dump(include=_FILTER_FIELDS) | {"statuses": flt.effective_statuses()}
        return await asyncio.to_thread(query_findings, self.run_id, limit=flt.limit, **filters)

    @staticmethod
    def tx_challenge(
        conn: _Conn, finding_id: str, ch: Challenge, revision: TaskSpec | None, now: datetime
    ) -> bool:
        """``uphold`` keeps ``proposed``; ``revise`` → ``challenged`` plus the revision task."""
        if ch.verdict == "reject" or (revision is not None) != (ch.verdict == "revise"):
            msg = "challenge needs uphold without a revision task or revise with one"
            raise ConfigError(msg)
        to: FindingStatus = "proposed" if ch.verdict == "uphold" else "challenged"
        ok = transition_finding(conn, finding_id, to, {"proposed"}, append_challenge=ch)
        if ok and revision is not None:
            insert_tasks(conn, [revision], now=now)
        return ok

    async def challenge(
        self, finding_id: str, ch: Challenge, *, revision: TaskSpec | None = None
    ) -> bool:
        """Append a Skeptic verdict; ``revise`` inserts its revision task in the same tx."""
        now = clock.now()
        fn = self._tx(lambda c: self.tx_challenge(c, finding_id, ch, revision, now), "bb_challenge")
        to = "proposed" if ch.verdict == "uphold" else "challenged"
        return _transitioned(await self.run_on_writer(fn), finding_id, to, ch.verdict)

    @staticmethod
    def tx_mark_verified(conn: _Conn, finding_id: str, v: VerificationRecord) -> bool:
        """``proposed`` → ``verified`` with the gate 1 record."""
        return transition_finding(conn, finding_id, "verified", {"proposed"}, verification=v)

    @staticmethod
    def tx_reject(
        conn: _Conn,
        finding_id: str,
        reason: RejectReason,
        v: VerificationRecord | None,
        append: Challenge | None,
    ) -> bool:
        """``proposed``/``challenged`` → ``rejected``; ``v`` gets ``reason`` when given."""
        record = None if v is None else v.model_copy(update={"reason": reason})
        return transition_finding(
            conn, finding_id, "rejected", _OPEN, verification=record, append_challenge=append
        )

    async def mark_verified(self, finding_id: str, v: VerificationRecord) -> bool:
        """Gate 1 pass (U06-56)."""
        fn = self._tx(lambda c: Blackboard.tx_mark_verified(c, finding_id, v), "bb_verify")
        return _transitioned(await self.run_on_writer(fn), finding_id, "verified")

    async def reject(
        self,
        finding_id: str,
        reason: RejectReason,
        v: VerificationRecord | None = None,
        *,
        append: Challenge | None = None,
    ) -> bool:
        """Reject a finding (U06-56); without ``v`` the reason is carried by the log event."""
        fn = self._tx(lambda c: Blackboard.tx_reject(c, finding_id, reason, v, append), "bb_reject")
        return _transitioned(await self.run_on_writer(fn), finding_id, "rejected", reason)

    @staticmethod
    def tx_supersede(conn: _Conn, old_id: str, new: Finding) -> str:
        """``challenged`` old → ``revised``; insert ``new`` superseding it with its history."""
        row = conn.execute(_CHALLENGE_SQL, (old_id,)).fetchone()
        if row is None:
            msg = f"finding {old_id} not found"
            raise ToolInputError(msg)
        if not transition_finding(conn, old_id, "revised", {"challenged"}):
            msg = f"finding {old_id} is not open for revision"
            raise ToolInputError(msg)
        stored = load_json(row[0], field="finding.challenge") or []
        history = [Challenge.model_validate(c) for c in cast("list[object]", stored)]
        update = {"supersedes": old_id, "status": "proposed", "challenge": history}
        insert_finding(conn, new.model_copy(update=update))
        return new.finding_id

    async def supersede(self, old_id: str, new: Finding) -> str:
        """Commit a revision (U06-57)."""
        fn = self._tx(lambda c: Blackboard.tx_supersede(c, old_id, new), "bb_supersede")
        result = await self.run_on_writer(fn)
        _transitioned(True, old_id, "revised")
        return result

    @staticmethod
    def tx_merge(conn: _Conn, keep_id: str, dup_ids: Collection[str]) -> list[str]:
        """Merge ``proposed`` dups into ``keep_id`` in sorted order; others are skipped."""
        if keep_id in dup_ids:
            msg = "keep_id must not be among dup_ids"
            raise ConfigError(msg)
        merged: list[str] = []
        for dup in sorted(set(dup_ids)):
            if transition_finding(conn, dup, "merged", {"proposed"}, merged_into=keep_id):
                merged.append(dup)
            else:
                _log.debug("harness.finding.merge_skipped", finding_id=dup, keep_id=keep_id)
        return merged

    async def merge(self, keep_id: str, dup_ids: list[str]) -> None:
        """Dedup merge (U06-58)."""
        fn = self._tx(lambda c: Blackboard.tx_merge(c, keep_id, dup_ids), "bb_merge")
        for dup in await self.run_on_writer(fn):
            _transitioned(True, dup, "merged")
