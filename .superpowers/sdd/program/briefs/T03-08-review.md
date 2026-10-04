# T03-08 review: Decision cache core (HEAD f0b192d, base 677d7bd)

**Verdict: Needs fixes** (1 Important, 4 Minor)

### Spec Compliance
- U03-36 `CACHE_SCHEMA`: ✅ verbatim (cache.py:30-42). Partition columns come from the Hive path.
- U03-37 `DecisionCache`: ✅ `dataset` / `register` / `existing_keys` / `writer` match the signatures. `.tmp` and dot-prefixed files are never read. A schema mismatch raises `SchemaViolation("cache part schema mismatch: <file name>")`. An empty table is registered when there are no parts.
- U03-38 `CacheWriter`: ✅ follows the spec's order: tmp write (zstd), fsync, `os.replace`, fault point, DEBUG log, then buffer clear. It keeps an in-memory key set, skips error outputs and unknown qids, raises `SchemaViolation` on a decider or version mismatch, maps EACCES/EBUSY to `StoreBusy` and everything else to `FatalError`, and its context manager flushes on normal exit only. There is one gap in the errno mapping on the cleanup path (see Important).
- UT03-33: ✅ extra column rejected through `register` with the exact message. A missing column, the schema constant, and the empty and decoded views are also tested.
- UT03-34: ✅ covers 2 partitions, a `.tmp`, an `_`-prefixed file and `questions.json`. The tmp is ignored, keys are filtered by fingerprint and partition, and a tmp-only partition gives None.
- UT03-35: ✅ covers duplicates, an error output and an unknown qid: one part, no duplicates, fault point called once after the rename and not called on failure. Auto-flush, the context manager, close, the mismatch error and the errno mapping are tested too.
- TH03-18: ✅ strict physical-schema check on every fragment on each `dataset()` call, so every read path goes through it.
- Rulings applied (not findings): `_fault_point` no-op until T08-08; FT03-03 is deferred to the fault suite.
- Builder deviations, judged on correctness:
  1. pyarrow `HivePartitioning(segment_encoding="uri")` in place of the Python join: ✅ correct. `layout.cache_partition` uses `quote(v, safe="")` (layout.py:115). The decider_version regex forbids `%`, so there is no double-decode ambiguity. Decoded values also reach `dataset()` and `existing_keys`, which the spec's join approach would not give. A test covers a version containing `/` and `:`.
  2. Explicit list of `decider=*/decider_version=*/part-*.parquet` files: ✅ correct and stricter. It keeps `questions.json`, `_migrated_from_*.json`, `.tmp` and stray files out. `ignore_prefixes` has no effect with an explicit list but does no harm.
  3. Computing missing fingerprints: ✅ reader and writer use the same `_fingerprint` helper, so they agree.
  4. `flush_rows < 1` guard: ✅ harmless. The error class is questionable (Minor 3).
- Gates re-run in the worktree (`uv run` worked): enrich unit tests 258 passed, 1 skipped (a pre-existing symlink-privilege skip). cache.py coverage is 100% line and 100% branch (126 statements, 28 branches). ruff check clean, ruff format 176 files unchanged, mypy clean (80 files), lint-imports 11 kept / 0 broken, check_module_size exit 0. No warnings in test output.

- ⚠️ Cannot verify from diff:
  - Parts written by other writers must pass the exact `physical_schema.equals` check. This covers T03-09 migrate and compact, and any DuckDB `COPY`: map field naming and nullability must round-trip exactly as pyarrow writes them. Only the pyarrow writer path is exercised here. T03-09 should write through `CACHE_SCHEMA`/pyarrow or test the round-trip.
  - FT03-03 (fault test on `enrich.after_batch_write`) is deferred to the fault suite by ruling.

### Strengths
- Atomic tmp, fsync, `os.replace` sequence with a unique ULID target, so a rename never collides with a reader holding an existing part open on Windows.
- A failed flush keeps the buffer and the key set, so a retried `flush()` writes the same rows once. The fault point is never called on failure.
- The schema check sits in `dataset()`, so `register`, `existing_keys` and future callers cannot bypass TH03-18.
- Tests assert real behaviour: exact rows and fingerprints, exact error types and messages, directory contents after failure, and fault-point call counts.

### Issues
#### Critical (Must Fix)
- None.

#### Important (Should Fix)
1. **cache.py:222-226: a cleanup failure hides the errno mapping.** In the `except OSError` handler, `tmp.unlink(missing_ok=True)` runs before the error is mapped. If the unlink raises, a raw `PermissionError` escapes instead of `StoreBusy`/`FatalError`, and the original exception is only kept as `__context__`. That is the typical Windows case: `os.replace` fails with EACCES because an AV scanner, the indexer or another process holds the tmp, and the unlink then fails for the same reason. The spec's contract that an OS error on write maps to `StoreBusy` on a Windows lock is then broken, and retry policies keyed on `StoreBusy` will not fire. Fix: `with contextlib.suppress(OSError): tmp.unlink(missing_ok=True)`, and add a test that makes the unlink raise too.

#### Minor (Nice to Have)
1. **cache.py:78-101: `dataset()` repeats the full scan on every call.** It re-globs the partition tree and opens every part's footer for the schema check each time. `existing_keys` is called "per entity and per decider" (U03-37), so each call costs O(parts) file opens. Compaction (T03-09) bounds this, but caching the validated dataset per instance, or per file list, would avoid it.
2. **cache.py:212: the Arrow table is built outside the `try`.** If a value does not fit the schema, for example `samples` > 32767 for int16, the raw `pyarrow.ArrowInvalid` escapes instead of a herness error. Low likelihood; a `SchemaViolation` wrap or a range check in `add` would close it.
3. **cache.py:148-150: `flush_rows < 1` raises `SchemaViolation`.** This is a caller or programming error rather than a data schema problem. The guard itself is fine; consider the core error class the codebase uses for invalid arguments or configuration, if one exists.
4. **cache.py:86-94: `ignore_prefixes=[".", "_"]` has no effect.** It does nothing when an explicit file list is passed; the glob is what enforces the "never read .tmp or dot-prefixed files" invariant. The docstring comment should say so, so that a future edit back to directory discovery keeps both.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** The implementation is spec-compliant and well tested, and all gates pass. The one Important item is a small but real hole in the specified errno mapping on Windows lock failures, and the fix is one line plus a test.

---

## Re-review round 1 (HEAD 48f28a3, fix commit on top of f0b192d)

**Verdict: Approved**

Scope: Important 1 and Minors 2, 3 and 4. Minor 1 (`dataset()` re-scans on every call) was parked by the sub-controller.

- **Important 1: resolved.** cache.py now wraps the tmp unlink in `contextlib.suppress(OSError)`, so a failed cleanup no longer hides the errno mapping and the original exception stays chained with `from exc`. The new test `test_ut03_35_os_error_mapping_when_unlink_fails_too` makes both `os.replace` and `Path.unlink` fail, parametrized over EACCES, EBUSY and ENOSPC. It asserts the exact class and that the fault point is not called. A tmp file that cannot be removed may be left behind; readers never see it, because the glob only matches `part-*.parquet`.
- **Minor 2: resolved.** The builder's choice is to validate `samples` at the top of `add()`: it must be None or 1..32767, otherwise `ConfigError`. This is sound:
  - The check runs before the loop, so nothing is partially buffered and the key set is not polluted. The test confirms that a later `flush()` returns None.
  - The lower bound of 1 matches what `samples` means (a count of samples, or None for deciders that don't sample).
  - `ConfigError` is the right class, since the value is a caller argument taken from decider settings.
  - The message carries no data values.
  - The builder argues that every other value reaching the Arrow table has already been validated by the pydantic models. That is plausible, and I accept it.
  - Tests cover 0, -1 and 32768 (rejected) and 32767 (accepted and round-tripped).
- **Minor 3: resolved.** `flush_rows < 1` now raises `ConfigError`, and the test is updated.
- **Minor 4: resolved.** The dead `ignore_prefixes` argument is removed, and the docstring now says the `_part_files` glob is what enforces the "never read .tmp or dot-prefixed files" invariant. Behaviour is unchanged, because an explicit file list never applied it. This departs from the spec's literal `ds.dataset(...)` call, but the spec's intent is kept and UT03-34 still covers it.
- **Gates re-run (`uv run` worked):**
  - `test_cache.py`: 20 passed. `cache.py` is at 100% line and 100% branch coverage (132 statements, 30 branches).
  - ruff check and ruff format clean (176 files).
  - mypy clean (80 files).
  - lint-imports 11 kept / 0 broken.
  - check_module_size exit 0.
  - No warnings in the test output.

No new findings. Open items: Minor 1, parked. The ⚠️ from round 1 carries over to T03-09: parts written by other writers must pass the exact schema check.
