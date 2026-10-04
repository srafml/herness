# T02-04 Ops store core: review

Reviewed: worktree agent-a7c5cb1e1f7b6ee89, base 7b02bdb, head 515d3cb.

**Verdict: Approved** (no Critical or Important findings; the Minor items are optional follow-ups)

## Gates (I ran them)
- `ruff check .`: clean. `ruff format --check .`: 202 files already formatted. `mypy`: 0 issues in 90 files.
- `lint-imports`: 12 kept, 0 broken. `ops-areas-acyclic` KEPT, with 1 ignored import.
- `python -m tools.check_module_size`: exit 0.
- Line counts: core.py 259/260, `__init__.py` 33/160, `_shims.py` 65/80 (override).
- `pytest -k "UT02_25 ... BT02_06" --cov=herness/store/ops --cov-branch`: 52 passed in 21 s. BT02-06 and ST02-18 are included in that run.
  - Coverage: `__init__`, `_shims` and core are all at 100 % line and 100 % branch.

## Contract check: ops-areas-acyclic
I rebuilt the contract in a scratch package outside the repo. It uses the same source and forbidden modules, `as_packages=false`, and the same two ignores.
- Broken, as required:
  - area -> sibling area
  - area -> the package itself, including the transitive chain area -> package -> core
- Allowed: area -> core through `from pkg.ops import core` and through `from pkg.ops.core import x`.

The contract means what §2.2 says.

## Spec compliance
| ID | Status | Notes |
|---|---|---|
| U02-36 | ✅ | `Final[int] = 65_536` |
| U02-37 | ✅ | See the step-by-step check below. |
| U02-38 | ✅ | See the step-by-step check below. |
| U02-39 | ✅ | busy -> StoreBusy with no retry; other errors -> SchemaViolation |
| U02-40 | ✅ | `fetchmany(max_rows+1)`; the cap raises "read exceeded N rows"; the range 1..1e6 is checked (ConfigError) |
| U02-41 | ✅ | See the step-by-step check below. |
| U02-42 | ✅ | None -> None; JSONDecodeError -> "invalid JSON in <field>" |
| U02-43 | ✅ | See the step-by-step check below. |
| U02-62 | ✅ | Block "02 core": a header comment, the import lines and `__all__` in U02-62 order (`noqa: RUF022` with its reason). Importing the package opens no connection. |
| UT02-25 | ✅ | All 7 PRAGMAs, Row factory and autocommit. Also: old SQLite, non-WAL, locked while opening, directory path. |
| UT02-26 | ✅ | Per-thread objects, reuse, foreign pid |
| UT02-27 | ✅ | Rollback, return value, cap and range, read and write error mapping, op regex, slow-write log |
| UT02-28 | ✅ | Real 0.5 s BEGIN IMMEDIATE holder with busy_timeout 100 ms. Nested call -> ConfigError while the outer write commits. The shim semantics tests are also tagged UT02-28. |
| UT02-29 | ✅ | The 5 encodings plus date and unicode. set, naive datetime, NaN, Inf, 70 KiB and a circular structure are rejected; max_bytes is checked. |
| UT02-30 | ✅ | |
| UT02-31 | ✅ | The old connection is closed (ProgrammingError on use); the new path shows in `PRAGMA database_list`; another thread's entry is invalidated after a reset to the same path. |
| UT02-68 | ✅ | Block "02 core" only, per the ruling. The `migrate` and `shared` TODOs are marked for T02-05 and T02-06. |
| FT02-02 | ✅ | Monkeypatched fault shim, per the ruling. count=4 -> 5 calls and success; count=7 -> StoreBusy after 6 attempts. The first call is `("sqlite.write", {"kind": "insert_item"})`. |
| ST02-18 | ✅ | Per the ruling: StoreBusy is seen inside run_write, and the call ends within 30 s. The WAL reader phase asserts under 1 s per write. |
| BT02-06 | ✅ | Test-local review_item table, per the ruling. Markers `[integration, slow]`, because `bench` is not registered; this follows the global-constraints convention. |

Step-by-step checks:
- **U02-37 `connection`**
  - Effective path: the override, else `data_layout().ops_db`.
  - Reuse is keyed on (pid, path, generation). A stale entry from the same pid is closed; one from a foreign pid is dropped without closing.
  - Version and FTS5 check is `functools.cache`d. A failure is not cached, because a raise is never cached.
  - The parent directory is created. `connect(timeout=10, isolation_level=None, check_same_thread=True)`.
  - `busy_timeout` is set before `journal_mode=WAL`. A result other than WAL raises ConfigError. Then the other 5 PRAGMAs and `row_factory`.
  - Error mapping: locked or busy -> StoreBusy; other -> SchemaViolation("cannot open ops store").
  - Register, then log a DEBUG `store.ops.connected` event with the thread name.
- **U02-38 `run_write`**
  - `fault_point("sqlite.write", kind=op)` is the first statement of `attempt()`, then `connection()`, BEGIN IMMEDIATE, fn, COMMIT.
  - On any exception it runs ROLLBACK if a transaction is open.
  - IntegrityError -> "ops constraint failed in <op>: <msg>"; busy -> StoreBusy; other sqlite errors -> "ops write failed in <op>: <Class>"; a HernessError passes unchanged.
  - The op regex and the nested-call ConfigError are checked before any transaction starts.
  - Calls `retry_call("sqlite_write", attempt)`. A write over 1 s logs a WARNING with op and duration_ms.
- **U02-41 `dump_json`**
  - All the `json.dumps` options.
  - Encoder: Decimal, aware datetime -> `format_utc`, date, PurePath -> POSIX, Enum, BaseModel.
  - Messages carry type names only. max_bytes is checked in bytes (UTF-8).
- **U02-43 `reset_connections`**
  - Everything happens under the lock: close each connection (suppressing ProgrammingError), clear the registry, set the override.
  - It also bumps a generation, so other threads' entries are invalidated even when the path is unchanged.

Controller rulings, checked as implemented:
- `_shims.py` carries the `# T08-07:` and `# T08-08:` markers.
- Retry shim:
  - 6 attempts, base 0.2, cap 5, 30 s, full jitter through `process_state()`.
  - It retries only `RetryableError`. Other exceptions and BaseException pass through, and a test covers KeyboardInterrupt.
  - An unknown policy raises ConfigError.
- The `ops_store` interim plugin is registered in the root conftest with a T11-40 note.
- The budget override is in pyproject; the spec §2 row is pending.

⚠️ Cannot verify here:
- BT02-06 p95 on CI. Locally it is 0.038 ms on Windows.
- The real U08-28 and U08-34 semantics, since impl 08 is absent. The shim may differ from them, for example on stop timing and on which exception classes are retried.
- Whether the spec §2 module-map row for `_shims.py` is added.

## Findings

### Critical
None.

### Important
None.

### Minor
1. **Retry comment does not match the code.** `herness/store/ops/_shims.py:28` says "(StoreBusy only)", but `:53` retries every `RetryableError`, which includes RateLimited and ModelUnavailable. `fn` is SQL-only, so this is harmless. Fix either the comment or the `except` clause.
2. **The retry can run slightly past 30 s.** `herness/store/ops/_shims.py:55` checks elapsed time only after a failure, which is tenacity `stop_after_delay` semantics. With busy_timeout 10 s, the third attempt ends at about 30.6 s, so the worst case is about 31 s against ST02-18's "never past 30 s". T08-07 decides the real semantics; note it for that card.
3. **The first open on a thread is not retried.** The nested check at `herness/store/ops/core.py:134` calls `connection()` outside `retry_call`. A StoreBusy raised while a thread opens its connection (the WAL switch or PRAGMAs under lock) is therefore not retried, although U02-38 puts `connection()` inside `attempt()`.
   - It waits only one busy_timeout (10 s) instead of the policy.
   - Possible fix: do the `in_transaction` check inside `attempt()`. ConfigError is not retryable, so the behaviour would otherwise be the same.
4. **Slow writes that fail are not logged.** `herness/store/ops/core.py:157` measures only successful attempts, so a write that is slow and then fails logs nothing. The spec only says "measure the transaction time", so this is acceptable.
5. **A failing ROLLBACK hides the original error.** At `herness/store/ops/core.py:147-148`, if ROLLBACK itself raises (for example, the connection is closed), that error replaces the original. Consider `contextlib.suppress(sqlite3.Error)` around the ROLLBACK.
6. **Some errors escape unmapped.**
   - `mkdir` at `herness/store/ops/core.py:84` can raise OSError.
   - RecursionError from deeply nested JSON can escape `dump_json` (`:226`) and `load_json` (`:245`).
   - None of these error classes is in the spec's error list.
7. **Reads put an empty op in the error context.** `_map_error` at `herness/store/ops/core.py:64-67` passes `op=""` for reads, so read errors carry the context `op=""`. Cosmetic: omit `op` when it is empty.
8. **The ST02-18 outcome assertion checks nothing.** `tests/security/test_st02_ops_core.py:91` (`outcome in {"busy","written"}`) is always true. The ruling accepts it, but the test could also assert that the "after-writer" row is present exactly when the outcome is "written".
9. **The UT02-68 reload replaces module state.** `tests/unit/store/ops/test_store_ops_package.py:126-127` reloads core, which replaces `_registry` and `_local`. Old function objects keep working, because they share the module dict. Any connection cached in another live thread's old thread-local is orphaned, which does no harm today. A subprocess import check would be isolation-proof.
10. **ST02-18 is slow for the default run.** It takes about 17 s and runs in the default `not slow` pre-commit selection. The `slow` marker means over 30 s, so the marking is correct; it is just a noticeable cost.

## Re-review 1 (fix round 1, commit 5c553d5)

**Verdict: Approved**

### Gates (I ran them)
- `ruff check`: clean. `ruff format --check`: 202 files already formatted. `mypy`: 0 issues in 90 files.
- `lint-imports`: 12 kept, 0 broken. `ops-areas-acyclic` KEPT.
- `check_module_size`: exit 0. core.py is 270/280, `_shims.py` 66/80, `__init__.py` 33/160.
- Targeted `-k` run with `PYTHONUTF8=1`: 58 passed in 21 s, including ST02-18 and BT02-06.
- Coverage of `herness/store/ops`: 100 % line and 100 % branch on all three modules.

### Fixes verified
| Item | Status | Notes |
|---|---|---|
| Minor 1 (shim comment) | ✅ | The comment now matches the code: every RetryableError is retried, as U08-28 does. |
| Minor 3 (open not retried) | ✅ | The nested check is now inside `attempt()`, after `connection()` (`core.py:147`), so a StoreBusy while opening is retried. ConfigError is not retryable, so the nested call gets exactly one attempt. New tests: `test_ut02_28_busy_while_opening_is_retried` and `test_ut02_28_nested_run_write_not_retried`. The existing nested test still shows the outer write committing. |
| Minor 5 (ROLLBACK masking) | ✅ | See the notes below. |
| Minor 6 (unmapped errors) | ✅ | See the notes below. |
| Minor 8 (ST02-18 assertion) | ✅ | Asserts StoreBusy on attempt 1 (`busy[:1] == [1]`) and completion within 30 s. The "after-writer" row exists exactly when `run_write` returned normally. The always-true assertion is removed. |
| Ruling: spec §2 row for `_shims.py` | ✅ | Budget 80, L1, marked interim and "not an area". The T02-04 card's Files row lists it. |
| Ruling: ST02-18 spec note | ✅ | The row note matches the controller ruling. |
| Ruling: core.py budget | ✅ | Spec §2 now says 280 and the pyproject override says 280; the file is 270 lines. |

Notes on the table:
- **Minor 5:** `_rollback` (`core.py:127-133`) logs `store.ops.rollback_failed` with op and the error class name only, and the original error is still mapped or passed through.
  - The new test `test_ut02_27_failed_rollback_keeps_original_error` covers both paths: an IntegrityError still becomes SchemaViolation, and a RuntimeError is kept unchanged.
- **Minor 6:**
  - `mkdir` is now inside the try block. OSError maps to `SchemaViolation("cannot open ops store")`, because `_map_error` sends it down the non-busy branch. This agrees with U02-37.
  - RecursionError maps to SchemaViolation in `dump_json` ("nested too deeply") and in `load_json` ("invalid JSON in <field>"). The `except RecursionError` in `dump_json` sits before `except ValueError`; the two classes are unrelated, so the order is harmless.
  - No message contains a value. New tests cover the parent-directory OSError and deep nesting in both directions.

No regressions found.

### Minor (new, optional)
1. **A failed ROLLBACK leaves the thread's connection unusable, and later writes get a misleading error.**
   - Where: `herness/store/ops/core.py:127-133` together with `:147`.
   - When ROLLBACK fails, `_rollback` swallows the error. The connection stays in a transaction, so every later `run_write` on that thread, including the retry of a StoreBusy attempt, raises `ConfigError("nested run_write in <op>")`. That message points at the wrong cause.
   - This existed before too: previously the ROLLBACK error was raised and the connection stayed poisoned the same way. What is new is the misleading nested-call message.
   - The test works around it by running a manual ROLLBACK (`tests/unit/store/ops/test_store_ops_core.py:402,405`).
   - Suggested fix: when ROLLBACK fails, drop and close the thread's cached connection (clear the thread-local entry, remove it from the registry), so the next call reopens a clean one.

Parked, not re-checked: Minor 2, 4, 7, 9 and 10.

## Re-review 2 (fix round 2, commit 11b4e29)

**Verdict: Approved**

### Gates (I ran them)
- `ruff check`: clean. `ruff format --check`: 202 files already formatted. `mypy`: 0 issues in 90 files.
- `lint-imports`: 12 kept. `ops-areas-acyclic` KEPT.
- `check_module_size`: exit 0. core.py is exactly 280/280, so it is at the budget.
- Targeted `-k` run with `PYTHONUTF8=1`: 58 passed in 21 s.
- Coverage of `herness/store/ops`: 100 % line and 99 % branch. The only partial branch is `core.py:109->exit`: `_drop_cached` called with no cached entry. Neither call site can reach it. Coverage stays above the 90/85 floor.

### Fix verified
The finding from re-review 1 was that a failed ROLLBACK leaves the thread's connection stuck in a transaction. It is fixed.
- **Helper:** `_drop_cached` (`core.py:105-115`) does three things:
  - Clears the thread-local entry.
  - Removes the connection from `_registry.connections` under the registry lock. A missing entry (ValueError) is ignored, which is the case when a reset already cleared the registry.
  - Closes the connection only when `close` is true, ignoring `sqlite3.Error` from the close.
- **Locking:** the helper holds the lock only for the list removal and never while `reset_connections` holds it. The lock is not taken twice on any path, so nothing can deadlock.
- **Fork safety:** `connection()` (`core.py:125`) passes `close=False` for an entry from a foreign pid. A handle inherited across fork is still never closed.
- **Rollback path:** `_rollback` (`core.py:143`) drops and closes this thread's connection after logging. By the time `_rollback` runs, `attempt()` has already replaced any foreign-pid or stale entry with the connection it is rolling back, so it closes the right handle.
  - If the failure was a retryable StoreBusy, the retry opens a fresh connection.
  - Otherwise, the next `run_write` on that thread opens a fresh one.
- **Unchanged behaviour:** `reset_connections`, the (pid, path, generation) invalidation, and the order of the stale-entry steps in `connection()` (clear the thread-local entry, unregister, then close) are the same as before. The logic was only moved into the helper.
- **Test:** `test_ut02_27_failed_rollback_keeps_original_error` now checks each case end to end:
  - the original error is raised;
  - the old connection is closed (ProgrammingError on use) and no longer in the registry;
  - the next `run_write` succeeds on a new connection, and both new rows are committed;
  - two `rollback_failed` log events are emitted.
  - The manual ROLLBACK workaround is gone.

### Findings
None.

Note: core.py is at exactly 280/280 lines, so it has no headroom left.
