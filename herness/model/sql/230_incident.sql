{#
  Canonical incidents (impl 02 U02-118; design 02 §4.3). One row per live stg.sn_incident
  record. References become record IDs (§3.10). service_id: business_service when set,
  else the single core.service that is the parent of a cmdb_rel_ci row whose child is the
  incident's CI (NULL when zero or several). team_id: the latest version's assignment
  group (OI-11). sla_breached: bool_or(task_sla.has_breached) when the incident has
  task_sla rows, else NOT made_sla. acknowledged_at and customer_impact_minutes come from
  configured custom fields only (NULL otherwise; never estimated). content_hash is filled
  by 300_attach_decisions.sql. The table is replaced, so a re-run gives the same rows.
#}
{% import "_macros.jinja" as m %}
{% set CI = 'servicenow:cmdb_ci' %}

CREATE OR REPLACE TABLE core.incident AS
WITH ci_service AS (
    SELECT trim(r.child) AS ci,
        CASE WHEN count(DISTINCT s.service_id) = 1 THEN min(s.service_id) END AS service_id
    FROM stg.sn_rel_ci AS r
    JOIN core.service AS s ON s.service_id = {{ m.rid(CI, 'r.parent') }}
    WHERE nullif(trim(r.child), '') IS NOT NULL
    GROUP BY trim(r.child)
),
sla AS (
    SELECT trim(task) AS task, bool_or(has_breached) AS breached
    FROM stg.sn_task_sla
    WHERE nullif(trim(task), '') IS NOT NULL
    GROUP BY trim(task)
)
SELECT
    CAST(i.record_id AS VARCHAR) AS record_id,
    CAST(i."number" AS VARCHAR) AS "number",
    CAST(i.opened_at AS TIMESTAMPTZ) AS opened_at,
    CAST(i.acknowledged_at AS TIMESTAMPTZ) AS acknowledged_at,
    CAST(i.resolved_at AS TIMESTAMPTZ) AS resolved_at,
    CAST(i.closed_at AS TIMESTAMPTZ) AS closed_at,
    CAST(i.priority AS SMALLINT) AS priority,
    CAST(i.state AS VARCHAR) AS state,
    CAST(coalesce({{ m.rid(CI, 'i.business_service') }}, cs.service_id) AS VARCHAR) AS service_id,
    CAST({{ m.rid(CI, 'i.cmdb_ci') }} AS VARCHAR) AS ci_id,
    CAST({{ m.rid('servicenow:sys_user_group', 'i.assignment_group') }} AS VARCHAR) AS team_id,
    CAST(i.reassignment_count AS INTEGER) AS reassignment_count,
    CAST(i.reopen_count AS INTEGER) AS reopen_count,
    CAST(i.short_description AS VARCHAR) AS short_description,
    CAST(i.description AS VARCHAR) AS description,
    CAST(i.close_notes AS VARCHAR) AS close_notes,
    CAST(i.close_code AS VARCHAR) AS close_code,
    CAST({{ m.rid('servicenow:problem', 'i.problem_id') }} AS VARCHAR) AS problem_id,
    CAST({{ m.rid('servicenow:change_request', 'i.caused_by') }} AS VARCHAR) AS caused_by_change_id,
    CAST(CASE WHEN sla.task IS NOT NULL THEN sla.breached ELSE NOT i.made_sla END AS BOOLEAN) AS sla_breached,
    CAST(i.business_duration_s AS BIGINT) AS business_duration_s,
    CAST(i.customer_impact_minutes AS DOUBLE) AS customer_impact_minutes,
    CAST(NULL AS VARCHAR) AS content_hash,
    CAST(i.source_updated_at AS TIMESTAMPTZ) AS source_updated_at
FROM stg.sn_incident AS i
LEFT JOIN ci_service AS cs ON cs.ci = trim(i.cmdb_ci)
LEFT JOIN sla ON sla.task = trim(i.source_key);
