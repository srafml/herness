# T02-12 review — Reference data, setup SQL and macros (commit f0e2523, base e51533e)

### Spec Compliance
- ✅ Spec compliant.
  - U02-87 `register_reference_tables` ✅ — six Arrow tables with the exact declared columns/types (refdata.py:28-54; verified by `test_ut02_59_exact_columns` through information_schema), enum source values `str.lower()` + dedup, aliases lower-cased, classes from `build_cfg.service_ci_classes`, deleted IDs de-duplicated, approved payload fields; spec skip rules (missing service_id, subject_type outside {jira_component, team}); `mapping_skipped` WARNING with count only when > 0; register → CTAS `stg.<name>` → unregister in `finally` (refdata.py:128-139); `duckdb.Error` → `SchemaViolation("refdata registration failed: stg.<name>")`; `model.build.refdata_registered` INFO with counts. Values reach DuckDB only as Arrow data; the only f-string SQL uses module-constant names (refdata.py:134). Case-variant enum collisions cannot produce duplicate join rows: MappingsConfig rejects them (settings.py `_check_domain`).
  - U02-128 `register_prev_row_counts` ✅ — `stg.prev_row_counts(table_name VARCHAR, row_count BIGINT)`, empty for None, non-`schema.table` keys skipped, same register/CTAS/unregister and error mapping.
  - U02-106 `_macros.jinja` ✅ — `latest`: present branch selects `_record_id, _source_key, _source_updated_at` + `raw(...) AS "c"` from `read_parquet(<sqlstr glob>, hive_partitioning = true, union_by_name = true, filename = true)` with `QUALIFY row_number() OVER (PARTITION BY _record_id ORDER BY _source_updated_at DESC, _fetched_at DESC, filename DESC) = 1`, outer `WHERE NOT _deleted AND _record_id NOT IN (SELECT record_id FROM stg.deleted_record)`; absent branch is a zero-row typed-NULL SELECT (`_source_updated_at` TIMESTAMPTZ matches the lake's `timestamp[us, UTC]`, store/lake.py:34-40; declared columns via `raw()` → `CAST(NULL AS <validated type>)`). `typed`, `cast_stats` (INSERT, one row per alias, `for…else` no-op when empty), `enum` (exact match on `lower(raw)`, no trim), `rid` (trim/nullif, sqlstr prefix) all match the table. Only `ident`/`sqlstr`/`raw` emit values; uses only the `lake`/`raw` globals and filters (sandbox carry-over respected; no str/list methods).
  - U02-107 `000_settings.sql` ✅ — six schemas, ten tables, every column/type/`PRIMARY KEY` exactly as the table, all `IF NOT EXISTS`, DDL only.
  - U02-108 `010_macros.sql` ✅ — all 12 macros in `main` (persistent). ts_utc: exactly the 8 formats via `try_strptime`, naive ones through `timezone('UTC', …)` (verified by probe: plain CAST would give 21600 under America/New_York, macro gives 3600 — the session-zone test is meaningful); to_date; to_bool sets; lead_int regex `^([0-9]+)(\s*-.*)?$` + BETWEEN lo/hi; to_double non-finite → NULL; to_int TRY_CAST; sn_duration_s digits → seconds else `epoch(ts_utc(x))`; jstr/json_names/json_str_list CASE-guarded by `json_valid`; jira_text regex, JSON unescape and single-space join; team_value object name/title/value, JSON string, else x.
  - Test support ✅ — `build_harness` renders+runs a file range on a temp build file via the real `open_for_build`, with inventory, RefData (incl. prev row counts) registered before file 100 per U02-97 step 5; `render`, `query`, `execute_sql`, custom `sql_dir` make it usable by T02-13+. `FakeJobContext` matches every member of `JobContext` (ports.py:169-189); `uv run mypy tests/support/build_harness.py` → 0 issues (note: repo mypy config covers only herness/tools, so the `_conforms` check is not enforced by the gate — I ran it explicitly).
  - Tests ✅ — UT02-59…UT02-64 each have functions; IDs in names and first docstring lines; `pytestmark` set. UT02-60 has 20 entries incl. `31/02/2024`, epoch-ms `1717000000000`, empty. The new ST02-10 test really goes through `register_reference_tables` (via `BuildHarness.run(refdata=…)`) and the real `enum` macro, stores the quoted `DROP` value as data and matches it literally and case-insensitively.
- ⚠️ Cannot verify from diff / accepted behaviour:
  - ts_utc also accepts leading/double whitespace and `+00:00` offsets (DuckDB strptime leniency) although the spec says "exactly these formats"; to_int('1.5') → 2 (DuckDB TRY_CAST). Both are literal spec implementations (`try_strptime`, `TRY_CAST`); accepted, not pinned by tests.
  - ST02-12 proper (lake_small build under socket guard) remains T02-18's; only a static no-INSTALL/LOAD scan exists here.
  - No RED run recorded (process; tests written after a DuckDB prototype).

### Builder concerns — rulings
- Extra approved-item skip rules (non-string jira_project/jira_component/team_id, non-finite/bool score, empty service_id): **accepted** — without them pyarrow raises a non-duckdb error that would escape the spec's error contract; items are still counted and logged.
- Non-negative-int64 filter in register_prev_row_counts: **accepted** (same reason; covered by test).
- fake_job_context as a factory: **accepted** — UT02-78/IT02-25/IT02-27 need per-test payload/yield options.
- cast_stats INSERT-only: **accepted** (spec says INSERT); carry-over below.
- ST02-12 static only: accepted as supplementary (see Minor 2).

### Strengths
- Tight, readable refdata module (192/220), 100 % line and branch coverage; views cleaned up even on failure (tested).
- Macro tests use a real Parquet lake exercising dedupe with fetched_at tiebreak, tombstones, deletion requests, missing-column typed NULL, cast-failure flags and the trailing-space enum case.
- Session-time-zone independence of naive ts_utc formats deliberately handled and tested.

### Issues
#### Critical (Must Fix)
- None.
#### Important (Should Fix)
- None.
#### Minor (Nice to Have)
1. tests/unit/model/test_model_sql_macros.py:87, :123 — `test_ut02_60_settings_schemas_and_tables` and `test_ut02_60_macros_live_in_main_not_temp` test U02-107/U02-108 placement, not ts_utc; UT02-60 is the ts_utc row (U02-107's spec test is IT02-21, owned by T02-18). Misleading `-k UT02_60` selection; relabel (e.g. rf_/cv_ prefix) when convenient.
2. tests/unit/model/test_model_sql_macros.py:215 — `test_st02_12_…` is a unit-marked static scan sharing the ST02-12 ID, whose spec kind is integration (T02-18). T02-18 must still deliver the real ST02-12; otherwise the ID looks satisfied by this scan.
3. herness/model/sql/_macros.jinja:52-60 — cast_stats only INSERTs; a re-run/resumed staging file appends duplicate `stg.cast_stats` rows. Carry-over for T02-13+: DELETE the table's rows (or CREATE OR REPLACE the stg table first) before `m.cast_stats`.
4. tests/support/build_harness.py:135 — when both `context` and `inventory` are passed, `inventory` is silently ignored; consider raising.
5. herness/model/sql/_macros.jinja:28 — the `filename DESC` final tiebreak is not exercised by any test (only updated_at and fetched_at ties are).
6. tests/unit/model/test_model_sql_macros.py:38-59 — "20 strings" is 19 strings + one NULL; fine but the spec's 20 are strings.

Gates run (worktree): `ruff check` clean, `ruff format --check` clean, `mypy` 0 issues (145 files), `lint-imports` 13 kept / 0 broken, `check_module_size` exit 0, `PYTHONUTF8=1 pytest tests/unit/model tests/integration/model -q -p no:logging --cov=herness.model.refdata --cov-branch` → 162 passed, 1 skipped (symlink), refdata.py 100 % line / 100 % branch. No warnings in output.

### Assessment
**Task quality:** Approved
**Reasoning:** Every unit matches the spec's columns, macro definitions and emissions; values reach SQL only as Arrow data or through the quoting filters, the harness and fake context are usable by later cards, and all gates pass. Remaining items are test-labelling and carry-over notes.

## Re-review 1 (commit 6415b54, base f0e2523)

Scope: Minors M1, M2, M4, M5, M6 (M3 parked) and regressions.

- M1 ✅ — the setup-DDL test moved to tests/integration/model/test_model_sql_settings.py as `test_it02_21_settings_schemas_and_tables` (docstring states it is a partial IT02-21 for U02-107 only; the lake_small IT02-21 stays T02-18's). The macros-in-`main` check is now `test_ut02_61_macros_stored_in_main_without_extensions` (test_model_sql_macros.py). U02-108's tests are UT02-60…UT02-64, so a U02-108 ID is acceptable, though not lead_int-specific (see note).
- M2 ✅ — the ST02-12-labelled unit function is gone. The static INSTALL/LOAD scan is kept inside the UT02-61 function, so ST02-12 is no longer falsely satisfied.
- M4 ✅ — `BuildHarness.run` raises ValueError when both `context` and `inventory` are passed (build_harness.py:136-138), and the docstring says so.
- M5 ✅ — inc6 has identical `_source_updated_at`/`_fetched_at` in a.parquet and b.parquet with distinguishable values (g0/New vs g6/Resolved). The expected row is from b.parquet (`filename DESC`). cast_stats stays (3, 1) because inc6 has NULL opened_at.
- M6 ✅ — TS_CASES now has 20 non-NULL strings plus NULL (added `2024-13-01 00:00:00` → NULL), and both counts are asserted.
- Regression checks:
  - The new IT02-21 file sets `pytestmark = pytest.mark.integration` and passes under `-m integration`.
  - Every function carries an ID: `pytest tests/unit/model tests/integration/model --require-test-ids` gives 161 passed, 1 skipped (symlink). That is −1 vs round 0, as expected from merging the ST02-12 scan into the UT02-61 function.
  - `ruff check` clean, `ruff format --check` clean, `check_module_size` exit 0.
  - No production code changed.

New findings:
- Minor (note only): tests/integration/model/test_model_sql_settings.py:20 uses the IT02-21 ID, which is owned by T02-18. It is documented as partial, but T02-18 must still deliver the full lake_small IT02-21 and should not treat this ID as covered.

**Task quality:** Approved
