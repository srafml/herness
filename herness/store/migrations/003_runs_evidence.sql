-- Migration 003 (impl 02 U02-51, §4.3.3; design 02 §5.3): runs, tasks, findings, evidence.
-- Applied files never change (checksum, TH02-15): a fix is a new migration.

CREATE TABLE run (
    run_id TEXT NOT NULL PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('funding_review', 'org_review', 'chat', 'eval')),
    depth TEXT NOT NULL CHECK (depth IN ('fast', 'standard', 'deep')),
    profile TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    build_id TEXT,
    status TEXT NOT NULL DEFAULT 'created'
        CHECK (status IN ('created', 'planning', 'running', 'challenging', 'verifying', 'writing', 'recording', 'done', 'partial', 'failed', 'canceled')),
    started_at TEXT NOT NULL
        CHECK (started_at IS NULL OR (length(started_at) = 27 AND started_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z')),
    finished_at TEXT
        CHECK (finished_at IS NULL OR (length(finished_at) = 27 AND finished_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z')),
    token_usage TEXT NOT NULL DEFAULT '{}' CHECK (token_usage IS NULL OR json_valid(token_usage)),
    meta TEXT NOT NULL DEFAULT '{}' CHECK (meta IS NULL OR json_valid(meta)),
    cost_usd TEXT
) STRICT;

-- Active-build lookup (U02-105).
CREATE INDEX run_status ON run (status);

-- checkpoint <= 4 MiB is enforced by spec 08 (R-21 envelope), not by a CHECK.
CREATE TABLE task (
    task_id TEXT NOT NULL PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES run (run_id),
    parent_task_id TEXT,
    role TEXT NOT NULL
        CHECK (role IN ('planner', 'judge', 'analyst', 'skeptic', 'verifier', 'writer', 'chat')),
    spec TEXT NOT NULL CHECK (spec IS NULL OR json_valid(spec)),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'done', 'failed', 'dead')),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    checkpoint TEXT CHECK (checkpoint IS NULL OR json_valid(checkpoint)),
    result TEXT CHECK (result IS NULL OR json_valid(result)),
    created_at TEXT NOT NULL
        CHECK (created_at IS NULL OR (length(created_at) = 27 AND created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z')),
    updated_at TEXT NOT NULL
        CHECK (updated_at IS NULL OR (length(updated_at) = 27 AND updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'))
) STRICT;

-- json_extract is deterministic (SQLite >= 3.38), so it may appear in an index expression.
CREATE UNIQUE INDEX task_dedup ON task (run_id, json_extract(spec, '$.dedup_key'));

CREATE INDEX task_run_status ON task (run_id, status);

CREATE TABLE finding (
    finding_id TEXT NOT NULL PRIMARY KEY,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    author_role TEXT NOT NULL,
    claim TEXT NOT NULL,
    entity_type TEXT,
    entity_id TEXT,
    supersedes TEXT,
    merged_into TEXT,
    numbers TEXT NOT NULL DEFAULT '[]' CHECK (numbers IS NULL OR json_valid(numbers)),
    query_ids TEXT NOT NULL DEFAULT '[]' CHECK (query_ids IS NULL OR json_valid(query_ids)),
    confidence REAL CHECK (confidence BETWEEN 0 AND 1),
    status TEXT NOT NULL DEFAULT 'proposed'
        CHECK (status IN ('proposed', 'challenged', 'verified', 'rejected', 'revised', 'merged')),
    challenge TEXT CHECK (challenge IS NULL OR json_valid(challenge)),
    verification TEXT CHECK (verification IS NULL OR json_valid(verification)),
    created_at TEXT NOT NULL
        CHECK (created_at IS NULL OR (length(created_at) = 27 AND created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'))
) STRICT;

CREATE INDEX finding_run_status ON finding (run_id, status);

-- run_id is NULL for ad-hoc calls; result_sample holds at most 50 rows (enforced by spec 05).
CREATE TABLE evidence (
    query_id TEXT NOT NULL PRIMARY KEY
        CHECK (query_id GLOB 'q_[0-9a-f]*' AND length(query_id) = 18),
    run_id TEXT,
    build_id TEXT NOT NULL,
    sql TEXT NOT NULL,
    result_hash TEXT NOT NULL,
    params TEXT NOT NULL CHECK (params IS NULL OR json_valid(params)),
    result_sample TEXT NOT NULL DEFAULT '[]'
        CHECK (result_sample IS NULL OR json_valid(result_sample)),
    row_count INTEGER NOT NULL CHECK (row_count >= 0),
    duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
    executed_at TEXT NOT NULL
        CHECK (executed_at IS NULL OR (length(executed_at) = 27 AND executed_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'))
) STRICT;

CREATE INDEX evidence_run ON evidence (run_id);

CREATE TABLE evidence_use (
    query_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    used_at TEXT NOT NULL
        CHECK (used_at IS NULL OR (length(used_at) = 27 AND used_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z')),
    PRIMARY KEY (query_id, run_id, task_id)
) STRICT;
