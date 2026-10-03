"""Run-end recommendations and outcome feedback into confidence (impl 07 U07-78 … U07-80).

Design 07 §5.9 "Run end" and §5.10. `write_recommendations` validates every draft and
adjusts its confidence outside the write transaction (adjusting embeds), then inserts the
rows and the run's `run_summary` item in one `run_write`; a resume with the same targets
returns the same ids. Only `recommendation.confidence` is adjusted (TH07-04); summaries
cite numbers through markers only (TH07-03). Logs carry ids and counts, never text.
"""

from __future__ import annotations

import math
import re
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from statistics import fmean
from typing import Final

import numpy as np
from pydantic import JsonValue

from herness.core import numbers as core_numbers
from herness.core import time as clock
from herness.core.errors import ModelUnavailable, ReportContractError
from herness.core.ids import new_ulid
from herness.core.logging import get_logger
from herness.core.redact import Redactor
from herness.core.types import (
    ConfidenceAdjustment,
    MemoryProposal,
    Provenance,
    RecommendationDraft,
    SimilarOutcome,
)
from herness.harness.memory.policy import check_markers, find_uncited_numerals, keyed_hash
from herness.harness.memory.settings import FeedbackConfig
from herness.harness.memory.types import MemoryNotFound
from herness.harness.memory.write import MemoryWriter
from herness.store import ops

__all__ = ["RecommendDeps", "SimInput", "outcome_adjustment", "recommendation_similarity",
           "write_recommendations"]  # fmt: skip

MAX_RECS: Final = 50
_TOP: Final = 10  # targets in the run_summary text; finding ids in its data
_QUESTION_MAX: Final = 500
_SIMILAR_MAX: Final = 20
_LOOKUP_CHUNK: Final = 500  # the ops id-list limit (C2)
_CENT: Final = Decimal("0.01")
_DELTA_TYPE: Final = (-0.25, 0.15)  # ConfidenceAdjustment field bounds
_CONF_TYPE: Final = (0.05, 0.95)
_HALF_LIFE_DAYS: Final = 365.0
_DAY_S: Final = 86_400.0
_VALUE: Final = {"paid_off": 1.0, "no_effect": -0.5, "worse": -1.0, "inconclusive": 0.0}
_WS: Final = re.compile(r"\s+")
_log = get_logger("memory")


@dataclass(frozen=True, slots=True)
class SimInput:
    """The fields of a recommendation that `recommendation_similarity` compares (U07-79)."""

    kind: str
    expected_metric: str | None
    target_type: str
    target_id: str


@dataclass(frozen=True, slots=True)
class RecommendDeps:
    """Collaborators of `write_recommendations` (U07-78); `adjust` is U07-80 bound to priors."""

    conn_factory: Callable[[], sqlite3.Connection]
    writer: MemoryWriter
    adjust: Callable[[RecommendationDraft, float], ConfidenceAdjustment]
    redactor: Redactor
    allowed: Sequence[re.Pattern[str]]


def _clamp(value: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, value))


# ---------------------------------------------------------------- U07-79


def recommendation_similarity(
    r: SimInput, p: SimInput, *, s_text: float, related: bool
) -> tuple[float, float, float, float]:
    """(`s_kind`, `s_target`, `s_text`, `sim`) of design 07 §5.10; `s_text` clamped to [0, 1]."""
    s_kind = 1.0 if (r.kind, r.expected_metric) == (p.kind, p.expected_metric) else 0.0
    if r.target_id == p.target_id:
        s_target = 1.0
    elif related:
        s_target = 0.5
    else:
        s_target = 0.2 if r.target_type == p.target_type else 0.0
    text = _clamp(s_text, 0.0, 1.0) if math.isfinite(s_text) else 0.0
    return s_kind, s_target, text, 0.4 * s_kind + 0.35 * s_target + 0.25 * text


# ---------------------------------------------------------------- U07-80


def _strip(text: str) -> str:
    """`text` with every marker removed and whitespace collapsed."""
    return _WS.sub(" ", core_numbers.ANY_MARKER_RE.sub(" ", text)).strip()


def _text_sims(
    summary: str, others: Sequence[str], embed: Callable[[str], np.ndarray]
) -> list[float]:
    """Dot products of the stripped summaries; all 0 (logged) when the embedder is down."""
    if not others:
        return []
    try:
        mine = embed(_strip(summary))
        return [float(np.dot(mine, embed(_strip(text)))) for text in others]
    except ModelUnavailable:
        _log.warning("memory.feedback.degraded", priors=len(others))
        return [0.0] * len(others)


def _decay(measured_at: str, now: datetime) -> float:
    """exp(-ln 2 * age_days / 365); an outcome dated after `now` counts as age 0."""
    age = max(0.0, (now - clock.parse_utc(measured_at)).total_seconds() / _DAY_S)
    return math.exp(-math.log(2) * age / _HALF_LIFE_DAYS)


def outcome_adjustment(  # noqa: PLR0913 - signature fixed by U07-80
    draft: RecommendationDraft, base: float, *, priors: Sequence[ops.SimilarityRow],
    embed: Callable[[str], np.ndarray], related: Callable[[str, str], bool],
    cfg: FeedbackConfig, now: datetime,
) -> ConfidenceAdjustment:  # fmt: skip
    """Confidence feedback from similar measured priors (design 07 §5.10); never raises."""
    mine = SimInput(draft.kind, draft.expected_metric, draft.target_type, draft.target_id)
    sims = [(p, SimInput(p["kind"], p["expected_metric"], p["target_type"], p["target_id"]))
            for p in priors if p["verdict"] in _VALUE]  # fmt: skip
    cands = [(p, s) for p, s in sims if s.target_id == mine.target_id  # step 1
             or (s.kind, s.expected_metric) == (mine.kind, mine.expected_metric)]  # fmt: skip
    texts = _text_sims(draft.summary, [p["summary"] for p, _ in cands], embed)
    kept: list[tuple[float, ops.SimilarityRow]] = []
    for (p, s), s_text in zip(cands, texts, strict=True):
        rel = s.target_id != mine.target_id and related(mine.target_id, s.target_id)
        sim = recommendation_similarity(mine, s, s_text=s_text, related=rel)[3]
        if sim >= cfg.sim_threshold:
            kept.append((sim, p))
    kept.sort(key=lambda k: (-k[0], k[1]["rec_id"]))  # a fixed summation order too
    num = den = 0.0
    for sim, p in kept:
        weight = sim * _decay(p["measured_at"], now)
        num, den = num + weight * _VALUE[p["verdict"]], den + weight
    lo, hi = max(_DELTA_TYPE[0], cfg.delta_bounds[0]), min(_DELTA_TYPE[1], cfg.delta_bounds[1])
    delta = _clamp(cfg.alpha * num / (den + cfg.k0), lo, hi) if kept else 0.0
    c_lo = max(_CONF_TYPE[0], cfg.confidence_bounds[0])
    c_hi = min(_CONF_TYPE[1], cfg.confidence_bounds[1])
    unit_base = _clamp(base, 0.0, 1.0)
    similar = [
        SimilarOutcome(rec_id=p["rec_id"], sim=round(sim, 4), verdict=p["verdict"],  # type: ignore[arg-type]
                       outcome_query_id=p["query_id"] or "")
        for sim, p in kept[:_SIMILAR_MAX]
    ]  # fmt: skip
    conf = _clamp(unit_base * (1 + delta), c_lo, c_hi)
    return ConfidenceAdjustment(confidence=conf, base=unit_base, delta=delta, similar=similar)


# ---------------------------------------------------------------- U07-78


def _chunked[T](fetch: Callable[[list[str]], Iterable[T]], ids: Iterable[str]) -> list[T]:
    """`fetch` over the sorted distinct `ids` in chunks of the ops id-list limit."""
    unique, n = sorted(set(ids)), _LOOKUP_CHUNK
    return [x for i in range(0, len(unique), n) for x in fetch(unique[i : i + n])]


def _failed_check(
    r: RecommendationDraft, run_id: str, facts: dict[str, ops.FindingFact],
    known_queries: set[str], allowed: Sequence[re.Pattern[str]],
) -> str | None:  # fmt: skip
    """The name of the first U07-78 step 3 check `r` fails, else None."""
    refs = {n.id: n for n in r.numbers}
    usd = refs.get(r.expected_usd_ref) if r.expected_usd_ref else None
    checks = (
        ("markers", check_markers(r.summary, r.numbers).ok),
        ("numerals", not find_uncited_numerals(r.summary, allowed)),
        ("expected_delta_ref", r.expected_delta_ref is None or r.expected_delta_ref in refs),
        ("expected_usd_ref", r.expected_usd_ref is None or usd is not None),
        ("expected_usd_unit", usd is None or usd.unit == "usd"),
        ("findings", all(
            f in facts and facts[f]["status"] == "verified" and facts[f]["run_id"] == run_id
            for f in r.finding_ids
        )),
        ("evidence", all(n.query_id in known_queries for n in r.numbers)),
    )  # fmt: skip
    return next((name for name, ok in checks if not ok), None)


def _validate(
    run_id: str, ordered: Sequence[RecommendationDraft], deps: RecommendDeps
) -> list[float]:
    """Step 3 for every draft (first failure raises); returns each draft's base confidence."""
    conn = deps.conn_factory()
    fids = (f for r in ordered for f in r.finding_ids)
    facts = dict(_chunked(lambda ids: ops.finding_facts(ids, conn=conn).items(), fids))
    qids = (n.query_id for r in ordered for n in r.numbers)
    known = set(_chunked(lambda ids: ops.existing_query_ids(ids, conn=conn), qids))
    bases = []
    for r in ordered:
        if (check := _failed_check(r, run_id, facts, known, deps.allowed)) is not None:
            msg = f"recommendation rank {r.rank}: {check}"
            raise ReportContractError(msg)
        bases.append(fmean(facts[f]["confidence"] for f in r.finding_ids))
    return bases


def _row(run_id: str, r: RecommendationDraft, adj: ConfidenceAdjustment, now: datetime
         ) -> ops.RecommendationRow:  # fmt: skip
    """The `recommendation` row of one validated draft (U07-78 step 5)."""
    refs = {n.id: n for n in r.numbers}
    delta = refs[r.expected_delta_ref].value if r.expected_delta_ref else None
    usd = refs[r.expected_usd_ref].value if r.expected_usd_ref else None
    basis: dict[str, JsonValue] = {
        "base": adj.base, "delta": adj.delta, "expected_delta_ref": r.expected_delta_ref,
        "expected_usd_ref": r.expected_usd_ref, "rank": r.rank,
        "similar": [s.model_dump(mode="json") for s in adj.similar],
    }  # fmt: skip
    return ops.RecommendationRow(
        rec_id="rec_" + new_ulid(), run_id=run_id, kind=r.kind, target_type=r.target_type,
        target_id=r.target_id, summary=r.summary,
        numbers=[n.model_dump(mode="json") for n in r.numbers],
        expected_metric=r.expected_metric,
        expected_delta=None if delta is None else float(delta),
        expected_usd=None if usd is None else str(Decimal(str(usd)).quantize(_CENT)),
        confidence=adj.confidence, confidence_basis=basis, finding_ids=list(r.finding_ids),
        created_at=clock.format_utc(now),
    )  # fmt: skip


def _question(run: ops.RunRow, redactor: Redactor) -> str | None:
    """`run.meta.request.question`, redacted and cut to 500 chars, or None."""
    request = run.meta.get("request")
    text = request.get("question") if isinstance(request, dict) else None
    if not isinstance(text, str):
        return None
    found = redactor.redact(text)
    return (text if found is None else found.text)[:_QUESTION_MAX]


def _summary_item(
    run: ops.RunRow, ordered: Sequence[RecommendationDraft], rec_ids: list[str],
    question: str | None, dead_tasks: int,
) -> MemoryProposal:  # fmt: skip
    """The numeral-free `run_summary` proposal of U07-78 step 5."""
    targets = ", ".join(f"{r.target_type}:{r.target_id}" for r in ordered[:_TOP])
    top = list(dict.fromkeys(f for r in ordered for f in r.finding_ids))[:_TOP]
    data: dict[str, JsonValue] = {
        "run_kind": run.kind, "question": question, "top_finding_ids": list(top),
        "rec_ids": list(rec_ids), "dead_task_count": dead_tasks,
    }  # fmt: skip
    prov = Provenance(author_type="system", author_role=None, author_ref=None,
                      run_id=run.run_id, task_id=None, via="pipeline")  # fmt: skip
    return MemoryProposal(
        layer="episodic", kind="run_summary", data=data, confidence=1.0, provenance=prov,
        content=f"Run {run.run_id} ({run.kind}) recorded recommendations for {targets}.",
    )  # fmt: skip


def _existing_order(row: ops.RecommendationRow) -> tuple[int, str]:
    rank = row["confidence_basis"].get("rank")
    return (rank if isinstance(rank, int) else MAX_RECS + 1), row["rec_id"]


def write_recommendations(
    run_id: str, recs: Sequence[RecommendationDraft], *, deps: RecommendDeps,
    now: datetime | None = None,
) -> list[str]:  # fmt: skip
    """Persist a publishable run's recommendations once per run; ids in the order of `recs`."""
    if not recs:
        return []
    if len(recs) > MAX_RECS:
        msg = f"recommendation count above {MAX_RECS}"
        raise ReportContractError(msg)
    ordered = sorted(recs, key=lambda r: r.rank)
    if len({r.rank for r in recs}) != len(recs):
        msg = "duplicate rank"
        raise ReportContractError(msg)
    run = ops.get_run(run_id)
    if run is None:
        raise MemoryNotFound("run", run_id)  # noqa: EM101 - a kind literal, not a message
    when = now or clock.now()
    bases = _validate(run_id, ordered, deps)
    adjusted = [deps.adjust(r, base) for r, base in zip(ordered, bases, strict=True)]
    rows = [_row(run_id, r, adj, when) for r, adj in zip(ordered, adjusted, strict=True)]
    question = _question(run, deps.redactor)
    wanted = [(r.kind, r.target_type, r.target_id) for r in ordered]

    def tx(conn: sqlite3.Connection) -> tuple[list[str], str | None]:
        existing = sorted(ops.run_recommendations(run_id, conn=conn), key=_existing_order)
        if existing:
            if [(e["kind"], e["target_type"], e["target_id"]) for e in existing] != wanted:
                _log.error("memory.recommendations.conflict", run_id=run_id)
                msg = "recommendations for run changed on resume"
                raise ReportContractError(msg)
            return [e["rec_id"] for e in existing], None
        ops.insert_recommendations(rows, conn=conn)
        rec_ids = [row["rec_id"] for row in rows]
        dead = ops.dead_task_count(run_id, conn=conn)
        item = _summary_item(run, ordered, rec_ids, question, dead)
        key = keyed_hash("run_summary:" + run_id)
        stored = deps.writer.insert_system_item(item, key_hash=key, conn=conn, now=when)
        return rec_ids, stored.memory_id

    ids, summary_id = ops.run_write(tx, op="write_recommendations")
    if summary_id is not None:
        deps.writer.embed_after_commit(summary_id)
    _log.info("memory.recommendations.written", run_id=run_id, n=len(ids),
              reused=summary_id is None)  # fmt: skip
    by_rank = {r.rank: rec_id for r, rec_id in zip(ordered, ids, strict=True)}
    return [by_rank[r.rank] for r in recs]
