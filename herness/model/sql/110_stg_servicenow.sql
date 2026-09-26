{#
  ServiceNow staging (impl 02 U02-109; design 02 §4.2): one row per live record of each
  entity, typed with cast flags, and the stg.cast_stats rows of this file. Raw reads go
  through `m.latest` only; an absent entity gives an empty table with the same columns
  (R-60: cmdb_ci_service, cmn_department and task_sla may be missing).
  Raw values are text (impl 01 flatten_record); empty strings count as NULL (§3.10 IDs).
#}
{% import "_macros.jinja" as m %}
{% set V = 'VARCHAR' %}
{% set sn_cf = custom_fields.servicenow %}
{#- s(col): the raw column `col` of the `latest` row alias `l` as text, '' as NULL. -#}
{% macro s(col, alias='l') %}nullif(CAST({{ alias | ident }}.{{ col | ident }} AS VARCHAR), ''){% endmacro %}
{% macro ts(col, alias) %}{{ m.typed(s(col), 'ts_utc(' ~ s(col) ~ ')', alias) }}{% endmacro %}
{% macro meta(alias='l') %}{{ alias | ident }}._record_id AS record_id, {{ alias | ident }}._source_key AS source_key, {{ alias | ident }}._source_updated_at AS source_updated_at{% endmacro %}

{# ---------------------------------------------------------------- incident #}
{% set inc = namespace(cols=[
    ('number', V), ('opened_at', V), ('resolved_at', V), ('closed_at', V), ('priority', V),
    ('state', V), ('incident_state', V), ('business_service', V), ('cmdb_ci', V),
    ('assignment_group', V), ('problem_id', V), ('caused_by', V), ('reassignment_count', V),
    ('reopen_count', V), ('short_description', V), ('description', V), ('close_notes', V),
    ('close_code', V), ('made_sla', V), ('business_duration', V)
], typed=[
    'opened_at', 'resolved_at', 'closed_at', 'priority', 'state', 'reassignment_count',
    'reopen_count', 'made_sla', 'business_duration_s'
]) %}
{% set ack = sn_cf.acknowledged_at %}
{% set impact = sn_cf.customer_impact_minutes %}
{#- a configured custom column is read once, even when it names a standard column -#}
{% for cf in [ack, impact] %}
{% if cf is not none and cf not in (inc.cols | map('first') | list) %}
{% set inc.cols = inc.cols + [(cf, V)] %}
{% endif %}
{% endfor %}
{% if ack is not none %}{% set inc.typed = inc.typed + ['acknowledged_at'] %}{% endif %}
{% if impact is not none %}{% set inc.typed = inc.typed + ['customer_impact_minutes'] %}{% endif %}
{% set inc_state = 'coalesce(' ~ s('state') ~ ', ' ~ s('incident_state') ~ ')' %}
CREATE OR REPLACE TABLE stg.sn_incident AS
SELECT
    {{ meta() }},
    {{ s('number') }} AS "number",
    {{ ts('opened_at', 'opened_at') }},
    {{ ts('resolved_at', 'resolved_at') }},
    {{ ts('closed_at', 'closed_at') }},
{% if ack is not none %}
    {{ ts(ack, 'acknowledged_at') }},
{% else %}
    CAST(NULL AS TIMESTAMPTZ) AS acknowledged_at,
{% endif %}
    {{ m.typed(s('priority'), 'CAST(lead_int(' ~ s('priority') ~ ', 1, 5) AS SMALLINT)', 'priority') }},
    {{ m.typed(inc_state, 'e_state.canonical', 'state') }},
    {{ s('business_service') }} AS business_service,
    {{ s('cmdb_ci') }} AS cmdb_ci,
    {{ s('assignment_group') }} AS assignment_group,
    {{ s('problem_id') }} AS problem_id,
    {{ s('caused_by') }} AS caused_by,
    {{ m.typed(s('reassignment_count'), 'to_int(' ~ s('reassignment_count') ~ ')', 'reassignment_count') }},
    {{ m.typed(s('reopen_count'), 'to_int(' ~ s('reopen_count') ~ ')', 'reopen_count') }},
    CAST(l.short_description AS VARCHAR) AS short_description,
    CAST(l.description AS VARCHAR) AS description,
    CAST(l.close_notes AS VARCHAR) AS close_notes,
    {{ s('close_code') }} AS close_code,
    {{ m.typed(s('made_sla'), 'to_bool(' ~ s('made_sla') ~ ')', 'made_sla') }},
    {{ m.typed(s('business_duration'), 'sn_duration_s(' ~ s('business_duration') ~ ')', 'business_duration_s') }},
{% if impact is not none %}
    {{ m.typed(s(impact), 'to_double(' ~ s(impact) ~ ')', 'customer_impact_minutes') }}
{% else %}
    CAST(NULL AS DOUBLE) AS customer_impact_minutes
{% endif %}
FROM {{ m.latest('servicenow', 'incident', inc.cols) }} AS l
{{ m.enum('e_state', 'servicenow.incident_state', inc_state) }};

{# ---------------------------------------------------------------- change_request #}
{% set chg_cols = [
    ('number', V), ('business_service', V), ('cmdb_ci', V), ('assignment_group', V),
    ('short_description', V), ('description', V), ('type', V), ('state', V),
    ('state_display', V), ('risk', V), ('risk_display', V), ('opened_at', V),
    ('start_date', V), ('end_date', V), ('work_start', V), ('work_end', V), ('close_code', V)
] %}
CREATE OR REPLACE TABLE stg.sn_change_request AS
SELECT
    {{ meta() }},
    {{ s('number') }} AS "number",
    {{ s('business_service') }} AS business_service,
    {{ s('cmdb_ci') }} AS cmdb_ci,
    {{ s('assignment_group') }} AS assignment_group,
    CAST(l.short_description AS VARCHAR) AS short_description,
    CAST(l.description AS VARCHAR) AS description,
    {{ m.typed(s('type'), 'e_type.canonical', 'type') }},
    coalesce({{ s('state_display') }}, {{ s('state') }}) AS state,
    coalesce({{ s('risk_display') }}, {{ s('risk') }}) AS risk,
    {{ ts('opened_at', 'opened_at') }},
    {{ ts('start_date', 'planned_start') }},
    {{ ts('end_date', 'planned_end') }},
    {{ ts('work_start', 'actual_start') }},
    {{ ts('work_end', 'actual_end') }},
    {{ m.typed(s('close_code'), 'e_outcome.canonical', 'outcome') }}
FROM {{ m.latest('servicenow', 'change_request', chg_cols) }} AS l
{{ m.enum('e_type', 'servicenow.change_type', s('type')) }}
{{ m.enum('e_outcome', 'servicenow.change_close_code', s('close_code')) }};

{# ---------------------------------------------------------------- problem #}
{% set prb_cols = [
    ('number', V), ('business_service', V), ('assignment_group', V), ('opened_at', V),
    ('resolved_at', V), ('problem_state_display', V), ('state_display', V),
    ('problem_state', V), ('state', V), ('known_error', V), ('cause_notes', V)
] %}
CREATE OR REPLACE TABLE stg.sn_problem AS
SELECT
    {{ meta() }},
    {{ s('number') }} AS "number",
    {{ s('business_service') }} AS business_service,
    {{ s('assignment_group') }} AS assignment_group,
    {{ ts('opened_at', 'opened_at') }},
    {{ ts('resolved_at', 'resolved_at') }},
    coalesce({{ s('problem_state_display') }}, {{ s('state_display') }}, {{ s('problem_state') }}, {{ s('state') }}) AS state,
    {{ m.typed(s('known_error'), 'to_bool(' ~ s('known_error') ~ ')', 'known_error') }},
    CAST(l.cause_notes AS VARCHAR) AS root_cause_text
FROM {{ m.latest('servicenow', 'problem', prb_cols) }} AS l;

{# ---------------------------------------------------------------- cmdb_ci ∪ cmdb_ci_service #}
{#- One row per sys_id (= _source_key). The newest row (a cmdb_ci_service row on a tie)
    gives the record columns; a cmdb_ci_service row, when one exists, gives name (unless
    NULL there) and criticality (`busines_criticality`, the source's spelling, R-60). A
    NULL class on the newest row falls back to the service row's class. -#}
{% set ci_cols = [
    ('name', V), ('owned_by', V), ('support_group', V), ('cost_center', V), ('company', V),
    ('sys_class_name', V)
] %}
CREATE OR REPLACE TABLE stg.sn_ci AS
WITH ci_rows AS (
    SELECT l._record_id, l._source_key, l._source_updated_at, false AS is_service,
        {{ s('name') }} AS name, {{ s('owned_by') }} AS owned_by,
        {{ s('support_group') }} AS support_group, {{ s('cost_center') }} AS cost_center,
        {{ s('company') }} AS company, {{ s('sys_class_name') }} AS ci_class,
        CAST(NULL AS VARCHAR) AS busines_criticality
    FROM {{ m.latest('servicenow', 'cmdb_ci', ci_cols) }} AS l
    UNION ALL
    SELECT l._record_id, l._source_key, l._source_updated_at, true AS is_service,
        {{ s('name') }}, {{ s('owned_by') }}, {{ s('support_group') }},
        {{ s('cost_center') }}, {{ s('company') }},
        coalesce({{ s('sys_class_name') }}, 'cmdb_ci_service'),
        {{ s('busines_criticality') }}
    FROM {{ m.latest('servicenow', 'cmdb_ci_service', ci_cols + [('busines_criticality', V)]) }} AS l
),
base AS (
    SELECT * FROM ci_rows
    QUALIFY row_number() OVER (
        PARTITION BY _source_key
        ORDER BY _source_updated_at DESC, is_service DESC, _record_id DESC
    ) = 1
),
svc AS (
    SELECT _source_key, name, ci_class, busines_criticality FROM ci_rows WHERE is_service
)
SELECT
    {{ meta('b') }},
    b._source_key AS sys_id,
    coalesce(svc.name, b.name) AS name,
    b.owned_by, b.support_group, b.cost_center, b.company,
    coalesce(b.ci_class, svc.ci_class) AS ci_class,
    {{ m.typed('svc.busines_criticality', 'CAST(lead_int(svc.busines_criticality, 1, 4) AS SMALLINT)', 'criticality') }}
FROM base AS b
LEFT JOIN svc ON svc._source_key = b._source_key;

{# ---------------------------------------------------------------- cmdb_rel_ci #}
CREATE OR REPLACE TABLE stg.sn_rel_ci AS
SELECT
    {{ meta() }},
    {{ s('parent') }} AS parent,
    {{ s('child') }} AS child,
    coalesce({{ s('type_display') }}, {{ s('type') }}) AS "type"
FROM {{ m.latest('servicenow', 'cmdb_rel_ci', [('parent', V), ('child', V), ('type', V), ('type_display', V)]) }} AS l;

{# ---------------------------------------------------------------- sys_user_group #}
{% set grp_cols = [
    ('sys_id', V), ('name', V), ('parent', V), ('manager', V), ('cost_center', V),
    ('type', V), ('active', V)
] %}
CREATE OR REPLACE TABLE stg.sn_group AS
SELECT
    {{ meta() }},
    {{ s('sys_id') }} AS sys_id,
    {{ s('name') }} AS name,
    {{ s('parent') }} AS parent,
    {{ s('manager') }} AS manager,
    {{ s('cost_center') }} AS cost_center,
    {{ s('type') }} AS "type",
    {{ m.typed(s('active'), 'to_bool(' ~ s('active') ~ ')', 'active') }}
FROM {{ m.latest('servicenow', 'sys_user_group', grp_cols) }} AS l;

{# ---------------------------------------------------------------- cmn_department #}
CREATE OR REPLACE TABLE stg.sn_department AS
SELECT
    {{ meta() }},
    {{ s('sys_id') }} AS sys_id,
    {{ s('name') }} AS name,
    {{ s('parent') }} AS parent,
    {{ s('cost_center') }} AS cost_center
FROM {{ m.latest('servicenow', 'cmn_department', [('sys_id', V), ('name', V), ('parent', V), ('cost_center', V)]) }} AS l;

{# ---------------------------------------------------------------- task_sla #}
CREATE OR REPLACE TABLE stg.sn_task_sla AS
SELECT
    {{ meta() }},
    {{ s('task') }} AS task,
    {{ m.typed(s('has_breached'), 'to_bool(' ~ s('has_breached') ~ ')', 'has_breached') }}
FROM {{ m.latest('servicenow', 'task_sla', [('task', V), ('has_breached', V)]) }} AS l;

{# ---------------------------------------------------------------- cast stats #}
{#- cast_stats only inserts: drop this file's rows first so a re-run stays idempotent. -#}
DELETE FROM stg.cast_stats WHERE table_name IN (
    'stg.sn_incident', 'stg.sn_change_request', 'stg.sn_problem', 'stg.sn_ci',
    'stg.sn_group', 'stg.sn_task_sla'
);
{{ m.cast_stats('sn_incident', inc.typed) }};
{{ m.cast_stats('sn_change_request', ['type', 'opened_at', 'planned_start', 'planned_end', 'actual_start', 'actual_end', 'outcome']) }};
{{ m.cast_stats('sn_problem', ['opened_at', 'resolved_at', 'known_error']) }};
{{ m.cast_stats('sn_ci', ['criticality']) }};
{{ m.cast_stats('sn_group', ['active']) }};
{{ m.cast_stats('sn_task_sla', ['has_breached']) }};
