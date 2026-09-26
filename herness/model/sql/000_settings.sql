-- Schemas and fixed tables that later stages and other specs rely on (impl 02 U02-107;
-- design 02 §4, §4.4, §4.7). DDL only; every statement is idempotent.
-- Enrich tables start empty so attach, facts and DQ work when enrichment is skipped;
-- spec 03 may CREATE OR REPLACE them with the same columns.

CREATE SCHEMA IF NOT EXISTS stg;
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS enrich;
CREATE SCHEMA IF NOT EXISTS metrics;
CREATE SCHEMA IF NOT EXISTS score;
CREATE SCHEMA IF NOT EXISTS meta;

CREATE TABLE IF NOT EXISTS meta.build (
    build_id VARCHAR,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    git_sha VARCHAR,
    config_hash VARCHAR,
    dataset_kind VARCHAR,
    source_watermarks JSON,
    row_counts JSON,
    status VARCHAR
);

CREATE TABLE IF NOT EXISTS meta.evidence (
    query_id VARCHAR PRIMARY KEY,
    sql VARCHAR,
    params JSON,
    result_hash VARCHAR,
    row_count BIGINT,
    result_sample JSON,
    executed_at TIMESTAMPTZ,
    producer VARCHAR
);

CREATE TABLE IF NOT EXISTS meta.dq_result (
    check_name VARCHAR,
    severity VARCHAR,
    value DOUBLE,
    threshold DOUBLE,
    passed BOOLEAN,
    details JSON
);

CREATE TABLE IF NOT EXISTS enrich.text_redacted (
    record_id VARCHAR,
    entity VARCHAR,
    text VARCHAR,
    content_hash VARCHAR
);

CREATE TABLE IF NOT EXISTS enrich.decision (
    record_id VARCHAR,
    question VARCHAR,
    answer VARCHAR,
    probability DOUBLE,
    agreement DOUBLE,
    decider VARCHAR,
    decider_version VARCHAR,
    question_set_version VARCHAR,
    content_hash VARCHAR,
    decided_at TIMESTAMPTZ,
    escalated BOOLEAN,
    review_status VARCHAR
);

CREATE TABLE IF NOT EXISTS enrich.cluster (
    cluster_id VARCHAR,
    label VARCHAR,
    root_cause_category VARCHAR,
    size BIGINT,
    first_seen TIMESTAMPTZ,
    last_seen TIMESTAMPTZ,
    top_terms VARCHAR[],
    service_ids VARCHAR[],
    algorithm_version VARCHAR
);

CREATE TABLE IF NOT EXISTS enrich.cluster_member (
    record_id VARCHAR,
    cluster_id VARCHAR,
    membership_prob DOUBLE
);

CREATE TABLE IF NOT EXISTS enrich.incident_change_link (
    incident_id VARCHAR,
    change_id VARCHAR,
    method VARCHAR,
    score DOUBLE
);

CREATE TABLE IF NOT EXISTS stg.cast_stats (
    table_name VARCHAR,
    column_name VARCHAR,
    non_null BIGINT,
    failed BIGINT
);

CREATE TABLE IF NOT EXISTS stg.build_counts (
    name VARCHAR,
    value BIGINT
);
