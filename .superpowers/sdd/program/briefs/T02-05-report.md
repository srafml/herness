# T02-05 report (relayed from build agent hand-back; agent could not write briefs/T02-05-report.md)
Commit dea8f0f feat(store): add migration runner and migrations 001-002 (T02-05). Status DONE_WITH_CONCERNS.
Built: migrate.py 253/260 (MIGRATION_RANGES + _owner_of, MigrationReport, migrate U02-45 steps 1-7, pending_migrations, schema_version, ops_health); 001_ingestion_health.sql 59/90 (watermark, sync_slice+sync_slice_status, file_ingest, source_health, STRICT, CHECKs); 002_jobs.sql 74/110 (job, worker, resilience_event + job_idem_active, job_claim, resilience_event_kind_ts, resilience_event_ts); ops/__init__.py 50/160 block "02 migrate"; errors.py unchanged 67/70 (MigrationError already present); pyproject comments only.
Checksum: SHA-256 of file bytes after CRLF->LF. Statement split via sqlite3.complete_statement. SchemaViolation -> MigrationError(apply_failed, "statement <i>: <cls>: <msg>"). Logs store.ops.migration_out_of_order, store.ops.migrated.
Tests: tests/unit/store/ops/test_store_ops_migrate.py (UT02-33,34,35,40,41,42,70,71, ST02-15, parts of UT02-32/37); test_store_ops_package.py UT02-68 migrate checks asserted; tests/bench/test_store_ops_bench.py BT02-07 (~10-15 ms). Temp-dir fixture copies package .sql + placeholder 003-006 files; monkeypatches migrate._migrations_root.
Gates: full not-slow 3245 passed/5 skipped/1 xfailed; migrate.py 100% line+branch; ruff, mypy, lint-imports (13 kept), module size, type ownership clean; hooks ran, no --no-verify.
Carry-overs: BT02-06 stays on test-local table (review_item is migration 005, T02-06).
Rulings/deviations:
1. 002 follows impl 02 §4.3.2 NOT NULL scheduled_for/created_at vs impl 08 U08-42 nullable.
2. CHECKs use §3.6 `col IS NULL OR ...` verbatim; job.payload length<=65536; sync_slice.last_error no CHECK (owner 01 enforces).
3. Runner refuses statements starting PRAGMA/BEGIN/COMMIT/END/ROLLBACK/SAVEPOINT/RELEASE/ATTACH/DETACH/VACUUM (apply_failed) - not in U02-45, enforces §3.6 content rule.
4. Malformed names ignored, not rejected.
5. schema_migration.name = slug; report/pending use NNN_slug.
6. sqlite_too_old never raised by runner (core raises ConfigError).
7. No __init__.py in herness/store/migrations (namespace resource).
8. Bootstrap before discovery: on out_of_range/duplicate an empty schema_migration exists.
Not verified: herness init (T09-22 absent).
