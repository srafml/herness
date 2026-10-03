"""Procedural memory: SQL templates grown from verified findings (impl 07 U07-88 … U07-91).

Design 07 §5.11. Only SQL behind verified findings creates a template; failures only lower its
Wilson score (TH07-20). Templates are stored parameterised and are never executed by string
substitution: binding swaps placeholder nodes for sqlglot literal nodes, and the bound SQL
passes the spec 05 guard before a template is created and before every nightly EXPLAIN.
SQL parameterisation, binding and the guard checks live in the private `_procedural_sql`.
Logs carry ids, fingerprints, scores and reasons only, never SQL text or values.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial
from typing import Final, Literal, cast

import duckdb
from pydantic import JsonValue

from herness.core import time as clock
from herness.core.errors import ModelUnavailable, PolicyViolation
from herness.core.logging import get_logger
from herness.core.redact import Redactor
from herness.core.types import MemoryProposal, Provenance
from herness.harness.memory._procedural_sql import (
    ParameterizedSql,
    ParamSpec,
    bind_template,
    explain_ok,
    ints,
    metrics_used,
    parameterize_sql,
    strs,
    unsafe_reason,
)
from herness.harness.memory.policy import InjectionScanner, keyed_hash
from herness.harness.memory.settings import ProceduralConfig
from herness.harness.memory.store import VectorIndex
from herness.harness.memory.types import MemoryNotFound, PromotionReport
from herness.harness.memory.write import MemoryWriter
from herness.harness.sql_guard import SqlGuard
from herness.store import ops
from herness.store.ops import core

__all__ = [
    "ParamSpec", "ParameterizedSql", "ProceduralDeps", "bind_template", "parameterize_sql",
    "promote_procedural", "validate_templates", "wilson_lower_bound",
]  # fmt: skip

type Row = ops.MemoryItemRow
type Conn = sqlite3.Connection

TTL: Final = timedelta(days=365)
_LIVE: Final = ("candidate", "active")
_OPEN: Final = ("candidate", "pending_approval", "active")  # one open template per fingerprint
_ALL: Final = ("candidate", "pending_approval", "active", "expired", "rejected")
_QUESTION_MAX, _EXAMPLES_MAX, _RUNS_MAX, _RECENT, _QA_MAX, _PROV_IDS = 500, 10, 50, 3, 200, 20
_VALIDATION_LIMIT: Final = 2  # consecutive EXPLAIN failures that expire a template
_log = get_logger("memory")


@dataclass(frozen=True, slots=True)
class ProceduralDeps:
    """Collaborators of `promote_procedural` and `validate_templates` (T07-19 spec note)."""

    conn_factory: Callable[[], sqlite3.Connection]
    writer: MemoryWriter
    vectors: VectorIndex
    redactor: Redactor
    config: ProceduralConfig
    guard: SqlGuard
    scanner: InjectionScanner  # the write pipeline's scanner (same configured patterns)


def wilson_lower_bound(passes: int, fails: int, z: float = 1.959963984540054) -> float:
    """Wilson lower bound of the pass rate in [0, 1]; 0.0 without observations (U07-89)."""
    n = passes + fails
    if n == 0:
        return 0.0
    p, z2 = passes / n, z * z
    margin = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    return min(1.0, max(0.0, (p + z2 / (2 * n) - margin) / (1 + z2 / n)))


@dataclass(slots=True)
class _Group:
    """Observations of one fingerprint in one run (step 4)."""

    parsed: ParameterizedSql
    passes: int = 0
    fails: int = 0
    results: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    pairs: dict[tuple[str, str], str] = field(default_factory=dict)
    query_ids: list[str] = field(default_factory=list)
    build_id: str = ""


@dataclass(slots=True)
class _Outcome:
    """What one fingerprint transaction did; applied to the report after its commit."""

    created: bool = False
    updated: bool = False
    repeated: bool = False
    qa_created: int = 0
    new_ids: list[str] = field(default_factory=list)
    moved: dict[str, str] = field(default_factory=dict)  # memory_id -> new status
    events: list[tuple[str, str, str, float]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Ctx:
    run_id: str
    build_id: str | None
    deps: ProceduralDeps
    now: datetime


def _json(values: Sequence[str]) -> JsonValue:
    return cast("JsonValue", list(values))


def _question(task_id: str, deps: ProceduralDeps, cache: dict[str, str]) -> str:
    """Step 2: the task objective, redacted and cut to 500 chars ("" when absent)."""
    if task_id not in cache:
        spec = ops.task_spec(task_id, conn=deps.conn_factory()) or {}
        text = spec.get("objective")
        found = deps.redactor.redact(text) if isinstance(text, str) and text.strip() else None
        cache[task_id] = "" if found is None else found.text[:_QUESTION_MAX]
    return cache[task_id]


def _collect(run_id: str, deps: ProceduralDeps) -> tuple[dict[str, _Group], int, int]:
    """Steps 1-4: (groups by fingerprint, queries seen, unparsable)."""
    groups: dict[str, _Group] = {}
    seen = skipped = 0
    cache: dict[str, str] = {}
    conn = deps.conn_factory()
    for src in ops.run_findings_for_promotion(run_id, conn=conn):
        passed, question = src["status"] == "verified", _question(src["task_id"], deps, cache)
        evidence = ops.evidence_rows(src["query_ids"], conn=conn) if src["query_ids"] else {}
        for qid in (q for q in dict.fromkeys(src["query_ids"]) if q in evidence):
            seen += 1
            parsed = parameterize_sql(evidence[qid]["sql"])
            if parsed is None:
                skipped += 1
                continue
            g = groups.setdefault(parsed.fingerprint, _Group(parsed))
            g.passes, g.fails = g.passes + passed, g.fails + (not passed)
            g.results.append("pass" if passed else "fail")
            g.query_ids.append(qid)
            if question and question not in g.questions:
                g.questions.append(question)
            if passed:
                g.build_id = evidence[qid]["build_id"]
                if question:
                    g.pairs.setdefault((question, qid), evidence[qid]["sql"])
    return groups, seen, skipped


def _proposal(ctx: _Ctx, kind: Literal["sql_template", "qa_pair"], content: str,
              data: dict[str, JsonValue], query_ids: Sequence[str], confidence: float,
              ) -> MemoryProposal:  # fmt: skip
    """A system `promotion` proposal; templates carry the 365-day TTL."""
    prov = Provenance(
        author_type="system", author_role=None, author_ref=None, run_id=ctx.run_id,
        task_id=None, query_ids=list(dict.fromkeys(query_ids))[:_PROV_IDS], via="promotion",
    )  # fmt: skip
    expires = ctx.now + TTL if kind == "sql_template" else None
    return MemoryProposal(layer="procedural", kind=kind, content=content, data=data,
                          confidence=confidence, expires_at=expires, provenance=prov)  # fmt: skip


def _insert(ctx: _Ctx, c: Conn, prop: MemoryProposal, key: str) -> str:
    """One procedural item through the system write path (`insert_system_item`, T07-08)."""
    return ctx.deps.writer.insert_system_item(prop, key_hash=key, conn=c, now=ctx.now).memory_id


def _create(ctx: _Ctx, g: _Group, c: Conn, out: _Outcome) -> Row | None:
    """Step 5, absent template: create it as a candidate through the system write path."""
    p, fp = g.parsed, g.parsed.fingerprint
    key = keyed_hash(f"sql_template\n{fp}\n{ctx.run_id}")
    if ops.find_memory_item(content_hash=key, statuses=_ALL, conn=c) is not None:
        out.repeated = True  # this run created it before and it has expired since
        return None
    data: dict[str, JsonValue] = {
        "fingerprint": fp, "sql_template": p.template, "params": [s.as_json() for s in p.params],
        "question_examples": _json(g.questions[:_EXAMPLES_MAX]), "passes": g.passes,
        "fails": g.fails, "run_ids": _json([ctx.run_id]),
        "build_id_last_ok": ctx.build_id or g.build_id,
        "metrics_used": _json(metrics_used(p.template)), "recent": _json(g.results[-_RECENT:]),
        "validation_failures": 0, "qa_ids": [],
    }  # fmt: skip
    content = (g.questions or [f"SQL template {fp}"])[0]
    lb = wilson_lower_bound(g.passes, g.fails)
    memory_id = _insert(ctx, c, _proposal(ctx, "sql_template", content, data, g.query_ids, lb), key)
    out.created = True
    out.new_ids.append(memory_id)
    return ops.get_memory_items([memory_id], conn=c)[0]


def _merge(ctx: _Ctx, g: _Group, data: dict[str, JsonValue]) -> None:
    """Step 5, present template: add this run's counts, run, questions and results.

    A new question example passes the write pipeline's injection scan first (TH07-01);
    a flagged one is not added (it still reaches a qa_pair only via the write path)."""
    data["passes"] = ints(data, "passes") + g.passes
    data["fails"] = ints(data, "fails") + g.fails
    data["run_ids"] = _json([*strs(data, "run_ids"), ctx.run_id][-_RUNS_MAX:])
    examples = strs(data, "question_examples")
    fresh = [q for q in g.questions if q not in examples]
    examples += [q for q in fresh if not ctx.deps.scanner.scan(q)]
    data["question_examples"] = _json(examples[:_EXAMPLES_MAX])
    data["recent"] = _json([*strs(data, "recent"), *g.results][-_RECENT:])
    if g.passes:
        data["build_id_last_ok"] = ctx.build_id or g.build_id


def _qa_pairs(ctx: _Ctx, g: _Group, tpl: Row, c: Conn, out: _Outcome) -> list[str]:
    """Step 6: one qa_pair per new (question, query_id) of a passing query."""
    made = []
    for (question, qid), sql in g.pairs.items():
        key = keyed_hash(question + "\n" + qid)
        if ops.find_memory_item(content_hash=key, statuses=_ALL, conn=c) is not None:
            continue
        data: dict[str, JsonValue] = {"question": question, "sql": sql, "query_id": qid,
                                      "template_id": tpl["memory_id"]}  # fmt: skip
        prop = _proposal(ctx, "qa_pair", question, data, [qid], tpl["confidence"])
        made.append(_insert(ctx, c, prop, key))
    out.qa_created, out.new_ids = len(made), [*out.new_ids, *made]
    if tpl["status"] == "active":  # the policy stores candidates; follow the active template
        for memory_id in made:
            ops.update_memory_item(memory_id, status="active", conn=c)
    return made


def _move_qa(data: dict[str, JsonValue], status: str, c: Conn, reason: str = "") -> list[str]:
    """Move the template's live qa_pairs to `status` (recording an expiry reason)."""
    if reason:
        data["expired_reason"] = reason
    qas = ops.get_memory_items(strs(data, "qa_ids"), conn=c)
    ids = [q["memory_id"] for q in qas if q["status"] in _LIVE and q["status"] != status]
    for memory_id in ids:
        ops.update_memory_item(memory_id, status=status, conn=c)
    return ids


def _score(ctx: _Ctx, tpl: Row, data: dict[str, JsonValue], c: Conn,
           out: _Outcome) -> tuple[str, float]:  # fmt: skip
    """Steps 7-8: Wilson score, utility, promotion and demotion."""
    cfg, gate, status = ctx.deps.config, ctx.deps.config.promote, tpl["status"]
    passes = ints(data, "passes")
    lb = wilson_lower_bound(passes, ints(data, "fails"))
    data["utility"] = lb * math.log1p(tpl["use_count"])
    runs, recent = set(strs(data, "run_ids")), strs(data, "recent")[-_RECENT:]
    ready = passes >= gate.min_passes and len(runs) >= gate.min_runs and lb >= gate.min_pass_lb
    new, event = status, ""
    if status == "candidate" and ready and "fail" not in recent:
        new, event = "active", "memory.procedural.promoted"
        out.moved |= dict.fromkeys(_move_qa(data, "active", c), "active")
    elif status == "active" and lb < cfg.demote_pass_lb:
        new, event = "expired", "memory.procedural.expired"
        out.moved |= dict.fromkeys(_move_qa(data, "expired", c, "low_pass_lb"), "expired")
    if new != status:
        out.moved[tpl["memory_id"]] = new
        out.events.append((event, tpl["memory_id"], str(data.get("fingerprint")), lb))
    return new, lb


def _fingerprint_tx(ctx: _Ctx, g: _Group, c: Conn) -> _Outcome:
    """Steps 5-8 for one fingerprint inside one run_write."""
    out, fp = _Outcome(), g.parsed.fingerprint
    tpl = ops.find_memory_item(layer="procedural", kind="sql_template", fingerprint=fp,
                               statuses=_OPEN, conn=c)  # fmt: skip
    expires: str | ops.Unchanged = ops.UNCHANGED
    if tpl is None:
        if g.passes == 0 or (tpl := _create(ctx, g, c, out)) is None:
            return out  # failures alone create nothing (TH07-20)
    elif ctx.run_id in strs(tpl["data"], "run_ids"):
        out.repeated = True
        return out
    else:
        out.updated = True
        expires = clock.format_utc(ctx.now + TTL) if g.passes else expires
    data = dict(tpl["data"])
    if out.updated:
        _merge(ctx, g, data)
    data["qa_ids"] = _json([*strs(data, "qa_ids"), *_qa_pairs(ctx, g, tpl, c, out)][-_QA_MAX:])
    status, lb = _score(ctx, tpl, data, c, out)
    ops.update_memory_item(tpl["memory_id"], status=status, data=data, confidence=lb,
                           expires_at=expires, conn=c)  # fmt: skip
    return out


def _after_commit(deps: ProceduralDeps, new_ids: Sequence[str], moved: Mapping[str, str]) -> None:
    """Step 9: embed new items, then mirror status changes onto their vectors."""
    for memory_id in new_ids:
        deps.writer.embed_after_commit(memory_id)
    for status in sorted(set(moved.values())):
        ids = [m for m, s in moved.items() if s == status]
        try:
            deps.vectors.set_status(ids, status)  # type: ignore[arg-type]
        except ModelUnavailable:  # maintenance step 5 repairs the mirror
            _log.warning("memory.embedding.failed", op="set_status", count=len(ids))


def promote_procedural(
    run_id: str, *, deps: ProceduralDeps, now: datetime | None = None
) -> PromotionReport:
    """Grow procedural memory from one run's verified SQL (design 07 §5.11 steps 1-7)."""
    now = now or clock.now()
    run = ops.get_run(run_id)
    if run is None:
        raise MemoryNotFound(kind="run", ident=run_id)
    ctx = _Ctx(run_id, run.build_id, deps, now)
    groups, seen, skipped = _collect(run_id, deps)
    created = updated = qa = repeated = 0
    promoted: list[str] = []
    expired: list[str] = []
    for fp, g in groups.items():
        reason = unsafe_reason(g.parsed, deps.guard) if g.passes else None
        out = _Outcome()
        try:
            if reason is None:
                out = core.run_write(partial(_fingerprint_tx, ctx, g), op="memory_promote")
        except PolicyViolation as exc:
            reason = "policy:" + str(exc.details.get("rule", "unknown"))
        if reason:
            skipped += len(g.results) * (not reason.startswith("policy:"))
            _log.warning("memory.procedural.skipped", run_id=run_id, fingerprint=fp, reason=reason)
            continue
        created, updated, qa = created + out.created, updated + out.updated, qa + out.qa_created
        repeated += out.repeated
        for event, memory_id, fingerprint, lb in out.events:
            _log.info(event, memory_id=memory_id, fingerprint=fingerprint, pass_lb=round(lb, 4))
            (promoted if event.endswith("promoted") else expired).append(memory_id)
        _after_commit(deps, out.new_ids, out.moved)
    return PromotionReport(
        run_id=run_id, queries_seen=seen, skipped_unparsable=skipped, templates_created=created,
        templates_updated=updated, qa_pairs_created=qa, promoted=promoted, expired=expired,
        already_processed=bool(repeated) and not (created or updated),
    )  # fmt: skip


def _validation_tx(memory_id: str, ok: bool, c: Conn) -> dict[str, str]:
    """Record one result; the second consecutive failure expires the template and its qa."""
    rows = ops.get_memory_items([memory_id], conn=c)
    if not rows or rows[0]["status"] != "active":
        return {}
    data = dict(rows[0]["data"])
    fails = 0 if ok else ints(data, "validation_failures") + 1
    data["validation_failures"] = fails
    if fails < _VALIDATION_LIMIT:
        ops.update_memory_item(memory_id, data=data, conn=c)
        return {}
    ids = [memory_id, *_move_qa(data, "expired", c, "validation_failed")]
    ops.update_memory_item(memory_id, status="expired", data=data, conn=c)
    return dict.fromkeys(ids, "expired")


def validate_templates(
    *,
    con: duckdb.DuckDBPyConnection,
    deps: ProceduralDeps,
    timeout_s: float = 5.0,
    now: datetime | None = None,
) -> tuple[int, int]:
    """Nightly EXPLAIN of every active template on `con`: (passed, expired) (U07-91)."""
    stamp = clock.format_utc(now or clock.now())
    rows = ops.maintenance_rows(selector="templates", now=stamp, limit=10_000,
                                conn=deps.conn_factory())  # fmt: skip
    validated = expired = 0
    for row in cast(list[Row], rows):
        if row["status"] != "active":
            continue
        ok = explain_ok(row["data"], con, deps.guard, timeout_s)
        validated += ok
        moved = core.run_write(partial(_validation_tx, row["memory_id"], ok), op="memory_validate")
        if moved:
            expired += 1
            fp, lb = str(row["data"].get("fingerprint")), row["confidence"]
            _log.info("memory.procedural.expired", memory_id=row["memory_id"], fingerprint=fp,
                      pass_lb=round(lb, 4))  # fmt: skip
            _after_commit(deps, (), moved)
    return validated, expired
