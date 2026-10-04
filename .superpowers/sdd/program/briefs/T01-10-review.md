# T01-10 review: Files ingest path (U01-50)

Reviewer: verify agent. Worktree agent-acad295fa58a49f8d, head cc4a06f (base f43b1f9). Read-only.

## Verdict: **Approved**

Counts: Critical 0 · Important 0 · Minor 4

## Evidence re-run by the reviewer
- `pytest -k "UT01_51 or UT01_52 or UT01_53 or ST01_10 or ST01_12"` -> 13 passed.
- `pytest tests/unit/connectors --cov files_ingest,runner --cov-branch` -> 474 passed, 4 skipped (pre-existing host skips: symlink privilege, DuckDB excel extension). files_ingest.py 100 % line / 100 % branch (116 stmts, 20 branches); runner.py 99 % (line 123 and branch 359->362 were already uncovered before this card and are not touched by it).
- Line counts: files_ingest.py 208/250, runner.py 384/390.

## Spec compliance (U01-50)
- ✅ Step 1: `DeletionFilter("files", entity)` + `reload()` (files_ingest.py:201-202).
- ✅ Step 2a: `fingerprint_file`; `InboxFileChanged` -> `rejected` reason `changed`, continue (106-112).
- ✅ Step 2b: known fingerprint -> INFO `connectors.files.skipped_known` (entity, fp[:12]) (175-177).
- ✅ Step 2c: `_KeyCollector.wrap` collects `_source_key` in snapshot mode only (77-100).
- ✅ Step 2d: `_write_stream(entity, wrapped, key="files", deletion, ordered=False, field=watermark_field, cap=f.mtime, advance_watermark=False)`; `InboxFileChanged` -> writer already aborted by `_WriteLoop.run`, `rejected changed`, continue (116-143). The except order is right: `InboxFileChanged` and `_SnapshotTooLarge` both subclass `SchemaViolation` and are caught before it.
- ✅ Step 2e: `fault_point("connector.before_watermark", source="files")` (183).
- ✅ Step 2f: `record_file_ingest(FileIngestRow(...))` after the commit; INFO `connectors.files.ingested` (entity, fp12, rows, files); counter `herness_connectors_files_ingested_total{entity}` (146-164).
- ✅ Step 2g: snapshot -> `reconcile_entity(runner, entity, keys=<KEY_SCHEMA batches of pc.unique>, deleted_at=f.mtime)`; totals include its tombstones (186-189).
- ✅ Step 3 / Returns: `SyncResult("files", entity, "incremental", sums, None, None)`.
- ✅ Invariant: `record_file_ingest` runs only after the commit. `_write_stream` returns only after `finish()` -> `_commit()`. UT01-51 asserts in the lake's event log that the event just before each `record` is a non-empty `commit`. The fault test shows that a failure between the commit and the record leaves no row, and that the rerun then records exactly one row.
- ✅ Errors §6: reading `SchemaViolation` -> `rejected unreadable`, re-raised, not recorded (UT01-51 unreadable test). Valve `SchemaViolation` -> raised after the record (UT01-52 valve test). `InboxFileChanged` -> skipped and retried (UT01-53, ST01-10 x2). `StoreBusy` passes through unchanged.
- ✅ Limits: the 5M key cap is checked per batch during accumulation, before the batch is yielded to the writer. The test shows events `["open","abort"]` with no write, and reason `too_large` is in the §8.1 closed set.
- ✅ Logs §8.1: levels and fields match (rejected WARNING entity/file/reason; skipped_known INFO; ingested INFO with rows/files). Only the 12-hex fingerprint appears. No error text or record data is logged, so no redact-before-cut is needed. ST01-12 asserts the log key set.
- ✅ Runner dispatch: `run_incremental` hands off to `ingest_files` after `_prepare` (runner.py:187-190). The lazy import is acceptable because it follows the existing pattern at runner.py:224 and :303 for the same runner <- reconcile/backfill cycle, and it carries a `noqa PLC0415` with a reason.
- ✅ Security: paths come only from `connector.candidates()` / `InboxFile`, so the symlink/junction, `outside_root`, `not_regular` and `too_large` policy of files.py (U01-47, ST01-08/09) is not bypassed. The ingest path builds no path from untrusted strings. The stored `files` are lake paths relative to `data_root`, in POSIX form. No egress client is constructed.
- ✅ Tests: UT01-51/52/53, ST01-10, ST01-12 exist; each test name and docstring carries its ID; `pytestmark = pytest.mark.unit`. The assertions check real behaviour: event order, rows in the ops store, the tombstone's `_source_updated_at` == file2 mtime, and the log fields.
- ✅ Budgets: module sizes are within budget. Complexity and argument counts pass ruff (C901 <= 10, PLR0913 <= 6); `_write` has 5 arguments.
- ⚠️ IT01-01 / FT01-02 are deferred by sub-controller ruling. They are replaced here by the UT01-51 fault/rerun test. Carry-over to T01-11 / lake_small.

## ⚠️ Interpretations (reviewer opinion)
1. `before_watermark` fires twice per file (in the write loop's `_commit`, then at step e). **Accept.** Both calls are after the commit and before the record; the spec prescribes both. Raise a spec note: a plan without `nth` hits the first call, and a mid-file checkpoint adds more calls.
2. Snapshot detection via `runner.cfg.entity(entity)` is `FilesEntity` and `mode == "snapshot"`. **Accept.** It reads the validated config and needs no new accessor.
3. `file_ingest.files` stored as POSIX paths relative to `data_root`. **Accept.** This matches the backfill `mark_slice_done` convention. U01-50 does not state the format, so raise a spec note.
4. Reject reasons `too_large` (for the key cap) and `unreadable` (for a reading `SchemaViolation`). **Accept.** Both are in the §8.1 closed set, and the §6 row "Unreadable inbox file" asks for `connectors.files.rejected`. `too_large` also names the U01-47 byte-size skip; the log alone cannot tell the two apart. See Minor 4.
5. `SyncResult.rows` includes reconcile tombstone rows. **Accept.** This is consistent with `_write_stream`, which counts tombstone rows in `rows`, and it keeps `rows >= tombstones`.
6. UT01-52 uses a local `ParquetLake` test double, and tests/support is unchanged. **Accept.**

## Findings

### Critical
- none

### Important
- none

### Minor
1. herness/connectors/files_ingest.py:149 - `path.relative_to(runner.data_root)` runs after the commit. If a writer factory ever places lake files outside `data_root`, the `ValueError` means the file is never recorded, so every run re-ingests it (duplicate raw rows). This cannot happen with the production writer factory (`data_root/raw`). A defensive check or a documented precondition would still close the gap.
2. herness/connectors/files_ingest.py:116-143 (spec-level, not a builder defect) - `read_file` re-checks size/mtime only at the end of the file (files.py:313). A file larger than `checkpoint_rows` gets checkpoint commits mid-file. If it then turns out changed, the earlier parts are already in the lake with no `file_ingest` row, and the next run ingests the whole file again. Raw is append-only and staging dedupes by key, so the impact is duplicate raw rows only. Worth a spec note next to TH01-10.
3. tests/unit/connectors/test_files_ingest.py:165 - the fault test targets step e with `nth: 2`, which couples it to the write loop's internal count of `before_watermark` calls. The comment documents this. It becomes fragile if `_commit` changes or if a checkpoint occurs. Acceptable for now.
4. herness/connectors/files_ingest.py:88 - the key cap counts rows, not distinct keys, so a snapshot with many duplicate keys can hit the cap early. This is conservative and matches the memory bound on what is held, so accept. Separately, `too_large` now has two meanings (the byte-size skip in U01-47 and the key cap here); the operator can only tell them apart from the exception. Consider a spec note.

## Assessment
**Task quality:** Approved
**Reasoning:** Every U01-50 step, error row, invariant and limit is implemented as specified, and each is shown by assertions on real behaviour (commit->record order, abort on change, rerun ingests, tombstone at the file2 mtime, cap enforced before any write), with 100 % line/branch coverage. The remaining points are spec notes or defensive hardening.
