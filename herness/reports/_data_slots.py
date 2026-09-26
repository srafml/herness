"""Warehouse slot loaders of the report data plan (impl 09 U09-15; split from ``_data.py``).

Every statement is a parameterised module constant with a ``LIMIT`` (UT09-79); ``Ctx`` registers
query ids as cells are built. Titles pass ``redact_text`` (TH09-11); no ``core.*`` is read.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Final, NamedTuple, NotRequired, TypedDict

import duckdb
from pydantic import TypeAdapter, ValidationError

from herness.core import time as clock
from herness.core.errors import QueryError, ReportContractError, SchemaViolation
from herness.core.numbers import NOT_AVAILABLE
from herness.core.redact import redact_text
from herness.core.types import NumberRef
from herness.reports import charts
from herness.reports._evidence import EvidenceCollector
from herness.reports._format import NOT_LINKED, confidence_label, format_value
from herness.reports._markup import Segment, segment_text
from herness.reports.contract import UncitedHit

# fmt: off
_BUILD_SQL: Final = "SELECT source_watermarks FROM meta.build WHERE build_id = $b LIMIT 1"
_DQ_SQL: Final = ("SELECT check_name, severity, value, threshold FROM meta.dq_result"
                  " WHERE NOT passed ORDER BY check_name LIMIT 500")
_UNMAPPED_SQL: Final = ("SELECT value FROM meta.dq_result"
                        " WHERE check_name = 'incidents_service_null' LIMIT 1")
FUNDING_COLUMNS: Final = ("rank", "candidate_id", "title", "priority", "wsjf", "confidence",
                          "effort_cost_usd")
_FUND_COLS: Final = (*FUNDING_COLUMNS, "unconfirmed", "query_ids")
_FUNDING: Final = ("SELECT rank, candidate_id, title, priority, wsjf, confidence, effort_cost_usd,"
                   " unconfirmed, query_ids FROM score.funding")  # columns in _FUND_COLS order
_FUNDING_SQL: Final = f"{_FUNDING} ORDER BY rank, candidate_id LIMIT $n"
_FUND_ROWS_SQL: Final = f"{_FUNDING} WHERE list_contains($ids, candidate_id) LIMIT 500"
_ORG_TABLE_SQL: Final = (
    "SELECT entity_id, composite, rank, query_ids FROM (SELECT *, row_number() OVER (PARTITION"
    " BY entity_id ORDER BY metric = 'composite' DESC, rank, metric) AS rn FROM score.org"
    " WHERE entity_type = $t) WHERE rn = 1 ORDER BY rank, entity_id LIMIT $n")
_ORG_Z_SQL: Final = ("SELECT entity_id, metric, z_score FROM score.org WHERE entity_type = $t"
                     " AND list_contains($ids, entity_id) AND metric <> 'composite'"
                     " ORDER BY entity_id, metric LIMIT 500")
_TOP_TEAMS_SQL: Final = ("SELECT entity_id FROM score.org WHERE entity_type = 'team'"
                         " GROUP BY entity_id ORDER BY min(rank), entity_id LIMIT $n")
_SCORECARD_SQL: Final = (
    "SELECT metric, value, peer_group, peer_median, z_score, sample_size, composite, rank,"
    " flags, query_ids FROM score.org WHERE entity_type = $t AND entity_id = $e"
    " ORDER BY metric LIMIT 500")
_SERIES_SQL: Final = (
    "SELECT metric, value FROM (SELECT metric, period_start, value, row_number() OVER"
    " (PARTITION BY metric ORDER BY period_start DESC) AS rn FROM metrics.metric_value"
    " WHERE entity_type = $t AND entity_id = $e AND period = 'month') WHERE rn <= 8"
    " ORDER BY metric, period_start LIMIT 500")
_SCENARIOS_SQL: Final = (
    "SELECT scenario, max(budget_usd) AS budget, max(solver_status) FROM score.portfolio"
    " GROUP BY scenario ORDER BY budget, scenario LIMIT 500")
_SELECTED_SQL: Final = (
    "SELECT p.scenario, p.candidate_id, p.order_rank, p.expected_impact_usd, p.query_ids,"
    " f.effort_cost_usd FROM score.portfolio AS p LEFT JOIN score.funding AS f"
    " ON f.candidate_id = p.candidate_id WHERE p.selected"
    " ORDER BY p.budget_usd, p.scenario, p.order_rank, p.candidate_id LIMIT 500")
_EFFORT_SQL: Final = ("SELECT candidate_id, effort_cost_usd FROM score.funding"
                      " WHERE list_contains($ids, candidate_id) LIMIT 500")
_FUND_FMT: Final = {"priority": "ratio2", "wsjf": "ratio2", "effort_cost_usd": "usd_compact"}
_FUND_EXTRAS: Final = ("priority", "wsjf", "confidence", "unconfirmed")
SCORECARD_COLUMNS: Final = ("metric", "value", "peer_group", "peer_median", "z_score",
                            "sample_size", "composite", "rank", "flags")
# fmt: on

type Row = tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class Cell:
    """One table cell: display text, its evidence link (None: not linked) and hover title."""

    text: str
    query_id: str | None
    title: str


@dataclass(frozen=True, slots=True)
class TextBlock:
    """One model-written text block at draft path ``where``, split into segments (U09-17)."""

    where: str
    segments: list[Segment]


# Functional NamedTuples keep the plan's row shapes inside the module budget (module map).
# fmt: off
Table = NamedTuple("Table", [  # noqa: UP014 - compact form, see above
    ("columns", tuple[str, ...]), ("rows", list[list[Cell]]), ("marked", list[bool])])
PortfolioBlock = NamedTuple("PortfolioBlock", [  # noqa: UP014 - compact form
    ("scenario", str), ("budget", Cell), ("solver_status", str), ("custom", bool),
    ("table", Table), ("chart_key", str)])
Scorecard = NamedTuple("Scorecard", [  # noqa: UP014 - spark_keys: one chart key per row
    ("entity_type", str), ("entity_id", str), ("table", Table), ("spark_keys", list[str])])
# fmt: on


@dataclass(slots=True)
class Ctx:
    """Per-render state: connection, collector, the current back-link ``place``, counters."""

    con: duckdb.DuckDBPyConnection
    build_id: str
    top_n: int
    collector: EvidenceCollector
    uncited: Sequence[UncitedHit] = ()
    finding_qids: Mapping[str, list[str]] = field(default_factory=dict)
    place: str = ""
    number_qids: list[str] = field(default_factory=list)
    charts: dict[str, str] = field(default_factory=dict)

    def rows(self, sql: str, params: Mapping[str, object] | None = None) -> list[Row]:
        """Rows of one warehouse statement; a DuckDB error → QueryError naming the build."""
        try:
            return self.con.execute(sql, dict(params or {})).fetchall()
        except duckdb.Error as exc:
            msg = f"report data query failed for build {self.build_id}"
            raise QueryError(msg, details={"build_id": self.build_id}) from exc

    def text(
        self, path: str, text: str, numbers: Sequence[NumberRef] = (), fids: Sequence[str] = ()
    ) -> list[Segment]:
        """Segments of ``text`` (uncited hits of ``path``); number ids, then finding ids."""
        segments = segment_text(text, numbers, uncited=[h for h in self.uncited if h.where == path])
        for seg in segments:
            if seg.query_id is not None:
                self.link(seg.query_id)
        for qid in (q for fid in fids for q in self.finding_qids.get(fid, [])):
            self.collector.use(qid, self.place)
        return segments

    def block(
        self, path: str, text: str, numbers: Sequence[NumberRef] = (), fids: Sequence[str] = ()
    ) -> TextBlock:
        return TextBlock(path, self.text(path, text, numbers, fids))

    def link(self, query_id: str) -> None:
        """Register one linkable number's query id."""
        self.collector.use(query_id, self.place)
        self.number_qids.append(query_id)

    def cell(self, text: str, query_ids: object, title: str) -> Cell:
        """A cell linked to ``query_ids[0]`` unless the text is a dash or ``n/a``."""
        qid = str(query_ids[0]) if isinstance(query_ids, list | tuple) and query_ids else None
        if qid is None or text in (NOT_LINKED, NOT_AVAILABLE):
            return Cell(text, None, title)
        self.link(qid)
        return Cell(text, qid, title)

    def num(self, value: object, fmt: str, query_ids: object, title: str) -> Cell:
        return self.cell(format_value(number(value), fmt), query_ids, title)


def plain(value: object) -> Cell:  # an unlinked text cell
    return Cell("" if value is None else str(value), None, "")


def number(value: object) -> Decimal | int | float | None:
    """``value`` when it is a number (not a bool), else None."""
    if isinstance(value, bool) or not isinstance(value, Decimal | int | float):
        return None
    return value


def _float(value: object) -> float | None:
    got = number(value)
    return None if got is None else float(got)


def json_object(raw: object) -> dict[str, object]:
    """A JSON object (text or mapping) as a dict; anything else gives ``{}``."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return {str(k): v for k, v in raw.items()} if isinstance(raw, Mapping) else {}


def redacted(text: object) -> str:
    """``text`` through ``redact_text``; a failed redaction shows ``[redacted]`` (TH09-11)."""
    out = redact_text("" if text is None else str(text))
    return "[redacted]" if out is None else out


def data_as_of(ctx: Ctx) -> datetime | None:
    """Max of the ISO timestamps in ``meta.build.source_watermarks``; NULL or empty → None."""
    rows = ctx.rows(_BUILD_SQL, {"b": ctx.build_id})
    stamps: list[datetime] = []
    for value in json_object(rows[0][0] if rows else None).values():
        try:
            stamps.append(clock.parse_iso(value))  # type: ignore[arg-type]
        except (SchemaViolation, AttributeError):
            continue
    return max(stamps, default=None)


def dq_table(ctx: Ctx) -> Table:
    """Failed ``meta.dq_result`` checks (not linked)."""
    rows = [
        [plain(name), plain(sev), plain(format_value(number(v), "plain")),
         plain(format_value(number(t), "plain"))]
        for name, sev, v, t in ctx.rows(_DQ_SQL)
    ]  # fmt: skip
    return Table(("check_name", "severity", "value", "threshold"), rows, [False] * len(rows))


def unmapped_share(ctx: Ctx) -> str | None:
    rows = ctx.rows(_UNMAPPED_SQL)
    return format_value(number(rows[0][0]), "plain") if rows else None


def _fund_cell(ctx: Ctx, row: Row, name: str) -> Cell:
    """One ``score.funding`` value cell of a ``_FUNDING`` row, linked to ``query_ids[0]``."""
    value = row[_FUND_COLS.index(name)]
    text: str
    if name == "confidence":
        text = NOT_LINKED if value is None else confidence_label(number(value))
    elif name == "unconfirmed":
        text = "yes" if value else "no"
    else:
        text = format_value(number(value), _FUND_FMT[name])
    return ctx.cell(text, row[-1], f"{name} · candidate_id={row[1]}")


def fund_rows(ctx: Ctx, candidate_ids: Sequence[str]) -> dict[str, Row]:
    """``score.funding`` rows of the fund cards' targets, by ``candidate_id``."""
    return {str(r[1]): r for r in ctx.rows(_FUND_ROWS_SQL, {"ids": list(candidate_ids)})}


def fund_extras(ctx: Ctx, row: Row) -> tuple[tuple[str, Cell], ...]:
    """A fund card's ``priority``, ``wsjf``, ``confidence`` label and ``unconfirmed`` cells."""
    return tuple((name, _fund_cell(ctx, row, name)) for name in _FUND_EXTRAS)


def funding_table(ctx: Ctx) -> Table:
    """Top ``top_n`` of ``score.funding`` by rank; chart ``funding_priority`` (bar_h)."""
    ctx.place = "funding_table"
    rows = ctx.rows(_FUNDING_SQL, {"n": ctx.top_n})
    body = [[plain(row[0]), plain(row[1]), plain(redacted(row[2])),
             *(_fund_cell(ctx, row, n) for n in FUNDING_COLUMNS[3:])] for row in rows]  # fmt: skip
    bars = [(cells[2].text, number(row[3]) or 0.0) for cells, row in zip(body, rows, strict=True)]
    ctx.charts["funding_priority"] = charts.bar_h(bars, title="Priority by candidate")
    return Table(FUNDING_COLUMNS, body, [row[7] is True for row in rows])


def org_table(ctx: Ctx, entity_type: str) -> Table:
    """One row per entity (composite, rank), top ``top_n``; chart ``org_z`` (dot_z)."""
    ctx.place = "org_table"
    rows = ctx.rows(_ORG_TABLE_SQL, {"t": entity_type, "n": ctx.top_n})
    body = [
        [plain(eid), ctx.num(comp, "ratio2", qids, f"composite · {entity_type}={eid}"),
         ctx.num(rank, "int", qids, f"rank · {entity_type}={eid}")]
        for eid, comp, rank, qids in rows
    ]  # fmt: skip
    order = {str(row[0]): i for i, row in enumerate(rows)}
    z_rows = ctx.rows(_ORG_Z_SQL, {"t": entity_type, "ids": list(order)})
    dots = [(f"{e} · {m}", _float(z)) for e, m, z in sorted(z_rows, key=lambda r: order[r[0]])]
    ctx.charts["org_z"] = charts.dot_z(dots, title="z-score by metric")
    return Table(("entity_id", "composite", "rank"), body, [False] * len(body))


# fmt: off
_Row = TypedDict("_Row", {  # noqa: UP013 - unknown extra keys are ignored
    "candidate_id": str, "selected": bool, "order_rank": int | None,
    "expected_impact_usd": Decimal, "query_ids": NotRequired[list[str]]})
_Named = TypedDict("_Named", {"name": str})  # noqa: UP013 - scenario given as an object
_Custom = TypedDict("_Custom", {  # noqa: UP013 - an impl 04 PortfolioResult (JSON)
    "scenario": str | _Named, "budget_usd": Decimal, "solver_status": str, "rows": list[_Row],
    "query_ids": NotRequired[list[str]]})
# fmt: on
_CUSTOM: Final = TypeAdapter(_Custom)


def _bad(index: int, path: str) -> ReportContractError:
    where = f"portfolio_custom[{index}]{path}"
    msg = f"{where} is missing or invalid"
    return ReportContractError(msg, details={"code": "draft_invalid", "where": where})


def _custom(index: int, raw: Mapping[str, object]) -> tuple[_Custom, list[tuple[_Row, list[str]]]]:
    """Entry ``index`` and its rows by order_rank, each with its ids (own, else the entry's)."""
    try:
        spec = _CUSTOM.validate_python(raw)
    except ValidationError as exc:
        loc = exc.errors()[0]["loc"]
        loc = loc[:1] if loc[0] == "scenario" else loc  # drop the union branch name
        path = "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in loc)
        raise _bad(index, path) from None
    entry = spec.get("query_ids", [])
    rows = sorted(spec["rows"], key=lambda r: (r["order_rank"] is None, r["order_rank"] or 0))
    pairs = [(row, row.get("query_ids") or entry) for row in rows]
    if not (entry or (pairs and all(ids for _, ids in pairs))):
        raise _bad(index, ".query_ids")
    return spec, pairs


def _block(ctx: Ctx, head: tuple[str, object, str, bool], rows: Sequence[Row]) -> PortfolioBlock:
    """``rows`` = selected (candidate_id, order_rank, impact, query_ids, effort) in order."""
    scenario, budget, status, custom = head
    ctx.place = f"portfolio: {scenario}"
    body = [
        [plain(rank), plain(cid), ctx.num(impact, "usd_compact", qids, f"impact · {cid}")]
        for cid, rank, impact, qids, _ in rows
    ]
    key = f"portfolio-{sum(1 for k in ctx.charts if k.startswith('portfolio-'))}"
    steps = [(Decimal(str(effort or 0)), Decimal(str(impact))) for _, _, impact, _, effort in rows]
    total = Decimal(str(budget or 0))
    ctx.charts[key] = charts.step_budget(steps, budget=total, title=f"Impact vs budget: {scenario}")
    table = Table(("order_rank", "candidate_id", "expected_impact_usd"), body, [False] * len(body))
    return PortfolioBlock(scenario, plain(format_value(total, "usd_compact")), status, custom,
                          table, key)  # fmt: skip


def portfolio_blocks(ctx: Ctx, customs: Sequence[Mapping[str, object]]) -> list[PortfolioBlock]:
    """Draft custom scenarios first (every row query id registered), then ``score.portfolio``."""
    blocks: list[PortfolioBlock] = []
    for index, raw in enumerate(customs):
        spec, pairs = _custom(index, raw)
        chosen = [pair for pair in pairs if pair[0]["selected"]]
        ids = [r["candidate_id"] for r, _ in chosen]
        effort = {str(c): e for c, e in ctx.rows(_EFFORT_SQL, {"ids": ids})} if ids else {}
        rows = [(r["candidate_id"], r["order_rank"], r["expected_impact_usd"], qids,
                 effort.get(r["candidate_id"])) for r, qids in chosen]  # fmt: skip
        name = spec["scenario"] if isinstance(spec["scenario"], str) else spec["scenario"]["name"]
        blocks.append(_block(ctx, (name, spec["budget_usd"], spec["solver_status"], True), rows))
        for qid in (*spec.get("query_ids", []), *(q for _, qids in pairs for q in qids)):
            ctx.collector.use(qid, ctx.place)  # every id of the entry and of every row
    selected = ctx.rows(_SELECTED_SQL)
    for scenario, budget, status in ctx.rows(_SCENARIOS_SQL):
        rows = [row[1:] for row in selected if row[0] == scenario]
        blocks.append(_block(ctx, (str(scenario), budget, str(status), False), rows))
    return blocks


def top_teams(ctx: Ctx) -> list[tuple[str, str]]:
    return [("team", str(row[0])) for row in ctx.rows(_TOP_TEAMS_SQL, {"n": ctx.top_n})]


def scorecards(ctx: Ctx, entities: Sequence[tuple[str, str]]) -> list[Scorecard]:
    """Per entity: ``score.org`` rows and a sparkline of the last 8 monthly values per metric."""
    cards: list[Scorecard] = []
    for etype, eid in entities[: ctx.top_n]:
        ctx.place = f"scorecard: {etype}={eid}"
        series: dict[str, list[float | None]] = {}
        for metric, value in ctx.rows(_SERIES_SQL, {"t": etype, "e": eid}):
            series.setdefault(str(metric), []).append(_float(value))
        body = [_scorecard_row(ctx, row, f"{etype}={eid}")
                for row in ctx.rows(_SCORECARD_SQL, {"t": etype, "e": eid})]  # fmt: skip
        keys = [f"spark-{len(cards)}-{j}" for j in range(len(body))]
        for key, cells in zip(keys, body, strict=True):
            metric = cells[0].text
            ctx.charts[key] = charts.sparkline(series.get(metric, []), title=f"{metric}: {eid}")
        table = Table(SCORECARD_COLUMNS, body, [False] * len(body))
        cards.append(Scorecard(etype, eid, table, keys))
    return cards


def _scorecard_row(ctx: Ctx, row: Row, key: str) -> list[Cell]:
    metric, value, group, median, z, size, comp, rank, flags, qids = row
    at = f"{key}, metric={metric}"
    return [
        plain(metric), ctx.num(value, "plain", qids, f"value · {at}"), plain(group),
        ctx.num(median, "plain", qids, f"peer_median · {at}"),
        ctx.num(z, "ratio2", qids, f"z_score · {at}"),
        ctx.num(size, "int", qids, f"sample_size · {at}"),
        ctx.num(comp, "ratio2", qids, f"composite · {at}"),
        ctx.num(rank, "int", qids, f"rank · {at}"), plain(", ".join(map(str, flags or ()))),
    ]  # fmt: skip
