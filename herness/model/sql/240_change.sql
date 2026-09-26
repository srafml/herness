{#
  Canonical changes (impl 02 U02-119; design 02 §4.3). One row per live
  stg.sn_change_request record. outcome: the staging outcome (enum
  servicenow.change_close_code on close_code; an unmapped code is NULL and is counted by
  110 as a cast failure of stg.sn_change_request.outcome in stg.cast_stats), else
  `canceled` when lower(state) is canceled/cancelled. service_id follows the core.incident
  rule: business_service when set, else the single core.service that is the parent of a
  cmdb_rel_ci row whose child is the change's CI (NULL when zero or several).
#}
{% import "_macros.jinja" as m %}
{% set CI = 'servicenow:cmdb_ci' %}

CREATE OR REPLACE TABLE core.change AS
WITH ci_service AS (
    SELECT trim(r.child) AS ci,
        CASE WHEN count(DISTINCT s.service_id) = 1 THEN min(s.service_id) END AS service_id
    FROM stg.sn_rel_ci AS r
    JOIN core.service AS s ON s.service_id = {{ m.rid(CI, 'r.parent') }}
    WHERE nullif(trim(r.child), '') IS NOT NULL
    GROUP BY trim(r.child)
)
SELECT
    CAST(c.record_id AS VARCHAR) AS record_id,
    CAST(c."number" AS VARCHAR) AS "number",
    CAST(c.state AS VARCHAR) AS state,
    CAST(c.risk AS VARCHAR) AS risk,
    CAST(c."type" AS VARCHAR) AS "type",
    CAST(c.opened_at AS TIMESTAMPTZ) AS opened_at,
    CAST(c.planned_start AS TIMESTAMPTZ) AS planned_start,
    CAST(c.planned_end AS TIMESTAMPTZ) AS planned_end,
    CAST(c.actual_start AS TIMESTAMPTZ) AS actual_start,
    CAST(c.actual_end AS TIMESTAMPTZ) AS actual_end,
    CAST(coalesce({{ m.rid(CI, 'c.business_service') }}, cs.service_id) AS VARCHAR) AS service_id,
    CAST({{ m.rid(CI, 'c.cmdb_ci') }} AS VARCHAR) AS ci_id,
    CAST({{ m.rid('servicenow:sys_user_group', 'c.assignment_group') }} AS VARCHAR) AS team_id,
    CAST(coalesce(
        c.outcome,
        CASE WHEN lower(c.state) IN ('canceled', 'cancelled') THEN 'canceled' END
    ) AS VARCHAR) AS outcome,
    CAST(c.short_description AS VARCHAR) AS short_description,
    CAST(c.description AS VARCHAR) AS description,
    CAST(c.source_updated_at AS TIMESTAMPTZ) AS source_updated_at
FROM stg.sn_change_request AS c
LEFT JOIN ci_service AS cs ON cs.ci = trim(c.cmdb_ci);
