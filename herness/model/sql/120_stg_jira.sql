{#
  Jira staging (impl 02 U02-110; design 02 §4.2): one row per live issue, typed with cast
  flags, plus the status transitions unnested from `changelog` and the issue links from
  `issuelinks` and `remotelinks`. Raw columns are those of impl 02 §4.1.2 (impl 01 owns the
  contract, R-59): objects and arrays arrive as JSON text. Raw reads go through `m.latest`
  only; an absent `jira/issue` gives empty tables with the same columns.
#}
{% import "_macros.jinja" as m %}
{% set V = 'VARCHAR' %}
{% set cf = custom_fields.jira %}
{#- s(col): the raw column `col` of the `latest` row alias `l` as text, '' as NULL. -#}
{% macro s(col) %}nullif(CAST(l.{{ col | ident }} AS VARCHAR), ''){% endmacro %}
{% macro ts(col, alias) %}{{ m.typed(s(col), 'ts_utc(' ~ s(col) ~ ')', alias) }}{% endmacro %}

{# ---------------------------------------------------------------- issue #}
{% set j = namespace(cols=[
    ('key', V), ('issuetype', V), ('parent', V), ('project', V), ('components', V),
    ('labels', V), ('status', V), ('created', V), ('resolutiondate', V), ('summary', V),
    ('description', V), ('changelog', V), ('issuelinks', V), ('remotelinks', V)
], typed=['type', 'status_category', 'created_at', 'resolved_at']) %}
{#- a configured custom column is read once, even when two fields name the same column -#}
{% for column in [cf.story_points, cf.estimate_cost_usd, cf.team, cf.epic_link] %}
{% if column is not none and column not in (j.cols | map('first') | list) %}
{% set j.cols = j.cols + [(column, V)] %}
{% endif %}
{% endfor %}
{% if cf.story_points is not none %}{% set j.typed = j.typed + ['story_points'] %}{% endif %}
{% if cf.estimate_cost_usd is not none %}{% set j.typed = j.typed + ['estimate_cost_usd'] %}{% endif %}
{% set type_name = "jstr(" ~ s('issuetype') ~ ", '$.name')" %}
{% set status_name = "jstr(" ~ s('status') ~ ", '$.name')" %}
{% set status_key = "jstr(" ~ s('status') ~ ", '$.statusCategory.key')" %}
CREATE OR REPLACE TABLE stg.jira_issue AS
SELECT
    l._record_id AS record_id, l._source_key AS source_key, l._source_updated_at AS source_updated_at,
    {{ s('key') }} AS "key",
    {{ m.typed(type_name, 'e_type.canonical', 'type') }},
{% if cf.epic_link is not none %}
    coalesce(jstr({{ s('parent') }}, '$.key'), {{ s(cf.epic_link) }}) AS parent_key,
{% else %}
    jstr({{ s('parent') }}, '$.key') AS parent_key,
{% endif %}
    jstr({{ s('project') }}, '$.key') AS project,
    CAST(json_names({{ s('components') }}) AS VARCHAR[]) AS components,
    CAST(json_str_list({{ s('labels') }}) AS VARCHAR[]) AS labels,
    {{ status_name }} AS status,
    {# the category key first, else the status name; a failure only when both miss #}
    {{ m.typed('coalesce(' ~ status_key ~ ', ' ~ status_name ~ ')', 'coalesce(e_sck.canonical, e_scn.canonical)', 'status_category') }},
    {{ ts('created', 'created_at') }},
    {{ ts('resolutiondate', 'resolved_at') }},
{% if cf.story_points is not none %}
    {{ m.typed(s(cf.story_points), 'to_double(' ~ s(cf.story_points) ~ ')', 'story_points') }},
{% else %}
    CAST(NULL AS DOUBLE) AS story_points,
{% endif %}
{% if cf.estimate_cost_usd is not none %}
    {{ m.typed(s(cf.estimate_cost_usd), 'TRY_CAST(' ~ s(cf.estimate_cost_usd) ~ ' AS DECIMAL(18,2))', 'estimate_cost_usd') }},
{% else %}
    CAST(NULL AS DECIMAL(18,2)) AS estimate_cost_usd,
{% endif %}
{% if cf.team is not none %}
    team_value({{ s(cf.team) }}) AS team_value,
{% else %}
    CAST(NULL AS VARCHAR) AS team_value,
{% endif %}
    CAST(l.summary AS VARCHAR) AS summary,
    jira_text(CAST(l.description AS VARCHAR)) AS description,
    CAST(l.changelog AS VARCHAR) AS changelog,
    CAST(l.issuelinks AS VARCHAR) AS issuelinks,
    CAST(l.remotelinks AS VARCHAR) AS remotelinks
FROM {{ m.latest('jira', 'issue', j.cols) }} AS l
{{ m.enum('e_type', 'jira.issue_type', type_name) }}
{{ m.enum('e_sck', 'jira.status_category_key', status_key) }}
{{ m.enum('e_scn', 'jira.status_category', status_name) }};

{# ---------------------------------------------------------------- transitions #}
{#- `changelog` is a JSON array of histories or an object whose `histories` key holds it
    (§4.1.2); every item with field `status` of every history is one transition. -#}
CREATE OR REPLACE TABLE stg.jira_transition AS
WITH histories AS (
    SELECT i.record_id, unnest(CASE WHEN json_valid(i.changelog) THEN CASE json_type(i.changelog)
        WHEN 'OBJECT' THEN json_extract(i.changelog, '$.histories[*]')
        ELSE json_extract(i.changelog, '$[*]')
    END END) AS history
    FROM stg.jira_issue AS i
),
items AS (
    SELECT h.record_id, json_extract_string(h.history, '$.created') AS created,
        unnest(json_extract(h.history, '$.items[*]')) AS item
    FROM histories AS h
),
status_items AS (
    SELECT t.record_id, t.created,
        json_extract_string(t.item, '$.fromString') AS from_status,
        json_extract_string(t.item, '$.toString') AS to_status
    FROM items AS t
    WHERE json_extract_string(t.item, '$.field') = 'status'
)
SELECT
    si.record_id,
    ts_utc(si.created) AS "at",
    si.from_status,
    si.to_status,
    e_from.canonical AS from_category,
    e_to.canonical AS to_category
FROM status_items AS si
{{ m.enum('e_from', 'jira.status_category', 'si.from_status') }}
{{ m.enum('e_to', 'jira.status_category', 'si.to_status') }};

{# ---------------------------------------------------------------- links #}
{#- `outwardIssue` → (issue, other); `inwardIssue` → (other, issue); remote links whose
    `object.url` or `object.title` mention a ticket number → `mentions_incident`. -#}
CREATE OR REPLACE TABLE stg.jira_link AS
WITH issue_links AS (
    SELECT i."key" AS issue_key,
        unnest(CASE WHEN json_valid(i.issuelinks) THEN json_extract(i.issuelinks, '$[*]') END) AS link
    FROM stg.jira_issue AS i
),
remote_texts AS (
    SELECT r.issue_key, unnest([
        json_extract_string(r.link, '$.object.url'), json_extract_string(r.link, '$.object.title')
    ]) AS mention_text
    FROM (
        SELECT i."key" AS issue_key,
            unnest(CASE WHEN json_valid(i.remotelinks) THEN json_extract(i.remotelinks, '$[*]') END) AS link
        FROM stg.jira_issue AS i
    ) AS r
),
edges AS (
    SELECT issue_key AS from_key, json_extract_string(link, '$.outwardIssue.key') AS to_key,
        json_extract_string(link, '$.type.name') AS link_type
    FROM issue_links
    UNION ALL
    SELECT json_extract_string(link, '$.inwardIssue.key'), issue_key,
        json_extract_string(link, '$.type.name')
    FROM issue_links
    UNION ALL
    SELECT issue_key, unnest(regexp_extract_all(mention_text, '(INC|CHG|PRB)[0-9]{4,}')),
        'mentions_incident'
    FROM remote_texts
)
SELECT DISTINCT from_key, to_key, link_type
FROM edges
WHERE from_key IS NOT NULL AND to_key IS NOT NULL;

{# ---------------------------------------------------------------- cast stats #}
{#- cast_stats only inserts: drop this file's rows first so a re-run stays idempotent. -#}
DELETE FROM stg.cast_stats WHERE table_name = 'stg.jira_issue';
{{ m.cast_stats('jira_issue', j.typed) }};
