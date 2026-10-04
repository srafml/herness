# Review: T05-13 Warehouse handles

Commit 3062d27 (base 8e63723), worktree `agent-a26e796c1c53753e8`.

## Spec compliance (U05-38)

| Clause | Status | Notes |
|---|---|---|
| `BUILD_ID_RE` exact | ✅ | `warehouse.py:43` verbatim match to spec regex. |
| Precondition: build_id regex → `ConfigError("invalid build_id")` | ✅ | `_validated_path` (`warehouse.py:68-71`). |
| Precondition: resolved path inside `warehouse_dir.resolve()`, not a symlink → `ConfigError` | ✅ | `_validated_path` (`warehouse.py:72-80`): `is_symlink()` (lstat, no-follow) checked *before* the `resolve().parent != base` containment check — correct order, each catches a distinct case (final-component symlink vs. any residual escape), and build_id's character class already excludes `/`, `\`, `.` so traversal via build_id is doubly blocked. |
| Precondition: file exists → `QueryError("warehouse build <id> not found", hint=None)` | ✅ | `open_warehouse` (`warehouse.py:169-171`), checked after path validation, matching precondition order. |
| Postcondition: `duckdb.connect(..., read_only=True, config={...})` exact keys | ✅ | `_connect_config` (`warehouse.py:59-65`) matches the spec dict verbatim (threads/memory_limit from `SqlSettings`, autoinstall/autoload off). |
| Postcondition: `SET enable_external_access = false` then `SET lock_configuration = true`, in that order | ✅ | `warehouse.py:177-178`. |
| Postcondition: `schema()` loaded once from `information_schema.columns` for core/enrich/metrics/score/meta, lower-case, cached | ✅ | `_load_schema` + `DuckWarehouse.schema()` (`warehouse.py:83-94`, `128-132`); identity-cache verified by `test_ut05_49_schema_and_table_comment`. |
| Postcondition: `table_comment()` from `duckdb_tables().comment`, `""` on NULL | ✅ | `warehouse.py:134-141`; verified by same test (comment case and NULL→`""` case). |
| Invariant: thread-local `cursor()` per calling thread | ✅ | Each `DuckWarehouse` owns its own `threading.local()` instance (`warehouse.py:107`), so cursors cannot leak across handles/builds; verified by `test_ut05_49_cursor_is_per_thread`. |
| Invariant: result cache ≤ 512 entries, LRU, only `row_count ≤ return_rows` | ✅ | `RESULT_CACHE_ENTRIES = 512` (`warehouse.py:44`); `cache_put` gates on `row_count > return_rows` before storing (`warehouse.py:151-160`), `OrderedDict` + `move_to_end`/`popitem(last=False)` under `self._cache_lock` for LRU. |
| Algorithm: `WarehousePool.get` under lock, opens or returns, closes LRU past `max_open` | ✅ | `warehouse.py:196-208`, whole body (including the `open_warehouse` call and eviction close) inside `with self._lock`, matching the spec's "under a threading.Lock" wording. |
| Algorithm: self-check `current_setting('enable_external_access')` false else `ConfigError` | ✅ | `warehouse.py:179-182`, uses `is not False` (identity, not `!=`) — correctly avoids `0 == False` truthiness ambiguity. |
| Errors: `ConfigError`/`QueryError` as specified; DuckDB `IOException` on open → `QueryError(...unreadable)` | ✅ | `warehouse.py:172-176`; try/except scoped only around `duckdb.connect`, matching "on open". |
| Concurrency: lock-protected pool; per-thread cursors | ✅ | See above. |
| Complexity/size | ✅ | 207 lines vs. 220-line budget (module map row); functions simple, no C901 risk apparent. |

## Test rows

| ID | Status | Notes |
|---|---|---|
| UT05-49 | ✅ | `test_ut05_49_*` (7 functions) cover open+lock+self-check on an inline tiny-build, schema/table_comment, per-thread cursor, missing-build → `QueryError`, self-check-fails-loudly via monkeypatched `duckdb.connect`, `IOException`→`QueryError`, cache LRU+`return_rows` gate. |
| UT05-50 | ⚠️ | Bad-id and pool-LRU cases (`test_ut05_50_bad_build_id_raises_config_error`, `test_ut05_50_pool_get_caches_and_lru_closes_over_max`) correctly drive `WarehousePool.get` as the spec table's Action column names. The symlink case (`test_ut05_50_symlinked_warehouse_file_raises_config_error`) calls `wh.open_warehouse(...)` directly rather than `pool.get(...)` — functionally equivalent (the pool never swallows `open_warehouse`'s exceptions) but doesn't literally exercise the pool path the table lists. Minor test-fidelity gap, not a behavioural one. |
| ST05-06 | ✅ | All six listed statements (CREATE, INSERT, COPY, `read_csv` absolute path, `SET enable_external_access=true`, ATTACH) parametrized and asserted to raise `duckdb.Error` at the locked connection (`test_st05_warehouse.py:254-274`). |
| ST05-17 | ✅ (with one ⚠️) | build_id traversal/absolute/separator variants and the symlinked-file case all raise `ConfigError`. The "run_id with separators" sub-case is covered by `test_st05_17_run_id_not_a_path_input_of_this_unit`, a signature-introspection test proving `run_id` is not a parameter of this unit at all rather than a runtime attack test — a reasonable and honestly-documented interpretation given the unit genuinely takes no `run_id`, but flagged since it's a different kind of test than the table implies. |

Confirmed by running: `PYTHONUTF8=1 uv run pytest tests -k "UT05_49 or UT05_50 or ST05_06 or ST05_17" -q -p no:logging` → **26 passed, 2 skipped** (both skips are the symlink cases, `OSError: WinError 1314` — no `SeCreateSymbolicLinkPrivilege` on this box), matching the build report exactly.

## Findings

### Critical
None.

### Important
None.

### Minor
1. `herness/harness/warehouse.py:177-178` — `SET enable_external_access = false` / `SET lock_configuration = true` are not wrapped in a try/except; if either ever raised (e.g. a future DuckDB version behaving differently from the VI-4 empirical check), a raw `duckdb.Error` would propagate instead of a Herness error class (`ConfigError`/`QueryError`), unlike the `IOException`-on-connect path which is explicitly translated. Spec's Errors field only requires translation for connect-time `IOException`, so this isn't a spec violation — just a residual robustness gap worth a follow-up note.
2. `herness/harness/warehouse.py:134-141` (`table_comment`) does not lower-case the `schema_name`/`table_name` split out of `qualified`, unlike `schema()` which lower-cases every key. Not exercised by any failing case here (all call sites use already-lowercase names), but could produce silent misses if a caller ever passes mixed-case input.
3. `tests/unit/harness/test_warehouse.py:526-539` — UT05-50's symlink sub-case calls `open_warehouse` directly rather than `WarehousePool.get`, per the ⚠️ test-row note above.

## Sub-controller rulings applied

- `_CachedRow` Protocol stand-in for `RecordedResult` (U05-35 not yet built): applied cleanly, documented in the module docstring and class docstring, isolated to the `_CacheValue` type alias — retyping later is a one-line change as claimed.
- Inline `tiny_build`-shaped fixture in both test files (spec 11 fixture absent): applied cleanly, same schema set (core/enrich/metrics/score/meta) as the eventual real fixture; carry-over noted in the report.
- Symlink tests skip on Windows without privilege: applied correctly via `pytest.skip` on `OSError`, confirmed by the test run above (2 skips, not silent passes/xfails).

## Verdict

**Approved.**

All named security-focus items — path containment + symlink check ordering, `BUILD_ID_RE` exactness, connection config and `SET` order, self-check, `IOException` mapping, thread-local cursors (no cross-handle leak), pool LRU under lock with evicted-handle close, and cache LRU(512) + `row_count ≤ return_rows` gating — match the spec exactly and are exercised by passing tests. Only Minor findings (test-fidelity nit on one UT05-50 sub-case, two small robustness/consistency notes); none block merge.
