-- Candidate incident<->change links (impl 03 U03-108; design 03 §5.10 steps 1, 2 and 4).
-- Parameters (bound, never formatted in): $before_h, $after_h, $tau_h, $ci_weight,
-- $service_weight, $min_score, $top_n. Output: temp table link_cand
-- (incident_id, change_id, method, score).
--
-- Window: the change time t_c = coalesce(actual_end, actual_start, planned_end) lies in
-- [opened_at - before_h, opened_at + after_h] (design 03 §5.10 step 2), or the incident opened
-- while the change ran (actual_start <= opened_at <= actual_end). Times are compared in
-- integer microseconds so the window edges are exact. Each change is expanded to the day
-- buckets its window covers, so the two range joins (by ci_id, by service_id) are bucketed
-- equi-joins. The score is heuristic_link_score (U03-107) evaluated per pair.
CREATE OR REPLACE TEMP TABLE link_cand AS
WITH inc AS (
    SELECT
        record_id AS incident_id,
        ci_id,
        service_id,
        epoch_us(opened_at) AS opened_us,
        CAST(floor(epoch_us(opened_at) / 86400000000.0) AS BIGINT) AS day
    FROM core.incident
    WHERE record_id IS NOT NULL AND opened_at IS NOT NULL
),
chg AS (
    SELECT
        record_id AS change_id,
        ci_id,
        service_id,
        outcome,
        type AS change_type,
        epoch_us(coalesce(actual_end, actual_start, planned_end)) AS t_us,
        epoch_us(actual_start) AS start_us,
        epoch_us(actual_end) AS end_us
    FROM core.change
    WHERE record_id IS NOT NULL
        AND coalesce(actual_end, actual_start, planned_end) IS NOT NULL
),
chg_day AS (
    SELECT
        change_id,
        ci_id,
        service_id,
        t_us,
        start_us,
        end_us,
        unnest(range(
            CAST(floor(least(t_us - $after_h * 3600000000.0, start_us) / 86400000000.0) AS BIGINT),
            CAST(floor(greatest(t_us + $before_h * 3600000000.0, end_us) / 86400000000.0) AS BIGINT)
            + 1
        )) AS day
    FROM chg
),
hits AS (
    SELECT i.incident_id, c.change_id
    FROM inc AS i
    JOIN chg_day AS c ON i.ci_id = c.ci_id AND i.day = c.day
    WHERE (i.opened_us - c.t_us BETWEEN -$after_h * 3600000000.0 AND $before_h * 3600000000.0)
        OR (i.opened_us BETWEEN c.start_us AND c.end_us)
    UNION
    SELECT i.incident_id, c.change_id
    FROM inc AS i
    JOIN chg_day AS c ON i.service_id = c.service_id AND i.day = c.day
    WHERE (i.opened_us - c.t_us BETWEEN -$after_h * 3600000000.0 AND $before_h * 3600000000.0)
        OR (i.opened_us BETWEEN c.start_us AND c.end_us)
),
scored AS (
    SELECT
        h.incident_id,
        h.change_id,
        least(
            1.0,
            CASE WHEN i.ci_id = c.ci_id THEN $ci_weight ELSE $service_weight END
            * exp(-greatest(0.0, (i.opened_us - c.t_us) / 3600000000.0) / $tau_h)
            * CASE
                WHEN c.outcome IN ('unsuccessful', 'backed_out', 'successful_with_issues')
                    THEN 1.25
                ELSE 1.0
            END
            * CASE WHEN c.change_type = 'emergency' THEN 1.1 ELSE 1.0 END
        ) AS score
    FROM hits AS h
    JOIN inc AS i ON i.incident_id = h.incident_id
    JOIN chg AS c ON c.change_id = h.change_id
),
src AS (
    SELECT DISTINCT i.record_id AS incident_id, c.record_id AS change_id
    FROM core.incident AS i
    JOIN core.change AS c ON c.record_id = i.caused_by_change_id
    WHERE i.record_id IS NOT NULL
),
win AS (
    SELECT s.incident_id, s.change_id, s.score
    FROM scored AS s
    WHERE s.score >= $min_score
        AND NOT EXISTS (
            SELECT 1 FROM src
            WHERE src.incident_id = s.incident_id AND src.change_id = s.change_id
        )
    QUALIFY row_number() OVER (
        PARTITION BY s.incident_id ORDER BY s.score DESC, s.change_id
    ) <= $top_n
)
SELECT
    incident_id,
    change_id,
    CAST('source_field' AS VARCHAR) AS method,
    CAST(1.0 AS DOUBLE) AS score
FROM src
UNION ALL
SELECT
    incident_id,
    change_id,
    CAST('time_ci_window' AS VARCHAR) AS method,
    CAST(score AS DOUBLE) AS score
FROM win;
