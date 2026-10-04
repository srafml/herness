# T02-09 report: Warehouse read side

Status: DONE_WITH_CONCERNS
Commit: dd05904 feat(store): add warehouse read side (T02-09)
Worktree/branch: D:\herness\.claude\worktrees\agent-a1af0ed3d76167e88 / worktree-agent-a1af0ed3d76167e88

## What was built
`herness/store/warehouse.py` (new, 388 lines): U02-24 `BUILD_ID_RE`, U02-25 `new_build_id`,
U02-26 `build_path`, U02-133 `build_exists`, U02-27 `read_current`, U02-28 `CurrentPointer`,
U02-29 `open_readonly`, U02-30 `BuildInfo`, U02-31 `list_builds`, U02-32 `delete_build_files`,
U02-33 `warehouse_health`; type aliases `BuildStatus`, `HealthResult`.
Reuses `herness.core.ids.new_build_id`, `herness.store.layout.data_layout`,
`herness.store.errors.NotFoundError`, core errors and logging. No pyproject change needed
(herness.store already in the L1 contract; mypy files = herness, tools).

## Tests
- tests/unit/store/test_store_warehouse.py (unit): UT02-17, UT02-18, UT02-19, UT02-20,
  UT02-21, UT02-22, UT02-24, UT02-77, ST02-03.
- tests/integration/store/test_store_warehouse_readonly.py (integration): ST02-04 (COPY,
  ATTACH, read_csv on ops.sqlite by absolute and relative path, SET/RESET
  enable_external_access, SET lock_configuration=false, INSTALL/LOAD httpfs, CREATE all
  fail; also via a cursor), UT02-21 locked build held by a writer subprocess (moved here
  because the unit marker forbids subprocesses; the unit file has a patched lock variant).
- UT02-20 asserts on pinned DuckDB 1.5.5: current_setting('enable_external_access')=false,
  'lock_configuration'=true, 'autoinstall_known_extensions'=false,
  'autoload_known_extensions'=false, TimeZone='UTC', threads, access_mode=read_only.
  Setting names confirmed (open-questions (b) item 4).

RED: `pytest tests/unit/store/test_store_warehouse.py` -> collection error (module
herness.store.warehouse missing). GREEN: 60 passed (card tests, unit + integration);
coverage of warehouse.py 98% line+branch (misses: hardening-statement failure path,
meta query error path in list_builds, a non-file wh-*.duckdb entry).
Full `pytest -m "(unit or integration) and not slow"`: 421 passed, 1 skipped.
Gates: ruff format --check clean, ruff check clean, mypy (strict) clean, lint-imports
8 kept 0 broken, check_type_ownership exit 0.

## Deviations
1. BUILD_ID_RE compiled with `re.ASCII` (spec text has no flag). Without it `\d` matches
   non-ASCII digits (e.g. Arabic-Indic), letting non-ASCII IDs through the TH02-03 gate.
   Stricter only; test UT02-17 `build_id_re_is_ascii_only`.
2. Lock detection (open_readonly -> StoreBusy, list_builds -> locked): spec says
   "IOException whose message contains 'lock'". On Windows DuckDB 1.5.5 reports
   "Cannot open file ...: The process cannot access the file because it is being used by
   another process. File is already open in ..." (no word "lock"). Matcher is regex
   `\block\b|being used by another process|already open` (case-insensitive); word
   boundary avoids "block" in corruption messages being treated as retryable.
3. read_current reads 65 bytes and rejects content longer than 64 bytes (spec: "read at
   most 64 bytes"), so a valid ID followed by padding/junk past byte 64 is rejected
   rather than silently truncated.
4. new_build_id: spec says ConfigError for non-UTC; core ids.new_build_id raises
   SchemaViolation for naive and converts offsets. Wrapper checks utcoffset()==0 ->
   ConfigError, then delegates to core ids.new_build_id.
5. warehouse_health: "none raised" applied to runtime conditions; precondition violations
   (stale_after_h <= 0, naive now) raise ConfigError (caller bug). Runtime failures
   including ImportError from an absent config loader (T10-03) map to `down`.
6. CurrentPointer: the first read is not a "change" (no log, changed_at stays None);
   needed for UT02-19 "logged once". recheck_s outside 1-3600 -> ConfigError. Default
   clock is herness.core.time.monotonic (global import rule) rather than time.monotonic.
7. delete_build_files refuses when CURRENT names the ID even if its file is missing
   (NotFoundError key == id); a malformed CURRENT is treated as "not current". StoreBusy
   from reading CURRENT propagates. `bytes` in the deleted log counts file + WAL only.
8. list_builds reads timestamps as epoch_us(...) so results do not depend on the reader
   time zone; unknown meta.build status values or row count != 1 -> unreadable.
9. open_readonly limit validation errors (threads, memory_limit) -> ConfigError (spec
   names no error). memory_limit pattern copied from BuildSettings (U02-75).
10. UT02-22 "file open by another handle (Windows)": on this machine os.unlink of a file
    held by a DuckDB writer succeeded (DuckDB opens with delete sharing), so the test uses
    the spec's alternative (unlink patched to raise PermissionError).
11. No `__all__` (to save lines; lake.py etc. also have none).

## Concerns
- Line budget: warehouse.py is 388 lines vs module-map budget 345 (under the 400 hard
  limit). Already compacted; further cuts would hurt readability. Needs a controller
  ruling to raise the budget (suggest 390).
- Commit trailer uses the harness attribution line (`Co-Authored-By: Claude Opus 5.5
  <noreply@anthropic.com>`) rather than the "(1M context)" variant in global-constraints.
- ST02-04 marker is `integration` per spec; UT02-21 locked case split into integration.

## Fix round 1

Commit: 6c586b9 fix(store): address T02-09 review round 1 (T02-09). Base HEAD was 7618573.
Only warehouse.py and tests/unit/store/test_store_warehouse.py were changed.

- I-1 (repeat open in one process): when the hardening statement fails, `_already_hardened`
  checks lock_configuration=true, enable_external_access=false and TimeZone='UTC'. If all
  three hold, the connection is returned. Otherwise it is closed and the error is raised
  as before. A connect that fails with ConnectionException "different configuration"
  (same file already open in this process with other threads/memory_limit, or by a
  writer) now maps to ConfigError "warehouse <id> is already open in this process with
  other settings". This is documented in the open_readonly docstring.
  - Probe finding: `SET TimeZone` is session-scoped on DuckDB 1.5.5. Cursors
    (`con.cursor()`, the per-thread pattern the spec prescribes) and later connections to
    the shared instance saw the local zone (America/Chicago), and a locked config then
    blocks setting it.
  - Changed to `SET GLOBAL TimeZone = 'UTC'`, which reaches cursors and later
    connections. This is a deviation from the spec's literal statement text; it keeps the
    same intent.
  - The three hardening statements now run as one multi-statement execute, still in spec
    order.
  - T02-10 should take note: open_for_build's `SET TimeZone` has the same cursor gap.
- Minor 1: warehouse_health now catches `Exception` (noqa BLE001 with reason), so any
  runtime failure returns `down`. Argument errors still raise ConfigError.
- Minor 2: health requires utcoffset()==0 (shared `_is_utc` helper with new_build_id).
- Minor 3: the health reason uses the status only if it is one of the known statuses;
  anything else is shown as "in an unknown status" (degraded).
- Trims, no behaviour change:
  - one-line module docstring
  - `_EXT_OFF` constant
  - `_existing_path` helper, which removes the duplicated missing-file check
  - health reads meta through `with open_readonly(...)`
  - `_inspect` uses `with duckdb.connect(...)`
  - `_meta_row` shortened
  - CURRENT decoding uses `errors="replace"`, since U+FFFD fails the gate
  - the redundant is_dir check is gone, because glob on a missing folder yields nothing
  - `_MONOTONIC` dropped
  - one-line docstrings
- warehouse.py is now **369 lines** (ruling: 370 or fewer).
- New tests:
  - UT02-20: two opens in one process; a different threads value gives ConfigError; a
    failing hardening statement on an unhardened instance gives SchemaViolation; a cursor
    sees TimeZone UTC.
  - UT02-24: ok while a reader is open; a non-UTC aware now gives ConfigError; unknown
    status text is not echoed; a ValueError from read_current gives down.
- Gates:
  - ruff check / format --check clean; mypy strict clean (the package, plus the 3 files
    explicitly).
  - lint-imports 8 kept; check_type_ownership exit 0.
  - Card tests: 66 passed, coverage 99% line+branch.
  - Full `(unit or integration) and not slow`: 434 passed, 1 skipped.
- Remaining note: list_builds opens with only the extension settings. If a reader in the
  same process opened a build with explicit threads/memory_limit, list_builds reports that
  build `unreadable` until the reader closes (a DuckDB per-process instance-cache limit).
