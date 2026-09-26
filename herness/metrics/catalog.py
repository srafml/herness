"""Metric catalog: read-only object, loaders, validator, units and flag vocabularies.

Impl 04 U04-23 … U04-28 and U04-83 (design 04 §3.1, §4.1, §4.3, §5.8, §5.9, §7.2, §10.5).
`validate_catalog` is never called from `herness.core.config` (R-03, DD04-20): the owner
validator `metrics_owner_validator` runs it on impl 10's start-up validation hook (R-71).
"""

import dataclasses
import datetime
import json
import re
import types
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Final, Literal

import yaml
from pydantic import ValidationError

from herness.core.config import ConfigIssue, HernessConfig, config_hash, get_config
from herness.core.config_validate import OwnerValidator
from herness.core.errors import ConfigError, ToolInputError
from herness.core.ids import canonical_json, sha256_hex
from herness.core.logging import get_logger
from herness.metrics._catalog_checks import raw_sql_problems, sql_problems, template_problems
from herness.metrics.render import render_metric_query
from herness.metrics.settings import (
    EntityType,
    FilterKey,
    MetricDef,
    MetricsCatalogConfig,
    MetricsDefaults,
    Period,
    ScoringConfig,
    Source,
    Unit,
    WeightChangePayload,
    WeightsConfig,
    check_weight_confirmations,
)
from herness.metrics.windows import default_window
from herness.store.ops import list_review_items

__all__ = [
    "LOW_COVERAGE_THRESHOLD",
    "METRIC_FLAGS",
    "SCORE_UNITS",
    "SOURCE_FILTERS",
    "EntityType",
    "MetricCatalog",
    "MetricDef",
    "Period",
    "Unit",
    "catalog_from_config",
    "load_catalog",
    "metrics_owner_validator",
    "unit_for",
    "validate_catalog",
]

_log = get_logger("metrics.catalog")

METRIC_FLAGS: Final[frozenset[str]] = frozenset(
    {"insufficient_sample", "estimate", "unconfirmed_weights", "low_coverage"}
    | {"partial_period", "unweighted"}
)
LOW_COVERAGE_THRESHOLD: Final[float] = 0.8
_SHARED: Final[frozenset[FilterKey]] = frozenset({"service_id", "team_id", "org_id"})
SOURCE_FILTERS: Final[Mapping[Source, frozenset[FilterKey]]] = types.MappingProxyType(
    {
        "incident": _SHARED | {"priority", "cluster_id"},
        "change": _SHARED | {"change_type"},
        "event": _SHARED | {"severity"},
        "work_item": _SHARED | {"work_item_type"},
        "metric_daily": _SHARED,
    }
)


def _units(unit: Unit | Literal["metric"], columns: str) -> dict[str, Unit | Literal["metric"]]:
    return dict.fromkeys(columns.split(), unit)


SCORE_UNITS: Final[Mapping[str, Unit | Literal["metric"]]] = types.MappingProxyType(
    {
        **_units(
            "metric",
            "metrics.metric_value.value score.org.value score.org.peer_median "
            "score.action_lever.current_value score.action_lever.target_value",
        ),
        **_units(
            "usd",
            "score.funding.annual_pain_usd score.funding.addressable_pain_usd "
            "score.funding.effort_cost_usd score.action_lever.delta_usd "
            "score.portfolio.expected_impact_usd score.portfolio.budget_usd "
            "score.funding_attribution.pain_usd",
        ),
        **_units(
            "ratio",
            "score.funding.confidence score.funding.strategic_weight "
            "score.funding.expected_reduction score.funding_attribution.share "
            "score.funding.priority",
        ),
        **_units(
            "score",
            "score.funding.wsjf score.org.z_score score.org.composite score.org.trend_slope",
        ),
        **_units("rank", "score.funding.rank score.org.rank score.portfolio.order_rank"),
        **_units(
            "count",
            "score.funding.n_incidents metrics.metric_value.sample_size score.org.sample_size",
        ),
    }
)

_SCORECARD_AGGREGATIONS: Final = frozenset({"mean", "median", "ratio"})
_VALIDATION_AS_OF: Final = datetime.date(2024, 1, 1)
_MAX_CATALOG_BYTES: Final = 1024 * 1024
_MAX_MESSAGE: Final = 300
_METRICS_FILE: Final = "metrics.yaml"
_WEIGHTS_FILE: Final = "weights.yaml"
_SNAPSHOT_HASH: Final = re.compile(r"cfg_[0-9a-f]{16}")
_UNREADABLE: Final = "previous config snapshot unreadable; every confirmed block is checked as new"


@dataclasses.dataclass(frozen=True, slots=True)
class MetricCatalog:
    """Read-only catalog for the harness, CLI and scoring (U04-23, design 04 §3.1)."""

    config: MetricsCatalogConfig
    version: str = dataclasses.field(init=False)
    _by_name: Mapping[str, MetricDef] = dataclasses.field(init=False, repr=False)

    def __post_init__(self) -> None:
        digest = sha256_hex(canonical_json(self.config.model_dump(mode="json")))
        object.__setattr__(self, "version", digest[:12])
        by_name = types.MappingProxyType({m.name: m for m in self.config.metrics})
        object.__setattr__(self, "_by_name", by_name)

    @property
    def defaults(self) -> MetricsDefaults:
        """`metrics.yaml: defaults`."""
        return self.config.defaults

    @property
    def scoring(self) -> ScoringConfig:
        """`metrics.yaml: scoring`."""
        return self.config.scoring

    def get(self, name: str, /) -> MetricDef:
        """The metric named `name`; ToolInputError listing the enabled names when unknown."""
        metric = self._by_name.get(name)
        if metric is None:
            msg = f"unknown metric {name[:64]}; known: {', '.join(self.names())}"
            raise ToolInputError(msg)
        return metric

    def names(self, *, enabled_only: bool = True) -> list[str]:
        """Metric names, sorted; disabled ones only with `enabled_only=False`."""
        return sorted(n for n, m in self._by_name.items() if m.enabled or not enabled_only)

    def describe(self) -> list[dict[str, object]]:
        """One summary dict per metric, sorted by name (`herness metrics list`)."""
        return [
            {
                "name": m.name,
                "description": m.description,
                "grains": list(m.grains),
                "unit": m.unit,
                "better": m.better,
                "min_sample_size": m.min_sample_size,
                "filters": list(m.filters),
                "estimate": m.estimate,
                "enabled": m.enabled,
            }
            for m in sorted(self._by_name.values(), key=lambda m: m.name)
        ]


def unit_for(column: str, metric: str | None, catalog: MetricCatalog) -> Unit:
    """Unit of a NumberRef-facing column; `metric` entries resolve to the metric's unit."""
    unit = SCORE_UNITS.get(column)
    if unit == "metric" and metric is not None:
        return catalog.get(metric).unit
    if unit is None or unit == "metric":
        msg = f"no unit for {column[:128]}"
        raise ToolInputError(msg)
    return unit


def catalog_from_config(cfg: HernessConfig | None = None, /) -> MetricCatalog:
    """The catalog of the already-validated process config (U04-25); never cached."""
    return MetricCatalog((cfg if cfg is not None else get_config()).metrics)


def load_catalog(path: Path = Path("config/metrics.yaml"), /) -> MetricCatalog:
    """Read, parse and validate a catalog file (U04-24); ConfigError on any error issue."""
    if not path.is_file() or path.stat().st_size > _MAX_CATALOG_BYTES:
        msg = f"catalog file {path} missing or too large"
        raise ConfigError(msg)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        msg = f"catalog file {path} is not valid UTF-8 YAML"
        raise ConfigError(msg) from exc
    try:
        cfg = MetricsCatalogConfig.model_validate(raw)
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_input=False, include_context=False)
        fields = sorted({".".join(str(p) for p in e["loc"]) or "<root>" for e in errors})
        msg = f"catalog file {path} invalid at: {', '.join(fields)}"
        raise ConfigError(msg) from None
    issues = validate_catalog(cfg, weights=get_config().weights)
    errors_found = [i for i in issues if i.severity == "error"]
    if errors_found:
        _log.error("metrics.catalog.rejected", errors=len(errors_found))
        msg = "; ".join(f"{i.path}: {i.message}" for i in errors_found)
        raise ConfigError(msg, issues=errors_found)
    catalog = MetricCatalog(cfg)
    _log.info("metrics.catalog.loaded", version=catalog.version, metrics=len(cfg.metrics))
    return catalog


# --- U04-26 validator ------------------------------------------------------------------------


def _issue(severity: Literal["error", "warn"], path: str, message: str) -> ConfigIssue:
    return ConfigIssue(severity, path, message[:_MAX_MESSAGE], _METRICS_FILE)


def _raw_sql_issues(m: MetricDef, weights: WeightsConfig) -> list[ConfigIssue]:
    """Steps 1-3: weight block names, forbidden text columns, raw SQL tokens."""
    known = weights.blocks()
    issues = [
        _issue("error", f"metrics.{m.name}.uses_weights", f"unknown weight block {block}")
        for block in m.uses_weights
        if block not in known
    ]
    issues += [_issue("error", f"metrics.{m.name}.sql", p) for p in raw_sql_problems(m.sql)]
    return issues


def _grain_issues(m: MetricDef, catalog: MetricCatalog, weights: WeightsConfig) -> list[str]:
    """Steps 4-6 for every grain of `m`: render, parse and check; problems deduplicated."""
    counts = {str(k): v for k, v in catalog.defaults.windows.items()}
    window = default_window("week", _VALIDATION_AS_OF, weights.business_timezone, counts)
    filters: dict[FilterKey, list[object]] = {
        key: [1] if key == "priority" else ["x"] for key in m.filters
    }
    problems: list[str] = []
    for grain in m.grains:
        try:
            rq = render_metric_query(
                m,
                entity_type=grain,
                window=window,
                filters=filters,
                entity_ids=["x"],
                catalog=catalog,
                weights=weights,
            )
        except ConfigError as exc:
            problems.append(f"grain {grain} not supported: {exc.message}")
            continue
        problems.extend(sql_problems(rq.sql))
        for source in sorted(rq.sources):
            unsupported = sorted(set(m.filters) - SOURCE_FILTERS[source])
            problems.extend(f"filter {k} not supported by source {source}" for k in unsupported)
    return list(dict.fromkeys(problems))


def _metric_issues(
    m: MetricDef, catalog: MetricCatalog, weights: WeightsConfig
) -> Iterator[ConfigIssue]:
    raw = _raw_sql_issues(m, weights)
    yield from raw
    if not raw:  # never render SQL that failed the raw checks
        for problem in _grain_issues(m, catalog, weights):
            yield _issue("error", f"metrics.{m.name}.sql", problem)
    if m.usd_model is not None and m.better != "lower":
        yield _issue("error", f"metrics.{m.name}.better", "usd_model needs better lower")
    if m.usd_model is not None and m.name not in catalog.scoring.org.metrics:
        yield _issue("warn", f"metrics.{m.name}.usd_model", "usd_model unused")


def _scorecard_issues(catalog: MetricCatalog) -> Iterator[ConfigIssue]:
    """Step 8: scorecard metrics are enabled rate-like metrics with team and org grains."""
    enabled = set(catalog.names())
    for name in sorted(catalog.scoring.org.metrics):
        m = catalog.get(name) if name in enabled else None
        rate_like = m is not None and m.aggregation in _SCORECARD_AGGREGATIONS
        if m is None or not rate_like or not {"team", "org"} <= set(m.grains):
            message = f"scorecard metric {name} is count-like or lacks team/org grain"
            yield _issue("error", f"scoring.org.metrics.{name}", message)


def _lever_issues(scoring: ScoringConfig) -> Iterator[ConfigIssue]:
    for key, template in sorted(scoring.levers.templates.items()):
        for problem in template_problems(template):
            yield _issue("error", f"scoring.levers.templates.{key}", f"template {problem}")


def validate_catalog(cfg: MetricsCatalogConfig, /, *, weights: WeightsConfig) -> list[ConfigIssue]:
    """All catalog rules of design 04 §4.1, §5.8, §5.9 and §10.5 (U04-26); empty iff valid."""
    catalog = MetricCatalog(cfg)
    issues = [i for m in cfg.metrics for i in _metric_issues(m, catalog, weights)]
    issues.extend(_scorecard_issues(catalog))
    issues.extend(_lever_issues(cfg.scoring))
    return issues


# --- U04-83 owner validator ------------------------------------------------------------------


def _previous_weights(cfg: HernessConfig) -> tuple[WeightsConfig | None, list[ConfigIssue]]:
    """Weights of the snapshot `LAST` names; None on a new install or an unreadable snapshot."""
    snaps = Path(cfg.paths.data) / "config_snapshots"
    last = snaps / "LAST"
    if not last.is_file():
        return None, []
    unreadable = [ConfigIssue("warn", "weights", _UNREADABLE, _WEIGHTS_FILE)]
    try:
        name = last.read_text(encoding="utf-8").strip()
        if _SNAPSHOT_HASH.fullmatch(name) is None:  # LAST names a file: accept a hash only
            return None, unreadable
        snapshot = snaps / f"{name}.yaml"
        if not snapshot.is_file():
            return None, []
        data = yaml.safe_load(snapshot.read_text(encoding="utf-8"))
        section = data["weights"] if isinstance(data, dict) else None
        # JSON mode: the snapshot's integer map keys come back as strings after a YAML dump.
        return WeightsConfig.model_validate_json(json.dumps(section)), []
    except (OSError, UnicodeDecodeError, yaml.YAMLError, KeyError, TypeError, ValidationError):
        return None, unreadable


def _approved_payloads() -> list[WeightChangePayload]:
    approved: list[WeightChangePayload] = []
    for item in list_review_items(kind="weight_change", status="approved", limit=5000):
        try:
            approved.append(WeightChangePayload.model_validate(dict(item.payload)))
        except ValidationError:
            _log.warning("metrics.weights.payload_invalid", item_id=item.item_id)
    return approved


def metrics_owner_validator(cfg: HernessConfig, *, offline: bool) -> list[ConfigIssue]:
    """Impl 04 owner validator (U04-83, R-71): catalog cross-check and the weight gate."""
    del offline  # the checks do no network I/O
    issues = validate_catalog(cfg.metrics, weights=cfg.weights)
    previous, snapshot_issues = _previous_weights(cfg)
    new_hash = config_hash(cfg)
    weight_issues = check_weight_confirmations(
        previous, cfg.weights, new_hash, _approved_payloads()
    )
    issues.extend(ConfigIssue(w.severity, w.path, w.message, _WEIGHTS_FILE) for w in weight_issues)
    issues.extend(snapshot_issues)
    rejected = sorted(w.path.split(".")[1] for w in weight_issues if w.severity == "error")
    if rejected:
        _log.error("metrics.weights.confirmation_rejected", blocks=rejected, config_hash=new_hash)
    return issues


# Static check that the validator satisfies the impl 10 hook protocol (U10-109).
_OWNER_VALIDATOR: Final[OwnerValidator] = metrics_owner_validator
# T09-20: the composition roots register this as
# register_owner_validator("metrics", metrics_owner_validator) before init_config.
