-- U03-78 resolve_decisions: set-based equivalent of decide.resolve_pair (U03-74) over every
-- in-scope (record, applicable non-pair question) pair (design 03 §5.7 step 1, §5.9, §4.1).
-- Inputs are registered relations (cache_rows, human_latest, calib, qmeta, versions,
-- pending_items) and the bound parameter $bootstrap_since; no value is formatted into SQL.
-- Outputs: temp tables enrich_cand (current calibrated candidate rows), enrich_laya_cal
-- (U03-88) and enrich_resolved.

-- Steps 2-3: current-fingerprint, current-version cache rows, latest per key, calibrated.
CREATE OR REPLACE TEMP TABLE enrich_cand AS
WITH cur AS (
    SELECT
        c.content_hash, c.question, c.decider, c.decider_version, c.answer, c.distribution,
        c.decided_at, q.qtype, coalesce(k.t, 1.0) AS t,
        CASE WHEN c.decider = 'ensemble' THEN c.backend_confidence END AS agreement
    FROM cache_rows AS c
    JOIN qmeta AS q ON q.question = c.question AND q.fingerprint = c.question_fingerprint
    JOIN versions AS v ON v.decider = c.decider AND v.decider_version = c.decider_version
    LEFT JOIN calib AS k
        ON k.decider = c.decider AND k.decider_version = c.decider_version
        AND k.question = c.question
    QUALIFY row_number() OVER (
        PARTITION BY c.content_hash, c.question, c.decider
        ORDER BY c.decided_at DESC, c.answer
    ) = 1
),
cal AS (
    SELECT
        *,
        1.0 / (1.0 + exp(-ln(pt / (1.0 - pt)) / t)) AS bool_true,
        exp(ln(coalesce(distribution[answer], 0.0) + 1e-9) / t)
            / list_sum(list_transform(map_values(distribution), lambda x: exp(ln(x + 1e-9) / t)))
            AS soft
    FROM (
        SELECT *, least(greatest(distribution['true'], 1e-9), 1.0 - 1e-9) AS pt FROM cur
    )
)
SELECT
    content_hash, question, decider, decider_version, answer, decided_at, agreement,
    CASE
        WHEN qtype <> 'bool' THEN soft
        WHEN answer = 'false' THEN 1.0 - bool_true
        ELSE bool_true
    END AS p_cal
FROM cal;

CREATE OR REPLACE TEMP TABLE enrich_laya_cal AS
SELECT t.record_id, c.question, c.answer, c.p_cal
FROM enrich.text_redacted AS t
JOIN enrich_cand AS c ON c.content_hash = t.content_hash AND c.decider = 'laya';

-- Steps 1, 4, 5.
CREATE OR REPLACE TEMP TABLE enrich_resolved AS
WITH opened AS (
    SELECT 'incident' AS entity, record_id, opened_at FROM core.incident
    UNION ALL
    SELECT 'change', record_id, opened_at FROM core.change
    UNION ALL
    SELECT 'problem', record_id, opened_at FROM core.problem
),
pairs AS (
    SELECT
        t.record_id, t.entity, t.content_hash, o.opened_at, q.question, q.fingerprint,
        q.scoring_use, q.threshold, q."primary", q.chain,
        coalesce(q."primary" = 'laya' OR o.opened_at >= $bootstrap_since, false) AS in_scope
    FROM enrich.text_redacted AS t
    LEFT JOIN opened AS o ON o.entity = t.entity AND o.record_id = t.record_id
    JOIN qmeta AS q ON list_contains(q.applies_to, t.entity)
),
ranked AS (
    SELECT
        p.record_id, p.entity, p.question, c.decider, c.decider_version, c.answer, c.p_cal,
        c.decided_at, c.agreement,
        CASE
            WHEN c.decider = 'ensemble' THEN 1
            WHEN c.decider = p."primary" AND c.p_cal >= p.threshold THEN 2
            WHEN list_position(p.chain, c.decider) IS NOT NULL
                THEN 3 + list_position(p.chain, c.decider)
        END AS rnk
    FROM pairs AS p
    JOIN enrich_cand AS c ON c.content_hash = p.content_hash AND c.question = p.question
),
best AS (
    SELECT * FROM ranked
    WHERE rnk IS NOT NULL
    QUALIFY row_number() OVER (PARTITION BY record_id, entity, question ORDER BY rnk) = 1
),
joined AS (
    SELECT
        p.*, b.decider, b.decider_version, b.answer AS m_answer, b.p_cal, b.decided_at,
        b.agreement, h.answer AS h_answer, h.labeled_at,
        pr.decider IS NOT NULL AS has_primary, lc.answer AS laya_answer,
        pi.question IS NOT NULL AS pending_review,
        h.answer IS NOT NULL AND (b.answer IS NULL OR b.answer <> h.answer) AS use_human
    FROM pairs AS p
    LEFT JOIN best AS b
        ON b.record_id = p.record_id AND b.entity = p.entity AND b.question = p.question
    LEFT JOIN human_latest AS h
        ON h.content_hash = p.content_hash AND h.question = p.question
        AND h.question_fingerprint = p.fingerprint
    LEFT JOIN enrich_cand AS pr
        ON pr.content_hash = p.content_hash AND pr.question = p.question
        AND pr.decider = p."primary"
    LEFT JOIN enrich_cand AS lc
        ON lc.content_hash = p.content_hash AND lc.question = p.question
        AND lc.decider = 'laya'
    LEFT JOIN (SELECT DISTINCT content_hash, question FROM pending_items) AS pi
        ON pi.content_hash = p.content_hash AND pi.question = p.question
)
SELECT
    record_id, entity, content_hash, opened_at, question, scoring_use,
    CASE
        WHEN decider IS NOT NULL OR h_answer IS NOT NULL THEN 'final'
        WHEN NOT in_scope AND NOT has_primary THEN 'out_of_scope'
        ELSE 'queue'
    END AS status,
    CASE WHEN use_human THEN h_answer ELSE m_answer END AS answer,
    CASE WHEN use_human THEN 1.0 ELSE p_cal END AS probability,
    CASE WHEN use_human THEN 'human' ELSE decider END AS decider,
    CASE WHEN use_human THEN 'human' ELSE decider_version END AS decider_version,
    CASE
        WHEN use_human OR decider IS NULL THEN false
        WHEN decider = 'ensemble' THEN laya_answer IS NULL OR laya_answer <> m_answer
        ELSE decider <> "primary"
    END AS escalated,
    CASE WHEN use_human THEN labeled_at ELSE decided_at END AS decided_at,
    CASE WHEN use_human THEN NULL ELSE agreement END AS agreement,
    CASE
        WHEN use_human AND decider IS NOT NULL THEN 'corrected'
        WHEN h_answer IS NOT NULL THEN 'confirmed'
        WHEN decider IS NOT NULL AND pending_review THEN 'pending'
        ELSE 'none'
    END AS review_status
FROM joined;
