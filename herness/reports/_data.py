"""The format-neutral report data plan and its banners (impl 09 U09-15, U09-16; design §5.2).

Query ids enter the collector in outline order (header, slots 2 → 9: text blocks, cards,
tables), then the draft's other ``query_ids``. Model text stays as ``Segment`` lists (TH09-01).
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal, NamedTuple

import duckdb
from pydantic import TypeAdapter, ValidationError

from herness.core import time as clock
from herness.core.errors import ReportContractError
from herness.core.types import Coverage, NumberRef, ReportDraft
from herness.reports import _data_slots as slots
from herness.reports._data_slots import Cell, Ctx, PortfolioBlock, Scorecard, Table, TextBlock
from herness.reports._evidence import EvidenceCollector
from herness.reports._format import format_value
from herness.reports._markup import Segment
from herness.reports.contract import SECTION_IDS, UncitedHit
from herness.reports.settings import ReportsSection
from herness.store import ops

__all__ = ["Cell", "PortfolioBlock", "ReportBanner", "ReportData", "Scorecard", "Table",
           "TextBlock", "derive_banners", "fill_rationale", "load_report_data"]  # fmt: skip

_BANNERS: Final = (
    ("unconfirmed_weights", "Dollar weights are placeholders and not yet confirmed."),
    ("dq_warnings", "Some data quality checks failed. See Data quality and caveats."),
    ("findings_only", "The report writer did not finish. This report lists verified findings"
     " only, without narrative or recommendations."),
    ("partial_run", "This run is partial: some tasks did not finish."),
    ("hybrid_fallback", "An off-network step fell back to a local model."),
    ("off_network_profile", "Parts of this run used the off-network profile `{profile}` under"
     " the approved data policy."),
    ("budget_exhausted", "The run budget ran out; some analysis was cut short."),
)  # fmt: skip
_LOCAL_PROFILES: Final = frozenset({"local", "synth"})
_PLACEHOLDER_RE: Final = re.compile(r"\{([a-z_][a-z0-9_]{0,40})\}")
_LEVERS_SQL: Final = (
    "SELECT entity_type, entity_id, metric, target_kind, delta_usd, rationale_template,"
    " template_params, unconfirmed, query_ids FROM score.action_lever"
    " ORDER BY delta_usd DESC, entity_type, entity_id, metric LIMIT $n"
)
_REC_KIND: Final = {"funding_review": "fund", "org_review": "org_action"}
_SCORECARD_TYPES: Final = ("team", "service", "org")
_NO_OUTCOMES: Final = "No measured outcomes yet."
_FINDINGS_HEADING: Final = "Verified findings"
_ERROR_MAX: Final = 200
_OUTCOME_COLUMNS: Final = ("measurement", "metric", "baseline", "actual", "delta", "verdict")
_REFS: Final = TypeAdapter(list[NumberRef])
_WINDOW: Final = ReportsSection().prior_outcomes_window_days


@dataclass(frozen=True, slots=True)
class ReportBanner:
    """One report banner (U09-16)."""

    code: str
    text: str
    details: tuple[str, ...]


# Functional NamedTuples keep the outline's slot shapes inside the module budget (module map).
# fmt: off
Header = NamedTuple("Header", [  # noqa: UP014 - outline slot 0; compact form, see above
    ("title", list[Segment]), ("run_id", str), ("kind", str), ("build_id", str),
    ("data_as_of", datetime | None), ("depth", str), ("profile", str), ("rendered_at", datetime),
    ("coverage", Coverage), ("gate2_passed", bool), ("gate2_numbers", int),
    ("gate2_failed", int)])
Card = NamedTuple("Card", [  # noqa: UP014 - anchor rec-<rank>; extras from score.funding
    ("rank", int), ("kind", str), ("target_type", str), ("target_id", str),
    ("headline", list[Segment]), ("summary", list[Segment]),
    ("extras", tuple[tuple[str, Cell], ...]), ("finding_ids", tuple[str, ...])])
LeverRow = NamedTuple("LeverRow", [  # noqa: UP014 - one score.action_lever row
    ("entity_type", str), ("entity_id", str), ("metric", str), ("target_kind", str),
    ("delta_usd", Cell), ("rationale", str), ("unconfirmed", bool),
    ("card_anchors", tuple[str, ...])])
RetroItem = NamedTuple("RetroItem", [  # noqa: UP014 - a prior recommendation
    ("rec_id", str), ("target_id", str), ("summary", list[Segment]),
    ("decision", str | None), ("outcomes", Table)])
Retrospective = NamedTuple("Retrospective", [  # noqa: UP014 - slot 7; empty_text or None
    ("blocks", list[TextBlock]), ("items", list[RetroItem]),
    ("verdict_counts", dict[str, int]), ("empty_text", str | None)])
Caveats = NamedTuple("Caveats", [  # noqa: UP014 - slot 8
    ("dq_failed", Table), ("unmapped_share", str | None), ("unconfirmed_keys", list[str]),
    ("dead_tasks", list[tuple[str, str, str]]), ("contested", list[str]),
    ("removed", list[tuple[str, str]]), ("flags", dict[str, list[str]]),
    ("blocks", list[TextBlock])])
MethodInfo = NamedTuple("MethodInfo", [  # noqa: UP014 - slot 9
    ("depth", str), ("profile", str), ("gate2_passed", bool), ("gate2_numbers", int),
    ("gate2_failed", int), ("role_calls", dict[str, int])])
RunAppendix = NamedTuple("RunAppendix", [  # noqa: UP014 - slot 11
    ("ranked_entities", list[tuple[int, str, str]]), ("task_counts", list[tuple[str, str, int]]),
    ("tokens_input", int), ("tokens_output", int), ("cost_usd", str), ("config_hash", str)])
# fmt: on


@dataclass(frozen=True, slots=True)
class ReportData:
    """Every number and text block of the outline, format-neutral (U09-15)."""

    header: Header
    publishable: bool
    banners: list[ReportBanner]
    sections: dict[str, list[TextBlock]]
    section_titles: dict[str, str]
    cards: list[Card]
    funding_table: Table | None
    org_table: Table | None
    portfolio_blocks: list[PortfolioBlock]
    scorecards: list[Scorecard]
    levers: list[LeverRow]
    retro: Retrospective
    caveats: Caveats
    method: MethodInfo
    run_appendix: RunAppendix
    charts: dict[str, str]
    numbers_total: int
    number_query_ids: list[str]


def derive_banners(  # noqa: PLR0913 - keyword-only signature fixed by U09-16
    draft_banners: Sequence[str],
    *,
    unconfirmed_keys: Sequence[str],
    dq_failed: bool,
    run_status: str,
    dead_tasks: int,
    profile: str,
    draft_mode: Literal["full", "findings_only"] = "full",
) -> list[ReportBanner]:
    """Union of draft and derived banners in table order, each code once (U09-16). Pure."""
    derived = {"unconfirmed_weights": bool(unconfirmed_keys), "dq_warnings": dq_failed,
               "findings_only": draft_mode == "findings_only",
               "partial_run": run_status == "partial" or dead_tasks > 0,
               "off_network_profile": profile not in _LOCAL_PROFILES}  # fmt: skip
    present = set(draft_banners)
    return [ReportBanner(code, text.replace("{profile}", profile),
                         tuple(unconfirmed_keys) if code == "unconfirmed_weights" else ())
            for code, text in _BANNERS if derived.get(code, False) or code in present]  # fmt: skip


def fill_rationale(template: str, params: Mapping[str, object]) -> str:
    """Fill ``{name}`` placeholders: numbers via ``format_value(v, "plain")``, None as the em
    dash, anything else ``str()``; names missing from ``params`` stay as written."""

    def sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in params:
            return match.group(0)
        value = params[name]
        if value is None or slots.number(value) is not None:
            return format_value(slots.number(value), "plain")
        return str(value)

    return _PLACEHOLDER_RE.sub(sub, template)


def _finding_ids(draft: ReportDraft) -> list[str]:
    ids = [f for s in draft.sections for p in s.paragraphs for f in p.finding_ids]
    ids += [f for rec in draft.recommendations for f in rec.finding_ids]
    if draft.prior_outcomes_commentary is not None:
        ids += draft.prior_outcomes_commentary.finding_ids
    return list(dict.fromkeys(ids))


def _section(ctx: Ctx, draft: ReportDraft, section_id: str) -> list[TextBlock]:
    ctx.place = section_id
    return [ctx.block(f"sections[{i}].paragraphs[{j}]", par.text, par.numbers, par.finding_ids)
            for i, section in enumerate(draft.sections) if section.id == section_id
            for j, par in enumerate(section.paragraphs)]  # fmt: skip


def _refs(raw: object) -> list[NumberRef]:
    try:
        return _REFS.validate_python(raw)
    except ValidationError:
        return []


def _safe_text(ctx: Ctx, text: str, raw_numbers: object) -> list[Segment]:
    """A stored prior summary; markers that do not resolve leave it as plain text."""
    try:
        return ctx.text("retrospective", text, _refs(raw_numbers))
    except ReportContractError:
        return [Segment("text", text, None, "")]


def _findings_blocks(ctx: Ctx, run_id: str) -> list[TextBlock]:
    """R-49: the run's verified findings (claim with numbers, then the finding's query ids)."""
    ctx.place = "executive_summary"
    blocks: list[TextBlock] = []
    for row in ops.ui_list_run_findings(run_id, status="verified"):
        path = f"findings[{row['finding_id']}]"
        blocks.append(ctx.block(path, row["claim"], _refs(row["numbers"])))
        for qid in row["query_ids"] if isinstance(row["query_ids"], list) else ():
            ctx.collector.use(str(qid), ctx.place)
    return blocks


def _cards(ctx: Ctx, draft: ReportDraft) -> list[Card]:
    kind = _REC_KIND.get(draft.kind, "fund")
    items = [(k, rec) for k, rec in enumerate(draft.recommendations) if rec.kind == kind]
    funds = slots.fund_rows(ctx, [r.target_id for _, r in items]) if kind == "fund" else {}
    cards: list[Card] = []
    for k, rec in items:
        ctx.place = f"rec-{rec.rank}"
        path = f"recommendations[{k}]"
        headline = ctx.text(f"{path}.headline", rec.headline, rec.numbers)
        summary = ctx.text(f"{path}.summary", rec.summary, rec.numbers, rec.finding_ids)
        row = funds.get(rec.target_id)
        extras = slots.fund_extras(ctx, row) if row is not None else ()
        cards.append(Card(rec.rank, rec.kind, rec.target_type, rec.target_id, headline,
                          summary, extras, tuple(rec.finding_ids)))  # fmt: skip
    return cards


def _levers(ctx: Ctx, draft: ReportDraft) -> list[LeverRow]:
    ctx.place = "actions"
    anchors: dict[tuple[str, str, str], list[str]] = {}
    for rec in draft.recommendations:
        for lever in rec.action_levers:
            key = (lever["entity_type"], lever["entity_id"], lever["metric"])
            anchors.setdefault(key, []).append(f"rec-{rec.rank}")
    out: list[LeverRow] = []
    rows = ctx.rows(_LEVERS_SQL, {"n": ctx.top_n})
    for etype, eid, metric, target, delta, template, params, unconf, qids in rows:
        cell = ctx.num(delta, "usd_compact", qids, f"delta_usd · {etype}={eid}, metric={metric}")
        text = fill_rationale(str(template or ""), slots.json_object(params))
        linked = tuple(anchors.get((etype, eid, metric), ()))
        out.append(LeverRow(str(etype), str(eid), str(metric), str(target), cell, text,
                            unconf is True, linked))  # fmt: skip
    return out


def _retro(ctx: Ctx, draft: ReportDraft, start: datetime, days: tuple[int, int]) -> Retrospective:
    blocks = _section(ctx, draft, "retrospective")
    commentary = draft.prior_outcomes_commentary
    if commentary is not None:
        blocks.append(ctx.block("prior_outcomes_commentary", commentary.text, commentary.numbers,
                                commentary.finding_ids))  # fmt: skip
    recs = ops.ui_list_recommendations(kind=_REC_KIND.get(draft.kind, "fund"),
                                       created_from=start - timedelta(days=days[1]),
                                       created_to=start - timedelta(days=days[0]))  # fmt: skip
    rec_ids = [rec["rec_id"] for rec in recs]
    decisions = ops.ui_latest_decisions(rec_ids)
    outcomes = ops.ui_list_outcomes(rec_ids)
    items: list[RetroItem] = []
    counts: Counter[str] = Counter()
    for rec in recs:
        ctx.place = f"retrospective: {rec['rec_id']}"
        summary = _safe_text(ctx, rec["summary"], rec["numbers"])
        measured = outcomes.get(rec["rec_id"], [])
        rows = [_outcome_row(ctx, o) for o in measured]
        counts.update(o["verdict"] for o in measured)
        decision = decisions.get(rec["rec_id"])
        items.append(RetroItem(rec["rec_id"], rec["target_id"], summary,
                               None if decision is None else decision["decision"],
                               Table(_OUTCOME_COLUMNS, rows, [False] * len(rows))))  # fmt: skip
    return Retrospective(blocks, items, dict(sorted(counts.items())),
                         None if counts else _NO_OUTCOMES)  # fmt: skip


def _outcome_row(ctx: Ctx, row: ops.UiOutcomeRow) -> list[Cell]:
    qids = [row["query_id"]] if row["query_id"] else []
    key = f"rec_id={row['rec_id']}, measurement={row['measurement']}"
    return [
        slots.plain(row["measurement"]), slots.plain(row["metric"]),
        ctx.num(row["baseline"], "plain", qids, f"baseline · {key}"),
        ctx.num(row["actual"], "plain", qids, f"actual · {key}"),
        ctx.num(row["delta"], "plain", qids, f"delta · {key}"), slots.plain(row["verdict"]),
    ]  # fmt: skip


def _caveats(ctx: Ctx, draft: ReportDraft, dq: Table, unconfirmed: Sequence[str]) -> Caveats:
    ctx.place = "risks_and_caveats"
    blocks = [ctx.block(f"caveats[{c}]", text) for c, text in enumerate(draft.caveats)]
    firsts = [next(iter((t["last_error"] or "").splitlines()), "") for t in draft.dead_tasks]
    dead = [(t["role"], t["objective"], slots.redacted(first[:_ERROR_MAX]))
            for t, first in zip(draft.dead_tasks, firsts, strict=True)]  # fmt: skip
    removed = [(item["where"], item["reason"]) for item in draft.removed]
    return Caveats(dq, slots.unmapped_share(ctx), list(unconfirmed), dead, list(draft.contested),
                   removed, dict(draft.flags), blocks)  # fmt: skip


def _int(value: object) -> int:
    return int(slots.number(value) or 0)


def _method(run: ops.RunRow, draft: ReportDraft) -> MethodInfo:
    """Slot 9; role call counts from ``run.token_usage["by_role"][role]["calls"]``."""
    by_role = slots.json_object(run.token_usage.get("by_role"))
    calls = {
        role: _int(slots.json_object(use).get("calls")) for role, use in sorted(by_role.items())
    }
    ver = draft.verification
    return MethodInfo(draft.depth, draft.profile, ver.passed, ver.n_numbers, ver.n_failed, calls)


def _appendix(run: ops.RunRow, draft: ReportDraft) -> RunAppendix:
    tokens = run.token_usage
    return RunAppendix(
        [(e.rank, e.entity_type, e.entity_id) for e in draft.ranked_entities],
        ops.ui_task_status_counts(run.run_id), _int(tokens.get("input")),
        _int(tokens.get("output")), format_value(run.cost_usd, "usd"), run.config_hash,
    )  # fmt: skip


def load_report_data(  # noqa: PLR0913 - keyword-only signature fixed by U09-15
    run: ops.RunRow,
    draft: ReportDraft,
    *,
    wh_con: duckdb.DuckDBPyConnection,
    collector: EvidenceCollector,
    uncited: Sequence[UncitedHit] = (),
    top_n: int,
    now: datetime,
    unconfirmed_keys: Sequence[str] = (),
    outcomes_window_days: tuple[int, int] = _WINDOW,
) -> ReportData:
    """The full outline plan from warehouse, ops and draft (U09-15). Raises ReportContractError
    (bad ``portfolio_custom``), QueryError naming the build; StoreBusy propagates."""
    ctx = Ctx(wh_con, draft.build_id, top_n, collector, uncited,
              ops.ui_finding_query_ids(_finding_ids(draft)))  # fmt: skip
    ver = draft.verification
    ctx.place = "header"
    header = Header(ctx.text("title", draft.title), run.run_id, draft.kind, draft.build_id,
                    slots.data_as_of(ctx), draft.depth, draft.profile, now, draft.coverage,
                    ver.passed, ver.n_numbers, ver.n_failed)  # fmt: skip
    dq = slots.dq_table(ctx)
    banners = derive_banners(draft.banners, unconfirmed_keys=unconfirmed_keys,
                             dq_failed=bool(dq.rows), run_status=run.status,
                             dead_tasks=len(draft.dead_tasks), profile=run.profile,
                             draft_mode=draft.mode)  # fmt: skip
    sections: dict[str, list[TextBlock]] = {sid: [] for sid in SECTION_IDS}
    findings_only = draft.mode == "findings_only"
    sections["executive_summary"] = (
        _findings_blocks(ctx, run.run_id) if findings_only
        else _section(ctx, draft, "executive_summary")
    )  # fmt: skip
    sections["recommendations"] = _section(ctx, draft, "recommendations")
    cards = _cards(ctx, draft)
    is_fund = draft.kind == "funding_review"
    funding = slots.funding_table(ctx) if is_fund else None
    first = draft.ranked_entities[0].entity_type if draft.ranked_entities else "team"
    org_type = first if first in _SCORECARD_TYPES else "team"
    org = None if is_fund else slots.org_table(ctx, org_type)
    sections["portfolio"] = _section(ctx, draft, "portfolio")
    portfolio = slots.portfolio_blocks(ctx, draft.portfolio_custom)
    sections["org_scorecards"] = _section(ctx, draft, "org_scorecards")
    entities = [(e.entity_type, e.entity_id) for e in draft.ranked_entities
                if e.entity_type in _SCORECARD_TYPES] or slots.top_teams(ctx)  # fmt: skip
    scorecards = slots.scorecards(ctx, entities)
    sections["actions"] = _section(ctx, draft, "actions")
    levers = _levers(ctx, draft)
    retro = _retro(ctx, draft, clock.ensure_utc(run.started_at), outcomes_window_days)
    sections["retrospective"] = retro.blocks
    sections["risks_and_caveats"] = _section(ctx, draft, "risks_and_caveats")
    caveats = _caveats(ctx, draft, dq, unconfirmed_keys)
    sections["method"] = _section(ctx, draft, "method")
    seen = set(collector.ordered_ids())
    for index, qid in enumerate(draft.query_ids):
        if qid not in seen:
            collector.use(qid, f"query_ids[{index}]")
    titles: dict[str, str] = {s.id: s.title for s in draft.sections}
    if findings_only:
        titles["executive_summary"] = _FINDINGS_HEADING
    return ReportData(
        header, draft.coverage.publishable, banners, sections, titles, cards, funding, org,
        portfolio, scorecards, levers, retro, caveats, _method(run, draft), _appendix(run, draft),
        ctx.charts, len(ctx.number_qids), list(ctx.number_qids),
    )  # fmt: skip
