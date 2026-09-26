{#
  Monitoring staging (impl 02 U02-111; design 02 §4.2): mon_event and mon_metric_daily,
  typed with cast flags, plus this file's stg.cast_stats rows. Raw reads go through
  `m.latest` only; an absent entity gives an empty table with the same columns. Raw
  values are text (impl 01 flatten_record); empty strings count as NULL (§3.10 IDs).
#}
{% import "_macros.jinja" as m %}
{% set V = 'VARCHAR' %}
{#- s(col): the raw column `col` of the `latest` row alias `l` as text, '' as NULL. -#}
{% macro s(col) %}nullif(CAST(l.{{ col | ident }} AS VARCHAR), ''){% endmacro %}
{% macro ts(col, alias) %}{{ m.typed(s(col), 'ts_utc(' ~ s(col) ~ ')', alias) }}{% endmacro %}

{# ---------------------------------------------------------------- event #}
{% set event_cols = [
    ('source_tool', V), ('event_key', V), ('ts', V), ('service', V), ('host', V),
    ('severity_raw', V), ('title', V), ('status', V), ('dedup_key', V), ('end_ts', V),
    ('incident_ref', V)
] %}
CREATE OR REPLACE TABLE stg.mon_event AS
SELECT
    l._record_id AS record_id, l._source_key AS source_key, l._source_updated_at AS source_updated_at,
    {{ s('source_tool') }} AS source_tool,
    {{ s('event_key') }} AS event_key,
    {{ s('host') }} AS host,
    {{ s('status') }} AS status,
    {{ s('dedup_key') }} AS dedup_key,
    {{ s('incident_ref') }} AS incident_ref,
    {{ s('service') }} AS service_name,
    {{ ts('ts', 'ts') }},
    {{ ts('end_ts', 'end_ts') }},
    {{ m.typed(s('severity_raw'), 'e_sev.canonical', 'severity') }},
    {{ s('title') }} AS alert_name
FROM {{ m.latest('monitoring', 'event', event_cols) }} AS l
{{ m.enum('e_sev', 'monitoring.severity', s('severity_raw')) }};

{# ---------------------------------------------------------------- metric_daily #}
{% set metric_cols = [
    ('source_tool', V), ('metric_name', V), ('unit', V), ('service', V), ('date', V), ('value', V)
] %}
CREATE OR REPLACE TABLE stg.mon_metric_daily AS
SELECT
    l._record_id AS record_id, l._source_key AS source_key, l._source_updated_at AS source_updated_at,
    {{ s('source_tool') }} AS source_tool,
    {{ s('metric_name') }} AS metric_name,
    {{ s('unit') }} AS unit,
    {{ s('service') }} AS service_name,
    {{ m.typed(s('date'), 'to_date(' ~ s('date') ~ ')', 'date') }},
    {{ m.typed(s('value'), 'to_double(' ~ s('value') ~ ')', 'value') }}
FROM {{ m.latest('monitoring', 'metric_daily', metric_cols) }} AS l;

{# ---------------------------------------------------------------- cast stats #}
{#- cast_stats only inserts: drop this file's rows first so a re-run stays idempotent. -#}
DELETE FROM stg.cast_stats WHERE table_name IN ('stg.mon_event', 'stg.mon_metric_daily');
{{ m.cast_stats('mon_event', ['ts', 'end_ts', 'severity']) }};
{{ m.cast_stats('mon_metric_daily', ['date', 'value']) }};
