"""Tests for herness.metrics.catalog and its checks (impl 04 U04-23 … U04-28, U04-83)."""

import dataclasses
import datetime as dt
import json
import re
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from tests.support.metrics_render import MTTR_SQL, ROOT, metric, source_sql, weights

from herness.core import config as c
from herness.core.config import ConfigIssue, config_hash
from herness.core.config_validate import register_owner_validator, run_owner_validators
from herness.core.errors import ConfigError, ToolInputError
from herness.metrics import _catalog_checks as checks
from herness.metrics import catalog as cat
from herness.metrics.catalog import (
    LOW_COVERAGE_THRESHOLD,
    METRIC_FLAGS,
    SCORE_UNITS,
    SOURCE_FILTERS,
    MetricCatalog,
    catalog_from_config,
    load_catalog,
    metrics_owner_validator,
    unit_for,
    validate_catalog,
)
from herness.metrics.render import make_environment
from herness.metrics.settings import MetricDef, MetricsCatalogConfig
from herness.store.ops.shared import ReviewItem

pytestmark = pytest.mark.unit

SHIPPED = ROOT / "config" / "metrics.yaml"
SQL_DIR = ROOT / "herness" / "metrics" / "sql"
T0 = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)


def _raw_config(metrics: list[MetricDef] | None = None, **scoring: Any) -> dict[str, Any]:
    raw: dict[str, Any] = yaml.safe_load(SHIPPED.read_text(encoding="utf-8"))
    entries = metrics if metrics is not None else [metric()]
    raw["metrics"] = [m.model_dump(mode="json") for m in entries]
    raw["scoring"]["org"]["metrics"] = scoring.pop("org_metrics", {"mttr_hours": 1.0})
    raw["scoring"]["levers"]["templates"].update(scoring.pop("templates", {}))
    return raw


def _config(metrics: list[MetricDef] | None = None, **scoring: Any) -> MetricsCatalogConfig:
    return MetricsCatalogConfig.model_validate(_raw_config(metrics, **scoring))


def _wrapped(inner: str) -> str:
    """`inner` in the wrapper's `FROM (...) q` position (parser input only, never executed)."""
    return f"SELECT * FROM ({inner}) q"  # noqa: S608 - parser input, never executed


def _errors(issues: list[ConfigIssue]) -> list[ConfigIssue]:
    return [i for i in issues if i.severity == "error"]


def _count_metric(**kw: Any) -> MetricDef:
    base: dict[str, Any] = {
        "name": "incident_count",
        "aggregation": "count",
        "unit": "count",
        "usd_model": None,
        "sql": source_sql("incident"),
    }
    return metric(**{**base, **kw})


@pytest.fixture
def fake_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """`get_config()` returns only the shipped weights (the loader's one config read)."""
    monkeypatch.setattr(cat, "get_config", lambda: SimpleNamespace(weights=weights()))


# --- UT04-13 load_catalog, version ------------------------------------------------------------


def test_ut04_13_shipped_metrics_yaml_loads(fake_config: None) -> None:
    """UT04-13 the shipped config/metrics.yaml loads with no error issue: metrics #1-#14 and a
    scorecard naming only shipped metrics (T04-10/11 restore the other design weights)."""
    raw = yaml.safe_load(SHIPPED.read_text(encoding="utf-8"))
    shipped = MetricsCatalogConfig.model_validate(raw)
    assert shipped.version == 1
    errors = [i for i in validate_catalog(shipped, weights=weights()) if i.severity == "error"]
    assert errors == []
    names = sorted(
        [
            "incident_count",
            "p1p2_count",
            "mttr_hours",
            "mttr_p50_hours",
            "mttr_business_hours",
            "mtta_minutes",
            "customer_impact_minutes",
            "repeat_incident_rate",
            "reopen_rate",
            "reassignment_rate",
            "sla_breach_rate",
            "alert_noise_ratio",
            "toil_hours_est",
            "incident_cost_usd",
        ]
    )
    assert load_catalog(SHIPPED).names() == names
    assert set(shipped.scoring.org.metrics) <= set(names)


def _reordered(value: object) -> object:
    if isinstance(value, dict):
        return {k: _reordered(value[k]) for k in reversed(list(value))}
    if isinstance(value, list):
        return [_reordered(v) for v in value]
    return value


def test_ut04_13_load_and_key_order_keeps_version(tmp_path: Path, fake_config: None) -> None:
    """UT04-13 shipped file plus fixture entries loads; `version` unchanged by key order."""
    raw = _raw_config([metric(), _count_metric()])
    first, second = tmp_path / "a.yaml", tmp_path / "b.yaml"
    first.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    second.write_text(yaml.safe_dump(_reordered(raw), sort_keys=False), encoding="utf-8")
    one, two = load_catalog(first), load_catalog(second)
    assert re.fullmatch(r"[0-9a-f]{12}", one.version)
    assert one.version == two.version
    assert one.names() == ["incident_count", "mttr_hours"]
    raw["metrics"][0]["min_sample_size"] = 3
    first.write_text(yaml.safe_dump(raw), encoding="utf-8")
    assert load_catalog(first).version != one.version


def test_ut04_13_load_rejects_bad_files(tmp_path: Path, fake_config: None) -> None:
    """UT04-13 missing, oversized, non-YAML and schema-invalid files raise ConfigError."""
    with pytest.raises(ConfigError, match="missing or too large"):
        load_catalog(tmp_path / "nope.yaml")
    big = tmp_path / "big.yaml"
    big.write_bytes(b"#" * (1024 * 1024 + 1))
    with pytest.raises(ConfigError, match="missing or too large"):
        load_catalog(big)
    bad = tmp_path / "bad.yaml"
    bad.write_text("a: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid UTF-8 YAML"):
        load_catalog(bad)
    raw = _raw_config()
    raw["metrics"][0]["min_sample_size"] = "secret-looking-value"
    bad.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ConfigError, match=r"invalid at: metrics\.0\.min_sample_size") as info:
        load_catalog(bad)
    assert "secret-looking-value" not in info.value.message


# --- UT04-14 forbidden text columns -----------------------------------------------------------


def test_ut04_14_short_description_rejected(tmp_path: Path, fake_config: None) -> None:
    """UT04-14 a template selecting `short_description` gives an error issue; load refuses."""
    sql = MTTR_SQL.replace("avg(f.resolve_h) AS value", "max(f.short_description) AS value")
    cfg = _config([metric(sql=sql)])
    issues = validate_catalog(cfg, weights=weights())
    assert [(i.severity, i.path, i.file) for i in issues] == [
        ("error", "metrics.mttr_hours.sql", "metrics.yaml")
    ]
    assert "short_description" in issues[0].message
    path = tmp_path / "m.yaml"
    path.write_text(yaml.safe_dump(cfg.model_dump(mode="json")), encoding="utf-8")
    with pytest.raises(ConfigError, match="text column short_description") as info:
        load_catalog(path)
    assert info.value.issues == tuple(issues)


def test_ut04_14_clean_catalog_has_no_issues() -> None:
    """UT04-14 control: the fixture catalog passes every rule."""
    assert validate_catalog(_config([metric(), _count_metric()]), weights=weights()) == []


# --- UT04-16 template contract, flags and filter matrix ---------------------------------------


def test_ut04_16_three_error_issues() -> None:
    """UT04-16 two statements, missing `sample_size`, `severity` on incident: three errors."""
    one = {"grains": ["service"], "usd_model": None}
    two = metric(name="two_statements", sql=MTTR_SQL + "; SELECT 1", **one)
    no_sample = MTTR_SQL.replace(", count(f.resolve_h) AS sample_size", "")
    missing = metric(name="no_sample", sql=no_sample, **one)
    severity = metric(name="sev_filter", filters=["severity"], **one)
    cfg = _config([metric(), two, missing, severity])
    errors = validate_catalog(cfg, weights=weights())
    assert [(i.severity, i.path) for i in errors] == [
        ("error", "metrics.two_statements.sql"),
        ("error", "metrics.no_sample.sql"),
        ("error", "metrics.sev_filter.sql"),
    ]
    assert "forbidden token ;" in errors[0].message
    assert "sample_size" in errors[1].message
    assert "filter severity not supported by source incident" in errors[2].message


@pytest.mark.parametrize(
    ("columns", "ok"),
    [
        ("", True),
        (", 1.0 AS coverage, 1 AS estimated_count, false AS unweighted", True),
        (", 1.0 AS coverage, false AS unweighted", True),
        (", false AS unweighted, 1.0 AS coverage", False),
        (", 1.0 AS coverage, 1.0 AS coverage", False),
        (", 1 AS extra", False),
    ],
)
def test_ut04_16_output_columns_contract(columns: str, ok: bool) -> None:
    """UT04-16 required columns in order, then an ordered subset of the optional columns."""
    sql = MTTR_SQL.replace("AS sample_size", "AS sample_size" + columns)
    issues = validate_catalog(_config([metric(sql=sql)]), weights=weights())
    assert (issues == []) is ok
    if not ok:
        assert "must output" in issues[0].message


def test_ut04_16_column_order_enforced() -> None:
    """UT04-16 the six required columns must come first and in contract order."""
    sql = MTTR_SQL.replace(
        "avg(f.resolve_h) AS value, sum(f.resolve_h) AS numerator",
        "sum(f.resolve_h) AS numerator, avg(f.resolve_h) AS value",
    )
    issues = validate_catalog(_config([metric(sql=sql)]), weights=weights())
    assert len(issues) == 1
    assert "must output entity_id, period_start, value" in issues[0].message


def test_ut04_16_filter_checked_against_every_source() -> None:
    """UT04-16 step 6: a filter key must be supported by every source the template uses."""
    sql = source_sql("incident").replace(
        "{{ entity_filter() }}",
        "{{ entity_filter() }} AND EXISTS (SELECT 1 FROM core.event e "
        "WHERE e.service_id = f.service_id {{ filter_clause('event', 'e') }})",
    )
    m = _count_metric(sql=sql, filters=["service_id"], grains=["service"])
    assert validate_catalog(_config([metric(), m]), weights=weights()) == []
    both = _count_metric(
        sql=source_sql("incident").replace(
            "FROM metrics.incident_fact f",
            "FROM metrics.incident_fact f {{ entity_join('event', 'f') }}",
        ),
        filters=["priority"],
        grains=["service"],
    )
    issues = validate_catalog(_config([metric(), both]), weights=weights())
    assert [(i.path, i.message) for i in issues] == [
        ("metrics.incident_count.sql", "filter priority not supported by source event")
    ]


def test_ut04_16_vocabularies() -> None:
    """UT04-16 METRIC_FLAGS, SOURCE_FILTERS and LOW_COVERAGE_THRESHOLD per U04-28."""
    assert {
        "insufficient_sample",
        "estimate",
        "unconfirmed_weights",
        "low_coverage",
        "partial_period",
        "unweighted",
    } == METRIC_FLAGS
    shared = {"service_id", "team_id", "org_id"}
    assert dict(SOURCE_FILTERS) == {
        "incident": shared | {"priority", "cluster_id"},
        "change": shared | {"change_type"},
        "event": shared | {"severity"},
        "work_item": shared | {"work_item_type"},
        "metric_daily": shared,
    }
    assert LOW_COVERAGE_THRESHOLD == 0.8
    with pytest.raises(TypeError):
        SOURCE_FILTERS["incident"] = frozenset()  # type: ignore[index]


def test_ut04_16_macro_filter_table_matches_source_filters() -> None:
    """UT04-16 carry-over: the `_macros` FILTER table equals SOURCE_FILTERS (U04-28)."""
    ctx = {"entity_type": "service", "period": "week", "filters": {}, "rs": object()}
    module = make_environment().get_template("_macros.sql.j2").make_module(vars=ctx)
    table = getattr(module, "FILTER", None)
    assert isinstance(table, dict)
    assert {src: frozenset(keys) for src, keys in table.items()} == dict(SOURCE_FILTERS)


def test_ut04_16_wrapper_threshold_matches_low_coverage() -> None:
    """UT04-16 carry-over: the wrapper's hard-coded coverage cut is LOW_COVERAGE_THRESHOLD."""
    text = (SQL_DIR / "metric_wrapper.sql.j2").read_text(encoding="utf-8")
    cuts = re.findall(r"q\.coverage < ([0-9.]+) THEN 'low_coverage'", text)
    assert [float(v) for v in cuts] == [LOW_COVERAGE_THRESHOLD]


# --- UT04-17 / UT04-18 MetricCatalog ----------------------------------------------------------


def test_ut04_17_unknown_metric_lists_enabled_names() -> None:
    """UT04-17 `get("nope")` raises ToolInputError listing the enabled names."""
    off = _count_metric(name="old_metric", enabled=False)
    catalog = MetricCatalog(_config([metric(), _count_metric(), off]))
    with pytest.raises(
        ToolInputError, match=r"unknown metric nope; known: incident_count, mttr_hours$"
    ):
        catalog.get("nope")
    assert catalog.get("old_metric").enabled is False


def test_ut04_18_describe_names_and_immutability(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT04-18 `describe()` keys per design, sorted; names; catalog_from_config; immutable."""
    off = _count_metric(name="aaa_disabled", enabled=False)
    cfg = _config([metric(), _count_metric(), off])
    catalog = catalog_from_config(SimpleNamespace(metrics=cfg))  # type: ignore[arg-type]
    rows = catalog.describe()
    assert [r["name"] for r in rows] == ["aaa_disabled", "incident_count", "mttr_hours"]
    assert list(rows[2]) == [
        "name",
        "description",
        "grains",
        "unit",
        "better",
        "min_sample_size",
        "filters",
        "estimate",
        "enabled",
    ]
    assert rows[2]["grains"] == ["service", "team", "org", "cluster"]
    assert catalog.names() == ["incident_count", "mttr_hours"]
    assert catalog.names(enabled_only=False) == ["aaa_disabled", "incident_count", "mttr_hours"]
    assert catalog.defaults is cfg.defaults
    assert catalog.scoring is cfg.scoring
    with pytest.raises(dataclasses.FrozenInstanceError):
        catalog.version = "x"  # type: ignore[misc]
    monkeypatch.setattr(cat, "get_config", lambda: SimpleNamespace(metrics=cfg))
    assert catalog_from_config().version == catalog.version


# --- UT04-21 / ST04-10 lever templates --------------------------------------------------------


def test_ut04_21_bad_lever_templates() -> None:
    """UT04-21 `{entity_name.__class__}`, `{delta_usd:>10}`, `{foo}`: three errors."""
    templates = {
        "mttr": "x {entity_name.__class__}",
        "repeat": "x {delta_usd:>10}",
        "reopen": "x {foo}",
    }
    issues = validate_catalog(_config(templates=templates), weights=weights())
    assert [(i.severity, i.path) for i in issues] == [
        ("error", "scoring.levers.templates.mttr"),
        ("error", "scoring.levers.templates.reopen"),
        ("error", "scoring.levers.templates.repeat"),
    ]


@pytest.mark.parametrize(
    ("template", "fragment"),
    [
        ("{entity_name.__class__.__init__}", "not a plain name"),
        ("{entity_name[0]}", "not a plain name"),
        ("{}", "not a plain name"),
        ("{unit!r}", "conversion or format spec"),
        ("{delta_usd:,.2f}", "conversion or format spec"),
        ("{unit:{period}}", "conversion or format spec"),
        ("{unit", "unbalanced braces"),
        ("unit}", "unbalanced braces"),
    ],
)
def test_st04_10_placeholder_attacks_rejected(template: str, fragment: str) -> None:
    """ST04-10 attribute/index access, conversions, format specs and bad braces are rejected."""
    problems = checks.template_problems(template)
    assert any(fragment in p for p in problems)
    issues = validate_catalog(_config(templates={"noise": template}), weights=weights())
    assert [i.path for i in issues] == ["scoring.levers.templates.noise"]


def test_st04_10_every_allowed_placeholder_passes() -> None:
    """ST04-10 control: every LEVER_PLACEHOLDERS name is accepted; literal `{{` is fine."""
    template = " ".join("{" + p + "}" for p in sorted(checks.LEVER_PLACEHOLDERS)) + " {{x}}"
    assert checks.template_problems(template) == []


# --- UT04-22 scorecard and per-metric rules ---------------------------------------------------


def test_ut04_22_count_metric_in_scorecard() -> None:
    """UT04-22 a scorecard including `incident_count` is an error."""
    cfg = _config([metric(), _count_metric()], org_metrics={"mttr_hours": 1, "incident_count": 1})
    issues = validate_catalog(cfg, weights=weights())
    assert [(i.severity, i.path, i.message) for i in issues] == [
        (
            "error",
            "scoring.org.metrics.incident_count",
            "scorecard metric incident_count is count-like or lacks team/org grain",
        )
    ]


def test_ut04_22_other_scorecard_and_metric_rules() -> None:
    """UT04-22 missing, disabled or grain-less scorecard metrics; usd_model; weight blocks."""
    no_org = metric(name="svc_only", grains=["service", "team"])
    off = metric(name="off_metric", enabled=False)
    higher = metric(name="uptime_ratio", better="higher", aggregation="ratio")
    unused = metric(name="unused_usd")
    blocks = metric(name="weighted", uses_weights=["toil", "no_such_block"])
    org = dict.fromkeys(("svc_only", "off_metric", "ghost", "uptime_ratio", "weighted"), 1.0)
    issues = validate_catalog(
        _config([no_org, off, higher, unused, blocks], org_metrics=org), weights=weights()
    )
    found = {(i.severity, i.path) for i in issues}
    assert found == {
        ("error", "scoring.org.metrics.svc_only"),
        ("error", "scoring.org.metrics.off_metric"),
        ("error", "scoring.org.metrics.ghost"),
        ("error", "metrics.uptime_ratio.better"),
        ("warn", "metrics.unused_usd.usd_model"),
        ("error", "metrics.weighted.uses_weights"),
    }
    assert all(i.file == "metrics.yaml" for i in issues)


# --- ST04-02 statement, table and function allowlists -----------------------------------------


@pytest.mark.parametrize(
    ("sql", "fragment"),
    [
        ("COPY (SELECT 1 AS entity_id) TO 'out.csv'", "Copy"),
        ("ATTACH 'other.db' AS other", "Attach"),
        ("CREATE TABLE core.x AS SELECT 1", "Create"),
        ("SELECT * FROM (SELECT * FROM read_csv('x.csv')) q", "function read_csv"),
        ("INSERT INTO core.x SELECT 1", "Insert"),
        ("UPDATE core.x SET a = 1", "Update"),
        ("DELETE FROM core.x", "Delete"),
        ("DROP TABLE core.x", "Drop"),
        ("ALTER TABLE core.x ADD COLUMN b INT", "Alter"),
        ("INSTALL httpfs", "Install"),
        ("LOAD httpfs", "Command"),
        ("PRAGMA version", "Pragma"),
        ("SET threads = 1", "Set"),
        ("USE other", "Use"),
        ("DETACH other", "Detach"),
        ("BEGIN", "Transaction"),
        ("SELECT 1; SELECT 2", "exactly one statement"),
        ("SELEC 1 FROM", "does not parse"),
    ],
)
def test_st04_02_forbidden_statements(sql: str, fragment: str) -> None:
    """ST04-02 COPY, ATTACH, read_csv, CREATE and the other forbidden forms are rejected."""
    assert any(fragment in p for p in checks.sql_problems(sql))


@pytest.mark.parametrize(
    "func",
    [
        "getenv('HOME')",
        "glob('*')",
        "query('SELECT 1')",
        "query_table('t')",
        "sqlite_scan('a', 'b')",
        "postgres_scan('a', 'b', 'c')",
        "pragma_table_info('t')",
        "current_setting('threads')",
        "load_extension('x')",
        "read_parquet('x')",
        "read_json_auto('x')",
    ],
)
def test_st04_02_forbidden_functions(func: str) -> None:
    """ST04-02 file, database, settings and dynamic-query functions are rejected."""
    problems = checks.sql_problems(_wrapped("SELECT " + func + " AS entity_id"))
    assert any(p.startswith("function ") for p in problems)


@pytest.mark.parametrize(
    ("table", "ok"),
    [
        ("core.incident", True),
        ("enrich.label", True),
        ("metrics.incident_fact", True),
        ("main.secrets", False),
        ("incident", False),
        ("'x.csv'", False),
        ("other.core.incident", False),
        ("glob('*')", False),
    ],
)
def test_st04_02_table_allowlist(table: str, ok: bool) -> None:
    """ST04-02 tables must be CTE names or core/enrich/metrics tables."""
    inner = "SELECT 1 AS entity_id FROM " + table + " t"  # noqa: S608 - parser input
    problems = [p for p in checks.sql_problems(_wrapped(inner)) if "must output" not in p]
    assert (problems == []) is ok


def test_st04_02_ctes_unions_and_template_level() -> None:
    """ST04-02 CTE names and set operations pass; a read_csv template fails validation."""
    cte = "WITH a AS (SELECT 1 AS x FROM core.t) SELECT x FROM a UNION ALL SELECT 2 FROM a"
    assert [p for p in checks.sql_problems(_wrapped(cte)) if "must output" not in p] == []
    assert "must be one SELECT" in checks.sql_problems("SELECT 1")[0]
    sql = MTTR_SQL.replace("FROM metrics.incident_fact f", "FROM read_csv('x.csv') f")
    issues = validate_catalog(_config([metric(sql=sql)]), weights=weights())
    messages = {i.message for i in issues}
    assert "function read_csv not allowed" in messages
    copy = metric(sql="COPY (SELECT 1) TO 'x.csv'")
    assert _errors(validate_catalog(_config([copy]), weights=weights()))


# --- ST04-04 text columns ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "select",
    [
        "max(f.description) AS value",
        "max(f.DESCRIPTION) AS value",
        'max(f."Close_Notes") AS value',
        "max(f.text_redacted) AS value",
        "max(e.host) AS value",
    ],
)
def test_st04_04_text_column_through_alias_rejected(select: str) -> None:
    """ST04-04 a template selecting a text column, also through an alias, is rejected."""
    sql = MTTR_SQL.replace("avg(f.resolve_h) AS value", select)
    issues = validate_catalog(_config([metric(sql=sql)]), weights=weights())
    assert [i.message.startswith("sql uses text column") for i in issues] == [True]


def test_st04_04_shipped_templates_select_no_text_column() -> None:
    """ST04-04 schema scan: no shipped metric, fact or score template names a text column."""
    templates = sorted(SQL_DIR.glob("*.sql.j2"))
    facts = ROOT / "herness" / "model" / "sql" / "400_facts.sql"
    assert facts.is_file()
    templates.append(facts)
    for path in templates:
        # Only the text-column rule applies here: macros, checks and the facts stage carry
        # `;`, `--` or `/*` legitimately (they are not catalog templates).
        problems = checks.raw_sql_problems(path.read_text(encoding="utf-8"))
        assert [p for p in problems if p.startswith("sql uses text column")] == [], path


# --- UT04-117 SCORE_UNITS ---------------------------------------------------------------------


def test_ut04_117_unit_for_matches_design_table() -> None:
    """UT04-117 `unit_for` follows the design 04 §4.3 table; `metric` rows use the catalog."""
    catalog = MetricCatalog(_config([metric(), _count_metric()]))
    expected = {
        "usd": 7,
        "ratio": 5,
        "score": 4,
        "rank": 3,
        "count": 3,
        "metric": 5,
    }
    counts: dict[str, int] = {}
    for unit in SCORE_UNITS.values():
        counts[unit] = counts.get(unit, 0) + 1
    assert counts == expected
    assert unit_for("score.funding.wsjf", None, catalog) == "score"
    assert unit_for("score.action_lever.delta_usd", "mttr_hours", catalog) == "usd"
    assert unit_for("metrics.metric_value.value", "mttr_hours", catalog) == "hours"
    assert unit_for("score.org.peer_median", "incident_count", catalog) == "count"
    assert unit_for("score.portfolio.order_rank", None, catalog) == "rank"
    with pytest.raises(ToolInputError, match=r"no unit for score\.org\.value"):
        unit_for("score.org.value", None, catalog)
    with pytest.raises(ToolInputError, match=r"no unit for core\.incident\.priority"):
        unit_for("core.incident.priority", None, catalog)
    with pytest.raises(ToolInputError, match="unknown metric"):
        unit_for("score.org.value", "nope", catalog)


# --- UT04-120 owner validator -----------------------------------------------------------------


@pytest.fixture
def owner_cfg(tmp_path: Path) -> Iterator[c.HernessConfig]:
    """A loaded config whose catalog has one forbidden column and `cost_per_engineer_hour`
    confirmed (every other block stays unconfirmed)."""
    from tests.support.config_tree import write_full_config  # noqa: PLC0415 - heavy import

    loaded = c.init_config(config_dir=write_full_config(tmp_path), env={})
    sql = MTTR_SQL.replace("avg(f.resolve_h) AS value", "max(f.summary) AS value")
    cfg = loaded.model_copy(
        update={
            "metrics": _config([metric(sql=sql)]),
            "weights": weights(cost_per_engineer_hour=False),
        }
    )
    yield cfg
    c.reset_config()


def _snapshot(
    cfg: c.HernessConfig, text: str | None, *, last: str = "cfg_0000000000000000"
) -> None:
    snaps = Path(cfg.paths.data) / "config_snapshots"
    snaps.mkdir(parents=True, exist_ok=True)
    (snaps / "LAST").write_text(last, encoding="utf-8")
    if text is not None:
        (snaps / f"{last}.yaml").write_text(text, encoding="utf-8")


def _previous_all_unconfirmed(cfg: c.HernessConfig) -> str:
    weights_section = json.loads(weights().model_dump_json())
    return yaml.safe_dump({"weights": weights_section, "profile": cfg.profile})


def _item(payload: dict[str, Any], item_id: str = "rev_1") -> ReviewItem:
    return ReviewItem(item_id, "weight_change", payload, "approved", T0, "ops-lead", T0, None)


def _fake_store(monkeypatch: pytest.MonkeyPatch, items: list[ReviewItem]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake(**kw: Any) -> list[ReviewItem]:
        calls.append(kw)
        return items

    monkeypatch.setattr(cat, "list_review_items", fake)
    return calls


def test_ut04_120_owner_validator(
    owner_cfg: c.HernessConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT04-120 catalog error kept; approved payload clears the block; else one weights error."""
    cfg = owner_cfg
    _snapshot(cfg, _previous_all_unconfirmed(cfg))
    good = {"blocks": ["cost_per_engineer_hour"], "origin": "operator"}
    good["proposed_config_hash"] = config_hash(cfg)
    calls = _fake_store(monkeypatch, [_item({"blocks": ["nope"]}, "rev_bad"), _item(good)])
    issues = metrics_owner_validator(cfg, offline=True)
    assert calls == [{"kind": "weight_change", "status": "approved", "limit": 5000}]
    assert [(i.severity, i.path, i.file) for i in issues] == [
        ("error", "metrics.mttr_hours.sql", "metrics.yaml")
    ]
    _fake_store(monkeypatch, [])
    issues = metrics_owner_validator(cfg, offline=False)
    assert [(i.severity, i.path, i.file) for i in issues[1:]] == [
        ("error", "weights.cost_per_engineer_hour.unconfirmed", "weights.yaml")
    ]
    register_owner_validator("metrics", metrics_owner_validator)
    hooked = run_owner_validators(cfg, offline=True)
    assert sorted(map(str, hooked)) == sorted(map(str, issues))


def test_ut04_120_missing_last_is_first_load(
    owner_cfg: c.HernessConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT04-120 missing `LAST` (or its snapshot) is a first load: confirmed blocks need approval."""
    _fake_store(monkeypatch, [])
    expected = [("error", "weights.cost_per_engineer_hour.unconfirmed")]
    issues = metrics_owner_validator(owner_cfg, offline=True)
    assert [(i.severity, i.path) for i in issues[1:]] == expected
    _snapshot(owner_cfg, None)
    issues = metrics_owner_validator(owner_cfg, offline=True)
    assert [(i.severity, i.path) for i in issues[1:]] == expected


@pytest.mark.parametrize("last", ["../../secrets", "cfg_ABC", "cfg_0000000000000000.yaml", ""])
def test_ut04_120_last_must_name_a_snapshot_hash(
    owner_cfg: c.HernessConfig, monkeypatch: pytest.MonkeyPatch, last: str
) -> None:
    """UT04-120 a `LAST` that is not a `cfg_<16 hex>` hash is an unreadable snapshot (warn)."""
    _fake_store(monkeypatch, [])
    _snapshot(owner_cfg, _previous_all_unconfirmed(owner_cfg), last=last)
    issues = metrics_owner_validator(owner_cfg, offline=True)
    assert [(i.severity, i.path) for i in issues[1:]] == [
        ("error", "weights.cost_per_engineer_hour.unconfirmed"),
        ("warn", "weights"),
    ]


@pytest.mark.parametrize("text", ["weights: [1, 2]", "- a\n- b\n", "a: [unclosed", "other: 1"])
def test_ut04_120_unreadable_snapshot_warns(
    owner_cfg: c.HernessConfig, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    """UT04-120 an invalid previous snapshot gives one `warn` at `weights` and checks all as new."""
    _fake_store(monkeypatch, [])
    _snapshot(owner_cfg, text)
    issues = metrics_owner_validator(owner_cfg, offline=True)
    assert [(i.severity, i.path, i.file) for i in issues[1:]] == [
        ("error", "weights.cost_per_engineer_hour.unconfirmed", "weights.yaml"),
        ("warn", "weights", "weights.yaml"),
    ]
