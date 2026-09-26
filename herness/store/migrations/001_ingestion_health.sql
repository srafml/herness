-- Migration 001 (impl 02 U02-49, §4.3.1; design 02 §5.1): ingestion state and source health.
-- Applied files never change (checksum, TH02-15): a fix is a new migration.

CREATE TABLE watermark (
    source TEXT NOT NULL,
    entity TEXT NOT NULL,
    field TEXT NOT NULL,
    value TEXT NOT NULL
        CHECK (value IS NULL OR (length(value) = 27 AND value GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z')),
    updated_at TEXT NOT NULL
        CHECK (updated_at IS NULL OR (length(updated_at) = 27 AND updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z')),
    PRIMARY KEY (source, entity)
) STRICT;

CREATE TABLE sync_slice (
    source TEXT NOT NULL,
    entity TEXT NOT NULL,
    slice_start TEXT NOT NULL
        CHECK (slice_start IS NULL OR (length(slice_start) = 27 AND slice_start GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z')),
    slice_end TEXT NOT NULL
        CHECK (slice_end IS NULL OR (length(slice_end) = 27 AND slice_end GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z')),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'done', 'failed')),
    rows INTEGER NOT NULL DEFAULT 0 CHECK (rows >= 0),
    files TEXT NOT NULL DEFAULT '[]' CHECK (files IS NULL OR json_valid(files)),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at TEXT NOT NULL
        CHECK (updated_at IS NULL OR (length(updated_at) = 27 AND updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z')),
    PRIMARY KEY (source, entity, slice_start)
) STRICT;

CREATE INDEX sync_slice_status ON sync_slice (status, updated_at);

CREATE TABLE file_ingest (
    fingerprint TEXT NOT NULL PRIMARY KEY CHECK (length(fingerprint) = 64),
    source TEXT NOT NULL,
    entity TEXT NOT NULL,
    path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    rows INTEGER NOT NULL CHECK (rows >= 0),
    mtime TEXT NOT NULL
        CHECK (mtime IS NULL OR (length(mtime) = 27 AND mtime GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z')),
    ingested_at TEXT NOT NULL
        CHECK (ingested_at IS NULL OR (length(ingested_at) = 27 AND ingested_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z')),
    files TEXT NOT NULL CHECK (files IS NULL OR json_valid(files))
) STRICT;

CREATE TABLE source_health (
    source TEXT NOT NULL PRIMARY KEY,
    state TEXT NOT NULL DEFAULT 'closed' CHECK (state IN ('closed', 'open', 'half_open')),
    failures INTEGER NOT NULL DEFAULT 0,
    trips INTEGER NOT NULL DEFAULT 0,
    opened_at TEXT
        CHECK (opened_at IS NULL OR (length(opened_at) = 27 AND opened_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z')),
    last_error TEXT,
    updated_at TEXT NOT NULL
        CHECK (updated_at IS NULL OR (length(updated_at) = 27 AND updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'))
) STRICT;
