{#
  Canonical problems (impl 02 U02-120; design 02 §4.3). One row per live stg.sn_problem
  record; service and team become record IDs (§3.10); root_cause_text is the raw
  cause_notes text. The table is replaced, so a re-run gives the same rows.
#}
{% import "_macros.jinja" as m %}

CREATE OR REPLACE TABLE core.problem AS
SELECT
    CAST(p.record_id AS VARCHAR) AS record_id,
    CAST(p."number" AS VARCHAR) AS "number",
    CAST(p.opened_at AS TIMESTAMPTZ) AS opened_at,
    CAST(p.resolved_at AS TIMESTAMPTZ) AS resolved_at,
    CAST(p.state AS VARCHAR) AS state,
    CAST({{ m.rid('servicenow:cmdb_ci', 'p.business_service') }} AS VARCHAR) AS service_id,
    CAST({{ m.rid('servicenow:sys_user_group', 'p.assignment_group') }} AS VARCHAR) AS team_id,
    CAST(p.known_error AS BOOLEAN) AS known_error,
    CAST(p.root_cause_text AS VARCHAR) AS root_cause_text,
    CAST(p.source_updated_at AS TIMESTAMPTZ) AS source_updated_at
FROM stg.sn_problem AS p;
