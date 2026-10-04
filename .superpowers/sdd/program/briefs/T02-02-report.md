# T02-02 Lake writer — build report

Status: DONE_WITH_CONCERNS
Commit: f8fe6d5 feat(store): add lake writer (T02-02) on branch worktree-agent-a1af0ed3d76167e88 (base 3bd317c)

## Context
A previous build agent was cut off mid-card. This agent took over its uncommitted work (lake.py, unit, property and integration tests, and a pyproject mypy override), checked it against the brief and spec §3.2 (U02-08…U02-19) unit by unit, strengthened ST02-02, ran every gate and committed.

## Implemented (herness/store/lake.py, 380 lines; budget 380, ENG limit 400)
- Constants: BUFFER_ROWS, BUFFER_BYTES, ZSTD_LEVEL, PARQUET_VERSION, LAKE_FILE_PATTERN="[!.]*.parquet", NAME_RE, COLUMN_RE, META_COLUMNS (U02-08).
- validate_name (U02-09), partition_dir with containment check (U02-10), lake_glob (U02-11), LakeFileSet frozen dataclass (U02-12).
- LakeWriter (U02-13…U02-18): name and range validation; root=None -> data_layout().raw; injected monotonic clock; age, dt_change, schema_change, size and commit rotation, each logged as store.lake.file_rotated (DEBUG); temp names .part-<ulidA>.parquet.tmp-<ulidB>; zstd level 3 Parquet 2.6 with dictionary and statistics; commit runs fsync then os.replace, can be retried safely, and fsyncs directories on POSIX only; store.lake.committed (INFO); abort is idempotent, suppresses only OSError and ArrowException, and logs locked temps as store.lake.abort_leftover (WARNING); the context manager aborts on exit and logs store.lake.uncommitted_exit on a normal exit without commit. OSError mapping: EACCES/EBUSY/winerror 32 or 33 -> StoreBusy; any other -> SchemaViolation("lake write failed for <source>/<entity>: <ERRNO>").
- _validate_batch (U02-19): rules 1–8 applied in the spec order with vectorised pyarrow.compute checks. Time columns are cast to timestamp(us, UTC); a cast overflow is reported as rule "type". large_string is cast to string. Columns are reordered with the metadata columns first.
- pyproject.toml: mypy override ignore_missing_imports for pyarrow / pyarrow.* (pyarrow ships no py.typed; no stub package is locked).

## Tests
- tests/unit/store/test_store_lake.py: UT02-01…UT02-13, ST02-01 (plus edge cases for type, null, column-name, duplicate and overflow rules; retrying commit after a failure; the OSError mapping; the default root).
- tests/unit/store/test_store_lake_property.py: PT02-01 (hypothesis, 60 examples; random batch sizes, dates, BUFFER_ROWS, age rotation, schema changes, tombstones).
- tests/integration/store/test_store_lake_glob.py: ST02-02. This agent strengthened it: it now places a dot-prefixed `.hidden.parquet` next to the temps and asserts DuckDB's glob() does not match it. That settles OI-06: DuckDB honours `[!.]`, so the fallback pattern is not needed.

RED: with lake.py moved away, `uv run pytest tests/unit/store/test_store_lake.py` -> ImportError: cannot import name 'lake' from 'herness.store' (collection error).
GREEN: `PYTHONUTF8=1 uv run pytest tests/unit/store tests/integration/store -q -p no:logging --cov=herness.store.lake --cov-branch` -> 54 passed (before the ST02-02 addition; 1/1 passing after); lake.py coverage 98 % (the only misses are the POSIX-only directory fsync branch and 2 partial branches).

## Gates
- ruff format: 43 files unchanged; ruff check: All checks passed
- mypy: Success, no issues in 19 source files
- lint-imports: 8 kept, 0 broken
- check_type_ownership: exit 0
- pytest -m "(unit or integration) and not slow": 213 passed, 4 deselected

## Deviations / concerns
1. Acceptance check "spec 11 generator smoke (T11-14 tools/synth_data.py --scale tiny) writes a readable lake" cannot be run: tools/synth_data.py does not exist yet (impl 11). The tests above check that committed files are readable, with pyarrow and with DuckDB read_parquet.
2. OI-06 in docs/impl/02-data-model.impl.md still says "Still open". ST02-02 now answers it (the answer is yes). The spec doc was not edited, as it is outside the card's files.
3. The `clock` parameter of LakeWriter.__init__ shadows the module alias `clock` (herness.core.time) inside __init__ only. This is harmless (the default binds when the function is defined; commit() uses the module alias) and kept because the spec names the parameter `clock`.
4. lake.py is at exactly its 380-line budget, which leaves no headroom for changes during review.

## Fix round 1 (commit d36af6a fix(store): address T02-02 review round 1 (T02-02))
- M1: `_normalise` (which wrapped the time cast in try/except) is replaced by `_cast_time`. It now runs after rules 4–8. For s/ms units it counts out-of-range values with vectorised int64 bounds checks, so it raises LakeContractError(type, column, bad_rows) with no exception chained (no raw values). The ns→us cast only truncates. `_TIME_COLUMNS` is now a tuple, so the error order is deterministic. Tests: the overflow test asserts bad_rows == 2 (±2**62), `__cause__`/`__context__` is None and the value is absent; a new test shows source_mismatch wins over a time overflow.
- M2: abort now catches any OSError from unlink (counted as leftover, logged, never raised). The leftover test is parametrised over PermissionError and OSError(EIO).
- M3: the temp entry is recorded before the ParquetWriter constructor runs. If the constructor fails, the temp is unlinked and the entry popped. If the unlink itself fails, the entry is kept so abort can delete the file. Two new tests cover this.
- An extra fix found while writing the M3 test: rows and max_source_updated_at were counted only after the whole batch was processed. A failed flush then left buffered rows uncounted, and a retry commit wrote them while LakeFileSet.rows said 0. They are now counted per buffered group.
- lake.py 377 lines (budget 380). Gates: ruff format/check clean, mypy clean, lint-imports 8 kept, type ownership 0, pytest 217 passed / 4 deselected; lake.py coverage 99 %. The only uncovered lines are the POSIX-only directory fsync.

## Fix round 2 (commit a34b01d fix(store): address T02-02 review round 2 (T02-02))
- N1 (Important): `_open_file` now appends (temp, final) to the publish list only after the ParquetWriter constructor succeeds. When the constructor fails, the temp is unlinked. If that unlink also fails, the temp goes to a separate `_discard` list, which commit never renames: it tries an unlink with OSError suppressed (the name starts with a dot, so the glob ignores it). abort also deletes these temps and counts them as leftover if deletion fails. New test UT02-10 `..._junk_never_published`: constructor failure plus a locked unlink, then a new write and a commit. It checks one committed file that is readable, with 2 rows, that the junk temp was not renamed and is gone, and that no temps remain.
- N2 (Minor): `_cast_time` checks the bounds of units s, ms and us against the Python datetime range (`_US_RANGE`, µs since the epoch for datetime.min/max). Out-of-range rows fail rule "type" with bad_rows before anything is buffered. ns always fits. The new parametrised test (s/ms/us) checks one row past 9999 and one before year 1, giving bad_rows == 2 with rows/max unchanged, and that the exact edge values are accepted.
- To stay within budget, the commit log call and the LakeFileSet return were condensed with no change in behaviour, and `_US_RANGE` is a literal constant.
- RED: with the round-1 lake.py, the 4 new test cases failed (the N1 test and 3 N2 cases). GREEN: all pass.
- lake.py 378 lines (budget 380). Gates: ruff format/check clean, mypy clean, lint-imports 8 kept, type ownership 0, pytest 221 passed / 4 deselected; lake.py coverage 98 % (the only misses are the POSIX dir fsync and one partial branch).
