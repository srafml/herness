# T05-25 Verifier: build report (resumed from killed WIP)

Status: DONE_WITH_CONCERNS (only the recorded stand-ins and deviations below)
Commit: 3799879 feat(harness): add deterministic Verifier with cached re-runs (T05-25)
Worktree: D:\herness\.claude\worktrees\agent-ab8d2158ef5032393 (base 6b70189)

## What was built
- `Verifier` (U05-63/64/67) in herness/harness/verifier.py: `__init__` (override patterns compiled, ConfigError on bad pattern; `sql=None` reads get_config().models.harness.sql), `verify_numbers`, `verify_findings`, `verify_draft`, `verify_answer`, `_verify_items` (BUILD_ID_RE check, `fault_point("verifier.mid_batch")` between items, VerificationResult). Steps 1-8 and 10 per U05-64: markers and uncited via herness.core.numbers, duplicate / named refs / `not_usd`, finding statuses, per-query groups, ops evidence then meta.evidence (tamper check -> missing_query), wrong_build, cached guarded re-run, cell tolerance (equal / equivalent via rows_equivalent / values_only; `result drift` -> query_failed), row selection and compare_value. `verifier_verdict` trace per item with the 9 fields; `harness.verifier.number_unused` WARNING and `harness.verifier.item_failed` INFO; `# T08-05:` markers for the two verifier counters.
- Re-run cache (U05-63): `(query_id, build_id)` -> Rerun under a threading.Lock; rows kept only up to VERIFIER_CACHE_ROWS=10,000, else per-row_key matches computed in the streaming pass (a later new row_key on a large result re-runs); `threading.Timer(rerun_timeout_s, cur.interrupt)`; `too large` above sql.scan_rows; `build unavailable`; `guard: <rule>`; DuckDB errors cut to 200 chars; no error escapes.

## WIP kept / rewritten
- Kept (reviewed line by line, sound): _verifier_rerun.py (re-run cache, streaming scan, StoredEvidence, load_meta tamper check, hash_kind), the Verifier class logic, the stand-in build and all 22 WIP unit tests.
- Fixed: (1) the class was complete but verifier.py was 492 lines; item-level helpers moved to a new private sibling `_verifier_items.py` (draft_items, marker_problems, make_check/actual_value, HashCounts, group_by_query) -> verifier.py 383. (2) BUG: every guarded re-run failed with sqlglot SchemaError because WarehouseHandle.schema() lists schemas with no tables; the guard now gets the schema map without empty schemas (13 WIP tests were red for this). (3) json_safe restructured (PLR0911). (4) WIP test fixes: log dict now includes the bound `component`; invalid `# fmt: skip` comments removed (RUF028); lambda lint.
- Added: IT05-07, ST05-11/12/23, FT05-03/05, BT05-09 tests and the IT05-07 stand-in corpus in _verifier_standin.py.

## Files and line counts
- herness/harness/verifier.py 383 (budget 400)
- herness/harness/_verifier_rerun.py 320 (new private sibling, default 400) -- module-map note needed
- herness/harness/_verifier_items.py 152 (new private sibling, default 400) -- module-map note needed
- herness/harness/warehouse.py 208 (+1 line: `python_enable_replacements: False`)
- tests: tests/unit/harness/test_verifier_numbers.py, tests/unit/harness/_verifier_standin.py, tests/integration/harness/test_verifier_corpus.py, tests/security/test_st05_verifier.py, tests/fault/harness/test_verifier_fault.py, tests/bench/test_harness_verifier_bench.py; tests/unit/harness/test_warehouse.py (UT05-49 asserts the new setting)
- check_module_size: unlisted private modules get the 400 default; exit 0.

## Test IDs (functions)
UT05-117 x9, UT05-118 x4, UT05-119 x2, UT05-120 x1, UT05-122 x1, UT05-129 x5, IT05-07 x3, ST05-11 x1, ST05-12 x1, ST05-23 x1 (5 params), FT05-03 x2, FT05-05 x1, BT05-09 x1.
IT05-07: 13/13 planted kinds detected with their expected signal; 50/50 correct items pass (incl. 7.4 for 7.41667, USD strings, Q3 2026, INC0012345, 2026-09-24); mixed batch of 63 fails exactly the 13.
BT05-09 (stand-in, 200k bulk rows, 100 findings of 3-5 numbers over 2-3 queries, cold cache each): passes, whole test ~12 s.

## Deviations
- Spec 11 fixtures tiny_build / lake_small / full absent: test-local wh-<id>.duckdb (_verifier_standin.make_build) and a stand-in IT05-07 corpus; BT05-09 on the stand-in dataset, markers [integration, slow] (controller ruling).
- U05-35 not built: `_verifier_rerun.json_safe` / `sample_rows` are private stand-ins for the step 6 JSON-safe conversion, marked `# U05-35: replace`; tools.py not created.
- FT05-03: `_shims.fault_point` is a no-op and T08-08 has no plan loader; the test uses a test-local JSON plan stand-in honoured only with HERNESS_ENV=test, monkeypatched onto `_shims.fault_point`; the "kill" is a BaseException unwinding the thread (no real process kill); the rerun uses a fresh Verifier and pool. Re-point to the `fault_plan` fixture when T08-08 lands.
- allowed_numeral_patterns=None: no loader for `reports.allowed_numeral_patterns` is reachable from L4 (herness.reports is L5; HernessConfig.app is a closed stub). `_configured_patterns` prefers `get_config().app.reports.allowed_numeral_patterns` when present, else a constant mirroring config/app.yaml (identical to the 5 patterns there).
- FT05-05 on Windows: an open DuckDB file cannot be deleted, so the file is deleted inside ops.get_evidence (after evidence load, before the pool opens the build) -> `build unavailable` -> query_failed.
- `_rerun` of U05-63 is implemented as `RerunCache.get` in the sibling (called from `_check_group`), not as a Verifier method.
- BUILD_ID_RE imported from herness.harness.warehouse (not exported by herness.core.types).

## Carry-overs
- Replace json_safe/sample_rows with U05-35 step 6 when herness.harness.tools lands.
- Re-point FT05-03 to the T08-08 fault plan fixture; metric counters at `# T08-05:` markers.
- Swap stand-ins for spec 11 tiny_build / lake_small / full and the real IT05-07 corpus / scripted runs.
- Replace the pattern fallback with the owner 09 config section when T09-01 AppConfig lands.
- Module-map note for _verifier_rerun.py (320) and _verifier_items.py (152) in spec 05 section 2.
- T05-14 carry-over `python_enable_replacements=false` DONE (trivial; UT05-49 extended).
- Observation (not changed): WarehouseHandle.schema() returns empty schema dicts that SqlGuard/sqlglot cannot take; the future tools.py caller of SqlGuard needs the same filtering (or schema() should drop empty schemas).

## Gates
- ruff check / ruff format --check: clean. mypy --strict (herness, tools): 0 issues. lint-imports: 13 kept, 0 broken. check_type_ownership: exit 0. check_module_size: exit 0.
- Coverage (card tests, branch on): verifier.py 100 %, _verifier_items.py 100 %, _verifier_rerun.py 99 % (2 lines), warehouse.py 96 %.
- Card tests with --require-test-ids: 112 passed, 1 skipped (symlink privilege).
- Regression `-m "(unit or integration) and not slow"`: 3873 passed, 5 skipped, 21 deselected, 1 xfailed.
- Pre-commit hooks all passed on commit (no --no-verify, no PRE_COMMIT_ALLOW_NO_CONFIG).

## Fix round 1 (review M1, M2, M4; M3 and M5 parked)
Commit: e9b0bc2 fix(harness): keep Verifier re-run failures contained and logged (T05-25)
- M1: `RerunCache._compute` now takes `pool.get` and `wh.schema()` inside one try (DuckDB errors included), and `_execute` guards `wh.cursor()`. A handle closed by another thread gives `build unavailable`, so the group is query_failed and nothing raises. `load_meta` treats a meta.evidence row with a NULL sql, result_hash or row_count as unusable, so the number is missing_query (the spec maps unusable or tampered meta rows to missing_query). The schema map is now read for meta.evidence re-runs too (cached per handle).
- M2: every query_failed group logs INFO `harness.verifier.query_failed` with `query_id` and `reason`. The reason is a category: `result drift`, `too large`, `build unavailable`, `guard: <rule>`, or `duckdb: <ExceptionClass>`. The DuckDB message text stays in the cached `Rerun.error` and is never logged. It comes from the new `Rerun.reason` field; `failed()` defaults it to the error text for the fixed categories.
- M4: `herness.metrics.evidence._is_numeric` is private, so I did not reuse it. `_EXACT_NUMERIC` is renamed `_NUMERIC_TYPES`.
- Tests added (UT05-117/118 IDs): handle closed on schema/cursor gives query_failed (2 params); NULL row_count/sql/result_hash gives missing_query (3 params); category-only logging for a DuckDB ConversionException that quotes data, a guard rejection and a result drift. RED against the previous commit: 5 failed, 1 passed (the NULL-sql case already gave missing_query through the tamper check).
- Line counts: verifier.py 386, _verifier_rerun.py 327, _verifier_items.py 152 (all <= 400).
- Gates: ruff check/format clean; mypy 0 issues; lint-imports 13 kept; type-ownership and module-size exit 0; coverage verifier.py 100 %, _verifier_items.py 100 %, _verifier_rerun.py 99 %; card tests 481 passed, 2 skipped (symlink privilege) with --require-test-ids (the run also covered the rest of tests/unit/harness); regression 3879 passed, 5 skipped, 1 xfailed; pre-commit hooks passed.
