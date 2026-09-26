-- Migration 005 (impl 02 U02-53, §4.3.5; design 02 §5.5): review queue, chat, deletion requests.
-- Applied files never change (checksum, TH02-15): a fix is a new migration.

-- payload and note carry the TH02-07 size caps.
CREATE TABLE review_item (
    item_id TEXT NOT NULL PRIMARY KEY,
    kind TEXT NOT NULL
        CHECK (kind IN ('mapping_suggestion', 'label_check', 'memory_write', 'weight_change')),
    payload TEXT NOT NULL
        CHECK (payload IS NULL OR (json_valid(payload) AND json_type(payload) = 'object'))
        CHECK (length(payload) <= 65536),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    created_at TEXT NOT NULL
        CHECK (created_at IS NULL OR (length(created_at) = 27 AND created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z')),
    decided_by TEXT,
    note TEXT CHECK (note IS NULL OR length(note) <= 2000),
    decided_at TEXT
        CHECK (decided_at IS NULL OR (length(decided_at) = 27 AND decided_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z')),
    CHECK ((status = 'pending') = (decided_at IS NULL)),
    CHECK ((decided_at IS NULL) = (decided_by IS NULL))
) STRICT;

CREATE INDEX review_item_status_kind ON review_item (status, kind, created_at);

CREATE TABLE chat_session (
    session_id TEXT NOT NULL PRIMARY KEY,
    user_ref TEXT NOT NULL CHECK (length(user_ref) = 32),
    title TEXT,
    summary TEXT,
    created_at TEXT NOT NULL
        CHECK (created_at IS NULL OR (length(created_at) = 27 AND created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z')),
    last_active_at TEXT NOT NULL
        CHECK (last_active_at IS NULL OR (length(last_active_at) = 27 AND last_active_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'))
) STRICT;

CREATE INDEX chat_session_user ON chat_session (user_ref, last_active_at);

CREATE INDEX chat_session_active ON chat_session (last_active_at);

CREATE TABLE chat_message (
    message_id TEXT NOT NULL PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES chat_session (session_id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('queued', 'streaming', 'done', 'failed')),
    verified TEXT CHECK (verified IS NULL OR verified IN ('verified', 'partial', 'unverified')),
    feedback TEXT CHECK (feedback IS NULL OR feedback IN ('up', 'down')),
    feedback_note TEXT,
    run_id TEXT,
    query_ids TEXT NOT NULL DEFAULT '[]' CHECK (query_ids IS NULL OR json_valid(query_ids)),
    meta TEXT NOT NULL DEFAULT '{}' CHECK (meta IS NULL OR json_valid(meta)),
    created_at TEXT NOT NULL
        CHECK (created_at IS NULL OR (length(created_at) = 27 AND created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'))
) STRICT;

CREATE INDEX chat_message_session ON chat_message (session_id, created_at);

CREATE TABLE deletion_request (
    request_id TEXT NOT NULL PRIMARY KEY,
    record_id TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    reason_ref TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'done', 'failed')),
    steps TEXT NOT NULL DEFAULT '{}' CHECK (steps IS NULL OR json_valid(steps)),
    created_at TEXT NOT NULL
        CHECK (created_at IS NULL OR (length(created_at) = 27 AND created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z')),
    completed_at TEXT
        CHECK (completed_at IS NULL OR (length(completed_at) = 27 AND completed_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'))
) STRICT;

CREATE INDEX deletion_request_status ON deletion_request (status);

CREATE INDEX deletion_request_record ON deletion_request (record_id);
