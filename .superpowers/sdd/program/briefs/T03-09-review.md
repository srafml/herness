# T03-09 review: Cache maintenance (migrate U03-39, compact U03-40, purge_hashes U03-41)

Reviewed: worktree agent-a24273d0fc964c7f6, base ee452a6, head 20ceb6f (1 commit, 3 files).

### Spec Compliance
- ❌ Issues found: one (log field name of `enrich.cache.compacted`, see Important #1). Everything else conforms.

| Item | Status | Notes |
|------|--------|-------|
| U03-39 migrate | ✅ | Precondition old != new -> ConfigError (cache_maint.py:124). Marker present -> returns its `rows` with no work (:129-132). Per old partition: batched read (1M), vectorised `(question, fingerprint)` filter against the new set (:133-139), dedupe latest `decided_at`, one atomic part written into `cache_partition(new, decider, unquote(v))` (:142-150). Marker `{rows, finished_at}` written last, atomically (:151-152). Log `enrich.cache.migrated` INFO old/new/rows (:153). Both qsvs are validated by `EnrichPaths.cache_dir` before the marker path is built. |
| U03-40 compact | ✅ (log ❌) | Parts < small_bytes; fewer than 2 skipped (:170-172); dedupe on the 3-column key keeping max `decided_at` (:173); merged part written atomically, then sources deleted (:174-177), so a crash leaves duplicates only. Returns parts removed. Log field is `parts`, catalogue says `parts_removed` (Important #1). |
| U03-41 purge_hashes | ✅ | Regex `^[0-9a-f]{32}$` via fullmatch -> ConfigError with no hash in the message (:205-207). Walks every version dir under `data/cache/decisions` (:208-210). Reads only `content_hash` first, skips when no hit (:185-188). Rewrites the same file name atomically or deletes it when empty (:189-195). Returns rows deleted, log `enrich.cache.purged` INFO rows/files (:221). Idempotent. |
| cache.py refactor | ✅ | `io_error`/`replace_atomic`/`write_part` (cache.py:72-110). `CacheWriter.flush` (:257-263) keeps the same tmp name `.part-<ulid>.parquet.tmp`, the mkdir/write/fsync/os.replace order, tmp cleanup under suppress(OSError), the message text `cannot write cache part: <name>`, the StoreBusy (EACCES/EBUSY) vs FatalError split with `decider=`, fault point, and the DEBUG log. The buffer is still not cleared on error. UT03-33..35 pass. |
| T03-08 carry-over: schema | ✅ | `write_part` does `select(CACHE_SCHEMA.names).cast(CACHE_SCHEMA)`, so no hive columns go into a part and map key/value names and nullability come from CACHE_SCHEMA. All reads check the exact schema (`_read`, :81-83). Tests check every maintained part with `read_schema().equals(CACHE_SCHEMA)` and read it back through `DecisionCache.dataset()`. |
| UT03-36 | ✅ | test_cache_maint.py:86-142. Two questions, q_b's fingerprint changed; the unchanged q_a is copied once, latest answer kept, one part; the second call is a no-op (returns 1 from the marker, still one part). Extra cases: URL-quoted version, empty old version, bad marker, foreign schema (no marker written). |
| UT03-37 | ✅ | :145-197. Five small parts with duplicate keys become one part, latest `decided_at`/answer kept, `.tmp` untouched, a single-part partition skipped, the second run returns 0. Also: large parts are left alone, bad threshold refused, a failed write keeps the sources (StoreBusy). |
| UT03-38 | ✅ | :200-249. Two versions: the mixed part is rewritten, the emptied part deleted, the untouched part keeps its mtime, count is 3, rerun returns 0. Also: hash validation, empty set, and OS-error mapping parametrised over EACCES/EBUSY/ENOSPC. |
| Test IDs / markers | ✅ | Every function name has `ut03_3x`, each docstring starts with the ID, and `pytestmark = unit`. |
| Budgets | ✅ | cache_maint.py is 222 lines (budget 260); cache.py is 280 (budget 360). check_module_size exits 0. |

- ⚠️ Cannot verify from diff: IT03-05 (the pipeline-level migrate) belongs to a later card. Nothing in T03-09 calls `compact` at the end of decide or `purge_hashes` from `purge_record`; later cards (decide pipeline, T10-29) wire those.

### Verification run (reviewer)
- `PYTHONUTF8=1 uv run pytest tests/unit/enrich -q -p no:logging` with `--cov-branch`: 306 passed, 1 skipped (symlink privilege, pre-existing). cache_maint.py and cache.py are both at 100% line and 100% branch.
- `uv run python -m tools.check_module_size`: exit 0. `uv run mypy`: no issues in 113 files. `uv run ruff check .`: clean. `ruff format --check`: clean.

### Strengths
- One atomic write path (`write_part`) for every maintained part removes any chance of schema drift or hive columns and reuses the U03-38 error mapping.
- Purge reads a single column first and leaves untouched files alone (the test checks mtime). Error messages and logs carry only file or partition names and counts, never hashes or row data (TH03-12, ENG §3.4).
- Tests assert real behaviour: row contents, `decided_at`, part counts, schema equality, read-back through the real reader, and error types checked exactly.

### Issues

#### Critical (Must Fix)
- None.

#### Important (Should Fix)
1. **The `enrich.cache.compacted` log field name does not match the binding log catalogue.** herness/enrich/cache_maint.py:179 logs `parts=removed`, but the spec's log catalogue (docs/impl/03-enrichment.impl.md:3398) defines the fields as `qsv`, `parts_removed`. Change it to `parts_removed=removed`. A test could assert the event fields (capture_logs) so this cannot drift.

#### Minor (Nice to Have)
1. **Private cross-module import.** cache_maint.py:28 imports `_fingerprint` from cache.py. Rename it to a public helper, or use `q.fingerprint or question_fingerprint(q)` from `herness.enrich.questions` directly.
2. **Migrate is not row-idempotent after a crash before the marker (build concern 1).** cache_maint.py:149 writes a fresh `part-<ulid>` on every run, so a crash between the part writes and the marker (:152) duplicates the rows on rerun. The spec's algorithm accepts this ("marker last"), and readers' QUALIFY dedupe and `compact` both absorb it. The returned count is still correct because it is recomputed. A deterministic name (e.g. `part-migrated-<old_qsv>.parquet`, replaced atomically) would make reruns overwrite instead. Optional.
3. **Python-loop dedupe (build concern 2).** cache_maint.py:91-101 turns the three key columns into Python lists. That is fine at compaction sizes, but in migrate a partition with more than 1M rows costs several seconds and a few hundred MB. It could be vectorised (e.g. join the key with `pc.binary_join_element_wise`, then group_by/aggregate max `decided_at`, or a stable sort with first-per-group). Not a correctness issue.
4. **TH03-12 hardening: stale tmp files.** Purge only scans `part-*.parquet` (cache_maint.py:217 via `_PART_GLOB`). A `.part-*.parquet.tmp` left by a crashed writer, or by a Windows lock that blocked the tmp unlink (cache.py:94-95), could still hold a purged hash. The postcondition covers only "parts", but the privacy intent suggests purge should also remove leftover `.part-*.tmp` files in each partition.
5. **Corrupt parts raise raw pyarrow errors.** A corrupt or non-parquet part raises `pyarrow.ArrowInvalid` (a ValueError) from `pq.ParquetFile` (cache_maint.py:80), which is not mapped to a Herness error. This matches the existing reader behaviour, so it is only noted.
6. **Misleading error text for the marker.** A marker write failure reports "cannot write cache part: _migrated_from_...json" (cache.py:96, via the call at cache_maint.py:152). The generic `replace_atomic` could take the noun as a parameter.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** The algorithms, atomicity, schema conformance, the refactor's preservation of flush behaviour, privacy handling and the tests are all sound, with 100% coverage and every gate green. The one binding-spec deviation, the `enrich.cache.compacted` field `parts` instead of `parts_removed`, is a one-line fix.


---

## Round 1 re-review

Scope: I-1, M1, M4 and M6, at commit 708a58b (diff 20ceb6f..708a58b). By ruling, M2, M3 and M5 are parked and not re-raised.

| Finding | Status | Evidence |
|---------|--------|----------|
| I-1 compacted log field | ✅ | cache_maint.py now logs `enrich.cache.compacted` with `qsv=..., parts_removed=removed`, matching the catalogue at 03-enrichment.impl.md:3398. The new test `test_ut03_37_compact_and_migrate_log_catalogue_fields` uses capture_logs to assert the exact field sets of both `compacted` and `migrated`. |
| M1 private `_fingerprint` import | ✅ | The import is gone. cache_maint.py now uses `q.fingerprint or question_fingerprint(q)` from `herness.enrich.questions`, which is the same rule as cache.py. |
| M4 stale tmp files (ruling: purge deletes them) | ✅ | The new `_drop_stale_tmp` deletes `.part-*.parquet.tmp` files in every partition of every version before the parts are scanned. OS errors go through `_os_errors`, so they map to StoreBusy or FatalError. The log's `files` count includes the deleted tmp files, and the docstring says so. Purge runs only inside the exclusive maintenance job, so no live writer's tmp file can be caught. The new test `test_ut03_38_purge_deletes_stale_tmp_files` checks that the tmp file is gone, the kept part stays, 0 rows are returned, and the log shows `files=1`. |
| M6 marker error text | ✅ | `replace_atomic` takes a keyword-only `kind` parameter that defaults to "cache part", so the `CacheWriter.flush` and `write_part` messages are unchanged. Migrate passes `kind="migration marker"`. The new test `test_ut03_36_marker_write_error_names_marker` asserts the exact message: FatalError on ENOSPC, naming only the file. |

Reviewer run:
- Enrich unit tests: 309 passed, 1 skipped (the existing symlink skip).
- Coverage: cache_maint.py and cache.py are both at 100% line and 100% branch.
- `check_module_size` exits 0. cache_maint.py is 238 lines (budget 260); cache.py is 285 (budget 360).

New findings: none.

**Task quality:** Approved
