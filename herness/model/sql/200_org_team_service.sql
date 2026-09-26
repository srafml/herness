{#
  Canonical org, team and service (impl 02 U02-116; design 02 §4.3).
  Org mode is `department` when stg.sn_department has a row, else `group_hierarchy`
  (a group with a child group is an org, a group without one is a team). Services are the
  stg.sn_ci rows whose class is configured (stg.service_ci_class); criticality comes from
  stg.sn_ci, i.e. from cmdb_ci_service only (R-60). IDs are record IDs (§3.10); every
  table is replaced, so a re-run gives the same rows.
#}
{% import "_macros.jinja" as m %}
{% set DEPT = 'servicenow:cmn_department' %}
{% set GRP = 'servicenow:sys_user_group' %}

{# ---------------------------------------------------------------- core.org #}
CREATE OR REPLACE TABLE core.org AS
WITH dept AS (
    SELECT {{ m.rid(DEPT, 'sys_id') }} AS org_id, name, {{ m.rid(DEPT, 'parent') }} AS parent_id,
        nullif(trim(cost_center), '') AS cost_center, record_id
    FROM stg.sn_department
),
grp AS (
    SELECT {{ m.rid(GRP, 'sys_id') }} AS gid, name, {{ m.rid(GRP, 'parent') }} AS parent_id,
        nullif(trim(cost_center), '') AS cost_center, record_id
    FROM stg.sn_group
),
org_grp AS (
    SELECT * FROM grp WHERE gid IN (SELECT parent_id FROM grp WHERE parent_id IS NOT NULL)
),
orgs AS (
    SELECT org_id, name, parent_id, cost_center, record_id FROM dept
    UNION ALL
    SELECT gid, name, parent_id, cost_center, record_id FROM org_grp
    WHERE NOT EXISTS (SELECT 1 FROM stg.sn_department)
)
SELECT
    o.org_id,
    o.name,
    CASE WHEN o.parent_id IN (SELECT org_id FROM orgs WHERE org_id IS NOT NULL) THEN o.parent_id END AS parent_org_id,
    o.cost_center,
    'servicenow' AS source
FROM orgs AS o
WHERE o.org_id IS NOT NULL
QUALIFY row_number() OVER (PARTITION BY o.org_id ORDER BY o.record_id) = 1;

{# ---------------------------------------------------------------- core.team #}
{#- department mode: every group, org = the department with the same non-empty cost
    center (lowest department sys_id, OI-04); hierarchy mode: leaf groups, org = the
    parent group when it is an org. -#}
CREATE OR REPLACE TABLE core.team AS
WITH grp AS (
    SELECT {{ m.rid(GRP, 'sys_id') }} AS team_id, name, {{ m.rid(GRP, 'parent') }} AS parent_id,
        nullif(trim(cost_center), '') AS cost_center, active, record_id
    FROM stg.sn_group
),
dept_by_cc AS (
    SELECT nullif(trim(cost_center), '') AS cost_center,
        arg_min({{ m.rid(DEPT, 'sys_id') }}, sys_id) AS org_id
    FROM stg.sn_department
    WHERE nullif(trim(cost_center), '') IS NOT NULL AND nullif(trim(sys_id), '') IS NOT NULL
    GROUP BY 1
),
dept_mode AS (
    SELECT EXISTS (SELECT 1 FROM stg.sn_department) AS dept_on
)
SELECT
    g.team_id,
    g.name,
    CASE WHEN (SELECT dept_on FROM dept_mode) THEN d.org_id ELSE o.org_id END AS org_id,
    'servicenow' AS source,
    coalesce(g.active, true) AS active
FROM grp AS g
LEFT JOIN dept_by_cc AS d ON d.cost_center = g.cost_center
LEFT JOIN core.org AS o ON o.org_id = g.parent_id
WHERE g.team_id IS NOT NULL
    AND ((SELECT dept_on FROM dept_mode) OR g.team_id NOT IN (SELECT org_id FROM core.org))
QUALIFY row_number() OVER (PARTITION BY g.team_id ORDER BY g.record_id) = 1;

{# ---------------------------------------------------------------- core.service #}
{#- owner: the first of owned_by, support_group that is a core.team (spec 01 §4.2). -#}
CREATE OR REPLACE TABLE core.service AS
SELECT
    {{ m.rid('servicenow:cmdb_ci', 'c.sys_id') }} AS service_id,
    c.name,
    c.ci_class,
    c.criticality,
    coalesce(own.team_id, sup.team_id) AS business_owner_team_id,
    CASE WHEN own.team_id IS NOT NULL THEN own.org_id ELSE sup.org_id END AS org_id,
    'servicenow' AS source
FROM stg.sn_ci AS c
LEFT JOIN core.team AS own ON own.team_id = {{ m.rid(GRP, 'c.owned_by') }}
LEFT JOIN core.team AS sup ON sup.team_id = {{ m.rid(GRP, 'c.support_group') }}
WHERE c.ci_class IN (SELECT ci_class FROM stg.service_ci_class)
    AND {{ m.rid('servicenow:cmdb_ci', 'c.sys_id') }} IS NOT NULL;
