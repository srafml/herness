"""Prior-run context and human decisions on recommendations (impl 07 U07-81, U07-82).

Design 07 §5.9: one escaped, budgeted `<untrusted_data>` block (TH07-07, R-20); decisions keep
`decided_by` (TH07-12). Logs carry ids and decisions only, never reason or summary text.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import UTC, date, datetime, time, timedelta
from functools import partial
from typing import Final, Literal, cast, get_args

from pydantic import JsonValue, ValidationError

from herness.core import time as clock
from herness.core.errors import HernessError, ToolInputError
from herness.core.jobs import queue as job_queue
from herness.core.logging import get_logger
from herness.core.redact import Redactor
from herness.core.types import (
    MemoryProposal,
    MemoryRunContext,
    NumberRef,
    PriorContext,
    PriorRecommendation,
    Provenance,
    SystemBlock,
)
from herness.harness.llm.tokens import estimate_tokens
from herness.harness.memory import render
from herness.harness.memory.outcome_stats import MetricWeeks, measurement_windows
from herness.harness.memory.policy import find_uncited_numerals, keyed_hash
from herness.harness.memory.settings import EpisodicConfig, OutcomeConfig
from herness.harness.memory.types import REC_ID_RE, MemoryNotFound
from herness.harness.memory.write import MemoryWriter
from herness.store import ops

__all__ = ["EpisodicDeps", "decide", "prior_context"]

Decision = Literal["accepted", "rejected", "deferred"]
MAX_PRIOR_RECS: Final = 100
REASON_MAX: Final = 1_000
_USER_REF_RE: Final = re.compile(r"^[0-9a-f]{32}$")
_LABEL_MAX: Final = 150  # one redacted target label in the decision note
_MEASURE_AT: Final = time(6, tzinfo=UTC)  # outcome jobs run at 06:00 UTC on the due date
_TALLY: Final = ("accepted", "paid_off", "no_effect", "worse", "inconclusive", "pending")
_log = get_logger("memory")


@dataclass(frozen=True, slots=True)
class EpisodicDeps:
    """Collaborators of `prior_context` and `decide` (T07-16 spec note: not named in impl 07)."""

    conn_factory: Callable[[], sqlite3.Connection]
    writer: MemoryWriter
    redactor: Redactor
    allowed: Sequence[re.Pattern[str]]  # the writer's allowed numeral patterns
    episodic: EpisodicConfig
    outcome: OutcomeConfig


def _est(text: str) -> int:  # spec 05's one estimate (R-17)
    return estimate_tokens((), (), [SystemBlock(text=text)])


def _weeks(cfg: OutcomeConfig, metric: str) -> MetricWeeks:
    """The four week counts of `metric`: `cfg.outcome` with its `per_metric` override."""
    over = cast("Mapping[str, int]", cfg.per_metric.get(metric, {}))
    return MetricWeeks(*(over.get(f.name, getattr(cfg, f.name)) for f in fields(MetricWeeks)))


def _due(cfg: OutcomeConfig, metric: str, effective: date, m: int) -> date:
    return measurement_windows(effective, cast("Literal[1, 2]", m), _weeks(cfg, metric)).due


def _refs(raw: Sequence[JsonValue]) -> list[NumberRef]:
    out: list[NumberRef] = []
    for entry in raw:
        try:
            out.append(NumberRef.model_validate(entry))
        except ValidationError:
            continue  # a malformed stored ref is shown as a bare marker
    return out


def _outcome(row: ops.OutcomeRow | None) -> dict[str, JsonValue] | None:
    if row is None:
        return None
    rel = row["details"].get("rel")
    keys = ("outcome_id", "measurement", "verdict", "baseline", "actual", "delta", "query_id")
    num = isinstance(rel, int | float) and not isinstance(rel, bool)
    return {**{k: row[k] for k in keys}, "rel": rel if num else None}  # type: ignore[literal-required]


def _next_due(rec: ops.RecommendationRow, dec: ops.DecisionRow | None,
              out: ops.OutcomeRow | None, deps: EpisodicDeps) -> date | None:  # fmt: skip
    """Due date of the first unmeasured measurement of an accepted rec with a metric (U07-83)."""
    metric = rec["expected_metric"]
    if dec is None or dec["decision"] != "accepted" or metric is None:
        return None
    eff = clock.parse_utc(dec["effective_at"]).date()
    measured = 0 if out is None else out["measurement"]
    if measured > 1 and not ops.outcome_exists(rec["rec_id"], 1, conn=deps.conn_factory()):
        measured = 0  # a second measurement without a first: the first is still due
    return None if measured > 1 else _due(deps.outcome, metric, eff, 1 if measured == 0 else 2)


def _item(rec: ops.RecommendationRow, dec: ops.DecisionRow | None, out: ops.OutcomeRow | None,
          deps: EpisodicDeps) -> PriorRecommendation:  # fmt: skip
    return PriorRecommendation(
        rec_id=rec["rec_id"], run_id=rec["run_id"], kind=rec["kind"],
        target_type=rec["target_type"], target_id=rec["target_id"], summary=rec["summary"],
        numbers=_refs(rec["numbers"]), expected_metric=rec["expected_metric"],
        confidence=rec["confidence"],
        decision=None if dec is None else dec["decision"],  # type: ignore[arg-type]
        decided_at=None if dec is None else clock.parse_utc(dec["decided_at"]),
        effective_at=None if dec is None else clock.parse_utc(dec["effective_at"]),
        outcome=_outcome(out), next_measurement_due=_next_due(rec, dec, out, deps),
    )  # fmt: skip


def _group(item: PriorRecommendation) -> int:
    """Step 5 order: accepted worse/no_effect, other measured, accepted pending, the rest."""
    if item.decision != "accepted":
        return 3
    if item.outcome is None:
        return 2
    return 0 if item.outcome["verdict"] in ("worse", "no_effect") else 1


def _tally(items: Sequence[PriorRecommendation]) -> dict[str, int]:
    counts = dict.fromkeys(_TALLY, 0)
    for item in items:
        if item.outcome is not None and isinstance(verdict := item.outcome["verdict"], str):
            counts[verdict] = counts.get(verdict, 0) + 1
        if item.decision == "accepted":
            counts["accepted"] += 1
            counts["pending"] += item.outcome is None
    return counts


def _record(item: PriorRecommendation) -> str:
    out = item.outcome or {}
    rel = out.get("rel")
    attrs: list[tuple[str, object]] = [
        ("id", item.rec_id), ("kind", "recommendation"), ("rec_kind", item.kind),
        ("target", f"{item.target_type}:{item.target_id}"), ("decision", item.decision),
        ("effective_at", item.effective_at and item.effective_at.date().isoformat()),
        ("verdict", out.get("verdict")),
        ("rel", None if rel is None else format(rel, ".3f")),
        ("outcome_query_id", out.get("query_id")),
        ("next_due", item.next_measurement_due and item.next_measurement_due.isoformat()),
    ]  # fmt: skip
    opening = "".join(f' {k}="{render.escape_attr(str(v))}"' for k, v in attrs if v is not None)
    body = render.escape_content(render.render_marker_values(item.summary, item.numbers))
    return f"<record{opening}>{body}</record>"


def _render(tally: dict[str, int], ordered: Sequence[PriorRecommendation],
            max_tokens: int) -> tuple[str, list[str]]:  # fmt: skip
    """Step 6: the block and its rendered rec ids; records drop from the end, then the tally."""
    counts = ", ".join(f"{k} {tally[k]}" for k in _TALLY)
    lines = [f'<record id="tally" kind="prior_tally">{counts}</record>', *map(_record, ordered)]
    wrap, note = partial(render.wrap_untrusted, "memory", None), render.CONTEXT_NOTE
    while _est(rendered := wrap("\n".join([note, *lines]))) > max_tokens and lines:
        lines.pop()  # from the end of the order; the tally last (T07-16 spec note)
    return rendered, [i.rec_id for i in ordered[: max(len(lines) - 1, 0)]]


def _recommendations(run_ctx: MemoryRunContext, deps: EpisodicDeps,
                     now: datetime) -> list[ops.RecommendationRow]:  # fmt: skip
    """Steps 1-2: recent runs' recs plus recently accepted ones, newest first, at most 100."""
    conn, cfg = deps.conn_factory(), deps.episodic
    runs = ops.recent_runs_with_recommendations(
        run_ctx.run_kind, limit=cfg.prior_runs, exclude_run_id=run_ctx.run_id, conn=conn
    )
    since = clock.format_utc(now - timedelta(days=cfg.prior_accepted_lookback_days))
    rows = [r for run in runs for r in ops.run_recommendations(run, conn=conn)]
    unique = {r["rec_id"]: r for r in [*rows, *ops.accepted_since(since, conn=conn)]}
    newest = sorted(unique.values(), key=lambda r: (r["created_at"], r["rec_id"]), reverse=True)
    return newest[:MAX_PRIOR_RECS]


def prior_context(
    run_ctx: MemoryRunContext, max_tokens: int = 3000, *, deps: EpisodicDeps,
    now: datetime | None = None,
) -> PriorContext:  # fmt: skip
    """Prior recommendations, decisions and outcomes for run start (design 07 §5.9)."""
    if max_tokens < render.MIN_MAX_TOKENS:
        msg = "max_tokens too small"
        raise ToolInputError(msg)
    recs = _recommendations(run_ctx, deps, now or clock.now())
    rec_ids = [r["rec_id"] for r in recs]
    conn = deps.conn_factory()
    decisions = ops.latest_decisions(rec_ids, conn=conn)
    outcomes = ops.latest_outcomes(rec_ids, conn=conn)
    items = [_item(r, decisions.get(r["rec_id"]), outcomes.get(r["rec_id"]), deps) for r in recs]
    tally = _tally(items)
    ordered = sorted(items, key=_group)  # stable: newest created_at first within each group
    rendered, shown = _render(tally, ordered, max_tokens)
    memory_ids = ops.rec_memory_ids(shown, kinds=["outcome_summary", "decision_note"], conn=conn)
    return PriorContext(items=ordered, rendered=rendered, memory_ids=memory_ids, tally=tally)


def _check(rec_id: str, decision: str, reason: str, user_ref: str,
           effective_at: datetime | None) -> str:  # fmt: skip
    """Preconditions of U07-82 (ToolInputError naming the argument); the stripped reason."""
    stripped = reason.strip()
    checks = (
        ("rec_id", REC_ID_RE.fullmatch(rec_id) is not None),
        ("decision", decision in get_args(Decision)),
        ("reason", 1 <= len(stripped) <= REASON_MAX),
        ("user_ref", _USER_REF_RE.fullmatch(user_ref) is not None),
        ("effective_at", effective_at is None or effective_at.utcoffset() is not None),
    )
    for name, ok in checks:
        if not ok:
            msg = f"decide: invalid {name}"
            raise ToolInputError(msg)
    return stripped


def _redact(redactor: Redactor, text: str) -> str:
    found = redactor.redact(text)
    return text if found is None else found.text


def _note_text(rec_id: str, rec: ops.UiRecommendationRow, decision: str, eff: date,
               deps: EpisodicDeps) -> str:  # fmt: skip
    """The decision_note sentence; a target label or date that holds an uncited numeral is
    left out so the system-text numeral rule cannot reject the decision (T07-16 spec note)."""
    label = _redact(deps.redactor, f"{rec['target_type']}:{rec['target_id']}")[:_LABEL_MAX]
    day = eff.isoformat()
    bad = [find_uncited_numerals(text, deps.allowed) for text in (label, day)]
    target = f"{rec['target_type']} target" if bad[0] else label
    effect = "" if bad[1] else f" with effect from {day}"
    return f"Recommendation {rec_id} ({rec['kind']} for {target}) was {decision}{effect}."


def _enqueue(rec_id: str, metric: str, eff: date, cfg: OutcomeConfig) -> None:
    """Step 5: the two measurement jobs; a failure is logged (the weekly sweep measures)."""
    for m in (1, 2):
        when = datetime.combine(_due(cfg, metric, eff, m), _MEASURE_AT)
        try:
            job_queue.enqueue("outcome_measure", {"rec_id": rec_id, "measurement": m}, "none", None,
                         when, idem_key=f"outcome:{rec_id}:{m}:{eff.isoformat()}")  # fmt: skip
        except HernessError as exc:
            _log.warning("memory.decision.enqueue_failed", rec_id=rec_id, measurement=m,
                         error=type(exc).__name__)  # fmt: skip


def decide(  # noqa: PLR0913 - signature fixed by U07-82
    rec_id: str, decision: Decision, reason: str, user_ref: str,
    effective_at: datetime | None = None, *, deps: EpisodicDeps, now: datetime | None = None,
) -> None:  # fmt: skip
    """Record a human decision on a recommendation (design 07 §5.9 "Decisions"; TH07-12)."""
    stripped = _check(rec_id, decision, reason, user_ref, effective_at)
    rec = ops.ui_get_recommendation(rec_id)
    if rec is None:
        raise MemoryNotFound("recommendation", rec_id)  # noqa: EM101 - a kind literal
    decided_at = (now or clock.now()).astimezone(UTC)
    eff = (effective_at or decided_at).astimezone(UTC)
    decided_text = clock.format_utc(decided_at)
    row = ops.DecisionRow(
        rec_id=rec_id, decision=decision,
        reason=_redact(deps.redactor, stripped)[:REASON_MAX], decided_by=user_ref,
        decided_at=decided_text, effective_at=clock.format_utc(eff),
    )  # fmt: skip
    prov = Provenance(author_type="human", author_role=None, author_ref=user_ref, run_id=None,
                      task_id=None, via="dashboard")  # fmt: skip
    note = MemoryProposal(
        layer="episodic", kind="decision_note", confidence=1.0, provenance=prov,
        content=_note_text(rec_id, rec, decision, eff.date(), deps),
        data={"rec_id": rec_id, "decision": decision},
    )  # fmt: skip
    key = keyed_hash("decision_note:" + rec_id + ":" + decided_text)

    def tx(conn: sqlite3.Connection) -> str:
        ops.insert_decision(row, conn=conn)
        stored = deps.writer.insert_system_item(note, key_hash=key, conn=conn, now=decided_at)
        return stored.memory_id

    memory_id = ops.run_write(tx, op="decide")
    deps.writer.embed_after_commit(memory_id)
    metric = rec["expected_metric"]
    if decision == "accepted" and metric is not None:
        _enqueue(rec_id, metric, eff.date(), deps.outcome)
    _log.info("memory.decision.recorded", rec_id=rec_id, decision=decision)
