{#
  Keep cluster membership consistent with enrich.cluster (impl 02 U02-125; design 02 §4.4).
  Members whose cluster_id is not a cluster (a NULL cluster_id included) are counted into
  stg.build_counts row `cluster_member_orphans` and deleted. Static statements only; a
  re-run gives the same rows.
#}
{% set ORPHAN = 'NOT EXISTS (SELECT 1 FROM enrich.cluster AS c WHERE c.cluster_id = m.cluster_id)' %}

{#- build_counts only inserts: drop this file's row first so a re-run stays idempotent. -#}
DELETE FROM stg.build_counts WHERE name = 'cluster_member_orphans';
INSERT INTO stg.build_counts
SELECT 'cluster_member_orphans', count(*) FROM enrich.cluster_member AS m WHERE {{ ORPHAN }};

DELETE FROM enrich.cluster_member AS m WHERE {{ ORPHAN }};
