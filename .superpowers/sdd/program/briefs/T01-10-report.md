# T01-10 report: Files ingest path (U01-50)

Status: DONE
Worktree: D:\herness\.claude\worktrees\agent-acad295fa58a49f8d (branch worktree-agent-acad295fa58a49f8d, base f43b1f9)

## Built
- `herness/connectors/files_ingest.py` (208/250 lines): `ingest_files(runner, entity) -> SyncResult`, `MAX_SNAPSHOT_KEYS = 5_000_000`.
  Algorithm per U01-50: DeletionFilter("files", entity).reload(); per `connector.candidates(entity)`:
  a) `fingerprint_file` (InboxFileChanged -> `connectors.files.rejected` reason `changed`, continue);
  b) `get_file_ingest(fp)` known -> INFO `connectors.files.skipped_known` (entity, fingerprint[:12]);
  c) `_KeyCollector.wrap(read_file(...))` keeps `_source_key` columns in snapshot mode and enforces the 5M cap WHILE accumulating (raises `SchemaViolation("snapshot too large")` before the over-cap batch reaches the writer);
  d) `runner._write_stream(entity, wrapped, key="files", deletion=..., ordered=False, field=watermark_field(entity), cap=f.mtime, advance_watermark=False)`; InboxFileChanged -> writer already aborted, rejected `changed`, continue;
  e) `fault_point("connector.before_watermark", source="files")`;
  f) `record_file_ingest(FileIngestRow(fp, "files", entity, rel_path, size, mtime, out.rows, files, runner.clock()))` (files = lake paths relative to `runner.data_root`, POSIX, as backfill's `mark_slice_done`), INFO `connectors.files.ingested` (entity, fingerprint[:12], rows, files count), metric `herness_connectors_files_ingested_total{entity}` via `record_metric_samples`;
  g) snapshot mode: `reconcile_entity(runner, entity, keys=<KEY_SCHEMA batches of pc.unique(keys), batch_rows each>, deleted_at=f.mtime)`; its rows/tombstones/skipped/files added to the totals.
  Returns `SyncResult("files", entity, "incremental", sums..., None, None)`.
- `herness/connectors/runner.py` (381 -> 384/390): dispatch at the `# T01-10:` marker in `run_incremental`, after `_prepare(entity)`: `if self.connector.name == "files":` lazy import of `ingest_files` (import cycle: files_ingest -> reconcile -> runner) and return. Module docstring sentence updated. No helper had to move out.
- `tests/unit/connectors/test_files_ingest.py` (13 tests): UT01-51 x4 (dedupe/rename/modified + record-after-commit order; sub-controller fault test; empty inbox; unreadable file), UT01-52 x5 (one tombstone at file2 mtime; valve after record; 5M cap enforced while accumulating via monkeypatched cap; header-only snapshot; key collector unit), UT01-53 x1, ST01-10 x2 (swap after fingerprint; change while fingerprinted), ST01-12 x1.

## Interpretations / deviations
1. Error logging: spec §6 row "Unreadable inbox file ... connectors.files.rejected". A `SchemaViolation` from the write stream logs `rejected` reason `unreadable` and re-raises (file not recorded). The snapshot cap error logs reason `too_large` (a private `_SnapshotTooLarge(SchemaViolation)` subclass, message exactly "snapshot too large") - both reasons are in the §8.1 closed set.
2. Fault point: `connector.before_watermark` (source "files") fires twice per file: once inside `WriteLoop._commit` (existing U01-40 behaviour) and once as U01-50 step e. Both are after the commit and before `record_file_ingest`. The UT01-51 fault test targets step e with `nth: 2` through the real `fault_env` fixture (HERNESS_ENV=test plan), not a monkeypatch.
3. Snapshot detection uses `runner.cfg.entity(entity)` being a `FilesEntity` with `mode == "snapshot"` (FilesConnector has no public mode accessor).
4. `SyncResult.rows` includes the reconcile tombstone rows (reconcile_entity returns rows == tombstones), keeping the `rows >= tombstones` invariant.
5. IT01-01 / FT01-02 deferred per sub-controller ruling (handle_sync, lake_small, core build absent); replaced by the UT01-51 fault/rerun test.
6. UT01-52 needs committed lake files on disk for the DuckDB anti-join: the test uses a local `ParquetLake` (FakeLake subclass whose writers also write the committed batches as Parquet); tests/support untouched.

## Security (verifier checklist)
- No path built from untrusted strings: only `connector.candidates()` / `InboxFile` are used (symlink / outside_root / too_large policy stays in files.py, ST01-08/09).
- Resource bound enforced while reading (cap checked per batch before the batch is yielded to the writer; test asserts events `["open", "abort"]`, no write).
- Logs: fingerprint first 12 hex only; `file` = inbox rel_path (per §8.1); no record text, no error text logged (only fixed reason codes). No new error messages built from untrusted text. No client construction.
- `record_file_ingest` only after the writer's commit (asserted by event order in UT01-51; UT01-51 fault test proves no row when failing between commit and record).

## Evidence
RED (before the dispatch line): `PYTHONUTF8=1 uv run pytest tests/unit/connectors/test_files_ingest.py -q -p no:logging` -> 10 failed, 2 passed (runner took the generic path: `ConfigError: resilience backend not bound` from `guard("files")`).
GREEN:
- `PYTHONUTF8=1 uv run pytest -k "UT01_51 or UT01_52 or UT01_53 or ST01_10 or ST01_12" -q -p no:logging` -> 13 passed.
- `PYTHONUTF8=1 uv run pytest tests/unit/connectors -q -p no:logging` -> 474 passed, 4 skipped (host symlink privilege / excel extension, pre-existing).
- Coverage files_ingest.py: 100 % line, 100 % branch (116 stmts, 20 branches). runner.py 99 % (the uncovered line 123 / branch 359->362 are pre-existing, not touched).
- ruff format/check clean; mypy (files_ingest.py, runner.py, test file) clean; lint-imports 13 kept 0 broken; check_type_ownership ok; check_module_size exit 0. Pre-commit hooks (incl. pytest-unit, detect-secrets) passed on the wip commit with no SKIP.
- No secret-looking fixtures added; .secrets.baseline untouched.

## Line counts
files_ingest.py 208/250; runner.py 384/390; test_files_ingest.py 435.

## Carry-overs
- IT01-01, FT01-02 (U01-50 Tests) remain for T01-11 / the lake_small fixture.
- `connectors.reconcile.skipped` (delta-mode files entity on `run_reconcile`) is not part of this card.

## Spec notes
- U01-50 step e duplicates the fault point already fired by U01-40 `_commit`; harmless, but a fault plan without `nth` fires on the first (write-loop) call. Consider noting that in the spec.
- The ops `files` column format (relative POSIX to `paths.data`) is not stated in U01-50; followed the backfill `mark_slice_done` convention.

## Commits
- 1f7df56 wip(T01-10): files ingest path, dispatch and tests green
- cc4a06f feat(connectors): files ingest path (T01-10) (adds the UT01-52 valve-after-record test; hooks passed, no SKIP)
