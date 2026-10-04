# Review: T02-03 Lake purge and retention

## Spec Compliance

| Item | Status | Notes |
|---|---|---|
| U02-20 `LakePurgeResult` | ✅ | Frozen dataclass, fields/order match spec exactly (`herness/store/lake_purge.py:34-41`). |
| U02-21 `purge_record_ids` | ✅ | Signature, algorithm (group→list→scan `_record_id`→filter→unlink/rewrite-via-temp+fsync+`os.replace`), error mapping (`ConfigError` on bad id/count, `StoreBusy` on sharing violation with temp cleaned up first, `SchemaViolation` on other `OSError`), and `store.lake.purged` log (counts + `id_count`, never the ids) all match §3.3 U02-21 (`lake_purge.py:54-144`). |
| U02-22 `LakeRetentionResult` | ✅ | Fields/order match spec exactly (`lake_purge.py:44-51`). |
| U02-23 `purge_partitions_before` | ✅ | `cutoff` positional, `today`/`root` keyword-only; `cutoff > today - 30d` → `ConfigError`; walks `source/entity/dt=*`, flags unparsable names (regex + calendar validity) with `store.lake.partition_unparsable` WARNING, sums bytes before `shutil.rmtree`, defers on `PermissionError`, logs `store.lake.retention_applied` INFO with all 4 counts (`lake_purge.py:157-198`). Containment check (`is_relative_to`) present and robust to symlink escapes since `resolve()` is applied to the whole path. |
| UT02-14 | ✅ | `test_ut02_14_rewrite_matching_file_others_untouched` reproduces the exact spec setup/expectation (3 files, 1 match, byte-identical + same-mtime siblings, `rows_removed==1`) — diff lines 261-277. |
| UT02-15 | ✅ | `test_ut02_15_delete_emptied_file` — file whose only row matches is deleted, `files_deleted==1` — diff lines 280-290. (Brief's own test table omitted this row; spec §11 line 3544 has it — build report correctly sourced it from the spec.) |
| UT02-16 | ✅ | `test_ut02_16_deletes_before_cutoff_skips_unparsable` reproduces the exact partitions/cutoff/expected counts; `test_ut02_16_cutoff_within_30_days_of_today_raises_config_error` covers the `ConfigError` half of the same row — diff lines 389-417. |
| FT02-01 | ✅ (⚠️ mechanism) | `test_ft02_01_replace_failure_leaves_original_and_no_temp` gets `StoreBusy`, original bytes intact, no dot-temp left — diff lines 343-361. Implemented as `pytest.mark.unit` + `monkeypatch` rather than `pytest.mark.fault` + the §11 `fault_plan`/`HERNESS_FAULTS` fixture, because that mechanism belongs to impl 08 which doesn't exist on this branch yet. Disclosed in the report with a cited precedent (`tests/unit/store/test_store_lake.py::test_ut02_11_commit_retry_is_idempotent`). Functionally sound, but flagged since it diverges from the layout impl 02 §11 specifies for fault tests. |
| TH02-01 | ✅ | `validate_name`'s `^[a-z][a-z0-9_]{0,63}$` (imported from `lake.py`) structurally forbids `..`/separators/drive letters in `source`/`entity`, so `purge_record_ids` needs no extra containment check; `purge_partitions_before` has an explicit `is_relative_to` check before every `rmtree`. |
| TH02-16 | ✅ (scope) | This card delivers the purge primitive only; wiring it into the spec 10 deletion procedure is out of this card's scope (`Blocked by: none`, `Depends on: T02-02`). |
| Gates | ✅ | Report shows `mypy --strict` 0 errors, `ruff check`/`format` clean, `lint-imports` 8/0, 198/200 line budget, 100% line/100% branch coverage (exceeds the ≥90/≥85 gate), full `unit+integration` suite green (241 passed). Verified line-count claim directly against the diff (`+198` lines). |

## Strengths

- Faithful, close reading of the (brief-incomplete) spec: U02-21/U02-22 text was correctly sourced from the impl doc rather than invented, and the gap was disclosed in the report.
- Good reuse of `lake.py`'s busy-errno classification and Parquet write settings rather than duplicating logic.
- Byte/mtime-identical assertions in tests genuinely exercise "untouched" postconditions, not just row counts.
- Symlink-safe containment check in `purge_partitions_before` (whole-path `resolve()` catches escapes anywhere in the `source`/`entity`/`dt=` chain, not just at the leaf).

## Issues

### Critical (Must Fix)
None.

### Important (Should Fix)
None.

### Minor (Nice to Have)

1. `_replace_with` (`herness/store/lake_purge.py:74-83`) fsyncs only the temp file before `os.replace`; it does not also fsync the containing directory afterward, unlike `LakeWriter.commit()`'s POSIX directory-fsync for the same kind of atomic rename (`herness/store/lake.py:347-349`). Not a spec violation — U02-21's algorithm text names only the one fsync — but it's a durability inconsistency between the two rewrite/rename paths in the same module family; worth a follow-up if lake durability guarantees are audited later.
2. `lake_purge.py:23-24` imports four underscore-prefixed private names from `herness.store.lake` (`_BUSY_ERRNOS`, `_BUSY_WINERRORS`, `_PARQUET_OPTIONS`, `_fsync`) instead of through a public surface. Disclosed and justified in the report (lake.py is at its 380-line budget). Acceptable given the constraint, but it's a cross-module privacy-boundary coupling that would need revisiting if `lake.py`'s internals change or a third private consumer appears.
3. FT02-01's fault mechanism deviation (see table above) — low risk, well-justified, but flagged for tracking until impl 08 lands.

## Assessment

**Task quality:** Approved
**Reasoning:** Implementation matches the binding spec's unit signatures, algorithms and error semantics exactly (verified by manual trace against docs/impl/02-data-model.impl.md §3.3), all four listed test IDs are present with correct expected values, and the report's gate evidence is corroborated where checkable from the diff (line count, error classes reused from `lake.py`, log event names). No Critical or Important findings; the three Minor items are disclosed deviations or low-risk style notes, not defects.
