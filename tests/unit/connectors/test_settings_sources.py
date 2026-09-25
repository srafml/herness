"""Tests for the per-source settings models and `validate_spl` (U01-07 ... U01-13, T01-02)."""

import datetime
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError
from tests.unit.connectors import _settings_data as d

from herness.connectors import settings as s
from herness.connectors import settings_base

pytestmark = pytest.mark.unit


def _rejects(model: type[BaseModel], data: dict[str, Any], match: str | None = None) -> str:
    with pytest.raises(ValidationError, match=match) as info:
        model.model_validate(data)
    return str(info.value)


# UT01-02 ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "data", "path"),
    [
        (s.ServiceNowSettings, d.servicenow(colour=1), "colour"),
        (
            s.ServiceNowSettings,
            d.servicenow(entities={"incident": {"fields": ["a"], "colour": 1}}),
            "entities.incident.colour",
        ),
        (s.JiraSettings, d.jira(entities={"issue": {"colour": 1}}), "entities.issue.colour"),
        (
            s.DataverseSettings,
            d.dataverse(
                entities={"x": {"entityset": "x", "key_field": "k", "select": ["a"], "q": 1}}
            ),
            "entities.x.q",
        ),
        (s.SourcesConfig, {"version": 1, "sources": {"splunk": {}}}, "sources.splunk"),
        (s.SourcesConfig, {"version": 1, "dq": {}}, "dq"),
    ],
)
def test_ut01_02_unknown_keys_rejected_with_path(
    model: type[BaseModel], data: dict[str, Any], path: str
) -> None:
    """UT01-02 an unknown key at source, entity or root level fails naming its key path."""
    text = _rejects(model, data)
    assert path in text
    assert "Extra inputs are not permitted" in text


def test_ut01_02_jira_entity_keys_and_defaults() -> None:
    """UT01-02 Jira entities default to `issue` and allow no other key."""
    cfg = s.JiraSettings.model_validate(d.jira())
    assert set(cfg.entities) == {"issue"}
    assert isinstance(cfg.entities["issue"], s.JiraEntity)
    assert cfg.page_size == 100
    assert cfg.overlap_minutes == 60
    assert cfg.max_concurrency == 2
    assert cfg.auth_key() == "jira:cloud"
    _rejects(s.JiraSettings, d.jira(entities={"epic": {}}), "entities must be exactly")
    _rejects(s.JiraSettings, d.jira(entities={}), "entities must be exactly")


def test_ut01_02_jira_flavor_page_bounds_and_auth() -> None:
    """UT01-02 Jira `flavor` decides the auth methods and page bounds."""
    _rejects(s.JiraSettings, d.jira(page_size=101), "page_size must be >= 1 and <= 100")
    dc_auth = {"method": "pat", "credentials": "secret:jira_pat"}
    dc = s.JiraSettings.model_validate(d.jira(flavor="datacenter", auth=dc_auth, page_size=1000))
    assert dc.page_size == 1000
    assert dc.auth_key() == "jira:datacenter"
    _rejects(s.JiraSettings, d.jira(flavor="datacenter", auth=dc_auth, page_size=1001))
    _rejects(s.JiraSettings, d.jira(flavor="datacenter"), "auth.method is not allowed")
    _rejects(s.JiraSettings, d.jira(flavor="server"))
    missing = {k: v for k, v in d.jira().items() if k != "flavor"}
    _rejects(s.JiraSettings, missing, "flavor")


def test_ut01_02_required_and_forbidden_base_url_and_auth() -> None:
    """UT01-02 each source requires or forbids `base_url` and `auth` per its unit spec."""
    no_url = {k: v for k, v in d.servicenow().items() if k != "base_url"}
    _rejects(s.ServiceNowSettings, no_url, "base_url is required")
    no_auth = {k: v for k, v in d.dataverse().items() if k != "auth"}
    _rejects(s.DataverseSettings, no_auth, "auth is required")
    _rejects(s.MongoSettings, d.mongodb(base_url="https://db.example.com"), "base_url must not")
    _rejects(s.SnowflakeSettings, d.snowflake(base_url="https://x.example.com"), "base_url must")
    auth = {"method": "basic", "credentials": "secret:x_y"}
    _rejects(s.FilesSettings, d.files(auth=auth), "auth.method is not allowed for files")
    _rejects(s.MonitoringSettings, d.monitoring() | {"auth": auth}, "auth.method is not allowed")
    no_auth = {k: v for k, v in d.mongodb().items() if k != "auth"}
    _rejects(s.MongoSettings, no_auth, "auth is required")


def test_ut01_02_entities_required_non_empty_with_key_pattern() -> None:
    """UT01-02 sources with free entity names need at least one entity with a valid key."""
    _rejects(s.ServiceNowSettings, d.servicenow(entities={}), "entities")
    _rejects(s.MongoSettings, d.mongodb(entities={}), "entities")
    _rejects(s.FilesSettings, d.files(entities={"Bad-Key": {"pattern": "*.csv"}}), "Bad-Key")
    bad = d.servicenow(entities={"incidents": {"fields": ["a"]}})
    _rejects(s.ServiceNowSettings, bad, "entities.incidents is not a ServiceNow entity")


def test_ut01_02_source_defaults() -> None:
    """UT01-02 per-source defaults of page size, overlap and concurrency."""
    sn = s.ServiceNowSettings.model_validate(d.servicenow())
    assert (sn.page_size, sn.overlap_minutes, sn.max_concurrency) == (1000, 60, 4)
    assert "max_concurrency" not in sn.model_fields_set
    mongo = s.MongoSettings.model_validate(d.mongodb())
    assert (mongo.page_size, mongo.max_time_ms, mongo.max_concurrency) == (1000, 60000, 4)
    snow = s.SnowflakeSettings.model_validate(d.snowflake())
    assert (snow.page_size, snow.statement_timeout_s, snow.max_scan_gb) == (10000, 900, 50.0)
    dv = s.DataverseSettings.model_validate(d.dataverse())
    assert (dv.page_size, dv.max_concurrency) == (5000, 4)
    assert dv.entities["msdyn_project"].updated_field == "modifiedon"
    files = s.FilesSettings.model_validate(d.files())
    assert (files.inbox.as_posix(), files.max_concurrency) == ("data/inbox", 1)
    _rejects(s.FilesSettings, d.files(max_concurrency=2), "max_concurrency")
    assert s.SourceSettings is settings_base.SourceSettings
    assert issubclass(s.FilesSettings, s.SourceSettings)


@pytest.mark.parametrize(
    ("model", "data", "value", "bounds"),
    [
        (s.ServiceNowSettings, d.servicenow, 99, "100 and <= 10000"),
        (s.ServiceNowSettings, d.servicenow, 10001, "100 and <= 10000"),
        (s.MongoSettings, d.mongodb, 10001, "1 and <= 10000"),
        (s.DataverseSettings, d.dataverse, 5001, "1 and <= 5000"),
    ],
)
def test_ut01_02_source_page_size_bounds(
    model: type[BaseModel], data: Any, value: int, bounds: str
) -> None:
    """UT01-02 source `page_size` stays within the source's bounds."""
    _rejects(model, data(page_size=value), f"page_size must be >= {bounds}")


def test_ut01_05_entity_page_size_bounded_by_source() -> None:
    """UT01-05 an entity `page_size` override is held to the owning source's bounds (M5)."""
    ok = d.servicenow(entities={"incident": {"fields": ["a"], "page_size": 100}})
    assert s.ServiceNowSettings.model_validate(ok).page_size_for("incident") == 100
    low = d.servicenow(entities={"incident": {"fields": ["a"], "page_size": 99}})
    _rejects(s.ServiceNowSettings, low, "entities.incident.page_size must be >= 100")
    _rejects(s.JiraSettings, d.jira(entities={"issue": {"page_size": 101}}), "issue.page_size")
    _rejects(s.JiraSettings, d.jira(entities={"issue": {"page_size": 0}}), "issue.page_size")
    orders = d.mongo_entity(page_size=10001)
    _rejects(s.MongoSettings, d.mongodb(entities={"orders": orders}), "orders.page_size")
    roster = {"pattern": "*.csv", "key_field": ["k"], "page_size": 0}
    _rejects(s.FilesSettings, d.files(entities={"roster": roster}), "roster.page_size must be")


# UT01-06 ----------------------------------------------------------------------


def _sn_entity(**extra: Any) -> dict[str, Any]:
    return d.servicenow(entities={"incident": {"fields": ["number"]} | extra})


@pytest.mark.parametrize(
    "value",
    [
        "active=true^NQpriority=1",
        "active=true^ORDERBYDESCx",
        "a=1\nb=2",
        "a=1\rb",
        "a^EQ",
        "x" * 1001,
    ],
)
def test_ut01_06_servicenow_filter_rejected(value: str) -> None:
    """UT01-06 filters with `^NQ`, `^EQ`, `ORDERBY`, a line break or over 1,000 chars fail."""
    _rejects(s.ServiceNowSettings, _sn_entity(filter=value), "entities.incident.filter")


def test_ut01_06_servicenow_filter_accepted() -> None:
    """UT01-06 a plain encoded query is kept."""
    cfg = s.ServiceNowSettings.model_validate(_sn_entity(filter="active=true^priority=1"))
    entity = cfg.entities["incident"]
    assert isinstance(entity, s.ServiceNowEntity)
    assert entity.filter == "active=true^priority=1"
    assert entity.window_hours == 24


def test_ut01_06_servicenow_classes_rule() -> None:
    """UT01-06 `classes` is required for `cmdb_ci` and forbidden for other entities."""
    _rejects(s.ServiceNowSettings, _sn_entity(classes=["cmdb_ci_appl"]), "incident.classes")
    no_classes = d.servicenow(entities={"cmdb_ci": {"fields": ["name"]}})
    _rejects(s.ServiceNowSettings, no_classes, "entities.cmdb_ci.classes is required")
    ok = d.servicenow(entities={"cmdb_ci": {"fields": ["name"], "classes": ["cmdb_ci_appl"]}})
    entity = s.ServiceNowSettings.model_validate(ok).entities["cmdb_ci"]
    assert isinstance(entity, s.ServiceNowEntity)
    assert entity.classes == ["cmdb_ci_appl"]
    bad = d.servicenow(entities={"cmdb_ci": {"fields": ["name"], "classes": ["Bad"]}})
    _rejects(s.ServiceNowSettings, bad, "classes")


@pytest.mark.parametrize(
    "entity",
    [
        {"fields": []},
        {"fields": ["Number"]},
        {"fields": ["a", "a"]},
        {"fields": ["a" * 81]},
        {"fields": ["a"], "window_hours": 0},
        {"fields": ["a"], "window_hours": 169},
        {"fields": ["a"], "window_hours": True},
    ],
)
def test_ut01_06_servicenow_entity_fields(entity: dict[str, Any]) -> None:
    """UT01-06 `fields` and `window_hours` constraints."""
    _rejects(s.ServiceNowSettings, d.servicenow(entities={"incident": entity}))


def test_ut01_06_incident_gets_slice_days_seven() -> None:
    """UT01-06 `incident` without a backfill slice gets `slice_days=7`; others keep theirs."""
    cfg = s.ServiceNowSettings.model_validate(
        d.servicenow(
            backfill={"slice_days": 20},
            entities={"incident": {"fields": ["a"]}, "problem": {"fields": ["a"]}},
        )
    )
    assert cfg.backfill_for("incident").slice_days == 7
    assert cfg.backfill_for("problem").slice_days == 20
    kept = _sn_entity(backfill={"start": datetime.date(2024, 1, 1)})
    cfg = s.ServiceNowSettings.model_validate(kept)
    merged = cfg.backfill_for("incident")
    assert (merged.slice_days, str(merged.start)) == (7, "2024-01-01")
    explicit = s.ServiceNowSettings.model_validate(_sn_entity(backfill={"slice_days": 3}))
    assert explicit.backfill_for("incident").slice_days == 3
    with pytest.raises(TypeError, match="read-only"):
        cfg.entities.clear()  # type: ignore[attr-defined]


# UT01-07 ----------------------------------------------------------------------


def _nested(depth: int) -> dict[str, Any]:
    node: dict[str, Any] = {"a": 1}
    for _ in range(depth - 1):
        node = {"a": node}
    return node


@pytest.mark.parametrize(
    ("flt", "match"),
    [
        ({"a": {"$and": [{"$where": "sleep(1)"}]}}, r"\$where"),
        ({"a": {"$function": {}}}, r"\$function"),
        ({"$accumulator": {}}, r"\$accumulator"),
        ({"ts": {"$gt": 1}}, "updated_field"),
        ({"$or": [{"a": 1}]}, r"\$or"),
        (_nested(9), "nesting"),
        ({f"k{i}": i for i in range(65)}, "64 keys"),
        ({"a": {1: 2}}, "keys must be strings"),
        ({"a": {1, 2}}, "JSON-compatible"),
    ],
)
def test_ut01_07_mongo_filter_rejected(flt: dict[str, Any], match: str) -> None:
    """UT01-07 nested `$where`/`$function`, top-level `updated_field`/`$or`, depth 9 fail."""
    data = d.mongodb(entities={"orders": d.mongo_entity(filter=flt)})
    _rejects(s.MongoSettings, data, match)


def test_ut01_07_mongo_filter_and_entity_accepted() -> None:
    """UT01-07 a JSON-compatible filter of depth 8 with 64 keys per level is accepted."""
    flt = _nested(8) | {f"k{i}": [i, None, True, 1.5, "x"] for i in range(63)}
    cfg = s.MongoSettings.model_validate(d.mongodb(entities={"orders": d.mongo_entity(filter=flt)}))
    entity = cfg.entities["orders"]
    assert isinstance(entity, s.MongoEntity)
    assert entity.filter == flt
    assert entity.key_field == "_id"


@pytest.mark.parametrize(
    "entity",
    [
        d.mongo_entity(collection="system.users"),
        d.mongo_entity(collection="a/b"),
        d.mongo_entity(fields=[]),
        d.mongo_entity(fields=["a", "a"]),
        d.mongo_entity(key_field="$x"),
        d.mongo_entity(updated_field="1ts"),
    ],
)
def test_ut01_07_mongo_entity_constraints(entity: dict[str, Any]) -> None:
    """UT01-07 MongoDB entity name rules."""
    _rejects(s.MongoSettings, d.mongodb(entities={"orders": entity}))


@pytest.mark.parametrize(
    "extra",
    [{"database": "a b"}, {"max_time_ms": 999}, {"max_time_ms": 600001}, {"entities": {"A": {}}}],
)
def test_ut01_07_mongo_source_constraints(extra: dict[str, Any]) -> None:
    """UT01-07 MongoDB source constraints."""
    _rejects(s.MongoSettings, d.mongodb(**extra))


# UT01-08 ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spl", "match"),
    [
        ("index=web status=500", "stats"),
        ("index=web | stats count by host | delete", "not allowed"),
        ("index=web | `evil_macro` | stats count", "backtick"),
        ("| tstats count where index=web" + " " * 4000, "4000"),
        ("index=web | stats count | outputlookup x.csv", "not allowed"),
        ("index=web | stats count |rest /services", "not allowed"),
    ],
)
def test_ut01_08_validate_spl_rejects(spl: str, match: str) -> None:
    """UT01-08 SPL without stats, with `| delete`, with a backtick or too long fails."""
    with pytest.raises(ValueError, match=match):
        s.validate_spl(spl)


@pytest.mark.parametrize(
    "spl",
    [
        "| tstats count where index=web by _time span=1d",
        "index=web | STATS count by service",
        "index=web | table _time service value",
    ],
)
def test_ut01_08_validate_spl_accepts(spl: str) -> None:
    """UT01-08 a read-only aggregate search passes."""
    assert s.validate_spl(spl) is None


def test_ut01_08_splunk_adapter_queries_use_validate_spl() -> None:
    """UT01-08 the Splunk adapter checks `event_query` and metric queries with `validate_spl`."""
    bad_event = d.adapter("splunk", event_query="index=web | delete")
    _rejects(s.MonitoringSettings, d.monitoring(splunk=bad_event), "adapters.splunk.event_query")
    query = {"name": "error_rate", "query": "index=web", "unit": "ratio"}
    bad_metric = d.adapter("splunk", metric_queries=[query])
    _rejects(s.MonitoringSettings, d.monitoring(splunk=bad_metric), "metric_queries")
    good = d.adapter(
        "splunk",
        event_query="| tstats count by host",
        metric_queries=[query | {"query": "index=web | stats avg(v) as value by service"}],
    )
    cfg = s.MonitoringSettings.model_validate(d.monitoring(splunk=good))
    assert cfg.adapters["splunk"].max_concurrency == 2


# UT01-09 ----------------------------------------------------------------------


def _prom(step: str) -> dict[str, Any]:
    query = {"name": "request_count", "query": "sum(x)", "unit": "count", "step": step}
    return d.monitoring(prometheus=d.adapter("prometheus", metric_queries=[query]))


@pytest.mark.parametrize("step", ["12h", "2d", "1w", "86401s", "d"])
def test_ut01_09_prometheus_step_rejected(step: str) -> None:
    """UT01-09 Prometheus `step` other than one day fails."""
    _rejects(s.MonitoringSettings, _prom(step), "prometheus step must be 1d")


@pytest.mark.parametrize("step", ["1d", "86400s", "1440m", "24h"])
def test_ut01_09_prometheus_step_accepted(step: str) -> None:
    """UT01-09 `1d` and `86400s` (and other spellings of one day) pass."""
    cfg = s.MonitoringSettings.model_validate(_prom(step))
    assert cfg.adapters["prometheus"].metric_queries[0].step == step


def test_ut01_09_monitoring_section_rules() -> None:
    """UT01-09 monitoring entities, reconcile, adapter and tool rules."""
    cfg = s.MonitoringSettings.model_validate(d.monitoring())
    assert set(cfg.entities) == {"event", "metric_daily"}
    assert cfg.max_concurrency == 4
    partial = d.monitoring() | {"entities": {"event": {"overlap_minutes": 5}}}
    cfg = s.MonitoringSettings.model_validate(partial)
    assert cfg.overlap_for("event").seconds == 300
    assert set(cfg.entities) == {"event", "metric_daily"}
    _rejects(s.MonitoringSettings, d.monitoring() | {"entities": {"alerts": {}}}, "alerts")
    reconcile = d.monitoring() | {"reconcile": {}}
    _rejects(s.MonitoringSettings, reconcile, "monitoring is not reconciled")
    off = d.monitoring(prometheus=d.adapter("prometheus", enabled=False))
    _rejects(s.MonitoringSettings, off, "at least one enabled adapter")
    assert not s.MonitoringSettings.model_validate(off | {"enabled": False}).enabled
    _rejects(s.MonitoringSettings, d.monitoring(zabbix=d.adapter("prometheus")), "zabbix")


@pytest.mark.parametrize(
    ("tool", "extra", "match"),
    [
        ("datadog", {"page_size": 1001}, "adapters.datadog.page_size"),
        ("dynatrace", {"page_size": 501}, "adapters.dynatrace.page_size"),
        ("datadog", {"max_concurrency": 3}, "adapters.datadog.max_concurrency"),
        ("prometheus", {"max_concurrency": 0}, "adapters.prometheus.max_concurrency"),
        ("datadog", {"tenant": "ops"}, "adapters.datadog.tenant"),
        ("prometheus", {"tenant": "bad tenant"}, "tenant"),
        ("prometheus", {"event_query": "up"}, "adapters.prometheus.event_query"),
        ("datadog", {"event_query": "x" * 2001}, "adapters.datadog.event_query"),
        ("dynatrace", {"event_query": "x" * 2001}, "adapters.dynatrace.event_query"),
        ("datadog", {"auth": {"method": "bearer", "credentials": "secret:dd"}}, "auth.method"),
        ("splunk", {"auth": {"method": "oauth_3lo", "credentials": "secret:sp"}}, "not supported"),
        ("datadog", {"base_url": "http://dd.example.com"}, "base_url"),
        ("datadog", {"timeout_s": 0.5}, "timeout_s"),
        ("datadog", {"verify": False}, "TLS verification cannot be disabled"),
    ],
)
def test_ut01_09_adapter_rules(tool: str, extra: dict[str, Any], match: str) -> None:
    """UT01-09 tool-specific adapter rules name `adapters.<tool>.<field>`."""
    _rejects(s.MonitoringSettings, d.monitoring(**{tool: d.adapter(tool, **extra)}), match)


def test_ut01_09_adapter_defaults() -> None:
    """UT01-09 adapter page size and concurrency defaults follow the tool."""
    cfg = s.MonitoringSettings.model_validate(
        d.monitoring(
            datadog=d.adapter("datadog"),
            dynatrace=d.adapter("dynatrace", event_query="status(open)"),
            prometheus=d.adapter("prometheus", tenant="ops", timeout_s=30),
        )
    )
    sizes = {tool: (a.page_size, a.max_concurrency) for tool, a in cfg.adapters.items()}
    assert sizes == {"datadog": (1000, 2), "dynatrace": (500, 2), "prometheus": (1000, 4)}
    assert cfg.adapters["prometheus"].timeout_s == 30.0
    with pytest.raises(TypeError, match="read-only"):
        cfg.adapters.clear()  # type: ignore[attr-defined]


def test_ut01_09_datadog_agg_required_and_unique_names() -> None:
    """UT01-09 Datadog metric queries need `agg`; names are unique per adapter."""
    query = {"name": "error_rate", "query": "avg:x{*} by {service}", "unit": "ratio"}
    no_agg = d.adapter("datadog", metric_queries=[query])
    _rejects(s.MonitoringSettings, d.monitoring(datadog=no_agg), "agg")
    twice = d.adapter("datadog", metric_queries=[query | {"agg": "avg"}] * 2)
    _rejects(s.MonitoringSettings, d.monitoring(datadog=twice), "unique")


@pytest.mark.parametrize(
    "extra",
    [
        {"query": ""},
        {"query": "x" * 4001},
        {"unit": "Ratio"},
        {"agg": "median"},
        {"service_label": "1x"},
        {"value_field": "a b"},
        {"service_dimension": "d" * 65},
    ],
)
def test_ut01_09_metric_query_constraints(extra: dict[str, Any]) -> None:
    """UT01-09 `MetricQuery` field constraints."""
    query = {"name": "error_rate", "query": "q", "unit": "ratio"} | extra
    with pytest.raises(ValidationError):
        s.MetricQuery.model_validate(query)


# UT01-10 ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "entity",
    [
        d.snowflake_entity(table="PUBLIC.COST_CENTER"),
        d.snowflake_entity(table="A.B.C.D"),
        d.snowflake_entity(table="A.B.1C"),
        d.snowflake_entity(columns=["CC_ID", "a;b", "UPDATED_AT"]),
        d.snowflake_entity(columns=["CC_ID", "CC_ID", "UPDATED_AT"]),
        d.snowflake_entity(columns=[]),
        d.snowflake_entity(key_field="OTHER"),
        d.snowflake_entity(updated_field="OTHER"),
        d.snowflake_entity(filter="1=1; drop table x"),
        d.snowflake_entity(filter="NAME = 'x' -- comment"),
        d.snowflake_entity(filter="NAME = 'x' /* c */"),
        d.snowflake_entity(filter="NAME in (select 1) or DROP"),
        d.snowflake_entity(filter="x" * 1001),
    ],
)
def test_ut01_10_snowflake_entity_rejected(entity: dict[str, Any]) -> None:
    """UT01-10 two-part table, column `a;b`, filters with `drop` or `--` fail."""
    _rejects(s.SnowflakeSettings, d.snowflake(entities={"cost_center": entity}))


def test_ut01_10_snowflake_accepted() -> None:
    """UT01-10 a read-only predicate and identifier-shaped names pass."""
    entity = d.snowflake_entity(filter="ORG_UNIT = 'OPS' AND USE_FLAG = 1")
    cfg = s.SnowflakeSettings.model_validate(d.snowflake(entities={"cost_center": entity}))
    assert cfg.account == "Acme-XY12345"
    ent = cfg.entities["cost_center"]
    assert isinstance(ent, s.SnowflakeEntity)
    assert ent.filter == "ORG_UNIT = 'OPS' AND USE_FLAG = 1"


@pytest.mark.parametrize(
    "extra",
    [
        {"account": "acme xy"},
        {"warehouse": "1WH"},
        {"role": "role-x"},
        {"statement_timeout_s": 0},
        {"statement_timeout_s": 86401},
        {"max_scan_gb": 0},
        {"max_scan_gb": 10000.5},
        {"max_scan_gb": True},
    ],
)
def test_ut01_10_snowflake_source_constraints(extra: dict[str, Any]) -> None:
    """UT01-10 Snowflake source constraints; YAML ints are accepted for `max_scan_gb`."""
    _rejects(s.SnowflakeSettings, d.snowflake(**extra))
    assert s.SnowflakeSettings.model_validate(d.snowflake(max_scan_gb=10000)).max_scan_gb == 10000


# UT01-11 ----------------------------------------------------------------------


def test_ut01_11_metric_name_must_be_daily_metric() -> None:
    """UT01-11 `metric_queries[].name: foo` fails; every DAILY_METRIC_NAMES entry passes."""
    query = {"name": "foo", "query": "sum(x)", "unit": "count"}
    bad = d.monitoring(prometheus=d.adapter("prometheus", metric_queries=[query]))
    _rejects(s.MonitoringSettings, bad, "metric_queries.0.name")
    for name in sorted(settings_base.DAILY_METRIC_NAMES):
        assert s.MetricQuery.model_validate(query | {"name": name}).name == name


# UT01-12 ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "entity",
    [
        {"pattern": "../*.csv", "key_field": ["k"]},
        {"pattern": "a/b.csv", "key_field": ["k"]},
        {"pattern": "a\\b.csv", "key_field": ["k"]},
        {"pattern": "a\x00.csv", "key_field": ["k"]},
        {"pattern": "..csv", "key_field": ["k"]},
        {"pattern": "*.exe", "key_field": ["k"]},
        {"pattern": "x" * 197 + ".csv", "key_field": ["k"]},
        {"pattern": "*.csv", "sheet": "Teams", "key_field": ["k"]},
        {"pattern": "*.xlsx", "sheet": "a:b", "key_field": ["k"]},
        {"pattern": "*.xlsx", "sheet": "x" * 32, "key_field": ["k"]},
        {"pattern": "*.xlsx", "sheet": "", "key_field": ["k"]},
        {"pattern": "*.csv", "key_field": []},
        {"pattern": "*.csv", "key_field": [f"k{i}" for i in range(11)]},
        {"pattern": "*.csv", "key_field": [""]},
        {"pattern": "*.csv", "key_field": ["k"], "updated_field": "u" * 129},
        {"pattern": "*.csv", "key_field": ["k"], "mode": "append"},
    ],
)
def test_ut01_12_files_entity_rejected(entity: dict[str, Any]) -> None:
    """UT01-12 escaping or unknown-suffix patterns and `sheet` on csv fail."""
    _rejects(s.FilesSettings, d.files(entities={"roster": entity}))


def test_ut01_12_files_accepted() -> None:
    """UT01-12 csv, xlsx with sheet and parquet patterns pass; suffix is case-insensitive."""
    entities = {
        "team_roster": {"pattern": "*.XLSX", "sheet": "Teams", "key_field": ["team_code"]},
        "legacy": {"pattern": "*.csv", "key_field": ["id"], "updated_field": "ts"},
        "facts": {"pattern": "f*.parquet", "key_field": ["id"], "mode": "snapshot"},
    }
    cfg = s.FilesSettings.model_validate(d.files(entities=entities, inbox="inbox/x"))
    assert cfg.inbox.as_posix() == "inbox/x"
    roster = cfg.entities["team_roster"]
    assert isinstance(roster, s.FilesEntity)
    assert (roster.sheet, roster.mode) == ("Teams", "delta")
    _rejects(s.FilesSettings, d.files(base_url="https://x.example.com"), "base_url must not")
    _rejects(s.FilesSettings, d.files(inbox=5))


# UT01-66 ----------------------------------------------------------------------


def test_ut01_66_jira_oauth_3lo_not_supported() -> None:
    """UT01-66 `auth.method: oauth_3lo` fails with "not supported in v1"."""
    auth = {"method": "oauth_3lo", "credentials": "secret:jira_oauth"}
    _rejects(s.JiraSettings, d.jira(auth=auth), "auth.method oauth_3lo is not supported in v1")


# ST01-07 ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "jql",
    [
        "project = OPS ORDER BY created DESC",
        "project = OPS order\tby key",
        "project = OPS\nOR project = PAY",
        "project = OPS\r",
        "p" * 2001,
    ],
)
def test_st01_07_jql_scope_injection_rejected(jql: str) -> None:
    """ST01-07 `jql_scope` cannot reorder results or add lines."""
    _rejects(s.JiraSettings, d.jira(jql_scope=jql), "jql_scope")


def test_st01_07_injection_strings_rejected_for_every_filter_type() -> None:
    """ST01-07 each filter type rejects its injection string."""
    _rejects(s.ServiceNowSettings, _sn_entity(filter="active=true^NQsys_id!=x"), "filter")
    mongo = d.mongo_entity(filter={"$where": "this.a > 0"})
    _rejects(s.MongoSettings, d.mongodb(entities={"orders": mongo}), "where")
    snow = d.snowflake_entity(filter="1=1; DELETE FROM X")
    _rejects(s.SnowflakeSettings, d.snowflake(entities={"cost_center": snow}), "filter")
    splunk = d.adapter("splunk", event_query="index=* | stats count | sendemail to=x")
    _rejects(s.MonitoringSettings, d.monitoring(splunk=splunk), "event_query")
    ok = s.JiraSettings.model_validate(d.jira(jql_scope="project in (PAY, OPS, PLAT)"))
    assert ok.jql_scope == "project in (PAY, OPS, PLAT)"
