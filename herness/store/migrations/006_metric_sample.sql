-- Migration 006 (impl 02 U02-54, §4.3.6; ENG delta E5, R-12, DD02-04): component metrics.
-- Applied files never change (checksum, TH02-15): a fix is a new migration.
-- Rowid primary key. Single writer herness.store.ops.metrics.record_metric_samples and the
-- 90-day retention purge belong to impl 08 (R-12). labels carries the TH02-07 size cap.

CREATE TABLE metric_sample (
    ts TEXT NOT NULL
        CHECK (ts IS NULL OR (length(ts) = 27 AND ts GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z')),
    name TEXT NOT NULL CHECK (name GLOB 'herness_*'),
    kind TEXT NOT NULL CHECK (kind IN ('counter', 'gauge', 'histogram')),
    value REAL NOT NULL,
    labels TEXT NOT NULL DEFAULT '{}'
        CHECK (labels IS NULL OR (json_valid(labels) AND json_type(labels) = 'object'))
        CHECK (length(labels) <= 1024),
    component TEXT NOT NULL
) STRICT;

CREATE INDEX metric_sample_name_ts ON metric_sample (name, ts);

CREATE INDEX metric_sample_ts ON metric_sample (ts);
