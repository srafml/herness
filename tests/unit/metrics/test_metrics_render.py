"""Tests for herness.metrics.render and the SQL macros (U04-33 … U04-40)."""

import dataclasses
import datetime as dt
import re
from decimal import Decimal
from typing import Any

import jinja2
import pytest
import sqlglot
from hypothesis import assume, given
from hypothesis import strategies as st
from tests.support.metrics_render import (
    AS_OF,
    TZ,
    FakeCatalog,
    metric,
    source_sql,
    tiny_warehouse,
    week_window,
    weights,
)

from herness.core.errors import ConfigError
from herness.metrics import render
from herness.metrics.evidence import canonical_params
from herness.metrics.render import (
    BIND_TYPES,
    RenderState,
    default_binds,
    make_environment,
    render_metric_query,
    render_named,
    weight_binds,
)
from herness.metrics.windows import custom_window, default_window

pytestmark = pytest.mark.unit

GRAINS = ("service", "team", "org", "cluster", "work_item")
SOURCES = ("incident", "change", "event", "work_item", "metric_daily")
_CAST = re.compile(r"CAST\(\$([a-z_0-9]+) AS ([A-Z\[\]]+)\)")

# (source, grain) -> (entity expression, join text); pairs absent here are unsupported.
_OWNER = (
    "LEFT JOIN (SELECT service_id, team_id FROM core.service_map WHERE role = 'owner'"
    " QUALIFY row_number() OVER (PARTITION BY service_id ORDER BY confidence DESC NULLS LAST,"
    " team_id) = 1) ow_a ON ow_a.service_id = a.service_id"
)
_CLOSURE = "JOIN metrics.org_closure oc_a ON oc_a.org_id = a.org_id"
_SVC_CLOSURE = (
    "LEFT JOIN core.service sv_a ON sv_a.service_id = a.service_id"
    " JOIN metrics.org_closure oc_a ON oc_a.org_id = sv_a.org_id"
)
EXPECTED = {
    ("incident", "service"): ("a.service_id", ""),
    ("incident", "team"): ("a.team_id", ""),
    ("incident", "org"): ("oc_a.ancestor_org_id", _CLOSURE),
    ("incident", "cluster"): ("a.cluster_id", ""),
    ("change", "service"): ("a.service_id", ""),
    ("change", "team"): ("a.team_id", ""),
    ("change", "org"): ("oc_a.ancestor_org_id", _CLOSURE),
    ("event", "service"): ("a.service_id", ""),
    ("event", "team"): ("ow_a.team_id", _OWNER),
    ("event", "org"): ("oc_a.ancestor_org_id", _SVC_CLOSURE),
    ("work_item", "service"): ("a.service_id", ""),
    ("work_item", "team"): ("a.team_id", ""),
    ("work_item", "org"): ("oc_a.ancestor_org_id", _CLOSURE),
    (
        "work_item",
        "work_item",
    ): (
        "wc_a.ancestor_record_id",
        "JOIN metrics.work_item_closure wc_a ON wc_a.record_id = a.record_id",
    ),
    ("metric_daily", "service"): ("a.service_id", ""),
    ("metric_daily", "team"): ("ow_a.team_id", _OWNER),
    ("metric_daily", "org"): ("oc_a.ancestor_org_id", _SVC_CLOSURE),
}


def _macros(
    entity_type: str = "service", period: str = "week", filters: dict[str, str] | None = None
) -> tuple[Any, RenderState]:
    rs = RenderState()
    env = make_environment()
    ctx = {"entity_type": entity_type, "period": period, "filters": filters or {}, "rs": rs}
    return env.get_template("_macros.sql.j2").make_module(vars=ctx), rs


def _flat(text: object) -> str:
    return " ".join(str(text).split())


def _render(**kw: Any) -> render.RenderedQuery:
    args: dict[str, Any] = {
        "entity_type": "service",
        "window": week_window(),
        "filters": {},
        "entity_ids": None,
        "catalog": FakeCatalog(),
        "weights": weights(),
    }
    args.update(kw)
    m = args.pop("metric", metric())
    return render_metric_query(m, **args)


# --- UT04-23: entity_col / entity_join per (source, grain) ---------------------------------


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("grain", GRAINS)
def test_ut04_23_entity_col_and_join(source: str, grain: str) -> None:
    """UT04-23 entity_col/entity_join give the grain table's expression; others fail."""
    mod, rs = _macros(grain)
    expected = EXPECTED.get((source, grain))
    if expected is None:
        with pytest.raises(ConfigError, match=f"grain {grain} not supported by source {source}"):
            mod.entity_col(source, "a")
        with pytest.raises(ConfigError, match="not supported"):
            mod.entity_join(source, "a")
        return
    assert str(mod.entity_col(source, "a")) == expected[0]
    assert rs.entity() == expected[0]
    assert source in rs.sources
    assert _flat(mod.entity_join(source, "a")) == expected[1]


def test_ut04_23_aliases_and_sources_validated() -> None:
    """UT04-23 bad aliases, unknown sources and entity_filter before entity_col fail."""
    mod, _ = _macros()
    for alias in ("A", "1a", "a b", "a;drop", "a" * 17, ""):
        with pytest.raises(ConfigError, match="bad alias"):
            mod.entity_col("incident", alias)
    with pytest.raises(ConfigError, match="unknown source"):
        mod.entity_col("ticket", "a")
    with pytest.raises(ConfigError, match="entity"):
        mod.entity_filter()


@pytest.mark.parametrize("grain", ["service", "team", "org", "cluster"])
@pytest.mark.parametrize("period", ["week", "month", "quarter", "t12w", "t12m"])
def test_ut04_23_rendered_metric_parses_for_every_grain(grain: str, period: str) -> None:
    """UT04-23 acceptance: the wrapped SQL for every grain and period parses with sqlglot."""
    q = _render(
        entity_type=grain,
        window=default_window(period, AS_OF, TZ, {"week": 4, "month": 3, "quarter": 2}),
        filters={"priority": [1, 2], "org_id": ["org-a"], "team_id": ["t"]},
        entity_ids=["svc-a"],
    )
    statements = sqlglot.parse(q.sql, read="duckdb")
    assert len(statements) == 1
    assert isinstance(statements[0], sqlglot.exp.Select)


@pytest.mark.parametrize(("source", "grain"), sorted(EXPECTED), ids=str)
def test_ut04_23_every_source_grain_parses(source: str, grain: str) -> None:
    """UT04-23 acceptance: each supported (source, grain) with every filter parses with sqlglot."""
    supported = {
        "incident": ["priority", "service_id", "team_id", "org_id", "cluster_id"],
        "change": ["service_id", "team_id", "org_id", "change_type"],
        "event": ["service_id", "team_id", "org_id", "severity"],
        "work_item": ["service_id", "team_id", "org_id", "work_item_type"],
        "metric_daily": ["service_id", "team_id", "org_id"],
    }[source]
    m = metric(name="probe_metric", sql=source_sql(source), grains=[grain], filters=supported)
    q = _render(
        metric=m,
        entity_type=grain,
        filters={k: [1] if k == "priority" else ["x"] for k in supported},
        entity_ids=["x"],
    )
    assert q.sources == frozenset({source})
    assert len(sqlglot.parse(q.sql, read="duckdb")) == 1


# --- UT04-24: values never render --------------------------------------------------------------


def test_ut04_24_filter_value_absent_from_sql_present_in_bind() -> None:
    """UT04-24 a filter value like `x' OR 1=1 --` is bound, never rendered."""
    evil = "x' OR 1=1 --"
    q = _render(filters={"service_id": [evil, "svc-a"]}, entity_ids=[evil])
    assert evil not in q.sql
    assert "OR 1=1" not in q.sql
    assert q.bind["f_service_id"] == ["svc-a", evil]
    assert q.bind["entity_ids"] == [evil]
    assert q.template == {
        "name": "mttr_hours",
        "entity_type": "service",
        "period": "week",
        "filters": {"service_id": ["svc-a", evil]},
    }


def test_ut04_24_filter_clause_columns_and_rules() -> None:
    """UT04-24 filter_clause emits one bound predicate per key in sorted order."""
    filters = {"team_id": "f_team_id", "org_id": "f_org_id", "priority": "f_priority"}
    mod, rs = _macros(filters=filters)
    text = _flat(mod.filter_clause("incident", "f"))
    assert text == (
        "AND EXISTS (SELECT 1 FROM metrics.org_closure ocf WHERE ocf.org_id = f.org_id"
        " AND list_contains(CAST($f_org_id AS VARCHAR[]), ocf.ancestor_org_id))"
        " AND list_contains(CAST($f_priority AS SMALLINT[]), f.priority)"
        " AND list_contains(CAST($f_team_id AS VARCHAR[]), f.team_id)"
    )
    assert rs.used == {"f_org_id", "f_priority", "f_team_id"}
    ev, _ = _macros(filters={"team_id": "f_team_id", "org_id": "f_org_id"})
    ev_text = _flat(ev.filter_clause("event", "e"))
    assert "otf.service_id = e.service_id" in ev_text
    assert "svf.service_id = e.service_id" in ev_text
    bad, _ = _macros(filters={"priority": "f_priority"})
    with pytest.raises(ConfigError, match="filter priority not supported by source change"):
        bad.filter_clause("change", "c")


# --- UT04-25: period macros ---------------------------------------------------------------------


@pytest.mark.parametrize("period", ["week", "month", "quarter"])
def test_ut04_25_period_start_calendar(period: str) -> None:
    """UT04-25 week/month/quarter period_start truncates in the business timezone."""
    mod, rs = _macros(period=period)
    assert str(mod.period_start("f.ts")) == (
        f"CAST(date_trunc('{period}', f.ts AT TIME ZONE CAST($tz AS VARCHAR)) AS DATE)"
    )
    assert str(mod.period_start_date("m.date")) == f"CAST(date_trunc('{period}', m.date) AS DATE)"
    assert rs.used == {"tz"}


@pytest.mark.parametrize("period", ["t12w", "t12m"])
def test_ut04_25_period_start_rolling(period: str) -> None:
    """UT04-25 rolling periods start at the window start date."""
    mod, _ = _macros(period=period)
    assert str(mod.period_start("f.ts")) == "CAST($window_start_date AS DATE)"
    assert str(mod.period_start_date("m.date")) == "CAST($window_start_date AS DATE)"


def _spine_rows(period: str, window: Any) -> list[tuple[Any, ...]]:
    mod, rs = _macros(period=period)
    sql = f"SELECT * FROM {mod.period_spine()} ORDER BY period_start"  # noqa: S608 - macro text, no values
    binds = {k: v for k, v in window.binds().items() if k in rs.used}
    params = canonical_params(binds, {})["bind"]
    assert isinstance(params, dict)
    return tiny_warehouse().execute(sql, params).fetchall()


@pytest.mark.parametrize(
    ("period", "n", "first", "last_end"),
    [
        ("week", 4, "2024-02-12", "2024-03-11"),
        ("month", 3, "2023-12-01", "2024-03-01"),
        ("quarter", 2, "2023-07-01", "2024-01-01"),
    ],
)
def test_ut04_25_period_spine_calendar(period: str, n: int, first: str, last_end: str) -> None:
    """UT04-25 the spine has one row per period of the window, with local-midnight bounds."""
    window = default_window(period, AS_OF, TZ, {"week": 4, "month": 3, "quarter": 2})
    rows = _spine_rows(period, window)
    assert len(rows) == n
    assert rows[0][0] == dt.date.fromisoformat(first)
    assert rows[-1][1] == dt.date.fromisoformat(last_end)
    for start, end, start_ts, end_ts, snap_ts in rows:
        assert start < end
        assert start_ts == window.__class__(period, start, end, AS_OF, TZ, False).start_ts
        assert snap_ts == end_ts  # every complete period ends on or before as_of


@pytest.mark.parametrize("period", ["t12w", "t12m"])
def test_ut04_25_period_spine_rolling_one_row(period: str) -> None:
    """UT04-25 t12 spines have exactly one row spanning the window; snapshot at as_of."""
    window = default_window(period, AS_OF, TZ, {"week": 4, "month": 3, "quarter": 2})
    rows = _spine_rows(period, window)
    assert len(rows) == 1
    assert rows[0][:2] == (window.start, window.end)
    assert rows[0][4] == window.as_of_ts


def test_ut04_25_spine_snapshot_capped_at_as_of() -> None:
    """UT04-25 a custom window past as_of snapshots at as_of; partial periods are spined."""
    window = custom_window("month", dt.date(2024, 2, 10), dt.date(2024, 4, 5), AS_OF, TZ)
    rows = _spine_rows("month", window)
    assert [r[0] for r in rows] == [dt.date(2024, 2, 1), dt.date(2024, 3, 1), dt.date(2024, 4, 1)]
    assert rows[1][4] == window.as_of_ts


def test_ut04_25_other_macros() -> None:
    """UT04-25 owner_team, cat_at_join, cat_at and lkp emit the spec text."""
    mod, _ = _macros()
    assert _flat(mod.owner_team("e.service_id", "ow")).endswith(
        "ow ON ow.service_id = e.service_id"
    )
    assert _flat(mod.cat_at_join("spine.end_ts", "w", "ce")) == (
        "ASOF LEFT JOIN (SELECT record_id, at, arg_max(to_category, CASE to_category"
        " WHEN 'done' THEN 3 WHEN 'in_progress' THEN 2 ELSE 1 END) AS to_category"
        " FROM core.work_item_transition GROUP BY record_id, at) ce"
        " ON ce.record_id = w.record_id AND spine.end_ts >= ce.at"
    )
    assert _flat(mod.cat_at("spine.end_ts", "w", "ce")) == (
        "coalesce(ce.to_category, CASE WHEN w.created_at <= spine.end_ts THEN 'todo' END)"
    )
    assert _flat(mod.lkp("k", "v", "x.c", "0")) == (
        "coalesce(list_extract(v, list_position(k, x.c)), 0)"
    )
    with pytest.raises(ConfigError, match="bad alias"):
        mod.owner_team("e.service_id", "Bad")


# --- UT04-26: binds ------------------------------------------------------------------------------


def test_ut04_26_bind_contains_only_used_names_with_types() -> None:
    """UT04-26 bind has exactly the used names; each placeholder casts to BIND_TYPES."""
    q = _render(filters={"priority": [2, 1, 2]}, entity_ids=["b", "a", "b"])
    used = dict(_CAST.findall(q.sql))
    assert set(q.bind) == set(used)
    assert list(q.bind) == sorted(q.bind)
    for name, sql_type in used.items():
        assert BIND_TYPES[name] == sql_type
    assert q.bind["f_priority"] == [1, 2]
    assert q.bind["entity_ids"] == ["a", "b"]
    assert q.bind["min_n"] == 2
    assert q.bind["static_flags"] == []
    assert (q.bind["metric"], q.bind["unit"], q.bind["period"]) == ("mttr_hours", "hours", "week")
    assert "d_max_resolve_days" not in q.bind


def test_ut04_26_unknown_parameter_is_config_error() -> None:
    """UT04-26 a template using an unknown parameter or a name without a candidate fails."""
    sql = "SELECT {{ p('nope') }} AS entity_id"
    with pytest.raises(ConfigError, match="template uses unknown parameter nope"):
        _render(metric=metric(sql=sql))
    rs = RenderState()
    assert rs.p("w_engineer_hour") == "CAST($w_engineer_hour AS DOUBLE)"
    with pytest.raises(ConfigError):
        rs.p("x' OR 1=1")


def test_ut04_26_static_flags() -> None:
    """UT04-26 static_flags: estimate for estimated metrics, unconfirmed_weights by block."""
    m = metric(estimate=True, uses_weights=["toil"])
    assert _render(metric=m).bind["static_flags"] == ["estimate", "unconfirmed_weights"]
    confirmed = weights(toil=False)
    assert _render(metric=m, weights=confirmed).bind["static_flags"] == ["estimate"]


def test_ut04_26_bind_types_cover_spec_names() -> None:
    """UT04-26 BIND_TYPES has the window, request, default, scoring and weight names."""
    for name, sql_type in {
        "window_start": "TIMESTAMPTZ",
        "window_end_date": "DATE",
        "tz": "VARCHAR",
        "entity_ids": "VARCHAR[]",
        "min_n": "BIGINT",
        "f_priority": "SMALLINT[]",
        "d_max_resolve_days": "INTEGER",
        "d_change_link_min_score": "DOUBLE",
        "s_org_weights": "DOUBLE[]",
        "s_top_entities": "INTEGER",
        "w_downtime_k": "INTEGER[]",
        "w_fallback_enabled": "BOOLEAN",
        "w_sw_org_k": "VARCHAR[]",
        "unconfirmed": "BOOLEAN",
        "portfolio_team_v": "DOUBLE[]",
        "observed_days_min": "INTEGER",
    }.items():
        assert BIND_TYPES[name] == sql_type
    catalog = FakeCatalog()
    candidates = {**default_binds(catalog), **weight_binds(weights())}
    # s_count_metrics and s_unconfirmed_models are step binds computed by
    # herness.metrics.context.StepContext.binds() (T04-12), not by default_binds.
    step_binds = {"s_count_metrics", "s_unconfirmed_models"}
    assert {n for n in BIND_TYPES if n[:2] in {"d_", "s_", "w_"}} - step_binds == set(candidates)


SPEC_SCORECARD = {
    "mttr_hours": 0.15,
    "repeat_incident_rate": 0.15,
    "change_failure_rate": 0.15,
    "sla_breach_rate": 0.10,
    "reopen_rate": 0.05,
    "reassignment_rate": 0.05,
    "alert_noise_ratio": 0.10,
    "cycle_time_days": 0.10,
    "unplanned_work_ratio": 0.10,
    "epic_predictability": 0.05,
}


def test_ut04_26_default_binds_values() -> None:
    """UT04-26 default_binds reads defaults and scoring; pairs are sorted by key."""
    lever = metric(name="reopen_rate", usd_model="reopen", unit="ratio")
    catalog = FakeCatalog([metric(), lever, metric(name="off_metric", enabled=False)])
    # The design 04 §7.1 scorecard (the shipped file trims it until T04-11 ships its
    # metrics), so the bind pairs and lower-better intersection are exercised in full.
    org = catalog.scoring.org.model_copy(update={"metrics": SPEC_SCORECARD})
    catalog.scoring = catalog.scoring.model_copy(update={"org": org})
    b = default_binds(catalog)
    assert b["d_exclude_incident_states"] == ["canceled"]
    assert b["d_exclude_close_codes"] == ["Cancelled", "Duplicate", "Not an incident"]
    assert b["d_noise_severities"] == ["critical", "major", "minor", "warning"]
    assert b["d_max_resolve_days"] == 365
    assert b["s_cluster_weight"] == 0.8
    assert b["s_org_metrics"][:3] == ["alert_noise_ratio", "change_failure_rate", "cycle_time_days"]
    assert b["s_org_weights"][:3] == [0.10, 0.15, 0.10]
    assert b["s_lower_better"] == ["mttr_hours", "reopen_rate"]
    assert (b["s_lever_models_k"], b["s_lever_models_v"]) == (
        ["mttr_hours", "reopen_rate"],
        ["mttr", "reopen"],
    )
    assert b["s_templates_k"] == sorted(b["s_templates_k"])
    assert len(b["s_templates_v"]) == 7
    assert (b["s_metric_units_k"], b["s_metric_units_v"]) == (
        ["mttr_hours", "reopen_rate"],
        ["hours", "ratio"],
    )


def test_ut04_26_weight_binds_values() -> None:
    """UT04-26 weight_binds flattens maps into sorted key lists and paired values."""
    b = weight_binds(weights())
    assert b["w_downtime_k"] == [1, 2, 3, 4]
    assert b["w_downtime_v"] == [Decimal(50000), Decimal(10000), Decimal(2000), Decimal(500)]
    assert b["w_engineer_hour"] == Decimal(95)
    assert b["w_prio_mult_v"] == [1.0, 0.5, 0.1, 0.0, 0.0]
    assert (b["w_fallback_enabled"], b["w_fallback_max_priority"]) == (True, 2)
    assert (b["w_sw_clip_min"], b["w_sw_clip_max"]) == (0.5, 2.0)
    assert (b["w_sw_org_k"], b["w_sw_org_v"]) == (["servicenow:org:ops"], [1.2])
    assert (b["w_er_override_k"], b["w_er_override_v"]) == (["PAY-123"], [0.6])
    assert (b["w_capacity_k"], b["w_capacity_v"]) == ([], [])
    assert b["w_horizon_quarters"] == 1
    params = canonical_params(b, {})["bind"]
    assert isinstance(params, dict)
    assert params["w_engineer_hour"] == "95"


def _checks_env(monkeypatch: pytest.MonkeyPatch, templates: dict[str, str]) -> None:
    real = render.make_environment

    def patched() -> jinja2.sandbox.ImmutableSandboxedEnvironment:
        env = real()
        env.loader = jinja2.ChoiceLoader([jinja2.DictLoader(templates), env.loader])  # type: ignore[list-item]
        return env

    monkeypatch.setattr(render, "make_environment", patched)


def test_ut04_26_render_named(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT04-26 render_named renders allowlisted and check templates with used binds only."""
    _checks_env(
        monkeypatch,
        {
            "org_score.sql.j2": "SELECT {{ p('s_trend_weight') }} AS w, '{{ period }}' AS p",
            "checks.sql.j2": (
                "-- @check first\nSELECT {{ p('as_of') }} AS a\n"
                "-- @check second\nSELECT {{ p('tz') }} AS t\n"
            ),
        },
    )
    candidates = {"s_trend_weight": 0.5, "as_of": AS_OF, "tz": TZ}
    q = render_named("org_score", {"period": "t12m"}, candidates)
    assert q.sql == "SELECT CAST($s_trend_weight AS DOUBLE) AS w, 't12m' AS p"
    assert (q.bind, q.template) == (
        {"s_trend_weight": 0.5},
        {"name": "org_score", "period": "t12m"},
    )
    c = render_named("checks:second", {}, candidates)
    assert (c.sql.strip(), c.bind) == ("SELECT CAST($tz AS VARCHAR) AS t", {"tz": TZ})
    with pytest.raises(ConfigError, match="unknown check"):
        render_named("checks:third", {}, candidates)
    with pytest.raises(ConfigError, match="unknown template"):
        render_named("../evil", {}, candidates)
    with pytest.raises(ConfigError):
        render_named("org_score", {"period": "t12m'; DROP"}, candidates)
    with pytest.raises(ConfigError, match="no candidate"):
        render_named("org_score", {"period": "week"}, {})


def test_ut04_26_render_named_missing_template() -> None:
    """UT04-26 an allowlisted template that does not exist yet is a ConfigError."""
    with pytest.raises(ConfigError):
        render_named("funding_score", {}, {})


# --- U04-33: environment ----------------------------------------------------------------------


def test_st04_03_environment_settings() -> None:
    """ST04-03 the environment is sandboxed, strict, unescaped and has only `range`."""
    env = make_environment()
    assert isinstance(env, jinja2.sandbox.ImmutableSandboxedEnvironment)
    assert env.undefined is jinja2.StrictUndefined
    assert env.autoescape is False
    assert (env.trim_blocks, env.lstrip_blocks) == (True, True)
    assert set(env.globals) == {"range"}
    assert not env.extensions
    assert make_environment() is not env


# --- U04-40: wrapper ----------------------------------------------------------------------------


def _run(q: render.RenderedQuery) -> list[tuple[Any, ...]]:
    params = canonical_params(q.bind, q.template)["bind"]
    assert isinstance(params, dict)
    return tiny_warehouse().execute(q.sql, params).fetchall()


def test_ut04_64_wrapper_rows_min_sample_and_flags() -> None:
    """UT04-64 the wrapper applies min_n, flags, identity columns and ordering."""
    window = default_window("month", AS_OF, TZ, {"week": 4, "month": 3, "quarter": 2})
    rows = _run(_render(window=window))
    assert [r[:5] for r in rows] == [
        ("mttr_hours", "service", "svc-a", "month", dt.date(2024, 2, 1)),
        ("mttr_hours", "service", "svc-b", "month", dt.date(2024, 2, 1)),
    ]
    assert rows[0][5:] == (3.0, 6.0, 2.0, 2, "hours", [])
    assert rows[1][5:] == (None, 1.0, 1.0, 1, "hours", ["insufficient_sample"])
    org = _run(_render(window=window, entity_type="org", metric=metric(estimate=True)))
    assert [r[2] for r in org] == ["org-a", "org-b", "org-root"]
    assert org[2][5:10] == (7 / 3, 7.0, 3.0, 3, "hours")
    assert org[1][10] == ["estimate", "insufficient_sample"]


def test_ut04_68_wrapper_optional_columns_and_partial_period() -> None:
    """UT04-68 coverage, estimated_count and unweighted flags; partial periods when custom."""
    sql = (
        "SELECT {{ entity_col('incident', 'f') }} AS entity_id,"
        " {{ period_start('f.resolved_at') }} AS period_start, 1.0 AS value, 1.0 AS numerator,"
        " 1.0 AS denominator, count(*) AS sample_size, 0.5 AS coverage,"
        " count(*) AS estimated_count, true AS unweighted"
        " FROM metrics.incident_fact f WHERE f.resolved_at >= {{ p('window_start') }}"
        " AND f.resolved_at < {{ p('window_end') }} {{ entity_filter() }} GROUP BY ALL"
    )
    m = metric(sql=sql, min_sample_size=1)
    custom = custom_window("month", dt.date(2024, 2, 10), dt.date(2024, 2, 25), AS_OF, TZ)
    rows = _run(_render(metric=m, window=custom))
    assert rows
    assert rows[0][10] == ["estimate", "low_coverage", "partial_period", "unweighted"]
    aligned = default_window("month", AS_OF, TZ, {"week": 4, "month": 3, "quarter": 2})
    assert _run(_render(metric=m, window=aligned))[0][10] == [
        "estimate",
        "low_coverage",
        "unweighted",
    ]


def test_ut04_26_render_error_paths() -> None:
    """UT04-26 unparsable SELECTs, mixed filter types, bad periods and contexts fail."""
    with pytest.raises(ConfigError, match="does not parse"):
        _render(metric=metric(sql="SELECT (( FROM"))
    with pytest.raises(ConfigError, match="share one type"):
        _render(filters={"service_id": ["a", 1]})
    bad_period = dataclasses.replace(week_window(), period="year")  # type: ignore[arg-type]
    with pytest.raises(ConfigError, match="unknown period"):
        _render(window=bad_period)
    with pytest.raises(ConfigError, match="context key"):
        render_named("org_score", {"rs": "x"}, {})
    with pytest.raises(ConfigError):  # identifiers-only context passes; template not shipped
        render_named("org_score", {"top": 3, "strict": True}, {})


def test_ut04_26_non_jinja_render_errors_become_config_error() -> None:
    """UT04-26 sandbox errors other than TemplateError (OverflowError, ...) are ConfigError."""
    with pytest.raises(ConfigError) as info:
        _render(metric=metric(sql="SELECT {{ range(10**6) }} AS entity_id"))
    assert isinstance(info.value.__cause__, OverflowError)


def test_ut04_26_template_filters_are_sorted_not_deduped() -> None:
    """UT04-26 template.filters keeps duplicates (sorted); f_<k> binds stay sorted-unique."""
    q = _render(filters={"priority": [2, 1, 2]})
    assert q.template["filters"] == {"priority": [1, 2, 2]}
    assert q.bind["f_priority"] == [1, 2]


def test_ut04_26_render_rejects_bad_request_identifiers() -> None:
    """UT04-26 entity types, periods and filter keys outside the literals are refused."""
    with pytest.raises(ConfigError):
        _render(entity_type="service'; DROP")
    with pytest.raises(ConfigError):
        _render(filters={"description": ["x"]})
    with pytest.raises(ConfigError, match="output columns"):
        _render(metric=metric(sql="SELECT 1 AS entity_id"))


# --- PT04-12: values never render --------------------------------------------------------------

_VALUE = st.text(min_size=6, max_size=40)


@given(
    services=st.lists(_VALUE, min_size=1, max_size=4),
    ids=st.none() | st.lists(_VALUE, min_size=1, max_size=4),
)
def test_pt04_12_random_filter_strings_never_rendered(
    services: list[str], ids: list[str] | None
) -> None:
    """PT04-12 random filter and entity_id strings never appear in the rendered SQL."""
    baseline = _render(filters={"service_id": ["base"]}, entity_ids=["base"])
    for value in [*services, *(ids or [])]:
        assume(value not in baseline.sql)
    q = _render(filters={"service_id": services}, entity_ids=ids)
    assert q.sql == baseline.sql
    for value in [*services, *(ids or [])]:
        assert value not in q.sql
    assert q.bind["f_service_id"] == sorted(set(services))
