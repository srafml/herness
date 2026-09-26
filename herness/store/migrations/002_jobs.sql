-- Migration 002 (impl 02 U02-50, §4.3.2; design 02 §5.2): job queue, workers, resilience log.
-- Applied files never change (checksum, TH02-15): a fix is a new migration.
-- Columns follow impl 02 §4.3.2 (binding over impl 08; scheduled_for and created_at NOT NULL).

CREATE TABLE job (
    job_id TEXT NOT NULL PRIMARY KEY,
    kind TEXT NOT NULL
        CHECK (kind IN ('sync', 'reconcile', 'build_pipeline', 'distill', 'review', 'chat', 'outcome_measure', 'memory_maintenance', 'maintenance', 'eval')),
    gpu_class TEXT NOT NULL CHECK (gpu_class IN ('none', 'reasoning', 'decider', 'large')),
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'done', 'failed', 'canceled')),
    priority INTEGER NOT NULL CHECK (priority BETWEEN 0 AND 100),
    payload TEXT NOT NULL
        CHECK (payload IS NULL OR json_valid(payload))
        CHECK (length(payload) <= 65536),
    idem_key TEXT,
    result TEXT CHECK (result IS NULL OR json_valid(result)),
    last_error TEXT CHECK (last_error IS NULL OR json_valid(last_error)),
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL CHECK (max_attempts >= 1),
    lease_owner TEXT,
    lease_expires_at TEXT
        CHECK (lease_expires_at IS NULL OR (length(lease_expires_at) = 27 AND lease_expires_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z')),
    started_at TEXT
        CHECK (started_at IS NULL OR (length(started_at) = 27 AND started_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z')),
    finished_at TEXT
        CHECK (finished_at IS NULL OR (length(finished_at) = 27 AND finished_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z')),
    scheduled_for TEXT NOT NULL
        CHECK (scheduled_for IS NULL OR (length(scheduled_for) = 27 AND scheduled_for GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z')),
    created_at TEXT NOT NULL
        CHECK (created_at IS NULL OR (length(created_at) = 27 AND created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'))
) STRICT;

CREATE UNIQUE INDEX job_idem_active ON job (idem_key) WHERE status IN ('queued', 'running');

CREATE INDEX job_claim ON job (status, gpu_class, scheduled_for, priority);

CREATE TABLE worker (
    worker_id TEXT NOT NULL PRIMARY KEY,
    host TEXT NOT NULL,
    pid INTEGER NOT NULL CHECK (pid >= 0),
    gpu_slot INTEGER NOT NULL CHECK (gpu_slot >= 0),
    cpu_slots INTEGER NOT NULL CHECK (cpu_slots >= 0),
    gpu_class_loaded TEXT NOT NULL DEFAULT 'none'
        CHECK (gpu_class_loaded IN ('none', 'reasoning', 'decider', 'large', 'swapping')),
    requested_class TEXT
        CHECK (requested_class IS NULL OR requested_class IN ('none', 'reasoning', 'decider', 'large')),
    status TEXT NOT NULL CHECK (status IN ('starting', 'running', 'draining', 'stopped')),
    current_jobs TEXT NOT NULL DEFAULT '[]' CHECK (current_jobs IS NULL OR json_valid(current_jobs)),
    started_at TEXT NOT NULL
        CHECK (started_at IS NULL OR (length(started_at) = 27 AND started_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z')),
    heartbeat_at TEXT NOT NULL
        CHECK (heartbeat_at IS NULL OR (length(heartbeat_at) = 27 AND heartbeat_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z')),
    version TEXT NOT NULL,
    faults_enabled INTEGER NOT NULL DEFAULT 0 CHECK (faults_enabled IN (0, 1))
) STRICT;

-- kind and component values are owned by spec 08 §4.2 (no CHECK); 90-day retention by spec 08.
CREATE TABLE resilience_event (
    event_id TEXT NOT NULL PRIMARY KEY,
    ts TEXT NOT NULL
        CHECK (ts IS NULL OR (length(ts) = 27 AND ts GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z')),
    kind TEXT NOT NULL,
    component TEXT NOT NULL,
    target TEXT,
    run_id TEXT,
    job_id TEXT,
    task_id TEXT,
    detail TEXT NOT NULL DEFAULT '{}' CHECK (detail IS NULL OR json_valid(detail))
) STRICT;

CREATE INDEX resilience_event_kind_ts ON resilience_event (kind, ts);

CREATE INDEX resilience_event_ts ON resilience_event (ts);
