-- Candidate incident<->change links (impl 03 U03-108; design 03 §5.10 steps 1, 2 and 4).
-- Parameters (bound): $before_h, $after_h, $tau_h, $ci_weight, $service_weight, $min_score,
-- $top_n, $window_only_cap. Needs link_corro (link_corroboration.sql). Output: link_cand.
-- Window: the change time t_c = coalesce(actual_end, actual_start, planned_end) lies in
-- [opened_at - before_h, opened_at + after_h] (design 03 §5.10 step 2), or the incident opened
-- while the change ran (actual_start <= opened_at <= actual_end). Times are compared in
-- integer microseconds so the window edges are exact. Each match is scored as
-- heuristic_link_score (U03-107), capped at $window_only_cap (>= $min_score, so the reach below
-- stays exact) unless the incident is in link_corro (T03-41); max score per pair over its matches.
-- Speed (T03-38): the score falls with d = opened_at - t_c and the boosts are at most 1.375,
-- so a match with d beyond reach = tau_h * ln(m * 1.375 / min_score) + 1 min (float slack)
-- cannot reach min_score and is pruned, as is every match scored below min_score. Each change
-- is expanded per key (ci_id, service_id) to the 3-hour buckets of its pruned window (bucketed
-- equi-joins); hits carry the scoring columns and dense-rank keys (change rank = id order).
CREATE OR REPLACE TEMP TABLE link_cand AS
WITH p AS (
    SELECT *,
        least(before_us, CASE WHEN $min_score > 0 AND m_ci > 0 THEN
            ($tau_h * ln(m_ci * 1.25 * 1.1 / $min_score) + 1.0 / 60) * 3600000000.0 END) AS r_ci,
        least(before_us, CASE WHEN $min_score > 0 AND m_svc > 0 THEN
            ($tau_h * ln(m_svc * 1.25 * 1.1 / $min_score) + 1.0 / 60) * 3600000000.0 END) AS r_svc
    FROM (SELECT CAST($ci_weight AS DOUBLE) AS m_ci, CAST($service_weight AS DOUBLE) AS m_svc,
        -$after_h * 3600000000.0 AS lo_us, $before_h * 3600000000.0 AS before_us)
),
inc AS (
    SELECT
        dense_rank() OVER (ORDER BY record_id) AS ik,
        record_id AS incident_id,
        ci_id,
        service_id,
        epoch_us(opened_at) AS opened_us,
        record_id IN (SELECT incident_id FROM link_corro) AS corro,
        CAST(floor(epoch_us(opened_at) / 10800000000.0) AS BIGINT) AS bucket
    FROM core.incident
    WHERE record_id IS NOT NULL AND opened_at IS NOT NULL
),
chg AS (
    SELECT
        dense_rank() OVER (ORDER BY record_id) AS ck,
        record_id AS change_id,
        CASE WHEN outcome IN ('unsuccessful', 'backed_out', 'successful_with_issues')
            THEN 1.25 ELSE 1.0 END AS b1,
        CASE WHEN type = 'emergency' THEN 1.1 ELSE 1.0 END AS b2,
        epoch_us(coalesce(actual_end, actual_start, planned_end)) AS t_us,
        epoch_us(actual_start) AS start_us,
        epoch_us(actual_end) AS end_us,
        unnest([ci_id, service_id]) AS key,
        unnest([true, false]) AS by_ci
    FROM core.change
    WHERE record_id IS NOT NULL
        AND coalesce(actual_end, actual_start, planned_end) IS NOT NULL
),
chg_b AS (  -- one row per (change, key, 3-hour bucket of its pruned window)
    SELECT ck, b1, b2, t_us, start_us, end_us, key, by_ci, lo_us, before_us,
        CASE WHEN by_ci THEN m_ci ELSE m_svc END AS m,
        CASE WHEN by_ci THEN r_ci ELSE r_svc END AS reach,
        unnest(range(
            CAST(floor(least(t_us + lo_us, start_us) / 10800000000.0) AS BIGINT),
            CAST(floor(greatest(t_us + reach, end_us) / 10800000000.0) AS BIGINT) + 1
        )) AS bucket
    FROM chg, p
    WHERE key IS NOT NULL
),
hits AS (
    SELECT i.ik, c.ck, c.m, i.opened_us - c.t_us AS d_us, c.b1, c.b2, i.corro
    FROM inc AS i
    JOIN chg_b AS c ON c.by_ci AND c.key = i.ci_id AND c.bucket = i.bucket
    WHERE i.opened_us - c.t_us <= c.reach
        AND ((i.opened_us - c.t_us BETWEEN c.lo_us AND c.before_us)
            OR (i.opened_us BETWEEN c.start_us AND c.end_us))
    UNION ALL
    SELECT i.ik, c.ck, c.m, i.opened_us - c.t_us AS d_us, c.b1, c.b2, i.corro
    FROM inc AS i
    JOIN chg_b AS c ON NOT c.by_ci AND c.key = i.service_id AND c.bucket = i.bucket
    WHERE i.opened_us - c.t_us <= c.reach
        AND ((i.opened_us - c.t_us BETWEEN c.lo_us AND c.before_us)
            OR (i.opened_us BETWEEN c.start_us AND c.end_us))
),
scored AS (  -- max score per pair over its CI and service matches that reach min_score
    SELECT ik, ck, max(score) AS score, max(raw) AS raw  -- raw breaks ties among capped scores
    FROM (SELECT ik, ck, raw, least(raw, 1.0) AS score
        FROM (SELECT *, least(1.0, m * exp(-greatest(0.0, d_us / 3600000000.0) / $tau_h) * b1 * b2)
            AS raw FROM hits))
    WHERE score >= $min_score
    GROUP BY ik, ck
),
src AS (
    SELECT DISTINCT i.record_id AS incident_id, c.record_id AS change_id
    FROM core.incident AS i
    JOIN core.change AS c ON c.record_id = i.caused_by_change_id
    WHERE i.record_id IS NOT NULL
),
src_k AS (
    SELECT DISTINCT inc.ik, chg.ck
    FROM src
    JOIN inc ON inc.incident_id = src.incident_id
    JOIN chg ON chg.change_id = src.change_id
),
win AS (
    SELECT s.ik, s.ck, s.score
    FROM scored AS s
    ANTI JOIN src_k ON src_k.ik = s.ik AND src_k.ck = s.ck
    QUALIFY row_number() OVER (PARTITION BY s.ik ORDER BY s.score DESC, s.raw DESC, s.ck) <= $top_n
)
SELECT
    incident_id,
    change_id,
    CAST('source_field' AS VARCHAR) AS method,
    CAST(1.0 AS DOUBLE) AS score
FROM src
UNION ALL
SELECT
    i.incident_id,
    c.change_id,
    CAST('time_ci_window' AS VARCHAR) AS method,
    CAST(w.score AS DOUBLE) AS score
FROM win AS w
JOIN (SELECT DISTINCT ik, incident_id FROM inc) AS i ON i.ik = w.ik
JOIN (SELECT DISTINCT ck, change_id FROM chg) AS c ON c.ck = w.ck;
