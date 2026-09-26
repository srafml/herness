"""Bind parameter types and config-derived bind candidates (impl 04 U04-34, U04-36, U04-37).

Private split of `herness.metrics.render` (module budget); `render` re-exports the public names.
"""

import types
from collections.abc import Mapping
from typing import Final, Protocol

from herness.metrics.settings import MetricDef, MetricsDefaults, ScoringConfig, WeightsConfig

__all__ = ["BIND_TYPES", "CatalogView", "default_binds", "weight_binds"]

# Names per DuckDB type, whitespace separated (U04-34 lists them by group).
_TYPED: Final[Mapping[str, str]] = {
    "TIMESTAMPTZ": "window_start window_end as_of_ts",
    "DATE": "window_start_date window_end_date as_of",
    "BIGINT": "min_n",
    "SMALLINT[]": "f_priority",
    "BOOLEAN": "w_fallback_enabled unconfirmed",
    "VARCHAR": ("tz metric entity_type period unit pg_entity_type pg_entity_id pg_metric scenario"),
    "VARCHAR[]": (
        "entity_ids static_flags f_service_id f_team_id f_org_id f_cluster_id "
        "f_work_item_type f_severity f_change_type d_exclude_incident_states "
        "d_exclude_close_codes d_noise_severities d_failure_outcomes s_org_metrics "
        "s_lower_better s_lever_models_k s_lever_models_v s_templates_k s_templates_v "
        "s_metric_units_k s_metric_units_v w_sw_portfolio_k w_sw_org_k "
        "w_er_override_k w_capacity_k portfolio_team_k s_count_metrics s_unconfirmed_models"
    ),
    "INTEGER": (
        "d_max_resolve_days d_repeat_window_days s_min_direct_links s_window_days "
        "s_min_peer_group s_min_service_peers s_top_entities w_fallback_max_priority "
        "w_cf_min_incidents w_horizon_quarters observed_days_min"
    ),
    "INTEGER[]": "w_downtime_k w_prio_mult_k w_outage_k w_effort_k",
    "DOUBLE": (
        "d_cluster_min_membership d_change_link_min_score s_cluster_weight "
        "s_root_cause_weight s_service_weight s_trend_weight s_min_weight_coverage "
        "w_downtime_default w_engineer_hour w_hours_per_point w_cap_hours "
        "w_business_share w_max_toil_hours w_triage_minutes w_reassign_hours "
        "w_reopen_factor w_backout_hours w_sla_penalty w_sw_default w_sw_clip_min "
        "w_sw_clip_max w_er_epic w_er_feature w_er_initiative w_er_cluster_fix "
        "w_cf_effort_hours w_cf_min_pain w_cf_max_linked_share w_capacity_default "
        "capacity_default"
    ),
    "DOUBLE[]": (
        "s_org_weights w_downtime_v w_prio_mult_v w_outage_v w_effort_v "
        "w_sw_portfolio_v w_sw_org_v w_er_override_v w_capacity_v portfolio_team_v"
    ),
}

# DuckDB type of every bind parameter; p(name) renders CAST($name AS <type>) (DD04-09).
BIND_TYPES: Final[Mapping[str, str]] = types.MappingProxyType(
    {name: sql_type for sql_type, names in _TYPED.items() for name in names.split()}
)


class CatalogView(Protocol):
    """The read surface of `herness.metrics.catalog.MetricCatalog` (U04-23) used here."""

    @property
    def defaults(self) -> MetricsDefaults: ...

    @property
    def scoring(self) -> ScoringConfig: ...

    def get(self, name: str, /) -> MetricDef: ...

    def names(self, *, enabled_only: bool = True) -> list[str]: ...


def _pairs[K: (int, str), V](mapping: Mapping[K, V]) -> tuple[list[K], list[V]]:
    """Keys sorted ascending and their values in the same order."""
    keys = sorted(mapping)
    return keys, [mapping[k] for k in keys]


def default_binds(catalog: CatalogView, /) -> dict[str, object]:
    """Candidate `d_*` and `s_*` bind values from the catalog config (U04-36)."""
    d = catalog.defaults
    s = catalog.scoring
    metrics = {name: catalog.get(name) for name in catalog.names()}
    org_k, org_v = _pairs(s.org.metrics)
    lever_k, lever_v = _pairs({n: m.usd_model for n, m in metrics.items() if m.usd_model})
    tmpl_k, tmpl_v = _pairs({str(k): v for k, v in s.levers.templates.items()})
    unit_k, unit_v = _pairs({n: m.unit for n, m in metrics.items()})
    tiers = s.funding.tier_weights
    return {
        "d_exclude_incident_states": sorted(d.exclude_incident_states),
        "d_exclude_close_codes": sorted(d.exclude_close_codes),
        "d_noise_severities": sorted(d.noise_severities),
        "d_failure_outcomes": sorted(d.failure_outcomes),
        "d_max_resolve_days": d.max_resolve_days,
        "d_repeat_window_days": d.repeat_window_days,
        "d_cluster_min_membership": d.cluster_min_membership,
        "d_change_link_min_score": d.change_link_min_score,
        "s_cluster_weight": tiers.cluster_weight,
        "s_root_cause_weight": tiers.root_cause_weight,
        "s_service_weight": tiers.service_weight,
        "s_trend_weight": s.org.trend_weight,
        "s_min_weight_coverage": s.org.min_weight_coverage,
        "s_min_direct_links": s.funding.min_direct_links,
        "s_window_days": s.funding.window_days,
        "s_min_peer_group": s.org.min_peer_group,
        "s_min_service_peers": s.peer_group.min_service_peers,
        "s_top_entities": s.levers.top_entities,
        "s_org_metrics": org_k,
        "s_org_weights": org_v,
        "s_lower_better": [k for k in org_k if k in metrics and metrics[k].better == "lower"],
        "s_lever_models_k": lever_k,
        "s_lever_models_v": lever_v,
        "s_templates_k": tmpl_k,
        "s_templates_v": tmpl_v,
        "s_metric_units_k": unit_k,
        "s_metric_units_v": unit_v,
    }


def weight_binds(weights: WeightsConfig, /) -> dict[str, object]:
    """Candidate `w_*` bind values; maps become paired key and value lists (U04-37)."""
    w = weights
    down_k, down_v = _pairs(w.cost_per_downtime_hour.by_criticality)
    prio_k, prio_v = _pairs(w.priority_impact_multiplier.values)
    out_k, out_v = _pairs(w.impact_fallback.outage_fraction)
    eff_k, eff_v = _pairs(w.toil.effort_factor)
    swp_k, swp_v = _pairs(w.strategic_weights.portfolio)
    swo_k, swo_v = _pairs(w.strategic_weights.org)
    er_k, er_v = _pairs(w.expected_reduction.overrides)
    cap_k, cap_v = _pairs(w.team_capacity_points_per_quarter.teams)
    er = w.expected_reduction
    cf = w.cluster_fix
    return {
        "w_downtime_k": down_k,
        "w_downtime_v": down_v,
        "w_downtime_default": w.cost_per_downtime_hour.default,
        "w_engineer_hour": w.cost_per_engineer_hour.value,
        "w_hours_per_point": w.hours_per_story_point.value,
        "w_prio_mult_k": prio_k,
        "w_prio_mult_v": prio_v,
        "w_fallback_enabled": w.impact_fallback.enabled,
        "w_fallback_max_priority": w.impact_fallback.max_priority,
        "w_outage_k": out_k,
        "w_outage_v": out_v,
        "w_cap_hours": w.impact_fallback.cap_hours,
        "w_effort_k": eff_k,
        "w_effort_v": eff_v,
        "w_business_share": w.toil.business_share,
        "w_max_toil_hours": w.toil.max_hours_per_incident,
        "w_triage_minutes": w.toil.triage_minutes_per_alert,
        "w_reassign_hours": w.toil.reassignment_hours,
        "w_reopen_factor": w.toil.reopen_rework_factor,
        "w_backout_hours": w.change.backout_effort_hours,
        "w_sla_penalty": w.sla_penalty_usd.value,
        "w_sw_default": w.strategic_weights.default,
        "w_sw_clip_min": w.strategic_weights.clip[0],
        "w_sw_clip_max": w.strategic_weights.clip[1],
        "w_sw_portfolio_k": swp_k,
        "w_sw_portfolio_v": swp_v,
        "w_sw_org_k": swo_k,
        "w_sw_org_v": swo_v,
        "w_er_epic": er.epic,
        "w_er_feature": er.feature,
        "w_er_initiative": er.initiative,
        "w_er_cluster_fix": er.cluster_fix,
        "w_er_override_k": er_k,
        "w_er_override_v": er_v,
        "w_cf_effort_hours": cf.effort_hours,
        "w_cf_min_incidents": cf.min_incidents_12m,
        "w_cf_min_pain": cf.min_annual_pain_usd,
        "w_cf_max_linked_share": cf.max_linked_share,
        "w_capacity_default": w.team_capacity_points_per_quarter.default,
        "w_capacity_k": cap_k,
        "w_capacity_v": cap_v,
        "w_horizon_quarters": w.portfolio.horizon_quarters,
    }
