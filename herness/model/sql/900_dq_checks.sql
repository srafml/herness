{#
  Data quality checks (impl 02 U02-127; design 02 §4.8, DD02-07) written into
  meta.dq_result with `"producer": "dq900"` in `details`. This file's earlier rows are
  deleted first; rows of other producers (spec 04 invariants) are kept, so a re-run leaves
  one row per check. Every statement is static: table and column names are the constants
  below, thresholds come from the `dq` settings through `num`, and no value read from the
  data becomes SQL text (TH02-10, TH02-17). The gate (U02-94) reads the rows back.
#}
{% set COUNTED = ['org', 'team', 'service', 'service_map', 'incident', 'change', 'problem', 'event', 'metric_daily', 'work_item', 'work_item_transition', 'work_item_link'] %}
{% set FUTURE = [('incident', 'opened_at'), ('incident', 'resolved_at'), ('incident', 'closed_at'), ('change', 'opened_at'), ('change', 'actual_start'), ('change', 'actual_end'), ('problem', 'opened_at'), ('problem', 'resolved_at'), ('event', 'ts'), ('work_item', 'created_at'), ('work_item', 'resolved_at')] %}
{% set KEYS = [('org', ['org_id']), ('team', ['team_id']), ('service', ['service_id']), ('incident', ['record_id']), ('change', ['record_id']), ('problem', ['record_id']), ('work_item', ['record_id']), ('event', ['event_id']), ('metric_daily', ['date', 'service_id', 'metric_name', 'source_tool'])] %}
{#- share(part, whole): part / whole as DOUBLE, 0 when whole is 0. -#}
{% macro share(part, whole) -%}
CASE WHEN {{ whole }} > 0 THEN CAST({{ part }} AS DOUBLE) / {{ whole }} ELSE 0.0 END
{%- endmacro %}

DELETE FROM meta.dq_result WHERE json_extract_string(details, '$.producer') = 'dq900';

{#- row_count_drop: share of rows lost against the promoted build (0 without one). -#}
INSERT INTO meta.dq_result
WITH dq_cur AS (
{% for t in COUNTED %}
    {% if not loop.first %}UNION ALL {% endif %}SELECT {{ ('core.' ~ t) | sqlstr }} AS table_name, count(*) AS cur FROM core.{{ t | ident }}
{% endfor %}
), dq_drop AS (
    SELECT c.table_name, c.cur, coalesce(p.row_count, 0) AS prev,
        NOT EXISTS (SELECT 1 FROM stg.prev_row_counts) AS no_prev
    FROM dq_cur AS c LEFT JOIN stg.prev_row_counts AS p ON p.table_name = c.table_name
)
SELECT 'row_count_drop:' || table_name, 'error', {{ share('prev - cur', 'prev') }},
    {{ dq.row_count_drop_max | num }}, {{ share('prev - cur', 'prev') }} <= {{ dq.row_count_drop_max | num }},
    CASE WHEN no_prev
        THEN json_object('producer', 'dq900', 'prev', prev, 'cur', cur, 'no_previous_build', true)
        ELSE json_object('producer', 'dq900', 'prev', prev, 'cur', cur)
    END
FROM dq_drop;

{#- incident_service_null: error above the error threshold, else warn. -#}
INSERT INTO meta.dq_result
WITH dq_null AS (
    SELECT count(*) FILTER (WHERE service_id IS NULL) AS null_rows, count(*) AS n_rows
    FROM core.incident
), dq_share AS (
    SELECT null_rows, n_rows, {{ share('null_rows', 'n_rows') }} AS v FROM dq_null
)
SELECT 'incident_service_null',
    CASE WHEN v > {{ dq.incident_service_null_error | num }} THEN 'error' ELSE 'warn' END, v,
    CASE WHEN v > {{ dq.incident_service_null_error | num }}
        THEN {{ dq.incident_service_null_error | num }}
        ELSE {{ dq.incident_service_null_warn | num }}
    END,
    v <= {{ dq.incident_service_null_warn | num }} AND v <= {{ dq.incident_service_null_error | num }},
    json_object('producer', 'dq900', 'null_rows', null_rows, 'rows', n_rows)
FROM dq_share;

INSERT INTO meta.dq_result
WITH dq_null AS (
    SELECT count(*) FILTER (WHERE service_id IS NULL) AS null_rows, count(*) AS n_rows
    FROM core.work_item
)
SELECT 'work_item_service_null', 'warn', {{ share('null_rows', 'n_rows') }},
    {{ dq.work_item_service_null_warn | num }},
    {{ share('null_rows', 'n_rows') }} <= {{ dq.work_item_service_null_warn | num }},
    json_object('producer', 'dq900', 'null_rows', null_rows, 'rows', n_rows)
FROM dq_null;

{#- cast_fail: one row per staging column with values; repeated stats rows are summed. -#}
INSERT INTO meta.dq_result
WITH dq_cast AS (
    SELECT table_name, column_name, CAST(sum(non_null) AS BIGINT) AS non_null,
        CAST(sum(failed) AS BIGINT) AS failed
    FROM stg.cast_stats
    WHERE table_name IS NOT NULL AND column_name IS NOT NULL
    GROUP BY table_name, column_name
)
SELECT 'cast_fail:' || table_name || '.' || column_name, 'warn', {{ share('failed', 'non_null') }},
    {{ dq.cast_fail_warn | num }}, {{ share('failed', 'non_null') }} <= {{ dq.cast_fail_warn | num }},
    json_object('producer', 'dq900', 'failed', failed, 'non_null', non_null)
FROM dq_cast
WHERE non_null > 0
ORDER BY table_name, column_name;

{#- future_timestamp: values later than this build's start. -#}
INSERT INTO meta.dq_result
WITH dq_start AS (SELECT max(started_at) AS started_at FROM meta.build), dq_future AS (
{% for t, c in FUTURE %}
    {% if not loop.first %}UNION ALL {% endif %}SELECT {{ ('future_timestamp:core.' ~ t ~ '.' ~ c) | sqlstr }} AS check_name, count(*) FILTER (WHERE x.{{ c | ident }} > s.started_at) AS n FROM core.{{ t | ident }} AS x CROSS JOIN dq_start AS s
{% endfor %}
)
SELECT check_name, 'warn', n, {{ dq.future_timestamp_max | num }}, n <= {{ dq.future_timestamp_max | num }},
    json_object('producer', 'dq900')
FROM dq_future;

INSERT INTO meta.dq_result
WITH dq_both AS (
    SELECT count(*) FILTER (WHERE resolved_at < opened_at) AS bad, count(*) AS n_rows
    FROM core.incident
    WHERE resolved_at IS NOT NULL AND opened_at IS NOT NULL
)
SELECT 'resolved_before_opened', 'warn', {{ share('bad', 'n_rows') }},
    {{ dq.resolved_before_opened_warn | num }},
    {{ share('bad', 'n_rows') }} <= {{ dq.resolved_before_opened_warn | num }},
    json_object('producer', 'dq900', 'rows', n_rows)
FROM dq_both;

{#- duplicate_key: rows beyond one per key (a NULL key counts as a duplicate). -#}
INSERT INTO meta.dq_result
WITH dq_dup AS (
{% for t, cols in KEYS %}
    {% if not loop.first %}UNION ALL {% endif %}SELECT {{ ('duplicate_key:core.' ~ t) | sqlstr }} AS check_name, count(*) - count(DISTINCT {% if cols | length > 1 %}row({% for c in cols %}{{ c | ident }}{% if not loop.last %}, {% endif %}{% endfor %}){% else %}{{ cols[0] | ident }}{% endif %}) AS n FROM core.{{ t | ident }}
{% endfor %}
)
SELECT check_name, 'error', n, {{ dq.duplicate_key_max | num }}, n <= {{ dq.duplicate_key_max | num }},
    json_object('producer', 'dq900')
FROM dq_dup;

{#- decision_coverage_incident: incidents with redacted text that have a decision. -#}
INSERT INTO meta.dq_result
WITH dq_text AS (
    SELECT i.record_id,
        EXISTS (SELECT 1 FROM enrich.decision AS d WHERE d.record_id = i.record_id) AS covered
    FROM core.incident AS i
    WHERE EXISTS (SELECT 1 FROM enrich.text_redacted AS t WHERE t.record_id = i.record_id)
), dq_cov AS (
    SELECT (SELECT count(*) FROM core.incident) AS incidents,
        (SELECT count(*) FROM dq_text) AS with_text,
        (SELECT count(*) FROM dq_text WHERE covered) AS covered
), dq_value AS (
    SELECT with_text, covered,
        CASE WHEN incidents = 0 THEN 1.0 ELSE {{ share('covered', 'with_text') }} END AS v
    FROM dq_cov
)
SELECT 'decision_coverage_incident', 'warn', v, {{ dq.decision_coverage_min | num }},
    v >= {{ dq.decision_coverage_min | num }},
    json_object('producer', 'dq900', 'covered', covered, 'with_text', with_text)
FROM dq_value;

{#- metric_daily_unmapped_service (DD02-07): metric rows 270 dropped for a NULL key part. -#}
INSERT INTO meta.dq_result
WITH dq_md AS (
    SELECT coalesce(sum(value) FILTER (WHERE name = 'metric_daily_unmapped'), 0) AS unmapped,
        coalesce(sum(value) FILTER (WHERE name = 'metric_daily_raw'), 0) AS raw
    FROM stg.build_counts
)
SELECT 'metric_daily_unmapped_service', 'warn', {{ share('unmapped', 'raw') }},
    {{ dq.metric_daily_unmapped_warn | num }},
    {{ share('unmapped', 'raw') }} <= {{ dq.metric_daily_unmapped_warn | num }},
    json_object('producer', 'dq900', 'unmapped', CAST(unmapped AS BIGINT), 'raw', CAST(raw AS BIGINT))
FROM dq_md;
