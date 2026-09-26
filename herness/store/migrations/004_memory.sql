-- Migration 004 (impl 02 U02-52, §4.3.4; design 02 §5.4): memory, its FTS5 index, closed loop.
-- Applied files never change (checksum, TH02-15): a fix is a new migration.

-- A rowid table (not WITHOUT ROWID) so memory_fts can use content_rowid = 'rowid'.
CREATE TABLE memory_item (
    memory_id TEXT NOT NULL PRIMARY KEY,
    layer TEXT NOT NULL CHECK (layer IN ('episodic', 'semantic', 'procedural')),
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}' CHECK (data IS NULL OR json_valid(data)),
    provenance TEXT NOT NULL DEFAULT '{}' CHECK (provenance IS NULL OR json_valid(provenance)),
    confidence REAL,
    status TEXT NOT NULL
        CHECK (status IN ('candidate', 'pending_approval', 'active', 'expired', 'rejected')),
    created_at TEXT NOT NULL
        CHECK (created_at IS NULL OR (length(created_at) = 27 AND created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z')),
    expires_at TEXT
        CHECK (expires_at IS NULL OR (length(expires_at) = 27 AND expires_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z')),
    last_used_at TEXT
        CHECK (last_used_at IS NULL OR (length(last_used_at) = 27 AND last_used_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z')),
    use_count INTEGER NOT NULL DEFAULT 0
) STRICT;

CREATE INDEX memory_item_layer_status ON memory_item (layer, status);

CREATE INDEX memory_item_kind_status ON memory_item (kind, status);

CREATE VIRTUAL TABLE memory_fts USING fts5 (
    content,
    kind,
    content = 'memory_item',
    content_rowid = 'rowid',
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TRIGGER memory_item_ai AFTER INSERT ON memory_item BEGIN
    INSERT INTO memory_fts (rowid, content, kind) VALUES (new.rowid, new.content, new.kind);
END;

CREATE TRIGGER memory_item_ad AFTER DELETE ON memory_item BEGIN
    INSERT INTO memory_fts (memory_fts, rowid, content, kind)
        VALUES ('delete', old.rowid, old.content, old.kind);
END;

CREATE TRIGGER memory_item_au AFTER UPDATE OF content, kind ON memory_item BEGIN
    INSERT INTO memory_fts (memory_fts, rowid, content, kind)
        VALUES ('delete', old.rowid, old.content, old.kind);
    INSERT INTO memory_fts (rowid, content, kind) VALUES (new.rowid, new.content, new.kind);
END;

CREATE TABLE recommendation (
    rec_id TEXT NOT NULL PRIMARY KEY,
    run_id TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    summary TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('fund', 'org_action')),
    numbers TEXT NOT NULL CHECK (numbers IS NULL OR json_valid(numbers)),
    expected_metric TEXT,
    expected_delta REAL,
    confidence REAL,
    expected_usd TEXT,
    confidence_basis TEXT NOT NULL DEFAULT '{}'
        CHECK (confidence_basis IS NULL OR json_valid(confidence_basis)),
    finding_ids TEXT NOT NULL DEFAULT '[]' CHECK (finding_ids IS NULL OR json_valid(finding_ids)),
    created_at TEXT NOT NULL
        CHECK (created_at IS NULL OR (length(created_at) = 27 AND created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'))
) STRICT;

CREATE INDEX recommendation_run ON recommendation (run_id);

CREATE TABLE decision_log (
    rec_id TEXT NOT NULL REFERENCES recommendation (rec_id),
    decision TEXT NOT NULL CHECK (decision IN ('accepted', 'rejected', 'deferred')),
    reason TEXT,
    decided_by TEXT NOT NULL,
    decided_at TEXT NOT NULL
        CHECK (decided_at IS NULL OR (length(decided_at) = 27 AND decided_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z')),
    effective_at TEXT NOT NULL
        CHECK (effective_at IS NULL OR (length(effective_at) = 27 AND effective_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'))
) STRICT;

CREATE INDEX decision_log_rec ON decision_log (rec_id, decided_at);

CREATE TABLE outcome (
    outcome_id TEXT NOT NULL PRIMARY KEY,
    rec_id TEXT NOT NULL REFERENCES recommendation (rec_id),
    measurement INTEGER NOT NULL CHECK (measurement >= 1),
    measured_at TEXT NOT NULL
        CHECK (measured_at IS NULL OR (length(measured_at) = 27 AND measured_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z')),
    metric TEXT NOT NULL,
    baseline REAL,
    actual REAL,
    delta REAL,
    query_id TEXT,
    verdict TEXT NOT NULL CHECK (verdict IN ('paid_off', 'no_effect', 'worse', 'inconclusive')),
    details TEXT NOT NULL DEFAULT '{}' CHECK (details IS NULL OR json_valid(details))
) STRICT;

CREATE UNIQUE INDEX outcome_rec_measurement ON outcome (rec_id, measurement);
