{#
  Canonical monitoring events (impl 02 U02-121; design 02 §4.3). One row per staged
  event; `event_id` is the staging record ID (`monitoring:event:<source_tool>:<event_key>`).
  Service by `stg.service_name_lookup` on the lower-cased service name (aliases and
  unambiguous names only, U02-117); incident by an exact `number` match on `incident_ref`
  when exactly one `core.incident` row matches. The table is replaced, so a re-run gives
  the same rows.
#}

{# ---------------------------------------------------------------- core.event #}
CREATE OR REPLACE TABLE core.event AS
WITH incident_by_number AS (
    SELECT "number", any_value(record_id) AS record_id
    FROM core.incident
    WHERE "number" IS NOT NULL
    GROUP BY "number"
    HAVING count(*) = 1
)
SELECT
    CAST(e.record_id AS VARCHAR) AS event_id,
    e.source_tool,
    e.ts,
    CAST(lk.service_id AS VARCHAR) AS service_id,
    e.host,
    e.severity,
    e.alert_name,
    e.status,
    e.dedup_key,
    CASE WHEN e.ts IS NOT NULL AND e.end_ts IS NOT NULL AND e.end_ts >= e.ts
        THEN CAST(epoch(e.end_ts) - epoch(e.ts) AS BIGINT) END AS duration_s,
    CAST(inc.record_id AS VARCHAR) AS incident_id
FROM stg.mon_event AS e
LEFT JOIN stg.service_name_lookup AS lk ON lk.name_lc = lower(e.service_name)
LEFT JOIN incident_by_number AS inc ON inc."number" = e.incident_ref;
