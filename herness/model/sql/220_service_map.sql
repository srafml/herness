{#
  Service map and service name lookup (impl 02 U02-117; design 02 §4.3; D1).
  Candidates in precedence order: mappings.yaml overrides (rank 1), CMDB owner and support
  group (rank 2), approved mapping suggestions (rank 3). stg.approved_mapping holds only
  human-approved items (U02-60): pending suggestions never reach this table (LLM04).
  One row per identity key: Jira rows by (project, component), other rows by
  (service, role, team); the lowest rank wins, then the lowest service_id and team_id.
#}
{% import "_macros.jinja" as m %}

{# ---------------------------------------------------------------- core.service_map #}
CREATE OR REPLACE TABLE core.service_map AS
WITH candidate AS (
    SELECT o.service_id, o.team_id, o.jira_project, o.jira_component,
        coalesce(o.org_id, t.org_id, s.org_id) AS org_id, o.role,
        'override' AS link_source, CAST(1.0 AS DOUBLE) AS confidence, 1 AS rnk
    FROM stg.service_override AS o
    LEFT JOIN core.team AS t ON t.team_id = o.team_id
    LEFT JOIN core.service AS s ON s.service_id = o.service_id
    UNION ALL
    SELECT s.service_id, s.business_owner_team_id, NULL, NULL, t.org_id, 'owner', 'cmdb', 1.0, 2
    FROM core.service AS s
    JOIN core.team AS t ON t.team_id = s.business_owner_team_id
    UNION ALL
    SELECT s.service_id, t.team_id, NULL, NULL, t.org_id, 'support', 'cmdb', 1.0, 2
    FROM core.service AS s
    JOIN stg.sn_ci AS c ON {{ m.rid('servicenow:cmdb_ci', 'c.sys_id') }} = s.service_id
    JOIN core.team AS t ON t.team_id = {{ m.rid('servicenow:sys_user_group', 'c.support_group') }}
    WHERE t.team_id IS DISTINCT FROM s.business_owner_team_id
    UNION ALL
    SELECT a.service_id, a.team_id,
        CASE WHEN a.subject_type = 'jira_component' THEN a.jira_project END,
        CASE WHEN a.subject_type = 'jira_component' THEN a.jira_component END,
        CASE WHEN a.subject_type = 'jira_component' THEN coalesce(t.org_id, s.org_id) ELSE t.org_id END,
        CASE WHEN a.subject_type = 'jira_component' THEN 'delivery' ELSE 'support' END,
        'suggested_approved', a.score, 3
    FROM stg.approved_mapping AS a
    LEFT JOIN core.team AS t ON t.team_id = a.team_id
    LEFT JOIN core.service AS s ON s.service_id = a.service_id
    WHERE a.subject_type IN ('jira_component', 'team')
),
keyed AS (
    SELECT *,
        CASE WHEN jira_project IS NOT NULL THEN 'jira' ELSE 'team' END AS k0,
        CASE WHEN jira_project IS NOT NULL THEN jira_project ELSE service_id END AS k1,
        CASE WHEN jira_project IS NOT NULL THEN coalesce(jira_component, '') ELSE role END AS k2,
        CASE WHEN jira_project IS NOT NULL THEN NULL ELSE team_id END AS k3
    FROM candidate
)
SELECT
    CAST(service_id AS VARCHAR) AS service_id,
    CAST(team_id AS VARCHAR) AS team_id,
    CAST(jira_project AS VARCHAR) AS jira_project,
    CAST(jira_component AS VARCHAR) AS jira_component,
    CAST(org_id AS VARCHAR) AS org_id,
    CAST(role AS VARCHAR) AS role,
    CAST(link_source AS VARCHAR) AS link_source,
    CAST(confidence AS DOUBLE) AS confidence
FROM keyed
QUALIFY row_number() OVER (
    PARTITION BY k0, k1, k2, k3
    ORDER BY rnk, service_id, team_id NULLS LAST, org_id NULLS LAST, confidence DESC NULLS LAST
) = 1;

{# ---------------------------------------------------------------- stg.service_name_lookup #}
{#- every override alias, plus each service name owned by exactly one service that is not
    already an alias (ambiguous names are left out; used by 260/270 via lower(name)). -#}
CREATE OR REPLACE TABLE stg.service_name_lookup AS
SELECT CAST(alias_lc AS VARCHAR) AS name_lc, CAST(service_id AS VARCHAR) AS service_id
FROM stg.service_alias
WHERE alias_lc IS NOT NULL
UNION ALL
SELECT lower(name), any_value(service_id)
FROM core.service
WHERE name IS NOT NULL
    AND lower(name) NOT IN (SELECT alias_lc FROM stg.service_alias WHERE alias_lc IS NOT NULL)
GROUP BY lower(name)
HAVING count(DISTINCT service_id) = 1;
