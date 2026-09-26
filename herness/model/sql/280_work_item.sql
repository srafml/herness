{#
  Canonical Jira work items, transitions and links (impl 02 U02-123; design 02 §4.3).
  `component` is the first component in source order. Service: the `core.service_map`
  rows with role `delivery` matching (project, component), else (when there is none)
  those matching (project, NULL component); the single service_id of the chosen rows, NULL
  when zero or several. Team: the team of the matched delivery rows when it is one
  non-NULL team, else the single `core.team` whose lower-cased name is the lower-cased
  `team_value`, else NULL. Every table is replaced, so a re-run gives the same rows.
#}

{# ---------------------------------------------------------------- core.work_item #}
CREATE OR REPLACE TABLE core.work_item AS
WITH issue AS (
    SELECT i.*, i.components[1] AS component
    FROM stg.jira_issue AS i
),
delivery AS (
    SELECT service_id, team_id, jira_project, jira_component
    FROM core.service_map
    WHERE role = 'delivery' AND jira_project IS NOT NULL
),
candidate AS (
    SELECT w.record_id, 1 AS lvl, d.service_id, d.team_id
    FROM issue AS w
    JOIN delivery AS d ON d.jira_project = w.project AND d.jira_component = w.component
    UNION ALL
    SELECT w.record_id, 2 AS lvl, d.service_id, d.team_id
    FROM issue AS w
    JOIN delivery AS d ON d.jira_project = w.project AND d.jira_component IS NULL
),
chosen AS (
    SELECT c.record_id, c.service_id, c.team_id
    FROM candidate AS c
    QUALIFY c.lvl = min(c.lvl) OVER (PARTITION BY c.record_id)
),
matched AS (
    SELECT record_id, any_value(service_id) AS service_id,
        CASE WHEN count(DISTINCT team_id) = 1 THEN any_value(team_id) END AS team_id
    FROM chosen
    GROUP BY record_id
    HAVING count(DISTINCT service_id) = 1 AND count(service_id) = count(*)
),
team_by_name AS (
    SELECT lower(name) AS name_lc, any_value(team_id) AS team_id
    FROM core.team
    WHERE name IS NOT NULL
    GROUP BY lower(name)
    HAVING count(*) = 1
)
SELECT
    w.record_id,
    w."key",
    w.type,
    w.parent_key,
    w.project,
    CAST(w.component AS VARCHAR) AS component,
    w.components,
    w.labels,
    w.status,
    w.status_category,
    w.created_at,
    w.resolved_at,
    w.story_points,
    w.estimate_cost_usd,
    CAST(coalesce(m.team_id, tn.team_id) AS VARCHAR) AS team_id,
    CAST(m.service_id AS VARCHAR) AS service_id,
    w.summary,
    w.description,
    w.source_updated_at
FROM issue AS w
LEFT JOIN matched AS m ON m.record_id = w.record_id
LEFT JOIN team_by_name AS tn ON tn.name_lc = lower(w.team_value);

{# ---------------------------------------------------------------- core.work_item_transition #}
CREATE OR REPLACE TABLE core.work_item_transition AS
SELECT t.record_id, t.from_status, t.to_status, t.from_category, t.to_category, t."at"
FROM stg.jira_transition AS t
WHERE t.record_id IN (SELECT record_id FROM core.work_item);

{# ---------------------------------------------------------------- core.work_item_link #}
CREATE OR REPLACE TABLE core.work_item_link AS
SELECT DISTINCT l.from_key, l.to_key, l.link_type
FROM stg.jira_link AS l;
