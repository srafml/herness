{#
  Attach enrichment to the canonical model (impl 02 U02-124; design 02 §4.4; TH02-16).
  Live record IDs are the record_id union of core.incident, core.change, core.problem and
  core.work_item (records under a deletion request never reach core). Rows of
  enrich.text_redacted, enrich.decision and enrich.cluster_member whose record_id is not
  live, and rows of enrich.incident_change_link whose incident_id or change_id is not live,
  are counted into stg.build_counts row `enrich_pruned` and deleted (a NULL ID is never
  live). Then core.incident.content_hash comes from enrich.text_redacted, and
  enrich.decision_wide is created when spec 03 did not create it. Static statements only:
  no value from the data becomes SQL text (ST02-16). A re-run leaves the same enrich and
  core rows; its `enrich_pruned` row replaces the earlier one and counts only the rows that
  run pruned (0 when nothing new was pruned).
#}
{#- dead(column): the ID in `column` is not a live record ID. -#}
{% macro dead(column) -%}
NOT EXISTS (SELECT 1 FROM attach_live_id AS l WHERE l.record_id = {{ column }})
{%- endmacro %}

CREATE OR REPLACE TEMP TABLE attach_live_id AS
SELECT record_id FROM core.incident
UNION
SELECT record_id FROM core.change
UNION
SELECT record_id FROM core.problem
UNION
SELECT record_id FROM core.work_item;

{#- build_counts only inserts: drop this file's row first so a re-run keeps one row (its own count). -#}
DELETE FROM stg.build_counts WHERE name = 'enrich_pruned';
INSERT INTO stg.build_counts
SELECT 'enrich_pruned', CAST(sum(n) AS BIGINT) FROM (
    SELECT count(*) AS n FROM enrich.text_redacted AS t WHERE {{ dead('t.record_id') }}
    UNION ALL
    SELECT count(*) AS n FROM enrich.decision AS d WHERE {{ dead('d.record_id') }}
    UNION ALL
    SELECT count(*) AS n FROM enrich.cluster_member AS m WHERE {{ dead('m.record_id') }}
    UNION ALL
    SELECT count(*) AS n FROM enrich.incident_change_link AS k
    WHERE {{ dead('k.incident_id') }} OR {{ dead('k.change_id') }}
) AS pruned;

DELETE FROM enrich.text_redacted AS t WHERE {{ dead('t.record_id') }};
DELETE FROM enrich.decision AS d WHERE {{ dead('d.record_id') }};
DELETE FROM enrich.cluster_member AS m WHERE {{ dead('m.record_id') }};
DELETE FROM enrich.incident_change_link AS k
WHERE {{ dead('k.incident_id') }} OR {{ dead('k.change_id') }};

DROP TABLE attach_live_id;

UPDATE core.incident AS i SET content_hash = t.content_hash
FROM enrich.text_redacted AS t
WHERE t.record_id = i.record_id;

CREATE VIEW IF NOT EXISTS enrich.decision_wide AS
SELECT DISTINCT record_id FROM enrich.decision;
