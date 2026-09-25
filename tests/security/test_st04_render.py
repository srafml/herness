"""Security tests for metric rendering (impl 04 ST04-01 render part, ST04-03; TH04-01, TH04-03)."""

from typing import Any

import duckdb
import jinja2
import pytest
from jinja2.exceptions import SecurityError
from tests.support.metrics_render import (
    FakeCatalog,
    metric,
    tiny_warehouse,
    week_window,
    weights,
)

from herness.core.errors import ConfigError
from herness.metrics.evidence import canonical_params
from herness.metrics.render import RenderedQuery, render_metric_query, render_named

pytestmark = pytest.mark.unit

INJECTIONS = [
    "x' OR 1=1 --",
    "'; DROP TABLE metrics.incident_fact; --",
    "svc-a') UNION SELECT * FROM metrics.org_closure --",
    "x) OR $window_start IS NOT NULL --",
    "{{ ''.__class__ }}",
    "CAST($tz AS VARCHAR)) OR (true",
    "\u0000\n/* */",
]


def _render(filters: dict[str, list[Any]], entity_ids: list[str] | None) -> RenderedQuery:
    return render_metric_query(
        metric(),
        entity_type="service",
        window=week_window(),
        filters=filters,
        entity_ids=entity_ids,
        catalog=FakeCatalog(),
        weights=weights(),
    )


def _run(q: RenderedQuery, con: duckdb.DuckDBPyConnection) -> list[tuple[Any, ...]]:
    params = canonical_params(q.bind, q.template)["bind"]
    assert isinstance(params, dict)
    return con.execute(q.sql, params).fetchall()


@pytest.mark.parametrize("evil", INJECTIONS)
def test_st04_01_injection_in_filters_and_entity_ids(evil: str) -> None:
    """ST04-01 injected values leave the SQL text unchanged and match no rows."""
    baseline = _render({"service_id": ["svc-a"], "team_id": ["team-a"]}, ["svc-a"])
    q = _render({"service_id": [evil], "team_id": [evil]}, [evil])
    assert q.sql == baseline.sql
    assert evil in baseline.sql or evil not in q.sql
    assert q.bind["entity_ids"] == [evil]
    con = tiny_warehouse()
    assert _run(baseline, con) != []
    assert _run(q, con) == []
    assert con.execute("SELECT count(*) FROM metrics.incident_fact").fetchone() == (3,)


def test_st04_01_injection_in_entity_ids_only() -> None:
    """ST04-01 an injected entity ID with benign filters still returns no rows and no error."""
    q = _render({}, [INJECTIONS[0], INJECTIONS[2]])
    assert _run(q, tiny_warehouse()) == []


@pytest.mark.parametrize(
    "payload",
    [
        "{{ ''.__class__.__mro__ }}",
        "{{ cycler.__init__ }}",
        "{{ ''.__class__.__mro__[1].__subclasses__() }}",
        "{{ rs.__class__ }}",
        "{{ rs._used }}",
        "{{ p.__globals__ }}",
        "{{ lipsum.__globals__ }}",
        "{{ self.__init__ }}",
        "{% set x = [] %}{{ x.append(1) }}",
        "{{ config }}",
        "{% include '../../pyproject.toml' %}",
    ],
)
def test_st04_03_template_escape_attempts(payload: str) -> None:
    """ST04-03 SSTI payloads in a metric template raise SecurityError or ConfigError."""
    m = metric(sql=f"SELECT {payload} AS entity_id")
    with pytest.raises((SecurityError, ConfigError, jinja2.TemplateError)) as info:
        render_metric_query(
            m,
            entity_type="service",
            window=week_window(),
            filters={},
            entity_ids=None,
            catalog=FakeCatalog(),
            weights=weights(),
        )
    # the sandbox, not the later output-column check, must be what stops the payload
    assert isinstance(info.value.__cause__, jinja2.TemplateError)


def test_st04_03_render_errors_are_config_errors() -> None:
    """ST04-03 sandbox and undefined-name failures surface as ConfigError to callers."""
    m = metric(sql="SELECT {{ ''.__class__.__mro__ }} AS entity_id")
    with pytest.raises(ConfigError) as info:
        render_metric_query(
            m,
            entity_type="service",
            window=week_window(),
            filters={},
            entity_ids=None,
            catalog=FakeCatalog(),
            weights=weights(),
        )
    assert isinstance(info.value.__cause__, jinja2.TemplateError)


def test_st04_03_named_context_is_identifiers_only() -> None:
    """ST04-03 render_named refuses non-identifier context values and unlisted names."""
    with pytest.raises(ConfigError):
        render_named("org_score", {"period": "{{ cycler }}"}, {})
    with pytest.raises(ConfigError):
        render_named("_macros", {}, {})
    with pytest.raises(ConfigError):
        render_named("checks:../x", {}, {})
