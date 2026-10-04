# T02-04 Ops store core — build report

Status: DONE_WITH_CONCERNS (minor spec notes only; all gates green)
Commit: 515d3cb feat(store): add ops store core (T02-04)
Worktree: D:\herness\.claude\worktrees\agent-a7c5cb1e1f7b6ee89 (branch worktree-agent-a7c5cb1e1f7b6ee89, base 7b02bdb)

## Files and line counts vs budgets
| File | Lines | Budget |
|---|---|---|
| herness/store/ops/__init__.py | 33 | 160 |
| herness/store/ops/core.py | 259 | 260 |
| herness/store/ops/_shims.py | 65 | 80 (new override in pyproject `[tool.herness.module_budgets.overrides]`, reason "interim resilience shims (T02-04)") |
| tests/support/ops_core_store.py | 22 | - |
| tests/unit/store/ops/test_store_ops_core.py, test_store_ops_package.py, test_store_ops_shims.py | - | - |
| tests/fault/store/test_store_ops_fault.py, tests/security/test_st02_ops_core.py, tests/bench/test_store_ops_bench.py | - | - |

Other changes:
- pyproject.toml: adds the ops-areas-acyclic contract and a module budget override. The inline `overrides = {...}` became a `[tool.herness.module_budgets.overrides]` table; the loop.py entry is unchanged.
- tests/conftest.py: adds `tests.support.ops_core_store` to pytest_plugins.

core.py is at 259/260: I compressed docstrings and merged the error-mapping helpers to fit.

## Units
- U02-36 OPS_JSON_MAX_BYTES = 65_536.
- U02-37 connection():
  - Effective path is the override, else data_layout().ops_db.
  - The thread-local entry is reused only when (pid, path, generation) match. A stale connection from the same pid is closed; one from a foreign pid (fork) is dropped without closing.
  - Checks SQLite >= 3.38 and ENABLE_FTS5 (functools.cache, probe on :memory:).
  - PRAGMAs: busy_timeout 10000, journal_mode WAL (any other result is a ConfigError), foreign_keys ON, synchronous NORMAL, trusted_schema OFF, temp_store MEMORY, cache_size -65536. Row factory; isolation_level=None.
  - Errors: locked/busy -> StoreBusy; any other sqlite3.Error -> SchemaViolation("cannot open ops store"). Logs store.ops.connected (DEBUG).
- U02-38 run_write(fn, *, op):
  - Validates op against ^[a-z_]{1,64}$ (ConfigError).
  - A nested call raises ConfigError("nested run_write in <op>") before anything touches the outer transaction.
  - attempt(): `_shims.fault_point("sqlite.write", kind=op)`, then BEGIN IMMEDIATE / fn / COMMIT, with ROLLBACK when a transaction is open.
  - Error mapping per spec: IntegrityError -> "ops constraint failed in <op>: <msg>"; busy -> StoreBusy; other -> "ops write failed in <op>: <Class>"; HernessError and other exceptions pass unchanged.
  - Wrapped in `_shims.retry_call("sqlite_write", attempt)`. A write over 1 s logs WARNING store.ops.slow_write (op, duration_ms).
- U02-39/40 read_one/read_all:
  - busy -> StoreBusy (no retry); other errors -> SchemaViolation("ops read failed: <Class>").
  - fetchmany(max_rows+1) enforces the cap: SchemaViolation("read exceeded N rows"). max_rows outside 1..1,000,000 -> ConfigError.
- U02-41 dump_json:
  - Options: sort_keys, compact separators, ensure_ascii=False, allow_nan=False.
  - Default encoder: Decimal->str, aware datetime->format_utc, date->ISO, PurePath->as_posix, Enum->value, BaseModel->model_dump(mode="json").
  - Errors: "JSON for <field> not serialisable: <type>", "... contains NaN or Infinity", "... is circular", "... exceeds <max_bytes> bytes". max_bytes outside 1 KiB..16 MiB -> ConfigError.
- U02-42 load_json: None -> None; JSONDecodeError -> SchemaViolation("invalid JSON in <field>").
- U02-43 reset_connections(*, path):
  - Under the lock: closes every registered connection (ProgrammingError from other threads' connections is suppressed), clears the registry and sets the override.
  - Bumps a generation counter, so another thread's entry is never reused, even after a reset to the same path.
- U02-62 package __init__: block "02 core" (imports plus `__all__` in U02-62 order, `# noqa: RUF022`).

## Tests per ID (all pass)
- UT02-25:
  - PRAGMAs, Row factory and autocommit.
  - Old SQLite -> ConfigError. Non-WAL (:memory:) -> ConfigError.
  - Locked while opening -> StoreBusy. Directory path -> SchemaViolation.
- UT02-26: per-thread objects and reuse; an entry from a foreign pid is replaced.
- UT02-27:
  - A callback that inserts then raises commits nothing. Return value plus commit.
  - read_all cap and max_rows range.
  - Read error mapping (bad SQL, locked fake).
  - Write error mapping (UNIQUE, missing table, HernessError kept).
  - Invalid op; slow-write log.
- UT02-28:
  - A real second connection holds BEGIN IMMEDIATE for 0.5 s with _BUSY_TIMEOUT_MS=100: success after >= 2 attempts.
  - Nested call -> ConfigError, and the outer write still commits.
  - Shim tests: retry to success with bounded full-jitter sleeps; stop at 6 attempts; stop at 30 s elapsed; non-retryable errors and KeyboardInterrupt pass through; unknown policy; fault_point is a no-op.
- UT02-29:
  - The five encodings, plus date and unicode. Path written as POSIX.
  - set, naive datetime, NaN, Inf and a 70 KiB string -> SchemaViolation.
  - Explicit max_bytes and its range; circular structure.
- UT02-30: "{bad" names the field; None and Decimal strings.
- UT02-31:
  - After reset the old connection is closed (ProgrammingError on use) and the next one uses the new path (PRAGMA database_list).
  - A reset to the same path invalidates another thread's entry.
- UT02-68:
  - No duplicates; `__all__` equals the parsed blocks.
  - The first block is ("02","core") with the U02-62 names.
  - Every name resolves to herness.store.ops.<area>, with the area in the §2.3 table; `ops.run_write is core.run_write`.
  - No name equals an area; no review_item outside "shared".
  - Import opens no connection: core, then the package, are reloaded with sqlite3.connect patched to fail; the registry stays empty.
- FT02-02 (marker fault): count=4 -> success after 5 fault_point calls; count=7 -> StoreBusy after exactly 6 attempts; the first call is ("sqlite.write", {"kind": "insert_item"}).
- ST02-18 (integration; about 17 s, so not `slow`, which means over 30 s):
  - 3 writes during a 5 s read transaction each finish in under 1 s.
  - The run_write blocked by the 12 s BEGIN IMMEDIATE holder sees StoreBusy inside run_write and returns within 30 s. See spec note 1.
- BT02-06 ([integration, slow]; no `bench` marker is registered, so I followed the global-constraints convention): 1,000 run_write inserts into a test-local review_item-shaped STRICT table. **p95 = 0.038 ms** (threshold 10 ms), measured locally on Windows.

Coverage (tests/unit/store/ops + fault + bench): __init__, _shims and core are all at 100 % line and branch.

## Gates (all clean)
- ruff format and ruff check --fix: clean. mypy (strict, 90 files): 0 errors.
- lint-imports: 12 kept, 0 broken (ops-areas-acyclic KEPT, 1 ignored import).
- check_module_size: 0. check_type_ownership: 0.
- `uv run pytest -m "(unit or integration) and not slow" -q -p no:logging`: 2179 passed, 3 skipped, 1 xfailed (pre-existing), 10 deselected.
- `--require-test-ids` collect on the new files: OK.
- RED: before the package existed, collection failed with `ImportError: Error importing plugin "tests.support.ops_core_store": No module named 'herness.store.ops'`.

## ops-areas-acyclic contract (ruling 6)
The contract is a forbidden contract:
- source: `herness.store.ops.*`
- forbidden: `herness.store.ops` and `herness.store.ops.*`, with `as_packages = false`
- ignored imports: `herness.store.ops.* -> herness.store.ops.core` and `herness.store.ops.core -> herness.store.ops._shims`
- `unmatched_ignore_imports_alerting = "none"`, because the area -> core ignore matches nothing until T02-05.

I checked it by planting two temporary area modules, removed before the commit. One imported the package and another area; the contract broke and named both imports. The other imported only core; the contract stayed kept.

The existing contracts are unchanged. store-no-upward still covers herness.store.ops through its package source.

## Shims, fakes and carry-overs
1. herness/store/ops/_shims.py `retry_call(name, fn)` (`# T08-07:` marker):
   - Covers sqlite_write only: 6 attempts, base 0.2, cap 5, max_elapsed 30, with tenacity stop_after_attempt | stop_after_delay semantics.
   - Retries only RetryableError. Full jitter uses `process_state().rng`, and sleeps go through `process_state().sleep`; tests use the `reset_process_state` fixture for no-op sleeps.
   - An unknown policy raises ConfigError.
   - For T08-07: switch core to `herness.core.resilience.retry_call`, delete the shim and drop the module-budget override. Re-point the tests that monkeypatch `_shims.retry_call` (the UT02-28 busy test and ST02-18) and test_store_ops_shims.py.
2. `_shims.fault_point(name, **labels)` (`# T08-08:` marker) is a no-op. For T08-08: swap the call site, and re-point FT02-02 from the monkeypatched counting fake to the `fault_plan` fixture (its docstring says so).
3. Fixture `ops_store` is an interim plugin, tests/support/ops_core_store.py. It yields the db path, calls reset_connections(path=tmp/ops.sqlite) before the test and reset_connections() after, and is registered in the root conftest pytest_plugins. T11-40 replaces it with tests.support.ops_store (U11-78, migrated store) and removes this plugin entry.
4. The `fault_plan` fixture is replaced by the counting fake `_BusyPlan` in FT02-02.
5. UT02-68 parts left for later cards are marked in the test:
   - T02-05 allows exactly `migrate` in the area-name check and asserts both `ops.migrate is herness.store.ops.migrate.migrate` and `from herness.store.ops.migrate import pending_migrations`.
   - T02-06 adds block "02 shared".
   - The review_item-only-in-shared assertion is already generic.
6. BT02-06 uses a test-local review_item-shaped table. Re-point it to migration 001's review_item when T02-05 lands.

## Deviations and spec notes
1. ST02-18 expectation vs the policy:
   - With busy_timeout 10 s and sqlite_write (6 attempts, 30 s), the first attempt raises StoreBusy at about 10 s. The retry gets the lock when the 12 s holder commits, so run_write succeeds at about 12 s instead of raising StoreBusy.
   - The test asserts what holds under both readings: StoreBusy was raised inside run_write (seen by wrapping `_shims.retry_call`), the call returned or raised within 30 s, and the outcome is either written or busy.
   - If the spec means run_write itself must raise StoreBusy, the holder would have to outlast the whole policy (over 30 s, so a `slow` test). Controller to rule.
2. Constraint handling the spec leaves open: a bad `op`, `max_rows` out of range and `max_bytes` out of range all raise ConfigError, as programmer errors.
3. StoreBusy messages: "ops store busy in <op>" with context op (the spec wrote `StoreBusy(op)`). Reads use "ops store busy during read" and "ops read failed: <Class>".
4. A foreign exception from `fn` (for example RuntimeError) passes through unchanged after rollback. The shim does not classify it the way the real U08-28 will.
5. The UT02-28 busy_timeout override is the module attribute `core._BUSY_TIMEOUT_MS`, which is deliberately not Final. `core._SLOW_WRITE_S` can be overridden the same way.
6. The PRAGMAs after journal_mode run as one executescript (autocommit, outside any transaction).

## Fix round 1

Commit: 5c553d5 fix(store): harden ops store core error paths (T02-04 review). Code and docs are in one commit.

- F1 `_shims.py`: the comment now says design 08 lists `sqlite_write` for StoreBusy, and that the shim, like U08-28, retries every RetryableError. run_write raises no other RetryableError.
- F3 `core.run_write`: the nested check moved inside `attempt()`, after `connection()`. A StoreBusy while a thread first opens its connection is now retried. The nested ConfigError is not retryable, so it is raised on the single attempt.
  - New test `test_ut02_28_busy_while_opening_is_retried`: `_open` fails once with StoreBusy, then succeeds.
  - New test `test_ut02_28_nested_run_write_not_retried`: the fault_point calls are exactly ["outer", "inner"].
- F5 new helper `_rollback(conn, op)`: a failing ROLLBACK logs WARNING `store.ops.rollback_failed` (op, error class), and the original error is kept and mapped.
  - New test `test_ut02_27_failed_rollback_keeps_original_error`: a connection proxy whose ROLLBACK raises. IntegrityError is still mapped to SchemaViolation, and a RuntimeError is kept unchanged.
- F6 unmapped errors:
  - `mkdir` moved inside `_open`'s try, and `(sqlite3.Error, OSError)` both map to `SchemaViolation("cannot open ops store")`. I chose SchemaViolation because U02-37 reserves ConfigError for the SQLite version, FTS5 and WAL checks, and says every other open failure is SchemaViolation("cannot open ops store").
  - `dump_json`: RecursionError -> `SchemaViolation("JSON for <field> is nested too deeply")`.
  - `load_json`: RecursionError -> `SchemaViolation("invalid JSON in <field>")`.
  - No values appear in any of these messages.
  - New tests: `test_ut02_25_parent_not_creatable_is_schema_violation`, `test_ut02_29_deep_nesting_rejected`, `test_ut02_30_load_json_deep_nesting_rejected`.
- F8 ST02-18, per the controller ruling:
  - Asserts that StoreBusy is raised on attempt 1 (`busy[:1] == [1]`) and that run_write completes within 30 s.
  - The row "after-writer" exists exactly when run_write returned normally.
  - The docstring cites the ruling.
- Docs (docs/impl/02-data-model.impl.md):
  - §2 row for `herness/store/ops/_shims.py` (L1, budget 80, interim T08-07/T08-08, not an area).
  - core.py budget 260 -> 280 (ruling), mirrored in pyproject as an override `"herness/store/ops/core.py" = { limit = 280 }`.
  - ST02-18 row note.
  - T02-04 card Files row lists `_shims.py` (interim).

Line counts: core.py 270 / 280; _shims.py 66 / 80; __init__.py 33 / 160.

Gates:
- ruff format and check: clean. mypy: 0 errors (90 files).
- lint-imports: 12 kept. check_module_size: 0. check_type_ownership: 0.
- Coverage: __init__, _shims and core are all at 100 % line and branch.
- `uv run pytest -m "(unit or integration) and not slow"`: 2185 passed, 3 skipped, 1 xfailed (pre-existing), 10 deselected. This includes ST02-18.
- BT02-06: p95 = 0.032 ms.

Concerns: none new. The core.py override in pyproject is redundant with the doc row, because check_module_size already takes the lower doc budget. I kept it because the ruling asked for both.

## Fix round 2

Commit: 11b4e29 fix(store): drop the ops connection when ROLLBACK fails (T02-04 review)

- New helper `_drop_cached(*, close)`. It clears the thread-local entry, removes the connection from `_registry.connections` under the lock (ValueError suppressed), and closes it with `sqlite3.Error` suppressed when `close` is set.
  - `connection()` now uses it for stale entries. It passes `close=False` for a foreign pid, so behaviour there is unchanged.
  - `_rollback` calls `_drop_cached(close=True)` after logging `store.ops.rollback_failed`. The next call on that thread opens a clean connection instead of hitting the nested-call ConfigError.
- `test_ut02_27_failed_rollback_keeps_original_error` no longer rolls back by hand. It runs both cases (IntegrityError mapped to SchemaViolation, RuntimeError kept). For each case it asserts:
  - the original error is raised;
  - the old connection is closed (ProgrammingError on use) and no longer in the registry;
  - the next run_write on the same thread succeeds on a new connection, and both rows ("ok0", "ok1") are committed;
  - two `store.ops.rollback_failed` log events are emitted.
- core.py is 280 / 280, which is at the budget.
- Coverage: core.py 100 % line, 99 % branch. The one partial branch is `_drop_cached` with no cached entry, which cannot be reached from the current call sites.
- Gates:
  - ruff, mypy (0 errors), lint-imports (12 kept), check_module_size and check_type_ownership: clean.
  - `uv run pytest -m "(unit or integration) and not slow"`: 2185 passed, 3 skipped, 1 xfailed (pre-existing), 10 deselected.
