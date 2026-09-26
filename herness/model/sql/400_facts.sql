{#- Stage 400 fact tables (impl 04 §3.5, U04-41 ... U04-45; design 04 §4.2, §5.1, §5.3, §5.4).
    Each marker line `-- @statement metrics.<table>` starts one SELECT body; marker lines are
    removed before rendering (U04-46) and `materialize_facts` (U04-47) stores each body with
    its query_id. Bodies render in the metric sandbox: every runtime value is a p() bind, and
    prose lives only in Jinja comments, so no SQL comment reaches the recorded SQL.
    No body selects a free-form column (TH04-04). Priority NULL counts as 5 in every weight
    lookup. Recursion is cut at the depth caps below (TH04-13). -#}
-- @statement metrics.org_closure
{#- U04-41: ancestor-or-self pairs of core.org with the minimum depth. -#}
{%- set ORG_MAX_DEPTH = 20 %}
WITH RECURSIVE walk (org_id, ancestor_org_id, depth) AS (
    SELECT o.org_id, o.org_id, 0
    FROM core.org o
    UNION ALL
    SELECT w.org_id, p.parent_org_id, w.depth + 1
    FROM walk w
    JOIN core.org p ON p.org_id = w.ancestor_org_id
    WHERE p.parent_org_id IS NOT NULL AND w.depth < {{ ORG_MAX_DEPTH }}
)
SELECT org_id, ancestor_org_id, CAST(min(depth) AS INTEGER) AS depth
FROM walk
GROUP BY org_id, ancestor_org_id
-- @statement metrics.work_item_closure
{#- U04-42: ancestor-or-self pairs over parent_key; candidate_record_id is the nearest
    funding candidate (design 04 §5.4), ties on depth to the lowest ancestor_record_id. -#}
{%- set WORK_ITEM_MAX_DEPTH = 10 %}
WITH RECURSIVE walk (record_id, ancestor_record_id, parent_key, depth) AS (
    SELECT w.record_id, w.record_id, w.parent_key, 0
    FROM core.work_item w
    UNION ALL
    SELECT a.record_id, p.record_id, p.parent_key, a.depth + 1
    FROM walk a
    JOIN core.work_item p ON p.key = a.parent_key
    WHERE a.depth < {{ WORK_ITEM_MAX_DEPTH }}
),
pairs AS (
    SELECT record_id, ancestor_record_id, min(depth) AS depth
    FROM walk
    GROUP BY record_id, ancestor_record_id
),
candidate AS (
    SELECT pr.record_id, pr.ancestor_record_id AS candidate_record_id
    FROM pairs pr
    JOIN core.work_item c ON c.record_id = pr.ancestor_record_id
    WHERE c.type IN ('initiative', 'epic', 'feature')
      AND c.status_category IN ('todo', 'in_progress')
    QUALIFY row_number() OVER (
        PARTITION BY pr.record_id ORDER BY pr.depth, pr.ancestor_record_id
    ) = 1
)
SELECT pr.record_id, pr.ancestor_record_id, cd.candidate_record_id,
       CAST(pr.depth AS INTEGER) AS depth
FROM pairs pr
LEFT JOIN candidate cd ON cd.record_id = pr.record_id
-- @statement metrics.incident_fact
{#- U04-43: one row per core.incident, columns in design 04 §4.2 order. `is_repeat` compares
    with the previous incident of the same cluster and service; partitioning by `excluded`
    keeps excluded incidents out of that window. -#}
{%- set PRIO = 'coalesce(b.priority, 5)' %}
WITH member AS (
    SELECT m.record_id, m.cluster_id, m.membership_prob
    FROM enrich.cluster_member m
    WHERE m.membership_prob >= {{ p('d_cluster_min_membership') }}
    QUALIFY row_number() OVER (
        PARTITION BY m.record_id ORDER BY m.membership_prob DESC, m.cluster_id
    ) = 1
),
linked AS (
    SELECT DISTINCT l.incident_id
    FROM enrich.incident_change_link l
    WHERE l.score >= {{ p('d_change_link_min_score') }}
),
base AS (
    SELECT i.record_id, i.number, i.opened_at, i.resolved_at, i.priority, i.service_id,
           i.team_id, t.org_id, s.criticality, m.cluster_id, m.membership_prob,
           (list_contains({{ p('d_exclude_incident_states') }}, coalesce(i.state, ''))
            OR list_contains({{ p('d_exclude_close_codes') }}, coalesce(i.close_code, '')))
               AS excluded,
           CASE WHEN i.resolved_at IS NOT NULL AND i.opened_at IS NOT NULL
                     AND i.resolved_at >= i.opened_at
                     AND date_diff('second', i.opened_at, i.resolved_at)
                         <= CAST({{ p('d_max_resolve_days') }} AS BIGINT) * 86400
                THEN CAST(date_diff('second', i.opened_at, i.resolved_at) AS DOUBLE) / 3600.0
           END AS resolve_h,
           i.business_duration_s, i.customer_impact_minutes, i.reopen_count,
           i.reassignment_count, i.sla_breached,
           (i.caused_by_change_id IS NOT NULL OR k.incident_id IS NOT NULL) AS change_caused
    FROM core.incident i
    LEFT JOIN core.team t ON t.team_id = i.team_id
    LEFT JOIN core.service s ON s.service_id = i.service_id
    LEFT JOIN member m ON m.record_id = i.record_id
    LEFT JOIN linked k ON k.incident_id = i.record_id
),
impact AS (
    SELECT b.*,
           CASE WHEN b.resolve_h IS NOT NULL AND b.business_duration_s >= 0
                THEN CAST(b.business_duration_s AS DOUBLE) / 3600.0
           END AS resolve_bh,
           CASE WHEN b.excluded THEN 'excluded'
                WHEN b.customer_impact_minutes >= 0 THEN 'measured'
                WHEN {{ p('w_fallback_enabled') }}
                     AND {{ PRIO }} <= {{ p('w_fallback_max_priority') }}
                     AND b.resolve_h IS NOT NULL THEN 'fallback'
                ELSE 'none'
           END AS impact_kind
    FROM base b
),
cost AS (
    SELECT b.*,
           CAST(CASE b.impact_kind
                WHEN 'measured' THEN b.customer_impact_minutes / 60.0
                WHEN 'fallback' THEN least(
                    b.resolve_h * {{ lkp(p('w_outage_k'), p('w_outage_v'), PRIO, '0') }},
                    {{ p('w_cap_hours') }})
                WHEN 'none' THEN 0
           END AS DOUBLE) AS impact_h,
           CASE WHEN NOT b.excluded
                     AND coalesce(b.resolve_bh, b.resolve_h * {{ p('w_business_share') }})
                         IS NOT NULL
                THEN least(
                    coalesce(b.resolve_bh, b.resolve_h * {{ p('w_business_share') }})
                        * {{ lkp(p('w_effort_k'), p('w_effort_v'), PRIO, '0') }},
                    {{ p('w_max_toil_hours') }})
           END AS toil_h,
           lag(b.opened_at) OVER (
               PARTITION BY b.excluded, b.cluster_id, b.service_id
               ORDER BY b.opened_at, b.record_id
           ) AS prev_opened_at
    FROM impact b
),
money AS (
    SELECT b.*,
           CASE WHEN NOT b.excluded THEN CAST(
               b.impact_h
               * {{ lkp(p('w_downtime_k'), p('w_downtime_v'), 'b.criticality',
                        p('w_downtime_default')) }}
               * {{ lkp(p('w_prio_mult_k'), p('w_prio_mult_v'), PRIO, '0') }}
               AS DECIMAL(18, 2))
           END AS downtime_usd,
           CASE WHEN NOT b.excluded
                THEN CAST(coalesce(b.toil_h, 0) * {{ p('w_engineer_hour') }} AS DECIMAL(18, 2))
           END AS toil_usd
    FROM cost b
)
SELECT b.record_id, b.number, b.opened_at, b.resolved_at, b.priority, b.service_id, b.team_id,
       b.org_id, b.criticality, b.cluster_id, b.membership_prob, b.excluded,
       b.resolve_h, b.resolve_bh, b.impact_h, b.impact_kind = 'fallback' AS impact_estimated,
       b.toil_h, b.downtime_usd, b.toil_usd,
       CAST(b.downtime_usd + b.toil_usd AS DECIMAL(18, 2)) AS total_usd,
       coalesce(NOT b.excluded AND b.cluster_id IS NOT NULL AND b.service_id IS NOT NULL
                AND b.opened_at IS NOT NULL
                AND date_diff('second', b.prev_opened_at, b.opened_at)
                    <= CAST({{ p('d_repeat_window_days') }} AS BIGINT) * 86400,
                false) AS is_repeat,
       coalesce(b.reopen_count, 0) > 0 AS is_reopened,
       coalesce(b.reassignment_count, 0) > 0 AS is_reassigned,
       b.sla_breached,
       b.change_caused
FROM money b
