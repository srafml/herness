{#
  Canonical daily metrics (impl 02 U02-122; design 02 §4.3), primary key (date,
  service_id, metric_name, source_tool). Service by `stg.service_name_lookup` on the
  lower-cased service name; rows with a NULL service_id, date or metric_name are dropped;
  per key the latest `source_updated_at` wins, then the highest `record_id`.
  `stg.build_counts` gets `metric_daily_raw` (staging rows) and `metric_daily_unmapped`
  (rows dropped because service_id or date is NULL; DD02-07).
#}
{#- resolved(): staging rows with their service_id (NULL when the name is not found). -#}
{% macro resolved() %}
(
    SELECT m.record_id, m.source_updated_at, m.date, CAST(lk.service_id AS VARCHAR) AS service_id,
        m.metric_name, m.value, m.unit, m.source_tool
    FROM stg.mon_metric_daily AS m
    LEFT JOIN stg.service_name_lookup AS lk ON lk.name_lc = lower(m.service_name)
)
{%- endmacro %}

{# ---------------------------------------------------------------- core.metric_daily #}
CREATE OR REPLACE TABLE core.metric_daily AS
SELECT r.date, r.service_id, r.metric_name, r.value, r.unit, r.source_tool
FROM {{ resolved() }} AS r
WHERE r.service_id IS NOT NULL AND r.date IS NOT NULL AND r.metric_name IS NOT NULL
QUALIFY row_number() OVER (
    PARTITION BY r.date, r.service_id, r.metric_name, r.source_tool
    ORDER BY r.source_updated_at DESC NULLS LAST, r.record_id DESC
) = 1;

{# ---------------------------------------------------------------- build counts #}
{#- build_counts only inserts: drop this file's rows first so a re-run stays idempotent. -#}
DELETE FROM stg.build_counts WHERE name IN ('metric_daily_raw', 'metric_daily_unmapped');
INSERT INTO stg.build_counts
SELECT 'metric_daily_raw', count(*) FROM {{ resolved() }} AS r
UNION ALL
SELECT 'metric_daily_unmapped', count(*) FROM {{ resolved() }} AS r
WHERE r.service_id IS NULL OR r.date IS NULL;
