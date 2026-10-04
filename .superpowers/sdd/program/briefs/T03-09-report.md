# T03-09 report: Cache maintenance (migrate, compact, purge_hashes)

Status: DONE_WITH_CONCERNS (minor; see Concerns)
Commit: 20ceb6f feat(enrich): add cache maintenance migrate, compact, purge_hashes (T03-09)
Worktree: D:\herness\.claude\worktrees\agent-a24273d0fc964c7f6 (base ee452a6)

## What was built
- herness/enrich/cache_maint.py (222 lines, budget 260; L3, pyarrow only):
  - `migrate(paths, old_qsv, new_qs) -> int` (U03-39): refuses old == new (ConfigError); marker
    `cache_dir(new)/_migrated_from_<old>.json` present -> returns its `rows` without work (unreadable
    marker -> ConfigError). Per old partition: reads parts in 1M-row batches, keeps rows whose
    (question, question_fingerprint) pair is in the new set (vectorized is_in on a joined key),
    dedupes by key keeping latest decided_at, writes ONE part in `cache_partition(new, decider,
    unquote(version))` (so partition names are re-validated by EnrichPaths). Marker
    `{rows, finished_at}` (format_utc) written last, atomically. Logs `enrich.cache.migrated` (INFO old/new/rows).
  - `compact(paths, qsv, *, small_bytes=64*2**20) -> int` (U03-40): per partition, parts < small_bytes;
    fewer than 2 -> skip; read, dedupe (latest decided_at), write merged part atomically, then delete
    sources; returns source parts removed. small_bytes < 1 -> ConfigError. Logs `enrich.cache.compacted` (qsv, parts).
  - `purge_hashes(paths, hashes) -> int` (U03-41): validates `^[0-9a-f]{32}$` (ConfigError); for every
    part under every version dir of data/cache/decisions reads only `content_hash`; untouched when no
    hit; else filters and replaces the file in place atomically (same name), or deletes it when empty.
    Returns rows deleted. Logs `enrich.cache.purged` (INFO rows, files).
  - All reads validate the part schema is exactly CACHE_SCHEMA (SchemaViolation otherwise, as readers).
    OSErrors on read/list/delete map via the U03-38 rule (StoreBusy for EACCES/EBUSY, else FatalError).
- herness/enrich/cache.py (247 -> 280 / 360): extracted shared helpers, reader semantics unchanged:
  `io_error(exc, msg, *, decider)`, `replace_atomic(target, write, *, decider)` (`.<name>.tmp` +
  fsync + os.replace, tmp cleanup, error mapping) and `write_part(partition, table, *, name=None,
  decider=None)` (select+cast to CACHE_SCHEMA, zstd). `CacheWriter.flush` now uses `write_part`;
  tmp name (`.part-<ulid>.parquet.tmp`), message text and error mapping are identical; all UT03-33..35 green.
- tests/unit/enrich/test_cache_maint.py (249 lines): UT03-36 x4, UT03-37 x3, UT03-38 x3 (+ parametrized
  OS-error mapping). Every maintained part is checked for exact CACHE_SCHEMA and read back through
  DecisionCache.dataset() (hive columns come from the path, none written into files; tmp files ignored).

## Evidence
- RED: `PYTHONUTF8=1 uv run pytest tests/unit/enrich/test_cache_maint.py -q -p no:logging` ->
  collection error (ModuleNotFoundError herness.enrich.cache_maint).
- GREEN: tests/unit/enrich: 306 passed, 1 skipped (symlink privilege). Coverage cache_maint.py 100% line /
  100% branch; cache.py 100/100.
- Repo `pytest -m "(unit or integration) and not slow"`: 3169 passed, 5 skipped, 1 xfailed (pre-existing).
- Gates: ruff format/check clean, mypy (113 files) clean, lint-imports 12 kept 0 broken,
  check_module_size exit 0, check_type_ownership clean. Pre-commit hooks all passed (no --no-verify,
  no PRE_COMMIT_ALLOW_NO_CONFIG). detect-secrets passed; .secrets.baseline not changed.

## Deviations / spec notes
- `_fingerprint` (private) is imported from cache.py into cache_maint.py to share the fingerprint rule
  (q.fingerprint or question_fingerprint(q)); no ruff finding. Could be made public later.
- compact returns the number of source parts removed (UT03-37: 5 parts -> 5) and only merges parts below
  small_bytes, per the algorithm; duplicates across a large part and small ones are left to readers' dedupe.
- purge_hashes with an empty set returns 0 without scanning. Version directories under data/cache/decisions
  are iterated as-is (not re-validated as qsv), since purge must reach every version on disk.
- No fault point added (spec names none for maintenance); no `_fault_point` call in cache_maint.

## Concerns
- migrate crash after writing some new parts but before the marker: a rerun writes the rows again as
  new parts (duplicates only; readers dedupe, compaction removes). Consistent with "marker written last",
  but not strictly row-idempotent on crash.
- Dedupe uses a Python loop over the three key columns (to_pylist); fine for "sum of small parts" /
  per-partition sizes, but a 1M+ row partition in migrate costs a few seconds and memory for the key lists.
- Global constraints say Co-Authored-By "Claude Opus 5.5 (1M context)"; I used the dispatch's
  "Claude Opus 5.5 <noreply@anthropic.com>" as instructed.

## Fix round 1
Commit: 708a58b fix(enrich): align cache maintenance logs and purge stale tmp parts (T03-09)
- I1: `enrich.cache.compacted` now logs `qsv`, `parts_removed` (catalogue line 3398). Checked `migrated`
  (`old`, `new`, `rows`) and `purged` (`rows`, `files`): both already matched. New test
  test_ut03_37_compact_and_migrate_log_catalogue_fields asserts the exact fields (structlog capture_logs).
- Minor 1: cache_maint no longer imports `_fingerprint`; it uses `q.fingerprint or question_fingerprint(q)`
  (herness.enrich.questions) directly.
- Minor 4: purge_hashes deletes every stale `.part-*.parquet.tmp` in each partition it scans (TH03-12).
  These files count toward the `files` log field; OSError maps as U03-38. New test
  test_ut03_38_purge_deletes_stale_tmp_files. If the hash set is empty, purge still returns 0 without scanning.
- Minor 6: `replace_atomic` takes keyword `kind` (default "cache part", so flush messages are unchanged).
  The marker is written with kind="migration marker", so the message is
  "cannot write migration marker: _migrated_from_<old>.json" (file name only). New test
  test_ut03_36_marker_write_error_names_marker.
- Line counts: cache_maint.py 238/260, cache.py 285/360, test_cache_maint.py 294.
- Tests: tests/unit/enrich 309 passed, 1 skipped. Coverage: cache_maint.py and cache.py both 100% line and branch.
  ruff, mypy, lint-imports and check_module_size (exit 0) are clean. All pre-commit hooks passed.
- Parked as instructed: M2, M3, M5.
