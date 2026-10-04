# T02-09 review (commit dd05904, Warehouse read side)

**Task quality: Needs fixes** (one Important item, small fix)

### Spec Compliance
- ✅ Spec compliant for U02-24…U02-33 and U02-133. BUILD_ID_RE, the build_path gate, build_exists (gates before any path is built), read_current steps 1-5, CurrentPointer (lock, propagates errors and leaves the cache as it was, logs a change once), the open_readonly config dict plus the 3 SETs in spec order, list_builds (foreign_file warning, locked/unreadable, newest first, the unreadable/locked => started_at None invariant holds), delete_build_files (order .wal -> .duckdb -> tmp/<id>, PermissionError -> deferred, other OSError -> SchemaViolation, CURRENT -> ConfigError) and warehouse_health classification all match.
- Every brief test ID has at least one function carrying it: UT02-17 (6), UT02-18 (3), UT02-19 (3), UT02-20 (6), UT02-21 (4 unit + 1 integration), UT02-22 (7), UT02-24 (4), UT02-77 (2), ST02-03 (1, parametrised over 7 payloads, and it asserts duckdb.connect is never called), ST02-04 (2, integration). pytestmark is set in both files.
- UT02-20 (tests/unit/store/test_store_warehouse.py:289) asserts on the pinned DuckDB **1.5.5** (checked): `current_setting` for enable_external_access=False, lock_configuration=True, autoinstall_known_extensions=False, autoload_known_extensions=False, TimeZone='UTC', threads=2, access_mode=read_only. So the setting names in open-questions (b) item 4 are confirmed.
- ⚠️ Cannot verify from the diff: the POSIX lock message form ("Could not set lock on file", which the unit test only simulates). The Windows form is exercised for real by the subprocess integration test.

### Deviation rulings (vs spec, TH02-03/TH02-04)
1. Windows lock regex `\block\b|being used by another process|already open` -> StoreBusy: **accept**. The spec's literal "contains 'lock'" does not match Windows DuckDB 1.5.5 messages, and test_ut02_21_list_builds_locked_by_writer shows the Windows form really occurs. The regex applies only to `duckdb.IOException`, so a ConnectionException or InvalidInput "already ..." message cannot reach it. The word boundary is a sensible guard.
2. BUILD_ID_RE with re.ASCII: **accept**. It only tightens the TH02-03 gate, and the spec's documented intent is ASCII digits.
3. CURRENT longer than 64 bytes rejected (reads 65): **accept**. A valid pointer is 23 bytes, and this avoids silently truncating a crafted pointer (ST02-03 padding case).
4. Non-UTC `now` in new_build_id -> ConfigError: this is **spec-mandated** (U02-25 preconditions), not a deviation. Any aware offset other than 0 is rejected, which is fine.
5. warehouse_health: bad arguments -> ConfigError, runtime failures -> `down`: **accept with a note**. The spec says "None raised". Without some guard a naive `now` would raise TypeError out of the subtraction, so ConfigError for caller bugs is reasonable. Catching ImportError is justified by `data_layout()` importing the config module lazily (herness/store/layout.py:58). See Minor 1 for types that still escape.
6. UT02-21 split into an integration test (real subprocess writer) plus a unit test (patched IOException), and UT02-22 using the patched-unlink variant: **accept**. The spec row itself allows the patched alternative. The unit marker forbids subprocesses, so the split keeps real lock coverage.

### Strengths
- Tight TH02-03 gating: `_valid_id` runs before any path join everywhere, including build_exists and list_builds. ST02-03 proves no connect happens.
- ST02-04 covers more than the brief (RESET, SET lock_configuration=false, LOAD, CREATE, relative read_csv, a cursor that inherits the hardening), and it confirms the COPY target was not created.
- Coverage 98% line+branch (re-run: 60 passed). ruff check, ruff format --check and mypy (strict) are clean on all 3 files. lint-imports: 8 kept, 0 broken. check_type_ownership exits 0. warehouse.py is 388 lines.

### Issues
#### Critical (Must Fix)
- None.

#### Important (Should Fix)
1. **A second `open_readonly` on the same build in the same process always fails as SchemaViolation, so `warehouse_health` reports `down` while any reader in that process is open.** herness/store/warehouse.py:216-224 and :357.
   - Cause: DuckDB's Python instance cache hands the second `connect` the same database instance, which already has `lock_configuration = true`. `SET TimeZone` then raises InvalidInputException "Cannot change configuration option "TimeZone" - the configuration has been locked", which is mapped to SchemaViolation "cannot open warehouse". With different `threads`/`memory_limit`, the error is ConnectionException "same database file with a different configuration", also mapped to SchemaViolation.
   - Reproduced with a probe on 1.5.5. With one reader open: second open_readonly -> SchemaViolation; open_readonly(threads=2) -> SchemaViolation; warehouse_health -> ('down', 'current build cannot be opened (SchemaViolation)'). After closing the reader, health -> 'ok'.
   - The spec's concurrency note "one connection per process per build" acknowledges the constraint. However, warehouse_health is declared thread-safe and is meant for status/doctor/dashboard use in processes that may hold a long-lived reader (spec 05 tools, the app), and a non-retryable "cannot open" misreports a healthy warehouse as down.
   - Fix (about 4-6 lines): when a hardening SET fails, check whether the instance is already hardened (lock_configuration true, enable_external_access false, TimeZone 'UTC'). If so, return the connection; if not, close it and raise.
   - Alternatively, read meta in warehouse_health through the plain read-only connect that `_inspect` uses (it shares the cached instance without SETs), and map the "different configuration" ConnectionException to StoreBusy or document it.
   - Add a test: two open_readonly calls in one process, and health while a reader is open.

#### Minor (Nice to Have)
1. herness/store/warehouse.py:377: warehouse_health still raises for exception types not in the tuple, for example a pydantic ValidationError or ValueError from the config loader behind `data_layout()`. This contradicts "Errors: None raised". Consider a broad `except Exception` (noqa with a reason) or document it.
2. herness/store/warehouse.py:372: the check accepts any aware `now` with an offset, while the message and spec say UTC. This is harmless (the arithmetic is correct), but it is inconsistent with new_build_id at line 69. Use the same `utcoffset() != timedelta(0)` test, or reword the message.
3. herness/store/warehouse.py:385 and :387: the reason string interpolates the raw `meta.build.status` from the file. list_builds filters it through _META_STATUSES, but health does not. Low risk, but it is unbounded text in a health reason; restrict it to known statuses.
4. The commit trailer uses "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>" where global-constraints.md asks for the "(1M context)" variant. The builder flagged this; the controller should decide.
5. Line budget: 388 vs 345. See the trim analysis below.

### Trim analysis (budget 345; now 388; hard limit 400)
Concrete, behaviour-preserving cuts:
- Module docstring, lines 1-5 -> 2 lines: **-3**.
- BUILD_ID_RE: the comment at line 31 and the docstring at line 33 overlap, so keep one: **-1**.
- `_extensions_off()` (lines 55-56 plus blanks) -> a `_EXT_OFF: Final` dict and `dict(_EXT_OFF)` at the 2 call sites: **-3**.
- `_MONOTONIC` (line 49) -> use `clock.monotonic` directly as the default: **-1**.
- Duplicate "file missing -> NotFoundError(kind='build')" at lines 122-124 and 211-214 -> one `_existing_path(build_id, lay)` helper: **-3**.
- Multi-line docstrings on read_current (104-108), open_readonly (200-203), delete_build_files (328-332) and warehouse_health (368-371) -> one-liners (the Raises lists live in the spec): **-9**.
- `_current_meta` (lines 352-362) -> inline into warehouse_health: **-3 to -4**.
- `_is_current` and the list_builds CURRENT handling overlap slightly: **0 to -2**.

Total: about **-23 to -26 lines -> about 362-365**. The Important fix adds back about 4-6, so a realistic final size is **about 366-370**. Reaching about 345 would mean dropping spec-mandated validation (threads/memory_limit/recheck_s ranges, the new_build_id UTC check) or merging helpers at a real readability cost, which is not recommended. Recommendation: apply the docstring and dedup trims with the fix, and set the budget to **370** (390 is not needed).

### Assessment
**Task quality:** Needs fixes
**Reasoning:** The implementation is spec-faithful, well gated and well tested, and the builder's deviations are all justified. One real in-process fragility (repeat hardened opens fail, and health reports `down` while a reader is open) should be fixed and tested. The line trims can go into the same fix round.


### Re-review round 1 (commit 6c586b9; scope I-1, Minor 1-3; trailer finding parked)

**Task quality: Approved**

Checks re-run:
- Card tests: 66 passed; coverage 99% line+branch. Misses: lines 200-201, the `_already_hardened` error path, and 294->290.
- ruff check and ruff format --check are clean on the 3 files.
- mypy (strict) is clean on `herness` plus the 2 test files.
- lint-imports: 8 kept, 0 broken.
- warehouse.py is 369 lines, within the 370 ruling. The worktree was not modified.

1. **I-1: resolved.** Probes on 1.5.5 in the same process:
   - Two plain `open_readonly` calls succeed, and a cursor on the second one sees UTC.
   - Closing the first connection leaves the second one locked and usable.
   - `warehouse_health` returns `ok` while a reader is open.

   **Can "already hardened" be fooled?** Not by anything an agent can reach.
   - The fallback returns the connection only if the hardening SET failed and the instance reports lock_configuration=true, enable_external_access=false and TimeZone='UTC'.
   - A partly hardened instance (lock still false) fails the check, so the connection is closed and the error is raised.
   - When another in-process connection holds a plain, unhardened instance, `open_readonly`'s SETs succeed and harden the shared instance. The plain connection is then locked too: its `SET enable_external_access=true` gives InvalidInputException.
   - Only a connection that is still unlocked can change settings, and agents only ever get locked ones.

   **Residual gap (Minor, below):** the check does not look at the extension settings. I opened a plain connection with the same config, ran `SET GLOBAL autoload_known_extensions=true`, and then applied the three hardening settings myself. `open_readonly` then returned a connection with autoload=true. This needs deliberate in-process code, so it is not an agent path, and it is Minor.

   **Different threads/memory_limit → ConfigError:** acceptable. It is deterministic, the docstring documents it, and it cannot be retried away.

2. **`SET GLOBAL TimeZone`: claim verified, change accepted.**
   - With a plain `SET TimeZone='UTC'` on 1.5.5, the connection sees UTC, but `con.cursor()` and a second connection to the same instance see America/Chicago.
   - The spec prescribes per-thread cursors (U02-29 concurrency) and requires the connection to be in UTC, so GLOBAL is the only way to meet that intent. It is a spelling deviation in service of the spec, and UT02-20 now asserts that the cursor sees UTC.
   - The three statements still run in spec order in one execute.
   - Recommend recording it as a spec note, and carrying it to T02-10 (open_for_build: `SET TimeZone` has the same gap for cursors).

3. **list_builds reports `unreadable` while a same-process reader holds the build with explicit threads/memory_limit: park it.**
   - The same limit makes `warehouse_health` return `down (ConfigError)` in that situation (probe confirmed).
   - No spec caller passes threads or memory_limit today. The impl 07 RelatednessCache and the outcome job call `open_readonly(build_id)`, the impl 09 app pool calls `open_readonly(build_id)`, and doctor runs `warehouse_health` in its own CLI process.
   - Park it with a note for impl 05/09. If a caller ever passes limits, it should use the same limits everywhere in the process.

4. **Minor 1-3: resolved.**
   - `except Exception` (with a BLE001 noqa and reason) maps every runtime failure to `down`; the new test covers a ValueError.
   - `_is_utc` is shared with new_build_id, and a non-UTC aware `now` raises ConfigError (tested).
   - An unknown status is shown as "in an unknown status" and the raw text is not echoed (tested).

5. **No regressions.** The trims keep behaviour the same:
   - `_parse_current` uses `errors="replace"`. U+FFFD cannot pass the ASCII gate, and ST02-03 still passes including the non-ASCII case.
   - The `is_dir` removal is fine: `glob` on a missing folder yields nothing (test_ut02_21_list_builds_no_folder passes).
   - `_inspect` keeps `locked` and `unreadable` and the started_at-None invariant.

#### New findings (all Minor, none block)
- **M-R1** (herness/store/warehouse.py:196-201): `_already_hardened` could also require `autoinstall_known_extensions`/`autoload_known_extensions` = false (add two `current_setting` columns and extend the tuple). That closes the in-process spoof above. It is optional because it is not agent-reachable, and it costs about 1 line against the 370 cap.
- **M-R2** (herness/store/warehouse.py:174-176): a same-process *writer* (a read_only=False instance on that file) also raises "different configuration", which is now reported as ConfigError "already open in this process with other settings". StoreBusy would describe that case more accurately. Readers normally open CURRENT, which is never being written, so this is low impact.
- **Cross-spec note for T09 (not this card):** impl 09 U09 app pool (09-outputs-and-cli.impl.md:1742, step 3) runs `SET enable_external_access = false` and `SET lock_configuration = true` after `open_readonly`. On 1.5.5 both fail on the locked configuration ("Cannot change configuration option ... locked"; probe confirmed), so that step must be dropped or made a check.

**Reasoning:** I-1 and Minor 1-3 are fixed and tested. The GLOBAL time-zone change is verified necessary and meets the spec's intent. The remaining items are Minor or park-able and do not affect correctness for any spec'd caller.
