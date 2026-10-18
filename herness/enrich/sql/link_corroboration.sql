-- Incident text corroboration for window links (T03-41; design 03 section 5.10 step 2).
-- Parameter (bound, never formatted in): $markers, one RE2 regex (the alternation of
-- change_link.corroboration_markers; case folding is inline, (?i)). Output: temp table
-- link_corro (incident_id): the incidents whose redacted text states or implies that the issue
-- started after a deployment, release, patch or change (the change_caused question's own
-- definition). link_candidates.sql caps the time_ci_window score of every other incident at
-- window_only_cap (< 0.5): a time/CI coincidence alone is not a confident link. One parallel
-- scan of the incident text rows; the result is tiny (planted follow-ups only), so the main
-- query semi-joins against it without a lake-sized join.
CREATE OR REPLACE TEMP TABLE link_corro AS
SELECT DISTINCT record_id AS incident_id
FROM enrich.text_redacted
WHERE entity = 'incident' AND record_id IS NOT NULL AND regexp_matches(text, $markers);
