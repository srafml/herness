"""Portfolio API and build step (impl 04 U04-76 … U04-81; design 04 §3.1, §5.10).

Optimizer input: the one recorded SELECT of `sql/portfolio_input.sql.j2` (U04-79); the CP-SAT
model, ordering and binding constraints live in `_solver`. `persist=True` writes `score.portfolio`
and `meta.evidence` on the build writer's connection; `persist=False` only ops `evidence`."""

import datetime
import re
import time
from collections.abc import Sequence
from contextlib import AbstractContextManager
from decimal import Decimal
from typing import Annotated, Any, Final, Self

import duckdb
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError, model_validator

from herness.core import ids
from herness.core.config import HernessConfig, get_config
from herness.core.errors import ConfigError, SchemaViolation
from herness.core.logging import get_logger
from herness.core.resilience.metrics import record_histogram
from herness.metrics import _solver as solver
from herness.metrics._scoring_checks import existing_tables
from herness.metrics.catalog import catalog_from_config
from herness.metrics.compute import _connection, _read_build
from herness.metrics.context import StepContext, StepResult
from herness.metrics.evidence import Producer, RecordedQuery, run_recorded
from herness.metrics.funding import FUNDING_TABLE
from herness.metrics.render import render_named
from herness.metrics.settings import PortfolioConfig, WeightsConfig
from herness.metrics.windows import resolve_as_of
from herness.store.ops.evidence import record_evidence

__all__ = [
    "PORTFOLIO_READBACK_SQL",
    "PortfolioResult",
    "PortfolioRow",
    "Scenario",
    "optimize_portfolio",
    "resolve_scenario",
    "run_portfolio_step",
]

PORTFOLIO_TABLE: Final = "score.portfolio"
UNCONSTRAINED: Final = "unconstrained"
PORTFOLIO_READBACK_SQL: Final = (
    "SELECT scenario, budget_usd, candidate_id, selected, order_rank, expected_impact_usd,"
    " solver_status, flags FROM score.portfolio WHERE scenario = CAST($scenario AS VARCHAR)"
    " ORDER BY candidate_id"
)
_DDL: Final = (
    "CREATE OR REPLACE TABLE score.portfolio (scenario VARCHAR NOT NULL,"
    " budget_usd DECIMAL(18,2) NOT NULL, candidate_id VARCHAR NOT NULL,"
    " selected BOOLEAN NOT NULL, order_rank INTEGER, expected_impact_usd DECIMAL(18,2) NOT NULL,"
    " solver_status VARCHAR NOT NULL, flags VARCHAR[] NOT NULL, query_ids VARCHAR[] NOT NULL)"
)
_INSERT: Final = (
    "INSERT INTO score.portfolio VALUES ($scenario, $budget_usd, $candidate_id, $selected,"
    " $order_rank, $expected_impact_usd, $solver_status, $flags, $query_ids)"
)
_CUSTOM_BUDGET: Final = re.compile(r"[0-9]{1,13}(\.[0-9]{1,2})?")
_DECIMAL_TEXT: Final = re.compile(r"[0-9]+(\.[0-9]+)?")
_SOLVED: Final = frozenset({"OPTIMAL", "FEASIBLE"})
_SOLVE_SECONDS: Final = "herness_metrics_portfolio_solve_seconds"
_log: Final = get_logger("metrics")

type _Build = tuple[str, datetime.datetime]
type _Candidates = Sequence[solver.PortfolioCandidate]


def _budget(value: object) -> object:
    """`budget_usd` accepts a Decimal, an int (not bool) or decimal text; strict otherwise."""
    if type(value) is int or (isinstance(value, str) and _DECIMAL_TEXT.fullmatch(value)):
        return Decimal(value)
    return value


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


_Id = Annotated[str, Field(max_length=256)]
_Capacity = Annotated[float, Field(gt=0, le=1e12, allow_inf_nan=False)]


class Scenario(_Frozen):
    """A budget scenario (U04-76, design 04 §3.1); bounded for UI input (TH04-08)."""

    name: str = Field(pattern=r"^[a-z0-9_]{1,64}$")
    budget_usd: Annotated[Decimal, BeforeValidator(_budget), Field(ge=0, le=Decimal("1e12"))]
    mandatory: list[_Id] = Field(default_factory=list, max_length=1000)
    excluded: list[_Id] = Field(default_factory=list, max_length=1000)
    team_capacity_points: dict[str, _Capacity] | None = None
    enforce_team_capacity: bool = True

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.budget_usd == 0 and self.name != UNCONSTRAINED:
            msg = "budget_usd must be > 0"
            raise ValueError(msg)
        if overlap := _overlap(self.mandatory, self.excluded):
            raise ValueError(overlap)
        return self


def _overlap(mandatory: Sequence[str], excluded: Sequence[str]) -> str | None:
    both = sorted(set(mandatory) & set(excluded))
    return f"candidate {both[0]} is both mandatory and excluded" if both else None


class PortfolioRow(_Frozen):
    """One candidate of a solved scenario (U04-77); `order_rank` is None iff not selected."""

    candidate_id: str
    selected: bool
    order_rank: int | None
    expected_impact_usd: Decimal
    flags: list[str]

    @model_validator(mode="after")
    def _rank(self) -> Self:
        if (self.order_rank is None) == self.selected:
            msg = "order_rank must be set exactly for selected rows"
            raise ValueError(msg)
        return self


class PortfolioResult(_Frozen):
    """`optimize_portfolio`'s result (U04-77, design 04 §3.1)."""

    scenario: str
    budget_usd: Decimal
    solver_status: solver.SolverStatus
    rows: list[PortfolioRow]
    selected: list[str]
    total_effort_usd: Decimal
    total_expected_impact_usd: Decimal
    binding_constraints: list[str]
    query_ids: list[str]
    flags: list[str]


def _validated(**fields: object) -> Scenario:
    try:
        return Scenario.model_validate(fields)
    except ValidationError as err:
        msg = f"invalid scenario: {err.errors()[0]['msg']}"
        raise ConfigError(msg) from err


def resolve_scenario(
    scenario: Scenario | str, portfolio: PortfolioConfig, total_effort_usd: Decimal, /
) -> Scenario:
    """The effective scenario of a Scenario, `unconstrained`, a configured name or a USD text,
    lists merged with the configured ones (U04-78); raises ConfigError."""
    if isinstance(scenario, Scenario):
        base = scenario
    elif scenario == UNCONSTRAINED:
        base = _validated(
            name=UNCONSTRAINED, budget_usd=total_effort_usd, enforce_team_capacity=False
        )
    elif named := [s for s in portfolio.scenarios if s.name == scenario]:
        base = _validated(
            name=named[0].name,
            budget_usd=named[0].budget_usd,
            enforce_team_capacity=portfolio.enforce_team_capacity,
        )
    elif _CUSTOM_BUDGET.fullmatch(scenario):
        base = _validated(name="custom_" + scenario.replace(".", "_"), budget_usd=scenario)
    else:
        msg = f"unknown scenario {scenario[:64]}"
        raise ConfigError(msg)
    mandatory = sorted({*base.mandatory, *portfolio.mandatory})
    excluded = sorted({*base.excluded, *portfolio.excluded})
    if overlap := _overlap(mandatory, excluded):
        raise ConfigError(overlap)
    return base.model_copy(update={"mandatory": mandatory, "excluded": excluded})


def _candidates(rq: RecordedQuery) -> list[solver.PortfolioCandidate]:
    """Solver candidates copied by column name from the input rows (U04-79 column types)."""
    names = [name for name, _ in rq.columns]
    out: list[solver.PortfolioCandidate] = []
    for row in rq.rows or []:
        rec: dict[str, Any] = dict(zip(names, row, strict=True))
        rec.update(descendants=tuple(rec["descendants"]), blockers=tuple(rec["blockers"]))
        out.append(solver.PortfolioCandidate(**rec))
    return out


def _input_query(
    con: duckdb.DuckDBPyConnection, cfg: HernessConfig, build: _Build, persist: bool
) -> RecordedQuery:
    """Render and run the recorded optimizer input query (U04-80 steps 1 and 4)."""
    build_id, started_at = build
    catalog = catalog_from_config(cfg)
    tz = cfg.weights.business_timezone
    as_of = resolve_as_of(started_at, tz, catalog.scoring.as_of)
    sc = StepContext(build_id, catalog, cfg.weights, as_of, tz, frozenset())
    rendered = render_named("portfolio_input", {}, sc.binds())
    params = {"bind": rendered.bind, "template": rendered.template}
    timeout = None if persist else catalog.defaults.compute_timeout_s
    producer: Producer | None = "score" if persist else None
    return run_recorded(con, rendered.sql, params, producer, build_id=build_id, timeout_s=timeout)


def _solve(
    weights: WeightsConfig, candidates: _Candidates, scenario: Scenario | str, query_id: str
) -> tuple[PortfolioResult, str]:
    """Resolve, solve, order and assemble the result (U04-80 steps 6-9) and its input digest."""
    pc = weights.portfolio
    total = sum((c.effort_cost_usd for c in candidates if c.effort_cost_usd), Decimal(0))
    sc = resolve_scenario(scenario, pc, total)
    capacity = weights.team_capacity_points_per_quarter
    caps = (sc.team_capacity_points or capacity.teams) if sc.enforce_team_capacity else None
    model: dict[str, Any] = {"candidates": candidates, "budget_usd": sc.budget_usd}
    model |= {"mandatory": frozenset(sc.mandatory), "excluded": frozenset(sc.excluded)}
    model |= {"capacities": caps, "capacity_default": capacity.default}
    model |= {"horizon_quarters": pc.horizon_quarters}
    outcome = solver.solve_portfolio(**model, solver=pc.solver)
    chosen = [c for c in candidates if c.candidate_id in outcome.selected]
    ranks, had_cycle = solver.order_selected(chosen)
    flags = set(outcome.flags) | ({"blocks_cycle"} if had_cycle else set())
    if outcome.status not in _SOLVED:
        flags.add("infeasible_mandatory")
        info = {"mandatory_count": len(sc.mandatory), "budget_usd": str(sc.budget_usd)}
        _log.warning("metrics.portfolio.infeasible", scenario=sc.name, **info)
    rows = [
        PortfolioRow(
            candidate_id=c.candidate_id,
            selected=c.candidate_id in ranks,
            order_rank=ranks.get(c.candidate_id),
            expected_impact_usd=c.expected_impact_usd,
            flags=list(outcome.row_flags[c.candidate_id]),
        )
        for c in sorted(candidates, key=lambda c: c.candidate_id)
    ]
    settings = {"scenario": sc, "portfolio": pc, "capacity": capacity}
    dump = {k: v.model_dump(mode="json") for k, v in settings.items()}
    inputs = ids.sha256_hex(ids.canonical_json({"input": query_id, **dump}))[:16]
    result = PortfolioResult(
        scenario=sc.name,
        budget_usd=sc.budget_usd,
        solver_status=outcome.status,
        rows=rows,
        selected=sorted(ranks, key=ranks.__getitem__),
        total_effort_usd=sum((c.effort_cost_usd or Decimal(0) for c in chosen), Decimal(0)),
        total_expected_impact_usd=sum((c.expected_impact_usd for c in chosen), Decimal(0)),
        binding_constraints=solver.binding_constraints(**model, selected=outcome.selected),
        query_ids=[query_id],
        flags=sorted(flags),
    )
    return result, inputs


def _persist(
    con: duckdb.DuckDBPyConnection, result: PortfolioResult, build_id: str, inputs: str
) -> None:
    """Rewrite this scenario's `score.portfolio` rows and record the read-back (step 10); the
    `inputs` digest keys the read-back, so other settings on the same build are a new query."""
    name = result.scenario
    con.execute("DELETE FROM score.portfolio WHERE scenario = ?", [name])
    if result.solver_status in _SOLVED:
        common = result.model_dump(include={"budget_usd", "solver_status", "query_ids"})
        rows = [{"scenario": name, **common, **row.model_dump()} for row in result.rows]
        con.executemany(_INSERT, rows)
    params = {
        "bind": {"scenario": name},
        "template": {"name": "portfolio_readback", "scenario": name, "inputs": inputs},
    }
    run_recorded(con, PORTFOLIO_READBACK_SQL, params, "score", build_id=build_id)


def _open(
    con: duckdb.DuckDBPyConnection | None, persist: bool
) -> AbstractContextManager[duckdb.DuckDBPyConnection]:
    """`con`, else the read-only CURRENT warehouse; persist=True never opens a file (C12)."""
    if persist and con is None:
        msg = "persist=True needs the build writer's connection"
        raise ConfigError(msg)
    return _connection(con)


def _checked_build(con: duckdb.DuckDBPyConnection, persist: bool, build_id: str | None) -> _Build:
    """`meta.build` (id, start) after the read-only and build_id preconditions (U04-80)."""
    mode = con.execute("SELECT current_setting('access_mode')").fetchone()
    if persist and mode is not None and str(mode[0]).lower() == "read_only":
        msg = "warehouse is read-only; use persist=False"
        raise ConfigError(msg)
    build = _read_build(con)
    if build_id is not None and build_id != build[0]:
        msg = "build_id mismatch"
        raise ConfigError(msg)
    return build


def optimize_portfolio(
    scenario: Scenario | str,
    /,
    *,
    persist: bool = True,
    build_id: str | None = None,
    run_id: str | None = None,
    con: duckdb.DuckDBPyConnection | None = None,
) -> PortfolioResult:
    """Solve one scenario on a build (persist=True) or the promoted warehouse (U04-80).

    Raises ConfigError (scenario, connection, read-only, build_id), QueryError and StoreBusy.
    Same scenario on the same build gives the same selection.
    """
    start = time.perf_counter()
    try:
        with _open(con, persist) as c:
            cfg = get_config()
            build = _checked_build(c, persist, build_id)
            rq = _input_query(c, cfg, build, persist)
            result, inputs = _solve(cfg.weights, _candidates(rq), scenario, rq.query_id)
            if persist:
                _persist(c, result, build[0], inputs)
    except ConfigError as err:
        name = scenario.name if isinstance(scenario, Scenario) else scenario[:64]
        _log.error("metrics.portfolio.rejected", scenario=name, reason=err.message)
        raise
    if not persist:
        record_evidence(rq.to_evidence(run_id))
    wall, status = time.perf_counter() - start, result.solver_status
    sizes = {"n_candidates": len(result.rows), "n_selected": len(result.selected)}
    event = {"scenario": result.scenario, "solver_status": status, "persist": persist}
    _log.info("metrics.portfolio.solved", **event, **sizes, wall_s=round(wall, 3))
    # T08-05: record_histogram is the interim sink until the ops metric writer lands.
    labels = {"status": status, "persist": str(persist).lower()}
    record_histogram(_SOLVE_SECONDS, wall, component="metrics", labels=labels)
    return result


def run_portfolio_step(con: duckdb.DuckDBPyConnection, sc: StepContext, /) -> StepResult:
    """Rewrite `score.portfolio` for the configured scenarios plus `unconstrained` (U04-81); a
    scenario's ConfigError becomes a warning. SchemaViolation when `score.funding` is missing."""
    if FUNDING_TABLE not in existing_tables(con):
        msg = "score.funding missing; run step funding first"
        raise SchemaViolation(msg)
    con.execute(_DDL)
    warnings: list[str] = []
    portfolio = sc.weights.portfolio
    mandatory = len(set(portfolio.mandatory))
    for name in [*(s.name for s in portfolio.scenarios), UNCONSTRAINED]:
        try:
            result = optimize_portfolio(name, persist=True, build_id=sc.build_id, con=con)
        except ConfigError as err:
            warnings.append(f"scenario {name}: {err.message}")
            continue
        if result.solver_status not in _SOLVED:
            status, budget = result.solver_status, result.budget_usd
            warnings.append(f"scenario {name}: {status}, mandatory={mandatory}, budget={budget}")
    row = con.execute(f"SELECT count(*) FROM {PORTFOLIO_TABLE}").fetchone()  # noqa: S608 - constant
    counts = {PORTFOLIO_TABLE: int(row[0]) if row else 0}
    return StepResult(row_counts=counts, warnings=warnings, flags=[], failed_checks=[])
