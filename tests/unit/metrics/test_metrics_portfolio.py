"""Tests for herness.metrics.portfolio (impl 04 U04-76 … U04-81; T04-20).

Each test builds a small funding graph on the empty `metrics_tiny` DDL (see
`tests/support/metrics_funding.py`), runs the real funding step, then the portfolio API.
Optimizer input (U04-79) on that graph, with `cluster_fix_weights()`:

| candidate        | effort | team | points | notes                                   |
|------------------|--------|------|--------|-----------------------------------------|
| E1 (PAY-1)       | 1000   | T1   | NULL   | parent of F5; blocks E2                 |
| E2 (PAY-2)       | 3000   | T1   | NULL   | blocked by E1                           |
| E3 (PAY-3)       | 3000   | T1   | 5      | blocked by the open story PAY-9         |
| E4 (PAY-4)       | NULL   | T1   | NULL   | no estimate                             |
| F5 (PAY-5)       | 500    | T1   | NULL   | child of E1                             |
| cluster_fix:C1   | 16000  | T2   | 26.67  | owner team of S2 via core.service_map   |

Σ effort = 23500. With budget 4000 the best set is {E1, E2} (impact 773 + 1546).
"""

import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import duckdb
import pytest
from structlog.testing import capture_logs
from tests.support.metrics_funding import (
    cluster_fix_weights,
    incident,
    item,
    member,
    mention,
    step_context,
    warehouse,
)
from tests.support.metrics_scoring import patches, save_as_file, small_catalog
from tests.support.metrics_tiny import BUILD_ID, shipped_catalog
from tests.support.ops_store import OpsStoreHandle

from herness.core.errors import ConfigError, SchemaViolation
from herness.metrics import compute, portfolio, scoring
from herness.metrics.evidence import IntoSpec, run_recorded
from herness.metrics.funding import run_funding_step
from herness.metrics.portfolio import (
    PortfolioResult,
    PortfolioRow,
    Scenario,
    optimize_portfolio,
    resolve_scenario,
    run_portfolio_step,
)
from herness.metrics.render import render_named
from herness.metrics.settings import ScenarioConfig, WeightsConfig
from herness.store.ops.evidence import get_evidence

pytestmark = pytest.mark.unit

TOTAL_EFFORT = Decimal(23500)
_LINKS: list[dict[str, object]] = [
    mention("PAY-1", "I1"),
    mention("PAY-2", "I2"),
    mention("PAY-3", "I3"),
    mention("PAY-5", "I1"),
    {"from_key": "PAY-1", "to_key": "PAY-2", "link_type": "blocks"},
    {"from_key": "PAY-9", "to_key": "PAY-3", "link_type": "blocks"},
]


def _rows(extra_links: Sequence[Mapping[str, object]] = ()) -> dict[str, list[dict[str, object]]]:
    return {
        "core.team": [
            {"team_id": "T1", "org_id": "O1", "active": True},
            {"team_id": "T2", "org_id": "O1", "active": True},
        ],
        "core.service_map": [
            {"service_id": "S2", "team_id": "T2", "role": "owner", "confidence": 1.0}
        ],
        "core.incident": [
            incident("I1"),
            incident("I2", minutes=120),
            incident("I3", minutes=30),
            incident("I6", service="S2"),
            incident("I7", service="S2", opened="2026-03-01 10:00:00-05"),
        ],
        "core.work_item": [
            item("E1", "PAY-1", estimate=1000),
            item("E2", "PAY-2", estimate=3000),
            item("E3", "PAY-3", points=5),
            item("E4", "PAY-4"),
            item("F5", "PAY-5", kind="feature", parent="PAY-1", estimate=500),
            item("S9", "PAY-9", kind="story", status="todo"),
            item("S8", "PAY-8", kind="story", parent="PAY-3", points=2, status="done"),
        ],
        "core.work_item_link": [*_LINKS, *extra_links],
        "enrich.cluster_member": [member("I6", "C1"), member("I7", "C1")],
    }


def _weights(**portfolio_update: object) -> WeightsConfig:
    w = cluster_fix_weights()
    return w.model_copy(update={"portfolio": w.portfolio.model_copy(update=portfolio_update)})


def _with_capacity(w: WeightsConfig, teams: dict[str, float]) -> WeightsConfig:
    cap = w.team_capacity_points_per_quarter.model_copy(update={"teams": teams})
    return w.model_copy(update={"team_capacity_points_per_quarter": cap})


type Build = Callable[..., duckdb.DuckDBPyConnection]


@pytest.fixture
def build(monkeypatch: pytest.MonkeyPatch, ops_store: OpsStoreHandle) -> Iterator[Build]:
    """Factory: a funded warehouse whose portfolio config is `weights` (patched into the API);
    a fresh ops store receives the persist=False evidence."""
    assert ops_store.db_path.is_file()
    opened: list[duckdb.DuckDBPyConnection] = []

    def make(
        weights: WeightsConfig | None = None, extra_links: Sequence[Mapping[str, object]] = ()
    ) -> duckdb.DuckDBPyConnection:
        w = weights if weights is not None else _weights()
        con = warehouse(_rows(extra_links), w)
        run_funding_step(con, step_context(w))
        cfg = SimpleNamespace(weights=w, metrics=shipped_catalog().config)
        monkeypatch.setattr(portfolio, "get_config", lambda: cfg)
        monkeypatch.setattr(portfolio, "catalog_from_config", lambda _cfg=None: shipped_catalog())
        opened.append(con)
        return con

    yield make
    for con in opened:
        con.close()


def _step(con: duckdb.DuckDBPyConnection, w: WeightsConfig | None = None) -> Any:
    return run_portfolio_step(con, step_context(w if w is not None else _weights()))


def _stored(con: duckdb.DuckDBPyConnection, scenario: str) -> list[tuple[Any, ...]]:
    return con.execute(
        "SELECT candidate_id, selected, order_rank, solver_status, flags, query_ids"
        " FROM score.portfolio WHERE scenario = ? ORDER BY candidate_id",
        [scenario],
    ).fetchall()


def _count(con: duckdb.DuckDBPyConnection, sql: str) -> int:
    row = con.execute(sql).fetchone()
    assert row is not None
    return int(row[0])


def _read_only_copy(con: duckdb.DuckDBPyConnection, path: Path) -> duckdb.DuckDBPyConnection:
    save_as_file(con, path)
    return duckdb.connect(str(path), read_only=True)


def _check_invariants(result: PortfolioResult, efforts: Mapping[str, Decimal | None]) -> None:
    """Selection within budget, no duplicates, ranks 1..n in `selected` order."""
    chosen = [r for r in result.rows if r.selected]
    assert sorted(r.candidate_id for r in result.rows) == [r.candidate_id for r in result.rows]
    assert len(set(result.selected)) == len(result.selected) == len(chosen)
    assert {r.candidate_id for r in chosen} == set(result.selected)
    ranks = {r.candidate_id: r.order_rank for r in chosen}
    assert [ranks[c] for c in result.selected] == list(range(1, len(chosen) + 1))
    spent = sum((efforts[c] or Decimal(0) for c in result.selected), Decimal(0))
    assert spent == result.total_effort_usd <= result.budget_usd
    assert all(r.expected_impact_usd >= 0 for r in result.rows)


def _efforts(con: duckdb.DuckDBPyConnection) -> dict[str, Decimal | None]:
    rows = con.execute("SELECT candidate_id, effort_cost_usd FROM score.funding").fetchall()
    return {str(cid): effort for cid, effort in rows}


# --- UT04-101 (U04-79 input rows) and U04-77 -----------------------------------------------------


def test_ut04_101_portfolio_input_rows(build: Build) -> None:
    """UT04-101 the U04-79 input rows: teams, points, descendants, blockers, open non-candidate
    blockers and expected impact = CAST(addressable x confidence x strategic_weight)."""
    con = build()
    rendered = render_named("portfolio_input", {}, step_context(_weights()).binds())
    rows = con.execute(rendered.sql, rendered.bind).fetchall()
    got = {r[0]: (r[1], r[2], r[3], r[6], r[7], r[8], r[9]) for r in rows}
    assert [r[0] for r in rows] == sorted(got)
    assert got == {
        "E1": ("epic", "T1", Decimal("1000.00"), None, ["F5"], [], 0),
        "E2": ("epic", "T1", Decimal("3000.00"), None, [], ["E1"], 0),
        "E3": ("epic", "T1", Decimal("3000.00"), 5.0, [], [], 1),
        "E4": ("epic", "T1", None, None, [], [], 0),
        "F5": ("feature", "T1", Decimal("500.00"), None, [], [], 0),
        "cluster_fix:C1": ("cluster_fix", "T2", Decimal("16000.00"), 160 / 6, [], [], 0),
    }
    impact = con.execute(
        "SELECT candidate_id, CAST(addressable_pain_usd * confidence * strategic_weight"
        " AS DECIMAL(18, 2)) FROM score.funding"
    ).fetchall()
    assert {r[0]: r[4] for r in rows} == dict(impact)


def test_ut04_101_rendered_input_sql_under_evidence_cap(build: Build) -> None:
    """UT04-101 the recorded input SQL fits the 20,000-character Evidence cap."""
    con = build()
    result = optimize_portfolio("4000", persist=False, con=con)
    row = con.execute("SELECT sql FROM meta.evidence").fetchall()
    assert all(r[0] != "" for r in row)
    rendered = render_named("portfolio_input", {}, step_context(_weights()).binds())
    assert len(rendered.sql) < 20_000
    assert result.query_ids[0].startswith("q_")


def test_ut04_101_row_order_rank_invariant() -> None:
    """UT04-101 PortfolioRow: order_rank is set iff the row is selected (U04-77)."""
    PortfolioRow(
        candidate_id="a", selected=True, order_rank=1, expected_impact_usd=Decimal(1), flags=[]
    )
    PortfolioRow(
        candidate_id="a", selected=False, order_rank=None, expected_impact_usd=Decimal(1), flags=[]
    )
    with pytest.raises(ValueError, match="order_rank"):
        PortfolioRow(
            candidate_id="a",
            selected=True,
            order_rank=None,
            expected_impact_usd=Decimal(1),
            flags=[],
        )
    with pytest.raises(ValueError, match="order_rank"):
        PortfolioRow(
            candidate_id="a", selected=False, order_rank=2, expected_impact_usd=Decimal(1), flags=[]
        )


# --- UT04-103 Scenario and resolve_scenario -------------------------------------------------------


def test_ut04_103_scenario_fields() -> None:
    """UT04-103 Scenario: frozen, forbid, strict; budget accepts int, Decimal and decimal text."""
    assert Scenario(name="a_1", budget_usd=5).budget_usd == Decimal(5)  # type: ignore[arg-type]
    assert Scenario(name="a", budget_usd="12.50").budget_usd == Decimal("12.50")  # type: ignore[arg-type]
    s = Scenario(name="a", budget_usd=Decimal(1))
    assert (s.mandatory, s.excluded, s.team_capacity_points, s.enforce_team_capacity) == (
        [],
        [],
        None,
        True,
    )
    bad: list[dict[str, Any]] = [
        {"name": "A", "budget_usd": 1},
        {"name": "x" * 65, "budget_usd": 1},
        {"name": "a", "budget_usd": 1.5},
        {"name": "a", "budget_usd": True},
        {"name": "a", "budget_usd": "1e3"},
        {"name": "a", "budget_usd": -1},
        {"name": "a", "budget_usd": 0},
        {"name": "a", "budget_usd": 1, "mandatory": ["x"], "excluded": ["x"]},
        {"name": "a", "budget_usd": 1, "bogus": 1},
        {"name": "a", "budget_usd": 1, "team_capacity_points": {"T1": 0}},
    ]
    for fields in bad:
        with pytest.raises(ValueError):  # noqa: PT011 - pydantic ValidationError
            Scenario.model_validate(fields)
    assert Scenario(name="unconstrained", budget_usd=0).budget_usd == 0  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="frozen"):
        s.name = "b"  # type: ignore[misc]


def test_ut04_103_resolve_named_custom_and_object() -> None:
    """UT04-103 a configured name, a USD text and a Scenario object; lists merged with config."""
    pc = _weights(mandatory=["E1"], excluded=["F5"], enforce_team_capacity=False).portfolio
    named = resolve_scenario("base", pc, TOTAL_EFFORT)
    assert named == Scenario(
        name="base",
        budget_usd=Decimal(2000000),
        mandatory=["E1"],
        excluded=["F5"],
        enforce_team_capacity=False,
    )
    custom = resolve_scenario("1500000", pc, TOTAL_EFFORT)
    assert (custom.name, custom.budget_usd, custom.enforce_team_capacity) == (
        "custom_1500000",
        Decimal(1500000),
        True,
    )
    cents = resolve_scenario("1500000.25", pc, TOTAL_EFFORT)
    assert (cents.name, cents.budget_usd) == ("custom_1500000_25", Decimal("1500000.25"))
    own = Scenario(name="mine", budget_usd=Decimal(9), mandatory=["E2", "E1"], excluded=["E3"])
    merged = resolve_scenario(own, pc, TOTAL_EFFORT)
    assert (merged.name, merged.mandatory, merged.excluded) == ("mine", ["E1", "E2"], ["E3", "F5"])
    unconstrained = resolve_scenario("unconstrained", pc, TOTAL_EFFORT)
    assert (unconstrained.budget_usd, unconstrained.enforce_team_capacity) == (TOTAL_EFFORT, False)


@pytest.mark.parametrize("name", ["bogus", "1.234", "", "12345678901234", "-5", "Base"])
def test_ut04_103_unknown_scenario(name: str) -> None:
    """UT04-103 an unknown name or a malformed USD text is ConfigError("unknown scenario ...")."""
    with pytest.raises(ConfigError, match=r"^unknown scenario "):
        resolve_scenario(name, _weights().portfolio, TOTAL_EFFORT)


def test_ut04_103_overlap_and_invalid_custom_budget() -> None:
    """UT04-103 own mandatory vs configured excluded overlap, and a zero custom budget."""
    pc = _weights(excluded=["E1"]).portfolio
    own = Scenario(name="mine", budget_usd=Decimal(9), mandatory=["E1"])
    with pytest.raises(ConfigError, match=r"^candidate E1 is both mandatory and excluded$"):
        resolve_scenario(own, pc, TOTAL_EFFORT)
    with pytest.raises(ConfigError, match="budget_usd must be > 0"):
        resolve_scenario("0", pc, TOTAL_EFFORT)


# --- ST04-08 bounds (TH04-08) ---------------------------------------------------------------------


def test_st04_08_huge_budget_and_lists_rejected() -> None:
    """ST04-08 a 1e13 budget and 2000 mandatory IDs are rejected by Scenario."""
    with pytest.raises(ValueError, match="less than or equal"):
        Scenario(name="a", budget_usd=Decimal("1e13"))
    with pytest.raises(ValueError, match="at most 1000"):
        Scenario(name="a", budget_usd=Decimal(1), mandatory=[f"c{i}" for i in range(2000)])
    with pytest.raises(ValueError, match="at most 1000"):
        Scenario(name="a", budget_usd=Decimal(1), excluded=[f"c{i}" for i in range(2000)])
    with pytest.raises(ValueError, match="at most 256"):
        Scenario(name="a", budget_usd=Decimal(1), mandatory=["x" * 257])
    assert Scenario(name="a", budget_usd=Decimal("1e12")).budget_usd == Decimal("1e12")
    with pytest.raises(ConfigError, match="invalid scenario"):
        resolve_scenario("9999999999999", _weights().portfolio, TOTAL_EFFORT)


@pytest.mark.parametrize("capacity", [float("inf"), float("nan"), 1e13, 1e30, 0.0, -1.0])
def test_st04_08_capacity_must_be_finite_and_bounded(capacity: float) -> None:
    """ST04-08 team capacities must be finite, > 0 and <= 1e12 (CP-SAT overflow, TH04-08)."""
    with pytest.raises(ValueError):  # noqa: PT011 - pydantic ValidationError
        Scenario(name="a", budget_usd=Decimal(1), team_capacity_points={"T1": capacity})
    assert Scenario(name="a", budget_usd=Decimal(1), team_capacity_points={"T1": 1e12})


# --- UT04-107, ST04-09 read-only ------------------------------------------------------------------


def test_ut04_107_persist_on_read_only_connection(build: Build, tmp_path: Path) -> None:
    """UT04-107 persist=True on a read-only connection is a ConfigError; nothing runs."""
    ro = _read_only_copy(build(), tmp_path / "wh.duckdb")
    try:
        msg = r"^warehouse is read-only; use persist=False$"
        with capture_logs() as logs, pytest.raises(ConfigError, match=msg):
            optimize_portfolio("base", persist=True, con=ro)
        assert [e["event"] for e in logs] == ["metrics.portfolio.rejected"]
    finally:
        ro.close()


def test_st04_09_read_only_and_into_allowlist_refused(build: Build, tmp_path: Path) -> None:
    """ST04-09 persist=True on a read-only warehouse and IntoSpec("core.incident") are both
    refused before any write."""
    ro = _read_only_copy(build(), tmp_path / "wh.duckdb")
    try:
        evidence = _count(ro, "SELECT count(*) FROM meta.evidence")
        with pytest.raises(ConfigError, match="read-only"):
            optimize_portfolio(Scenario(name="x", budget_usd=Decimal(5)), con=ro)
        assert _count(ro, "SELECT count(*) FROM meta.evidence") == evidence
    finally:
        ro.close()
    with pytest.raises(ConfigError, match="not writable by metrics"):
        IntoSpec("core.incident", "replace", "query_id")


# --- UT04-108 persist=False -----------------------------------------------------------------------


def test_ut04_108_persist_false_records_ops_evidence(
    build: Build, tmp_path: Path, ops_store: OpsStoreHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT04-108 persist=False on a read-only file: the input query is in ops evidence (with
    run_id), the warehouse file is unchanged, and the connection opened is closed."""
    path = tmp_path / "wh.duckdb"
    save_as_file(build(), path)
    before = path.read_bytes()
    opened: list[duckdb.DuckDBPyConnection] = []

    def fake_open(build_id: str | None) -> duckdb.DuckDBPyConnection:
        assert build_id is None
        opened.append(duckdb.connect(str(path), read_only=True))
        return opened[-1]

    monkeypatch.setattr(compute, "open_readonly", fake_open)
    result = optimize_portfolio("4000", persist=False, run_id="run_1")
    assert len(opened) == 1
    with pytest.raises(duckdb.ConnectionException):
        opened[0].execute("SELECT 1")
    ev = get_evidence(result.query_ids[0])
    assert ev is not None
    assert (ev.run_id, ev.build_id, ev.row_count) == ("run_1", BUILD_ID, 6)
    assert path.read_bytes() == before
    check = duckdb.connect(str(path), read_only=True)
    try:
        tables = check.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = 'portfolio'"
        ).fetchone()
        assert tables == (0,)
    finally:
        check.close()
    assert result.selected == ["E1", "E2"]


# --- UT04-109 unconstrained -----------------------------------------------------------------------


def test_ut04_109_unconstrained_budget_and_capacity(build: Build) -> None:
    """UT04-109 unconstrained: budget = Σ effort and capacity ignored; a configured scenario
    with the same tiny T2 capacity leaves the cluster fix out (capacity:T2 binds)."""
    w = _with_capacity(_weights(), {"T2": 1.0})
    con = build(w)
    _step(con, w)
    free = optimize_portfolio("unconstrained", con=con)
    assert (free.scenario, free.budget_usd) == ("unconstrained", TOTAL_EFFORT)
    assert free.solver_status == "OPTIMAL"
    assert free.selected == ["E1", "E2", "cluster_fix:C1", "E3"]
    assert not any(c.startswith("capacity:") for c in free.binding_constraints)
    assert all("no_points" not in r.flags for r in free.rows)
    _check_invariants(free, _efforts(con))
    capped = optimize_portfolio("lean", con=con)
    assert "cluster_fix:C1" not in capped.selected
    assert "capacity:T2" in capped.binding_constraints
    assert {r.candidate_id: r.flags for r in capped.rows}["E1"] == ["no_points"]


def test_ut04_109_run_scoring_wires_portfolio_step(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT04-109 run_scoring(steps=["portfolio"]) runs the wired step: score.portfolio holds the
    configured scenarios plus unconstrained."""
    for obj, name, value in patches(small_catalog()):
        if name != "_STEP_FUNCS":
            monkeypatch.setattr(obj, name, value)
    assert scoring._STEP_FUNCS["portfolio"] is run_portfolio_step
    cfg = scoring.get_config()
    monkeypatch.setattr(portfolio, "get_config", lambda: cfg)
    monkeypatch.setattr(portfolio, "catalog_from_config", lambda _cfg=None: shipped_catalog())
    con = warehouse(_rows(), cfg.weights)
    try:
        sc = step_context(cfg.weights)
        run_funding_step(con, sc)
        report = scoring.run_scoring(BUILD_ID, steps=["portfolio"], con=con)
        assert report.steps_done == ["validate", "portfolio"]
        names = con.execute("SELECT DISTINCT scenario FROM score.portfolio ORDER BY 1").fetchall()
        assert [n[0] for n in names] == ["base", "lean", "stretch", "unconstrained"]
        rows = _count(con, "SELECT count(*) FROM score.portfolio")
        assert report.row_counts == {"score.portfolio": rows}
        assert rows == 4 * 5  # shipped cluster-fix thresholds: no cluster_fix:C1
    finally:
        con.close()


# --- UT04-122 no connection -----------------------------------------------------------------------


def test_ut04_122_persist_without_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT04-122 persist=True with build_id and no con: ConfigError; no file opened."""

    def no_open(*args: object, **kwargs: object) -> None:
        raise AssertionError

    monkeypatch.setattr(compute, "open_readonly", no_open)
    monkeypatch.setattr(portfolio, "get_config", no_open)
    msg = r"^persist=True needs the build writer's connection$"
    with pytest.raises(ConfigError, match=msg):
        optimize_portfolio("base", persist=True, build_id=BUILD_ID)


# --- ST04-14 build_id -----------------------------------------------------------------------------


@pytest.mark.parametrize("persist", [True, False])
def test_st04_14_build_id_of_another_build(build: Build, persist: bool) -> None:
    """ST04-14 a build_id that is not meta.build's is ConfigError("build_id mismatch") before
    any query is recorded or row written."""
    con = build()
    _step(con)
    stored = _count(con, "SELECT count(*) FROM score.portfolio")
    evidence = _count(con, "SELECT count(*) FROM meta.evidence")
    with pytest.raises(ConfigError, match=r"^build_id mismatch$"):
        optimize_portfolio("base", persist=persist, build_id="20990101-000000-OTHER1", con=con)
    assert _count(con, "SELECT count(*) FROM score.portfolio") == stored
    assert _count(con, "SELECT count(*) FROM meta.evidence") == evidence
    assert optimize_portfolio("base", persist=persist, build_id=BUILD_ID, con=con).selected


# --- U04-80 / U04-81 behaviour (controller checks) ------------------------------------------------


def test_cv_t04_20_persist_writes_rows_and_evidence(build: Build) -> None:
    """CV-T04-20 persist=True: score.portfolio holds exactly this scenario's rows with the
    input query_id; the input and the read-back are in meta.evidence (producer score)."""
    con = build()
    _step(con)
    result = optimize_portfolio(
        Scenario(name="lean", budget_usd=Decimal(4000), team_capacity_points={"T1": 50}), con=con
    )
    assert result.selected == ["E1", "E2"]
    assert result.binding_constraints == ["budget"]
    assert result.total_effort_usd == Decimal(4000)
    stored = _stored(con, "lean")
    assert [r[0] for r in stored] == [r.candidate_id for r in result.rows]
    assert [(r[1], r[2]) for r in stored] == [(r.selected, r.order_rank) for r in result.rows]
    assert {r[3] for r in stored} == {"OPTIMAL"}
    assert all(r[5] == result.query_ids for r in stored)
    producers = con.execute(
        "SELECT producer FROM meta.evidence WHERE query_id = ?", result.query_ids
    ).fetchall()
    assert producers == [("score",)]
    readback = con.execute(
        "SELECT row_count FROM meta.evidence WHERE sql LIKE '%FROM score.portfolio WHERE%'"
    ).fetchall()
    assert (6,) in readback
    _check_invariants(result, _efforts(con))
    assert {r.candidate_id: r.flags for r in result.rows}["E3"] == ["blocked_by_noncandidate"]
    assert {r.candidate_id: r.flags for r in result.rows}["E4"] == ["no_estimate"]


def test_cv_t04_20_deterministic_and_persist_modes_agree(build: Build) -> None:
    """CV-T04-20 same scenario on the same build: identical results over 5 runs, and
    persist=False selects what persist=True stored."""
    con = build()
    _step(con)
    results = [optimize_portfolio("4000", con=con) for _ in range(5)]
    assert all(r == results[0] for r in results)
    adhoc = optimize_portfolio("4000", persist=False, con=con)
    assert adhoc.model_dump() == results[0].model_dump()


def test_cv_t04_20_budget_sweep_invariants(build: Build) -> None:
    """CV-T04-20 no scenario exceeds its budget; no duplicated or negative allocation."""
    con = build()
    efforts = _efforts(con)
    for budget in ("1", "499", "500", "1500", "4000", "7000", "23499", "23500", "1000000"):
        result = optimize_portfolio(budget, persist=False, con=con)
        assert result.solver_status == "OPTIMAL"
        _check_invariants(result, efforts)
        for row in result.rows:
            if row.candidate_id in ("E2",) and row.selected:
                assert "E1" in result.selected


def test_ut04_105_step_infeasible_mandatory_and_rejected_scenarios(build: Build) -> None:
    """UT04-105 step: a mandatory set above a scenario's budget gives INFEASIBLE, no rows and a
    warning; a ConfigError scenario becomes a warning and the loop continues."""
    scenarios = [
        ScenarioConfig(name="tight", budget_usd=Decimal(5000)),
        ScenarioConfig(name="lean", budget_usd=Decimal(1000000)),
    ]
    w = _weights(scenarios=scenarios, mandatory=["cluster_fix:C1"])
    con = build(w)
    with capture_logs() as logs:
        result = _step(con, w)
    assert result.warnings == ["scenario tight: INFEASIBLE, mandatory=1, budget=5000"]
    assert _stored(con, "tight") == []
    assert len(_stored(con, "lean")) == len(_stored(con, "unconstrained")) == 6
    events = [e for e in logs if e["event"] == "metrics.portfolio.infeasible"]
    assert events[0]["mandatory_count"] == 1
    tight = optimize_portfolio("tight", persist=False, con=con)
    assert (tight.solver_status, tight.selected, tight.flags) == (
        "INFEASIBLE",
        [],
        ["infeasible_mandatory"],
    )
    bad = _weights(mandatory=["E4"])
    con2 = build(bad)
    result = _step(con2, bad)
    no_effort = "mandatory candidate E4 has no effort estimate"
    assert result.warnings == [
        f"scenario {n}: {no_effort}" for n in ("lean", "base", "stretch", "unconstrained")
    ]
    assert result.row_counts == {"score.portfolio": 0}


def test_cv_t04_20_step_needs_funding_and_flags_cycles(build: Build) -> None:
    """CV-T04-20 the step raises SchemaViolation without score.funding; a blocks cycle among
    selected candidates is ordered and flagged blocks_cycle."""
    con = build(extra_links=[{"from_key": "PAY-2", "to_key": "PAY-1", "link_type": "blocks"}])
    result = optimize_portfolio("unconstrained", persist=False, con=con)
    assert "blocks_cycle" in result.flags
    assert {"E1", "E2"} <= set(result.selected)
    con.execute("DROP TABLE score.funding")
    with pytest.raises(SchemaViolation, match=r"score\.funding"):
        _step(con)


def test_cv_t04_20_solved_log_event(build: Build) -> None:
    """CV-T04-20 metrics.portfolio.solved carries scenario, status, counts, wall time, persist."""
    con = build()
    with capture_logs() as logs:
        optimize_portfolio("4000", persist=False, con=con)
    solved = [e for e in logs if e["event"] == "metrics.portfolio.solved"]
    assert len(solved) == 1
    assert {k: solved[0][k] for k in ("scenario", "solver_status", "n_candidates", "n_selected")}
    assert (solved[0]["scenario"], solved[0]["n_candidates"], solved[0]["persist"]) == (
        "custom_4000",
        6,
        False,
    )
    assert isinstance(solved[0]["wall_s"], float)


def test_cv_t04_20_rescore_after_funding_change(
    build: Build, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CV-T04-20 (U04-80 step 4 amendment) re-scoring the same build after a weights change
    that alters score.funding records a new input query keyed by the funding query ids;
    identical inputs keep the same input query_id."""
    w = _weights()
    con = build(w)
    _step(con, w)
    before = optimize_portfolio("unconstrained", con=con)
    assert before.budget_usd == TOTAL_EFFORT
    rate = w.cost_per_engineer_hour
    w2 = w.model_copy(
        update={"cost_per_engineer_hour": rate.model_copy(update={"value": rate.value * 2})}
    )
    run_funding_step(con, step_context(w2))
    cfg = SimpleNamespace(weights=w2, metrics=shipped_catalog().config)
    monkeypatch.setattr(portfolio, "get_config", lambda: cfg)
    _step(con, w2)
    after = optimize_portfolio("unconstrained", con=con)
    efforts = _efforts(con)
    assert after.budget_usd == sum((e or Decimal(0) for e in efforts.values()), Decimal(0))
    assert after.budget_usd != before.budget_usd
    assert after.query_ids != before.query_ids
    _check_invariants(after, efforts)
    stored = _stored(con, "unconstrained")
    assert [(r[0], r[1], r[2]) for r in stored] == [
        (r.candidate_id, r.selected, r.order_rank) for r in after.rows
    ]
    assert all(r[5] == after.query_ids for r in stored)
    upstream = con.execute(
        "SELECT DISTINCT unnest(query_ids) AS q FROM score.funding ORDER BY q"
    ).fetchall()
    params = con.execute(
        "SELECT params FROM meta.evidence WHERE query_id = ?", after.query_ids
    ).fetchone()
    assert params is not None
    assert json.loads(params[0])["template"]["upstream"] == [u[0] for u in upstream]
    _step(con, w2)
    assert optimize_portfolio("unconstrained", con=con).query_ids == after.query_ids


@pytest.mark.parametrize("persist", [True, False])
def test_cv_t04_20_input_query_timeout(
    build: Build, monkeypatch: pytest.MonkeyPatch, persist: bool
) -> None:
    """CV-T04-20 the input query runs with the configured compute timeout for persist=False
    and with none (the build's own budget) for persist=True."""
    con = build()
    _step(con)
    seen: list[tuple[object, object]] = []
    real = run_recorded

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append((args[2]["template"]["name"], kwargs.get("timeout_s")))
        return real(*args, **kwargs)

    monkeypatch.setattr(portfolio, "run_recorded", spy)
    optimize_portfolio("4000", persist=persist, con=con)
    timeout = shipped_catalog().defaults.compute_timeout_s
    assert seen[0] == ("portfolio_input", None if persist else timeout)
