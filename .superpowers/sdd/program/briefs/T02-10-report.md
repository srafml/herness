# T02-10 report: Warehouse write side and import contract

## What was built

- `herness/store/_warehouse_rw.py` (120 lines, budget 120): the only writable warehouse
  connection.
  - `open_for_build(build_id, *, create, cfg, layout=None) -> duckdb.DuckDBPyConnection`
    (U02-34): validates `build_id` and the `create` existence rule (both directions raise
    `ConfigError`), creates `layout.warehouse` and `tmp/<build_id>/`, opens the file with
    extension autoinstall/autoload off, external access left on (default), sets
    `SET GLOBAL TimeZone = 'UTC'` (deviation, see below), logs
    `store.warehouse.opened_for_build` (INFO), and maps duckdb errors via the reused
    `warehouse._open_error` (lock -> `StoreBusy`, "different configuration" ->
    `ConfigError`, else `SchemaViolation`).
  - `write_current(build_id, *, layout=None) -> str | None` (U02-35): validates via the
    reused `warehouse._existing_path` (raises `NotFoundError` if the build file is
    missing), reads the previous pointer via `warehouse.read_current` (any error ->
    `None`, per the unit's step 2), writes `.CURRENT.tmp-<ulid>` (ASCII `<id>\n`), fsyncs
    it (`herness.store.lake._fsync`, reused), `os.replace`s it onto `CURRENT`, fsyncs the
    warehouse directory on POSIX, logs `store.warehouse.current_switched` (INFO), and maps
    `PermissionError` -> `StoreBusy`, other `OSError` -> `SchemaViolation` (temp file
    removed in both cases).
  - `cfg` is typed as a local structural `Protocol` (`threads: int | None`,
    `memory_limit: str`) mirroring `herness.model.settings.BuildSettings` (U02-75)
    rather than importing that class, because `herness.store` importing
    `herness.model` would break the `store-no-upward` contract. This matches the
    `herness/store/layout.py` precedent (`_ConfigWithPaths`) and the module map's
    "Extra imports: duckdb" for this file (no pydantic/model import). Recorded as a
    deviation from the unit table's literal `BuildSettings` type; the dispatch note about
    using a Protocol for `HernessConfig` pointed at the same technique.
  - Reused (not duplicated) from `herness/store/warehouse.py`: `build_path`,
    `_existing_path`, `read_current`, `_open_error` (cross-module private-name reuse,
    same pattern `herness/store/lake_purge.py` already uses for `lake._fsync`).
    `warehouse.py` was not touched (already at its 369-line ceiling per the dispatch).

- `pyproject.toml`: added import-linter contract `store-rw-restricted` (forbidden:
  `herness` -> `herness.store._warehouse_rw`, with `ignore_imports` for
  `herness.model.build -> ...` and `herness.model.promote -> ...`,
  `unmatched_ignore_imports_alerting = "none"` since neither module exists on this
  branch yet -- same pattern already used for the `herness.core.config` settings
  exception). `ops-areas-acyclic` is **deferred**: `herness/store/ops/` and its area
  modules (owned by T02-04..T02-07) have not landed on this branch, so the contract
  cannot name a concrete module without inventing a file that doesn't exist (and
  `tests/unit/repo/test_import_contracts.py::test_ut00_58_...` asserts every contract's
  named modules exist unless the name contains `*`). Left a comment recording the
  deferral and pointing at the card that should add it (the one creating
  `herness/store/ops/__init__.py`).

- `tests/unit/store/test_store_warehouse_rw.py` (new, 20 tests): UT02-20 build-side
  settings/temp-dir/UTC/error-mapping cases, UT02-23 write_current cases (first switch,
  previous returned, missing build, invalid id, tampered-CURRENT read treated as `None`,
  `PermissionError` -> `StoreBusy`, other `OSError` -> `SchemaViolation`, both leaving no
  temp file; the POSIX directory-fsync branch forced via `monkeypatch.setattr(os, "name",
  "posix")` plus a stubbed `_fsync` so it's exercised on this Windows CI), and ST02-05 (an
  AST scan of `herness/` and `app/` -- `app/` doesn't exist yet -- asserting no importer
  of `_warehouse_rw` outside `herness.model.build`/`herness.model.promote`; currently the
  importer set is empty, which is correct and will start enforcing once T02-18 lands).

## Deviations / concerns

1. **`SET GLOBAL TimeZone`, not `SET TimeZone`** (dispatch-directed deviation, same as
   T02-09): a session-level `SET` does not reach cursors or later connections to the
   shared per-file DuckDB instance. Verified in `test_ut02_20_open_for_build_settings`
   via `con.cursor().execute(...)`.
2. **`BuildSettings` default `memory_limit` ("75%") cannot open a build on the pinned
   DuckDB 1.5.5.** Confirmed empirically: `duckdb.connect(config={"memory_limit": "75%"})`
   and `SET memory_limit='75%'` both raise `Parser Error: Unknown unit for memory: '%'`
   on this DuckDB build -- percentage `memory_limit` is not supported at all, neither via
   the config dict nor via `SET`. `open_for_build` passes `cfg.memory_limit` through
   verbatim per the U02-34 algorithm (no percent-to-bytes conversion -- that's outside
   this card's unit spec and would need e.g. `psutil` RAM sizing, a judgment call for a
   later card). Net effect: **calling `open_for_build` with a default, unmodified
   `BuildSettings()` fails today** with a somewhat misleading `SchemaViolation("cannot
   open warehouse ...")` instead of a clear message about the bad `memory_limit`. Added
   `test_ut02_20_open_for_build_percent_memory_limit_rejected` to document this rather
   than hide it; all other tests pass an explicit `memory_limit="1GB"` (matching the
   convention `test_store_warehouse.py` already uses for `open_readonly`). This is a
   genuine gap between impl 02's own `BuildSettings` default (T02-01) and impl 02's own
   warehouse write side (this card) against the pinned DuckDB version -- worth a
   controller decision (fix the default, or convert percent to bytes in `open_for_build`
   or in a config-loading layer).
3. `ops-areas-acyclic` import-linter contract deferred (see above) -- not a test
   failure, just not yet expressible against the current tree.
4. If `SET GLOBAL TimeZone = 'UTC'` were ever to fail on an otherwise-successful `create`
   connect, the build file would already exist on disk (DuckDB creates the file on
   connect), so a retry of `open_for_build(..., create=True, ...)` would then hit the
   "already exists" `ConfigError` instead of retrying cleanly. This can only happen via
   the monkeypatched-`_SET_TZ` test path in practice (the real statement should never
   fail); not fixed, flagged as a minor concern.

## Gates

All run on this worktree's own `.venv` directly (`.venv\Scripts\*.exe`), because
`D:\herness\pyproject.toml` on the base repo has an unresolved merge conflict that makes
`uv run` fail here (dispatch's documented fallback):

- `pytest tests/unit/store/test_store_warehouse_rw.py -q` -> 20 passed.
- `pytest tests/unit/store/ -q` -> 203 passed (no regression in `warehouse.py`'s own
  suite).
- Coverage of the new module: 100% line, 100% branch (68 stmts, 6 branches, 0 missed).
- `ruff format --check .` -> clean; `ruff check --fix .` -> "All checks passed!".
- `mypy` (whole `herness`/`tools`) -> "Success: no issues found in 26 source files".
- `python -m tools.check_type_ownership` -> exit 0.
- `lint-imports` -> "Contracts: 9 kept, 0 broken" (includes the new
  `store-rw-restricted`).
- `pytest -m "(unit or integration) and not slow" -q` -> 454 passed, 1 skipped
  (pre-existing, unrelated symlink skip in `test_model_lakeinfo.py`), 4 deselected
  (slow).
- `pytest -k ut00_58` -> 1 passed (import-linter contracts still match the repository).

## Files changed

- `herness/store/_warehouse_rw.py` (new)
- `tests/unit/store/test_store_warehouse_rw.py` (new)
- `pyproject.toml` (import-linter contracts)
