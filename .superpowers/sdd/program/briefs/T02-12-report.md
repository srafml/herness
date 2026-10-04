# T02-12 report — Reference data, setup SQL and macros

Status: DONE_WITH_CONCERNS (minor, see Concerns). Commit f0e2523 on worktree-agent-a8e0df3b13859f72a:
`feat(model): reference tables, setup SQL, macros and build harness (T02-12)`.

## What was built
- `herness/model/refdata.py` (192 lines / budget 220)
  - U02-87 `register_reference_tables(con, *, mappings, build_cfg, deleted_ids, approved) -> dict[str, int]`:
    six Arrow tables with exact schemas -> `con.register("_ref_<name>")`, `CREATE OR REPLACE TABLE stg.<name> AS SELECT * FROM _ref_<name>`, `unregister` (in `finally`).
    enum_map (domain, source_value_lc via str.lower, canonical; de-duplicated rows), service_override (as given),
    service_alias (lower-cased, de-duplicated), service_ci_class (build_cfg.service_ci_classes), deleted_record (set, sorted),
    approved_mapping (payload subject_type/jira_project/jira_component/team_id/service_id/score).
    Skipped items counted; count > 0 logs `model.build.mapping_skipped` WARNING `count=`; then `model.build.refdata_registered`
    INFO with one count field per table (short names). Returns counts keyed `stg.<name>`.
    `duckdb.Error` -> `SchemaViolation("refdata registration failed: stg.<name>")` (herness.core.errors.SchemaViolation).
  - U02-128 `register_prev_row_counts(con, counts)`: `stg.prev_row_counts(table_name VARCHAR, row_count BIGINT)`, empty for None;
    keys not matching `^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$` skipped.
  - Logger `get_logger("model.build")` (same as lakeinfo). ReviewItem imported under TYPE_CHECKING from herness.store.ops.
- `herness/model/sql/000_settings.sql` (102 lines): 6 schemas + 10 fixed tables, all IF NOT EXISTS, DDL only.
- `herness/model/sql/010_macros.sql` (85 lines): 12 `CREATE OR REPLACE MACRO main.*` macros per U02-108.
  Naive formats use `timezone('UTC', try_strptime(...))` so they are UTC regardless of session TimeZone (tested with America/New_York).
  JSON readers guarded by CASE (not AND). jira_text uses `lambda s:` syntax (DuckDB 1.5).
- `herness/model/sql/_macros.jinja` (71 lines / budget 150): latest, typed, cast_stats, enum, rid. Only ident/sqlstr/raw emit values;
  the absent-entity branch emits typed NULLs through `raw()` (validated SQL type) rather than printing the type string.
- `tests/support/build_harness.py` (test support): `build_harness` fixture -> `BuildHarness` on tmp_path/data using the real
  `open_for_build` (build id 20260901-120000-01ABCD, `BuildSettings(memory_limit="1GB", threads=2)` because DuckDB 1.5.5 rejects
  the 75% default). `run(lo, hi, inventory=, refdata=RefData(...), context=, sql_dir=)` renders + executes files via
  extract_statements; RefData is registered before the first file >= 100 (pipeline order U02-97 step 5), plus
  `render`, `register`, `query`, `execute_sql`, `files`, `context`, `empty_lake`. `fake_job_context` fixture returns the
  `FakeJobContext` factory: in-memory JobContext (JobRow kind build_pipeline, payload), `yield_after=n` / `request_yield()`,
  records heartbeats, gpu_requests, gpu_scopes (+ current_class), service calls, JSON-round-tripped save/load_state;
  a `_conforms` function makes mypy check protocol conformance. Registered in tests/conftest.py pytest_plugins.

## Tests
- tests/unit/model/test_model_refdata.py — UT02-59 (9 functions: rows, exact columns/types, skip+log, no-skip, rerun, error mapping, prev counts, None).
- tests/unit/model/test_model_sql_macros.py — UT02-60 (20-string ts_utc table incl. 31/02/2024, epoch-ms, empty, NULL; session-zone independence; to_date; 000_settings tables/PK/idempotence; macros persistent in main), UT02-61, UT02-62 (to_bool, sn_duration_s, to_double/to_int), UT02-63, UT02-64, ST02-12 static (no INSTALL/LOAD in herness/model/sql).
- tests/unit/model/test_model_sql_jinja_macros.py — UT02-60 for U02-106: real Parquet lake, dedupe (updated_at then fetched_at tiebreak), tombstone, deletion via stg.deleted_record, missing column typed NULL, typed flags, enum exact match (trailing space unmapped), rid trimming/blank, cast_stats rows; absent entity typed zero-row; rendered quoting.
- tests/integration/model/test_model_sql_injection.py — new `test_st02_10_enum_value_via_refdata_matched_literally`: quoted enum value registered by register_reference_tables, stored as data (domain, source_value_lc, canonical), matched literally (and case-insensitively) through the `enum` macro; existing ST02-10 tests unchanged and passing.
- `PYTHONUTF8=1 uv run pytest tests/unit/model tests/integration/model -q -p no:logging`: 162 passed, 1 skipped (symlink).
- Broad: `pytest -m "(unit or integration) and not slow" -q -p no:logging -x --require-test-ids`: 3881 passed, 5 skipped, 1 xfailed (pre-existing), incl. UT11-41 SQL-table coverage.
- Coverage herness/model/refdata.py: 100% line, 100% branch.
- Gates: ruff format/check clean, mypy 0, lint-imports 13 kept / 0 broken (no pyproject change needed), check_type_ownership 0, check_module_size exit 0; pre-commit hooks all passed.

## Deviations / spec notes
1. Approved items: besides the two spec skip rules (missing service_id, subject_type outside {jira_component, team}), items whose
   jira_project/jira_component/team_id are non-string or whose score is not a finite number (bool excluded) are also skipped and
   counted — otherwise pyarrow would raise a non-duckdb error. Empty-string service_id counts as missing. Missing score -> NULL.
2. register_prev_row_counts also skips values that are not non-negative int64 (bools excluded), same reason.
3. Return keys of register_reference_tables are `stg.<name>`; log fields use short names (enum_map=..., ...).
4. cast_stats macro emits only the INSERT (as specified); re-running a staging file would append duplicate rows —
   staging files (T02-13+) should DELETE their table_name rows first if idempotence on resume matters. Empty alias list emits a
   no-op INSERT ... WHERE false.
5. latest: inner select also carries `_deleted` for the outer WHERE; the outer select exposes only the spec columns.
6. ST02-12 is owned by T02-18 (lake_small build with socket guard); I added only a static check function sharing that ID.
7. TDD: tests were written right after a DuckDB prototype of the macros, so no separate RED run is recorded.

## Concerns
- fake_job_context returns a factory (callable) rather than a ready instance; later cards (UT02-78, IT02-25, IT02-27) call
  `fake_job_context({"stages": [...]}, yield_after=3)`.
- ts_utc tolerates leading/double whitespace because DuckDB strptime is whitespace-lenient (e.g. " 2024-03-01 10:20:30" parses).
- to_int('1.5') returns 2 (DuckDB TRY_CAST rounds VARCHAR decimals); spec only says TRY_CAST.

## Fix round 1 (commit 6415b54)
- M1: settings DDL test moved to tests/integration/model/test_model_sql_settings.py as `test_it02_21_settings_schemas_and_tables`
  (integration marker; docstring states it is partial — U02-107 only, the lake_small pipeline IT02-21 stays with T02-18).
  The macros-in-main check folded into `test_ut02_61_macros_stored_in_main_without_extensions`.
- M2: the INSTALL/LOAD static scan lives in that same UT02-61 test; the ST02-12-labelled function is removed.
- M4: `BuildHarness.run` raises ValueError when both `context` and `inventory` are given.
- M5: `latest` test adds record inc6 with identical `_source_updated_at` and `_fetched_at` in a.parquet and b.parquet; b.parquet's version wins (filename DESC).
- M6: ts_utc table now 20 strings (added `2024-13-01 00:00:00` -> NULL) plus the NULL case; asserted explicitly.
- M3 left as is (per review).
- Gates: ruff, mypy, lint-imports (13 kept), type-ownership, module-size clean; model tests 161 passed, 1 skipped; refdata.py 100%/100%;
  broad non-slow unit+integration with --require-test-ids: 3880 passed, 5 skipped, 1 xfailed (pre-existing).
