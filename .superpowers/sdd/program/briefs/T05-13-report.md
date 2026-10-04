# Report for T05-13: Warehouse handles

Status: DONE

Commit: 3062d27 "feat(harness): add read-only warehouse handles and pool (T05-13)"
(worktree D:\herness\.claude\worktrees\agent-a26e796c1c53753e8, branch
worktree-agent-a26e796c1c53753e8)

## What was built

`herness/harness/warehouse.py` (207 lines, budget 220):

- `BUILD_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9A-HJKMNP-TV-Z]{6}$")`
- `open_warehouse(build_id, *, warehouse_dir, sql) -> DuckWarehouse` - validates
  build_id and path containment/symlink, checks the file exists, connects read-only
  with the design section 5.4.1 config, runs `SET enable_external_access = false` then
  `SET lock_configuration = true`, then a self-check re-reading
  `current_setting('enable_external_access')`.
- `DuckWarehouse` - implements `WarehouseHandle` (U05-08: `build_id`, `path`,
  `cursor()`, `schema()`, `table_comment()`) structurally (Protocol, no explicit base
  needed), plus `close()` and the private result cache `cache_get`/`cache_put`.
  `cursor()` caches one DuckDB cursor per calling thread via `threading.local`.
  `schema()` loads once from `information_schema.columns` for schemas
  core/enrich/metrics/score/meta and caches the `Mapping[str, Mapping[str, Mapping[str,
  str]]]`. `table_comment()` reads `duckdb_tables().comment`, `""` when NULL.
- `WarehousePool(warehouse_dir, sql, *, max_open=3)` - `get(build_id)` returns a
  cached handle or opens one under a lock, evicting (closing) the LRU handle once
  more than `max_open` are open; `close_all()`.
- `RESULT_CACHE_ENTRIES = 512`.

### Sub-controller ruling 1 (RecordedResult stand-in)

`RecordedResult` (U05-35) is not built. Added a private `_CachedRow` Protocol
(`row_count: int`) and `type _CacheValue = tuple[_CachedRow, Evidence]`;
`cache_get(query_id) -> _CacheValue | None`, `cache_put(query_id, value)` stores only
when `value[0].row_count <= sql.return_rows`, LRU via `collections.OrderedDict`
capped at `RESULT_CACHE_ENTRIES`. The module docstring and the `_CachedRow` docstring
both note this is retyped to `RecordedResult` once U05-35 lands - no other production
code needs to change, only the type alias.

### Sub-controller ruling 2 (tiny_build)

Spec 11's `tiny_build` fixture does not exist. Both test files build a
`wh-<build_id>.duckdb` inline in `tmp_path`: a writable `duckdb.connect` creates
schemas core/enrich/metrics/score/meta, a `core.incident` table with a
`COMMENT ON TABLE ... IS 'tiny incident table'`, and a `meta.build_info` table
without a comment (to exercise the NULL to "" case), then closes it before
`open_warehouse` reopens it read-only.

Carry-over: once T11's real `tiny_build` fixture lands, these inline helpers in
`tests/unit/harness/test_warehouse.py` and `tests/security/test_st05_warehouse.py`
should be replaced with the fixture per the brief's "tests pass on tiny_build"
acceptance check; the inline builder is a faithful stand-in (same schema set) so no
test logic should need to change, only the fixture wiring.

### Sub-controller ruling 3 / VI-4 (setting names)

Verified empirically against the pinned DuckDB in this venv (1.5.5) with a
throwaway script before writing any test:

- `duckdb.connect(path, read_only=True, config={...})` then
  `SET enable_external_access = false` then `SET lock_configuration = true` all
  succeed exactly as design section 5.4.1 specifies, in that order, on a read-only
  connection.
- Self-check `SELECT current_setting('enable_external_access')` correctly returns
  Python `False` after locking.
- After locking, `SET enable_external_access = true` raises
  `InvalidInputException: ... configuration has been locked` - settings cannot be
  changed back, confirming TH05-06 mitigation.
- `CREATE`/`INSERT` fail with `InvalidInputException` (read-only mode).
- `ATTACH ':memory:'` fails with `CatalogException` (read-only mode).
- `read_csv('C:/...')` fails with `PermissionException: ... file system operations
  are disabled by configuration` (external access false).

No deviation from the spec was needed - VI-4 resolves as: the setting names and
order in design section 5.4.1 are correct on the pinned DuckDB. This is recorded as a
finding, not a workaround.

## Tests

- `tests/unit/harness/test_warehouse.py` - UT05-49 (open/lock/self-check, schema +
  table_comment, thread-local cursor, missing-build QueryError, cache LRU +
  return_rows cap, self-check-fails-loudly via a monkeypatched `duckdb.connect`,
  IOException to QueryError via monkeypatch), UT05-50 (bad build_ids incl. traversal and
  bad charset raise ConfigError before any file I/O, symlinked warehouse file raises
  ConfigError, WarehousePool LRU eviction over `max_open`).
- `tests/security/test_st05_warehouse.py` - ST05-06 (parametrized: CREATE, INSERT,
  COPY, `read_csv` of an absolute Windows path, `SET enable_external_access=true`,
  ATTACH - all raise `duckdb.Error` at the connection), ST05-17 (build_id
  `../../x`, `../../../etc/passwd`, `/etc/passwd`, `C:/Windows/win.ini`, and
  separator-embedding ids raise `ConfigError`; symlinked warehouse file raises
  `ConfigError`; a documentation test that `open_warehouse`/`WarehousePool.get` take no
  `run_id` parameter at all, so a run_id with separators has no path to reach in this
  unit).

Symlink-dependent cases (`test_ut05_50_symlinked_warehouse_file_raises_config_error`,
`test_st05_17_symlinked_warehouse_file_raises_config_error`) use `pytest.skip` with
reason on `OSError` - this Windows dev box denies `os.symlink` without elevated
privilege (`WinError 1314`), confirmed skipping both times, all other 26 tests pass.

## Gate results

- `ruff format --check .` - clean (88 files).
- `ruff check .` - clean (0 issues after one `RUF100` auto-fix of a since-unneeded
  `# noqa`).
- `mypy` (strict) - 0 errors, 39 source files (one fix needed: `_connect_config`'s
  return type had to match duckdb's `connect(config=...)` stub,
  `dict[str, str | bool | int | float | list[str]]`, not `dict[str, object]`).
- `lint-imports` - 8/8 contracts kept; no pyproject contract changes needed
  (`herness.harness.warehouse` is a module inside the already-declared
  `herness.harness` package/layer).
- `python -m tools.check_module_size` - clean; `warehouse.py` picks up the 220-line
  budget automatically from the spec 05 section 2 module map row, actual size 207
  lines.
- `python -m tools.check_type_ownership` - clean after switching the `Evidence`
  import from `herness.core.types.harness.evidence` to the public
  `herness.core.types` (submodule import was flagged `OWN041`); only informational
  `pending owner 06/07/09` lines remain (pre-existing, unrelated).
- `pytest -m "(unit or integration) and not slow" -q -p no:logging` - 699 passed,
  2 skipped (symlink, expected), 5 deselected, 1 pre-existing xfail
  (`test_it00_02_check_scripts_pass_on_repo`, unrelated doc-consistency xfail).
- Coverage of `herness/harness/warehouse.py` alone (`--cov-branch`): 96% line/branch
  combined (128 stmts, 4 missed; 28 branches, 2 partial) - the 4 missed lines are the
  symlink-check branch, uncovered only because this box skips the symlink test; on a
  platform that permits symlinks it would be effectively 100%. Meets the >=90%
  line / >=85% branch bar.
- `pytest --require-test-ids` is not yet available on this branch (impl 11 /
  IT11-31 not built) - not run; every test function here still carries its ID in its
  name and docstring per the global constraints.

## Deviations from the brief

None beyond the two sub-controller rulings already applied (private `_CachedRow`
Protocol stand-in for `RecordedResult`; inline `tiny_build`-shaped fixture). VI-4
required no code deviation, only verification (see above).

## Concerns / carry-overs

1. Once T05-24/U05-35 lands `RecordedResult`, `cache_get`/`cache_put`'s
   `_CacheValue` alias in `herness/harness/warehouse.py` should be retyped from the
   private `_CachedRow` Protocol to the real `RecordedResult`.
2. Once impl 11 lands the real `tiny_build` fixture, prefer swapping the inline
   builder in both test files for it, per the brief's acceptance check.
3. `execute_recorded` (which will actually call `cache_get`/`cache_put` in
   production) is out of scope for this card and not built yet - the cache is
   exercised directly in tests with a `_FakeResult` stand-in.
