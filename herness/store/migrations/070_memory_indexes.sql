-- Migration 070 (impl 07 U07-20, §4.1; R-11): memory indexes only this spec needs.
-- Tables, memory_fts, its triggers and the other memory indexes come from migration 004.
-- Indexes only: no table DDL, no data change. Applied files never change (TH02-15).

CREATE INDEX IF NOT EXISTS ix_memory_expires ON memory_item (expires_at)
    WHERE expires_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_memory_content_hash
    ON memory_item (json_extract(data, '$.content_hash'), layer, kind);

CREATE INDEX IF NOT EXISTS ix_memory_task
    ON memory_item (json_extract(provenance, '$.task_id'), json_extract(data, '$.content_hash'));

CREATE INDEX IF NOT EXISTS ix_memory_fingerprint ON memory_item (json_extract(data, '$.fingerprint'))
    WHERE kind = 'sql_template';

CREATE INDEX IF NOT EXISTS ix_memory_rec ON memory_item (json_extract(data, '$.rec_id'))
    WHERE kind IN ('outcome_summary', 'decision_note');

CREATE INDEX IF NOT EXISTS ix_memory_prov_run ON memory_item (json_extract(provenance, '$.run_id'));

CREATE INDEX IF NOT EXISTS ix_memory_prov_session
    ON memory_item (json_extract(provenance, '$.session_id'));

CREATE INDEX IF NOT EXISTS ix_memory_prov_author
    ON memory_item (json_extract(provenance, '$.author_ref'));

CREATE INDEX IF NOT EXISTS ix_rec_target ON recommendation (target_type, target_id);

CREATE INDEX IF NOT EXISTS ix_rec_metric ON recommendation (expected_metric);
