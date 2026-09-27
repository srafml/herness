"""Tests for herness.metrics.compute (impl 04 U04-49 … U04-53) and metrics #1-#4 (U04-48).

Per-metric tests (UT04-36 … UT04-39) run every declared grain on `metrics_tiny`: incidents I1-I3
of team T1 (org O2 under O1), service S1 and cluster C1, resolved in 2, 4 and 6 hours; I4 is
canceled (excluded). The build started 2026-04-01 06:00 UTC, so `as_of` is 2026-04-01 in the
business timezone and every incident falls in quarter 2026Q1 (design 04 §10.1).
"""

import datetime
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import duckdb
import pytest
from freezegun import freeze_time
from pydantic import ValidationError
from structlog.testing import capture_logs
from tests.support.metrics_render import MTTR_SQL, metric
from tests.support.metrics_tiny import (
    BUILD_ID,
    build_metrics_tiny,
    patch_facts_config,
    shipped_catalog,
    tiny_weights,
)

from herness.core.errors import ConfigError, QueryError, SchemaViolation, ToolInputError
from herness.metrics import compute
from herness.metrics.catalog import MetricCatalog
from herness.metrics.compute import (
    MetricResult,
    MetricRow,
    compute_metric,
    metric_series,
    validate_metric_request,
)
from herness.metrics.evidence import RecordedQuery
from herness.metrics.facts import materialize_facts
from herness.metrics.settings import MetricDef

pytestmark = pytest.mark.unit

Q1 = datetime.date(2026, 1, 1)
GRAIN_IDS = {"service": ["S1"], "team": ["T1"], "org": ["O1", "O2"], "cluster": ["C1"]}
ALL_FILTERS = {
    "priority": [1, 2, 3],
    "service_id": ["S1"],
    "team_id": ["T1"],
    "org_id": ["O1"],
    "cluster_id": ["C1"],
}
SQL_CAP = 20_000  # Evidence.sql limit (to_evidence)
INJECTIONS = (
    "S1' OR '1'='1",
    "x'); DROP TABLE metrics.incident_fact; --",
    "$window_start",
    "{{ p('tz') }}",
)
SLOW_SQL = MTTR_SQL.replace(
    "{{ entity_join('incident', 'f') }}\n",
    "{{ entity_join('incident', 'f') }} CROSS JOIN range(4000000000) r\n",
)


def _catalog(*extra: MetricDef, timeout: float | None = None, **min_n: int) -> MetricCatalog:
    """The shipped catalog plus `extra` entries; `timeout` bypasses the >= 1 s config floor."""
    cfg = shipped_catalog(**min_n).config
    if extra:
        cfg = cfg.model_copy(update={"metrics": [*cfg.metrics, *extra]})
    if timeout is not None:
        defaults = cfg.defaults.model_copy(update={"compute_timeout_s": timeout})
        cfg = cfg.model_copy(update={"defaults": defaults})
    return MetricCatalog(cfg)


def _use(monkeypatch: pytest.MonkeyPatch, catalog: MetricCatalog) -> None:
    monkeypatch.setattr(compute, "catalog_from_config", lambda: catalog)


@pytest.fixture
def tiny(monkeypatch: pytest.MonkeyPatch) -> Iterator[duckdb.DuckDBPyConnection]:
    """`metrics_tiny` with materialized facts; compute reads the shipped catalog."""
    patch_facts_config(monkeypatch)
    con = build_metrics_tiny()
    with freeze_time("2026-04-01 06:30:00"):
        materialize_facts(con, BUILD_ID)
    cfg = SimpleNamespace(weights=tiny_weights())
    monkeypatch.setattr(compute, "get_config", lambda: cfg)
    _use(monkeypatch, shipped_catalog())
    yield con
    con.close()


@pytest.fixture
def counters(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, str]]]:
    calls: list[tuple[str, dict[str, str]]] = []

    def record(name: str, value: float = 1.0, **kw: Any) -> None:
        calls.append((name, dict(kw["labels"])))

    monkeypatch.setattr(compute, "record_counter", record)
    return calls


def _values(result: MetricResult) -> list[tuple[str, datetime.date, float | None]]:
    return [(r.entity_id, r.period_start, r.value) for r in result.rows]


def _check_grains(
    con: duckdb.DuckDBPyConnection,
    name: str,
    value: float,
    num: float | None,
    den: float | None,
    n: int = 3,
) -> None:
    """Every declared grain at `quarter`: one Q1 row per entity with the expected columns."""
    for grain, ids in GRAIN_IDS.items():
        result = compute_metric(name, grain, None, "quarter", con=con)  # type: ignore[arg-type]
        assert [(r.entity_id, r.period_start) for r in result.rows] == [(i, Q1) for i in ids]
        for row in result.rows:
            assert (row.value, row.numerator, row.denominator) == (value, num, den)
            assert row.sample_size == n
            assert row.flags == []
        assert result.row_count == len(ids)
        evidence = result.recorded().to_evidence(None)
        assert evidence.query_id == result.query_id
        filtered = compute_metric(name, grain, None, "quarter", ALL_FILTERS, con=con)  # type: ignore[arg-type]
        assert len(filtered.sql) < SQL_CAP


# --- UT04-36 … UT04-39 metrics #1-#4 -----------------------------------------------------------


def test_ut04_36_incident_count_every_grain(tiny: duckdb.DuckDBPyConnection) -> None:
    """UT04-36 incident_count = 3 (I4 canceled is excluded) at every declared grain."""
    _check_grains(tiny, "incident_count", 3.0, 3.0, None)
    week = compute_metric("incident_count", "team", None, "week", con=tiny)
    assert _values(week) == [
        ("T1", datetime.date(2026, 1, 5), 1.0),
        ("T1", datetime.date(2026, 1, 19), 1.0),
        ("T1", datetime.date(2026, 2, 23), 1.0),
    ]


def test_ut04_37_p1p2_count_every_grain(tiny: duckdb.DuckDBPyConnection) -> None:
    """UT04-37 p1p2_count = 2 (I1 P1, I3 P2; I2 is P3) at every declared grain."""
    _check_grains(tiny, "p1p2_count", 2.0, 2.0, None, n=2)


def test_ut04_38_mttr_hours_every_grain(
    tiny: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT04-38 mttr_hours = 4.0 (sum 12 over 3) at every grain; the shipped minimum of 10 is
    lowered to the fixture's 3 resolved incidents."""
    _use(monkeypatch, _catalog(mttr_hours=3))
    _check_grains(tiny, "mttr_hours", 4.0, 12.0, 3.0)


def test_ut04_38_acceptance_mttr_team_quarter(
    tiny: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT04-38 acceptance: compute_metric("mttr_hours", "team", None, "quarter") gives 4.0."""
    _use(monkeypatch, _catalog(mttr_hours=3))
    result = compute_metric("mttr_hours", "team", None, "quarter", con=tiny)
    assert _values(result) == [("T1", Q1, 4.0)]
    assert (result.unit, result.better, result.flags) == ("hours", "lower", [])


def test_ut04_39_mttr_p50_hours_every_grain(
    tiny: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT04-39 mttr_p50_hours = median(2, 4, 6) = 4.0 at every grain; numerator NULL."""
    _use(monkeypatch, _catalog(mttr_p50_hours=3))
    _check_grains(tiny, "mttr_p50_hours", 4.0, None, 3.0)


# --- UT04-64 MetricRow, MetricResult, insufficient sample -------------------------------------


def test_ut04_64_below_min_sample_value_null(tiny: duckdb.DuckDBPyConnection) -> None:
    """UT04-64 shipped min_sample_size 10 > 3 resolved: value NULL, flag insufficient_sample,
    numerator and denominator still reported."""
    result = compute_metric("mttr_hours", "team", None, "quarter", con=tiny)
    (row,) = result.rows
    assert (row.value, row.numerator, row.denominator, row.sample_size) == (None, 12.0, 3.0, 3)
    assert row.flags == ["insufficient_sample"]
    assert result.flags == []


def _row(**kw: Any) -> dict[str, Any]:
    base = {
        "entity_id": "T1",
        "period_start": Q1,
        "value": 1.0,
        "numerator": 1.0,
        "denominator": None,
        "sample_size": 1,
        "flags": ["estimate", "insufficient_sample"],
    }
    return base | kw


def test_ut04_64_metric_row_and_result_models(tiny: duckdb.DuckDBPyConnection) -> None:
    """UT04-64 MetricRow and MetricResult are frozen, strict and closed; flags from the
    vocabulary and sorted; row_count equals len(rows); recorded() needs a computed result."""
    row = MetricRow.model_validate(_row())
    with pytest.raises(ValidationError):
        row.value = 2.0  # type: ignore[misc]
    for bad in (
        _row(flags=["made_up"]),
        _row(flags=["insufficient_sample", "estimate"]),
        _row(sample_size=-1),
        _row(extra=1),
        _row(value="1"),
    ):
        with pytest.raises(ValidationError):
            MetricRow.model_validate(bad)
    result = compute_metric("incident_count", "team", None, "quarter", con=tiny)
    data = result.model_dump()
    with pytest.raises(ValidationError, match="row_count"):
        MetricResult.model_validate(data | {"row_count": 5})
    copy = MetricResult.model_validate(data | {"rows": [row]})
    with pytest.raises(ConfigError, match="not produced by compute_metric"):
        copy.recorded()


# --- UT04-65 request validation --------------------------------------------------------------


def _enum_metric() -> MetricDef:
    return metric(name="enum_probe", filters=["severity", "change_type", "work_item_type"])


@pytest.mark.parametrize(
    ("args", "match"),
    [
        (("nope", "team", None, "week", None), "unknown metric nope; known: .*incident_count"),
        (("disabled_probe", "team", None, "week", None), "metric disabled_probe is disabled"),
        (("mttr_hours", "team", None, "day", None), "bad period day; allowed: week, month, q"),
        (("mttr_hours", "work_item", None, "week", None), "grain work_item not supported by m"),
        (("mttr_hours", "team", [], "week", None), "entity_ids must be null or non-empty"),
        (("mttr_hours", "team", "T1", "week", None), "entity_ids must be null or a list"),
        (("mttr_hours", "team", ["x"] * 501, "week", None), "at most 500 entity_ids"),
        (("mttr_hours", "team", ["a\nb"], "week", None), "1-256 printable characters"),
        (("mttr_hours", "team", ["a" * 257], "week", None), "1-256 printable characters"),
        (("mttr_hours", "team", [7], "week", None), "1-256 printable characters"),
        (("mttr_hours", "team", None, "week", ["priority"]), "filters must be null or a mapping"),
        (("mttr_hours", "team", None, "week", {"severity": "x"}), "filter severity not allowed"),
        (("mttr_hours", "team", None, "week", {1: "x"}), "filter int not allowed for mttr"),
        (("mttr_hours", "team", None, "week", {"priority": []}), "needs 1-500 values"),
        (("mttr_hours", "team", None, "week", {"priority": [1] * 501}), "needs 1-500 values"),
        (("mttr_hours", "team", None, "week", {"priority": [0]}), "integers 1-5"),
        (("mttr_hours", "team", None, "week", {"priority": [True]}), "integers 1-5"),
        (("mttr_hours", "team", None, "week", {"priority": ["1"]}), "integers 1-5"),
        (("mttr_hours", "team", None, "week", {"team_id": [""]}), "printable characters"),
        (("enum_probe", "team", None, "week", {"severity": "bad"}), "one of critical, major"),
        (("enum_probe", "team", None, "week", {"change_type": ["x"]}), "standard, normal, em"),
        (("enum_probe", "team", None, "week", {"work_item_type": 1}), "initiative, epic, fe"),
    ],
)
def test_ut04_65_invalid_inputs_list_allowed_values(args: tuple[Any, ...], match: str) -> None:
    """UT04-65 each invalid input is a ToolInputError naming the allowed values."""
    catalog = _catalog(metric(name="disabled_probe", enabled=False), _enum_metric())
    with pytest.raises(ToolInputError, match=match):
        validate_metric_request(catalog, *args)


def test_ut04_65_filter_key_message_lists_allowed_filters() -> None:
    """UT04-65 a disallowed filter lists the metric's filters and never echoes the value."""
    with pytest.raises(ToolInputError) as info:
        validate_metric_request(
            _catalog(), "mttr_hours", "team", None, "week", {"severity": "secret-value"}
        )
    message = info.value.message
    assert "allowed: priority, service_id, team_id, org_id, cluster_id" in message
    assert "secret-value" not in message


def test_ut04_65_valid_request_is_normalized() -> None:
    """UT04-65 scalars wrap into lists; entity IDs come back sorted and unique."""
    catalog = _catalog(_enum_metric())
    metric_def, filters, ids = validate_metric_request(
        catalog, "mttr_hours", "org", ["O2", "O1", "O2"], "t12w", {"priority": 1, "org_id": "O1"}
    )
    assert metric_def.name == "mttr_hours"
    assert filters == {"priority": [1], "org_id": ["O1"]}
    assert ids == ["O1", "O2"]
    enum = validate_metric_request(
        catalog, "enum_probe", "team", None, "week", {"severity": ("info", "major")}
    )
    assert enum[1] == {"severity": ["info", "major"]}
    assert validate_metric_request(catalog, "mttr_hours", "team", None, "week", None)[1:] == (
        {},
        None,
    )


def test_ut04_65_window_and_required_columns(
    tiny: duckdb.DuckDBPyConnection,
    monkeypatch: pytest.MonkeyPatch,
    counters: list[tuple[str, dict[str, str]]],
) -> None:
    """UT04-65 a malformed window and a column absent from the build are ToolInputErrors."""
    present = metric(name="needs_present", requires_columns=["core.incident.acknowledged_at"])
    absent = metric(name="needs_absent", requires_columns=["core.incident.nope_column"])
    _use(monkeypatch, _catalog(present, absent))
    for bad in (
        (Q1,),
        (Q1, "2026-02-01"),
        (datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC), datetime.date(2026, 2, 1)),
        [Q1, datetime.date(2026, 2, 1)],
    ):
        with pytest.raises(ToolInputError, match="pair of dates"):
            compute_metric("incident_count", "team", None, "week", window=bad, con=tiny)  # type: ignore[arg-type]
    with pytest.raises(ToolInputError, match="window start must be before end"):
        compute_metric("incident_count", "team", None, "week", window=(Q1, Q1), con=tiny)
    with pytest.raises(ToolInputError, match=r"needs_absent needs core\.incident\.nope_column"):
        compute_metric("needs_absent", "team", None, "quarter", con=tiny)
    assert compute_metric("needs_present", "team", None, "quarter", con=tiny).row_count == 1
    outcomes = [labels["outcome"] for name, labels in counters]
    assert outcomes == ["input_error"] * 6 + ["ok"]
    assert {name for name, _ in counters} == {"herness_metrics_compute_calls_total"}


# --- UT04-66 timeout ---------------------------------------------------------------------------


def test_ut04_66_slow_query_times_out(
    tiny: duckdb.DuckDBPyConnection,
    monkeypatch: pytest.MonkeyPatch,
    counters: list[tuple[str, dict[str, str]]],
) -> None:
    """UT04-66 a slow query with timeout 0.1 s is a QueryError timeout with its query_id."""
    _use(monkeypatch, _catalog(metric(name="slow_probe", sql=SLOW_SQL), timeout=0.1))
    with pytest.raises(QueryError, match=r"timeout after 0\.1s") as info:
        compute_metric("slow_probe", "team", None, "quarter", con=tiny)
    assert str(info.value.context["query_id"]).startswith("q_")
    assert counters == [("herness_metrics_compute_calls_total", {"outcome": "query_error"})]


# --- UT04-67 metric_series -------------------------------------------------------------------


def test_ut04_67_series_equals_compute_and_calls_back_once(
    tiny: duckdb.DuckDBPyConnection,
) -> None:
    """UT04-67 metric_series over (start, end) returns compute_metric's rows and query_id;
    the callback runs once with the RecordedQuery."""
    start, end = datetime.date(2026, 1, 1), datetime.date(2026, 4, 1)
    seen: list[RecordedQuery] = []
    rows, query_id = metric_series(
        "incident_count", "team", None, start, end, "month", con=tiny, on_evidence=seen.append
    )
    direct = compute_metric("incident_count", "team", None, "month", window=(start, end), con=tiny)
    assert rows == direct.rows
    assert query_id == direct.query_id
    assert [r.query_id for r in seen] == [query_id]
    assert seen[0].result_hash == direct.result_hash
    assert seen[0].row_count == len(rows) == 2
    assert seen[0].to_evidence("run-1").run_id == "run-1"
    week_rows, _ = metric_series("incident_count", "team", ["T1"], start, end, con=tiny)
    assert [r.period_start.weekday() for r in week_rows] == [0, 0, 0]


# --- UT04-68 flags -----------------------------------------------------------------------------


def test_ut04_68_static_coverage_and_partial_period_flags(
    tiny: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT04-68 unconfirmed weights and estimate (static), low coverage and partial period."""
    weighted = metric(name="weighted_probe", uses_weights=["toil"], estimate=True)
    coverage_sql = MTTR_SQL.replace(
        "count(f.resolve_h) AS sample_size", "count(f.resolve_h) AS sample_size, 0.5 AS coverage"
    )
    covered = metric(name="coverage_probe", sql=coverage_sql)
    _use(monkeypatch, _catalog(weighted, covered))
    static = compute_metric("weighted_probe", "team", None, "quarter", con=tiny)
    assert static.flags == ["estimate", "unconfirmed_weights"]
    assert static.rows[0].flags == ["estimate", "unconfirmed_weights"]
    low = compute_metric("coverage_probe", "team", None, "quarter", con=tiny)
    assert low.rows[0].flags == ["low_coverage"]
    assert low.flags == []
    window = (datetime.date(2026, 1, 10), datetime.date(2026, 3, 1))
    partial = compute_metric("incident_count", "team", None, "month", window=window, con=tiny)
    assert [(r.period_start, r.value, r.flags) for r in partial.rows] == [
        (datetime.date(2026, 1, 1), 1.0, ["partial_period"]),
        (datetime.date(2026, 2, 1), 1.0, []),
    ]


# --- UT04-69 entity_ids ------------------------------------------------------------------------


def test_ut04_69_entity_ids_subset(tiny: duckdb.DuckDBPyConnection) -> None:
    """UT04-69 entity_ids limits rows to those entities; unknown IDs give no rows."""
    subset = compute_metric("incident_count", "org", ["O2"], "quarter", con=tiny)
    assert _values(subset) == [("O2", Q1, 3.0)]
    both = compute_metric("incident_count", "org", ["O2", "O1", "O1"], "quarter", con=tiny)
    assert [r.entity_id for r in both.rows] == ["O1", "O2"]
    assert compute_metric("incident_count", "org", ["O9"], "quarter", con=tiny).rows == []


# --- UT04-70 filters -------------------------------------------------------------------------


def test_ut04_70_priority_and_org_descendant_filters(tiny: duckdb.DuckDBPyConnection) -> None:
    """UT04-70 priority=[1] counts I1 only; org_id=O1 (parent of T1's org O2) keeps all
    three; org_id=O3 (a sibling subtree) keeps none."""
    p1 = compute_metric("incident_count", "team", None, "quarter", {"priority": [1]}, con=tiny)
    assert _values(p1) == [("T1", Q1, 1.0)]
    parent = compute_metric("incident_count", "team", None, "quarter", {"org_id": "O1"}, con=tiny)
    assert _values(parent) == [("T1", Q1, 3.0)]
    other = compute_metric("incident_count", "team", None, "quarter", {"org_id": ["O3"]}, con=tiny)
    assert other.rows == []
    both = compute_metric(
        "p1p2_count", "service", None, "quarter", {"priority": [2, 3], "cluster_id": "C1"}, con=tiny
    )
    assert _values(both) == [("S1", Q1, 1.0)]


# --- connection handling ---------------------------------------------------------------------


class _Tracked:
    """Delegates to a connection and records `close()` without closing it."""

    def __init__(self, con: duckdb.DuckDBPyConnection) -> None:
        self._con = con
        self.closed = 0

    def close(self) -> None:
        self.closed += 1

    def __getattr__(self, name: str) -> Any:
        return getattr(self._con, name)


def test_ut04_64_opens_and_closes_readonly_when_con_is_none(
    tiny: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT04-64 con=None opens CURRENT read-only (build None) and closes it on exit, also on
    error; a given connection is never closed."""
    tracked = _Tracked(tiny)
    builds: list[str | None] = []

    def fake_open(build_id: str | None) -> _Tracked:
        builds.append(build_id)
        return tracked

    monkeypatch.setattr(compute, "open_readonly", fake_open)
    assert compute_metric("incident_count", "team", None, "quarter").row_count == 1
    with pytest.raises(ToolInputError):
        compute_metric("incident_count", "team", None, "quarter", window=(Q1, Q1))
    assert (builds, tracked.closed) == ([None, None], 2)
    compute_metric("incident_count", "team", None, "quarter", con=tiny)
    assert tiny.execute("SELECT 1").fetchone() == (1,)


def test_ut04_64_meta_build_must_hold_one_row(tiny: duckdb.DuckDBPyConnection) -> None:
    """UT04-64 no or two meta.build rows is a SchemaViolation; nothing is written."""
    evidence_before = tiny.execute("SELECT count(*) FROM meta.evidence").fetchone()
    tiny.execute("INSERT INTO meta.build SELECT * REPLACE ('other' AS build_id) FROM meta.build")
    with pytest.raises(SchemaViolation, match=r"meta\.build must hold one row"):
        compute_metric("incident_count", "team", None, "quarter", con=tiny)
    tiny.execute("DELETE FROM meta.build")
    with pytest.raises(SchemaViolation, match=r"meta\.build must hold one row"):
        compute_metric("incident_count", "team", None, "quarter", con=tiny)
    tiny.execute("DROP TABLE meta.build")
    with pytest.raises(SchemaViolation, match=r"meta\.build must hold one row"):
        compute_metric("incident_count", "team", None, "quarter", con=tiny)
    assert tiny.execute("SELECT count(*) FROM meta.evidence").fetchone() == evidence_before


# --- ST04-01, ST04-07, ST04-11 -----------------------------------------------------------------


def test_st04_01_injection_strings_are_bound_values(tiny: duckdb.DuckDBPyConnection) -> None:
    """ST04-01 injection strings in entity_ids and filters leave the SQL text unchanged,
    return no rows and raise nothing from injected SQL."""
    keys = {"service_id", "team_id", "org_id", "cluster_id"}
    benign = compute_metric(
        "incident_count", "team", ["T1"], "quarter", dict.fromkeys(keys, "S1"), con=tiny
    )
    for text in INJECTIONS:
        result = compute_metric(
            "incident_count", "team", [text], "quarter", {k: [text] for k in keys}, con=tiny
        )
        assert result.sql == benign.sql
        assert result.rows == []
    for text in INJECTIONS:
        filtered = compute_metric(
            "incident_count", "team", None, "quarter", {"team_id": text}, con=tiny
        )
        assert filtered.rows == []
    count = tiny.execute("SELECT count(*) FROM metrics.incident_fact").fetchone()
    assert count == (4,)


def test_st04_07_resource_caps(
    tiny: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST04-07 501 IDs and 501 filter values are rejected; a 40-month window is rejected; a
    slow query times out."""
    with pytest.raises(ToolInputError, match="at most 500 entity_ids"):
        compute_metric("incident_count", "team", [f"T{i}" for i in range(501)], "week", con=tiny)
    many = {"team_id": [f"T{i}" for i in range(501)]}
    with pytest.raises(ToolInputError, match="needs 1-500 values"):
        compute_metric("incident_count", "team", None, "week", many, con=tiny)
    long_window = (datetime.date(2022, 1, 1), datetime.date(2025, 5, 1))
    with pytest.raises(ToolInputError, match="longer than 36 months"):
        compute_metric("incident_count", "team", None, "month", window=long_window, con=tiny)
    _use(monkeypatch, _catalog(metric(name="slow_probe", sql=SLOW_SQL), timeout=0.1))
    with pytest.raises(QueryError, match="timeout"):
        compute_metric("slow_probe", "org", None, "t12m", con=tiny)


def test_st04_11_filter_values_never_logged(
    tiny: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST04-11 PII-like filter values and IDs are absent from every log record of a
    successful call, a rejected call and a timed-out call."""
    pii = ("alice.smith@example.com", "555-867-5309", "Alice Smith")
    filters = {"service_id": [pii[0]], "team_id": [pii[1], "T1"]}
    with capture_logs() as logs:
        ok = compute_metric("incident_count", "team", [pii[2], "T1"], "quarter", filters, con=tiny)
        with pytest.raises(ToolInputError) as rejected:
            compute_metric("incident_count", "team", None, "quarter", {"priority": pii[0]})
        _use(monkeypatch, _catalog(metric(name="slow_probe", sql=SLOW_SQL), timeout=0.1))
        slow = {"team_id": [pii[1], "T1"]}
        with pytest.raises(QueryError):
            compute_metric("slow_probe", "team", [pii[2], "T1"], "quarter", slow, con=tiny)
    assert ok.rows == []
    completed = [e for e in logs if e["event"] == "metrics.compute.completed"]
    assert len(completed) == 1
    assert set(completed[0]) >= {"metric", "entity_type", "period", "row_count", "query_id"}
    text = repr(logs) + rejected.value.message
    for value in pii:
        assert value not in text
