# T02-13 report: ServiceNow and Jira staging

Status: DONE_WITH_CONCERNS (interpretation notes only; all card tests green)
Worktree: D:\herness\.claude\worktrees\agent-a6821c8395615d9aa (branch worktree-agent-a6821c8395615d9aa, base 7941c06)
Checkpoints: 448430d wip 110; 55f2403 wip 120; final b338534 feat(model): ServiceNow and Jira staging SQL (T02-13) (empty commit: all content landed in the two wip checkpoints).

## Built
- `herness/model/sql/110_stg_servicenow.sql` (U02-109): stg.sn_incident, sn_change_request, sn_problem,
  sn_ci (cmdb_ci UNION cmdb_ci_service, one row per sys_id), sn_rel_ci, sn_group, sn_department,
  sn_task_sla. Every table has record_id/source_key/source_updated_at; typed columns via `m.typed`
  (flags `_nn_*`/`_cf_*`); enums via `m.enum`; raw reads only through `m.latest`. Custom fields
  `acknowledged_at`/`customer_impact_minutes` typed with flags when configured, else NULL without flags.
- `herness/model/sql/120_stg_jira.sql` (U02-110): stg.jira_issue (typed type, status_category with
  key-then-name fallback, created_at/resolved_at, story_points/estimate_cost_usd typed when configured,
  team_value, parent_key with epic_link fallback, components/labels VARCHAR[], jira_text description,
  raw changelog/issuelinks/remotelinks), stg.jira_transition (changelog array or {histories} object,
  status items only, categories via jira.status_category), stg.jira_link (outward/inward issuelinks,
  remotelinks (INC|CHG|PRB)[0-9]{4,} mentions in object.url/title, DISTINCT).
- Carry-over fixed: each file first `DELETE FROM stg.cast_stats WHERE table_name IN (<its tables>)`, so a
  re-run keeps one row per column (asserted in IT02-02 tests for both files).

## Files and sizes (budget: §2 row "000_settings.sql … 900_dq_checks.sql: per file <= 400")
- herness/model/sql/110_stg_servicenow.sql 219 / 400
- herness/model/sql/120_stg_jira.sql 148 / 400
- tests/integration/model/_stg_lake.py 117 (helper: real LakeWriter rows, build(), columns(), cast_stats())
- tests/integration/model/test_model_stg_servicenow.py 417
- tests/integration/model/test_model_stg_jira.py 382
- `uv run python -m tools.check_module_size` exit 0; ruff clean; lint-imports 13 kept; check_type_ownership 0.
  No herness Python changed (mypy scope unchanged).

## Tests (IDs -> functions), all integration
- IT02-02: sn test_it02_02_latest_incident_version_wins, test_it02_02_ci_one_row_per_sys_id;
  jira test_it02_02_latest_jira_issue_wins, test_it02_02_transitions_from_latest_changelog, test_it02_02_links_distinct
- IT02-03: test_it02_03_tombstones (sn), test_it02_03_jira_tombstone
- IT02-04: test_it02_04_deletion_requests (real ops store: create_deletion_request + set_deletion_status
  running/done/pending -> deleted_record_ids -> RefData; running+done removed, pending kept),
  test_it02_04_jira_deleted_record
- IT02-05: test_it02_05_unknown_column_and_absent_custom_field, test_it02_05_custom_fields_present,
  test_it02_05_jira_custom_fields_absent, test_it02_05_jira_custom_fields_present
- IT02-06: test_it02_06_temp_files_not_staged (uncommitted LakeWriter flush, BUFFER_ROWS=1)
- IT02-07: test_it02_07_cast_stats_bad_timestamps (100 rows, 3 bad -> (100, 3)),
  test_it02_07_typed_columns_of_other_entities, test_it02_07_jira_cast_stats (also status_category fallback)
- IT02-08: test_it02_08_absent_optional_entities, test_it02_08_empty_lake_same_columns, test_it02_08_jira_issue_absent
RED: with each SQL file moved out, `pytest tests/integration/model/test_model_stg_servicenow.py` -> 11 failed;
`... test_model_stg_jira.py` -> 9 failed. GREEN: `PYTHONUTF8=1 uv run pytest tests/integration/model tests/unit/model -q -p no:logging`
-> 181 passed, 1 skipped (symlink skip). UT11-41 SQL coverage gate passes (every new stg table named by a test).

## Deviations / spec notes
1. Empty strings as NULL: every raw VARCHAR read (except free text short_description, description,
   close_notes, cause_notes, summary, Jira JSON columns) is `nullif(CAST(col AS VARCHAR), '')`, and typed
   columns use that as their raw_expr, following §3.10 "Empty strings are NULL". Effect: an empty
   ServiceNow date (e.g. resolved_at of an open incident) is neither non_null nor a cast failure.
2. stg.sn_ci merge ("the cmdb_ci_service row wins for name, criticality; ties by later source_updated_at"):
   implemented as: the record columns (record_id, owned_by, support_group, cost_center, company, ci_class)
   come from the newest row per sys_id (cmdb_ci_service row on an equal timestamp, then record_id DESC);
   name and criticality come from the cmdb_ci_service row whenever one exists. record_id is that newest
   row's `_record_id` (so a service-only CI keeps `servicenow:cmdb_ci_service:<sys_id>`; core maps IDs).
   ci_class for cmdb_ci_service rows = coalesce(sys_class_name, 'cmdb_ci_service') (covers both a missing
   column and a NULL value). IT02-10 (T02-15) may want to confirm this reading.
3. stg.jira_link drops rows whose from_key or to_key is NULL (e.g. an issuelink with neither outwardIssue
   nor inwardIssue key); spec is silent.
4. `stg.jira_transition."at"`: `at` is a DuckDB reserved word; the column keeps the spec name and callers
   must quote it (T02-17 280_work_item note).
5. Raw passthrough VARCHAR columns are CAST to VARCHAR so absent and present entities give identical
   column types (IT02-08 compares the full column lists of empty vs populated builds).
6. A configured custom column that equals a standard column (or two Jira fields naming the same column)
   is read once (dedupe in the latest column list) instead of producing a duplicate-column SQL error.
7. Test IDs: U02-110 lists IT02-18..IT02-20 (T02-17's); per controller ruling the Jira tests use the
   card-owned IT02-02..08.

## Concerns
- Interpretations 1-3 above are judgement calls on under-specified points; none contradicts a test row.
- tests/integration/model/test_model_stg_servicenow.py is 417 lines (test file; the checker only budgets
  herness/app/tools modules; many existing test files exceed 400).

## Fix round 1 (review D:\herness\.superpowers\sdd\program\briefs\T02-13-review.md, base 9e69e9a)
- M1 `110_stg_servicenow.sql` sn_ci: `name = coalesce(svc.name, b.name)` (the service row's name
  wins unless it is NULL) and `ci_class = coalesce(b.ci_class, svc.ci_class)` (a newest cmdb_ci row
  without a class takes the service row's class). New test IT02-02
  `test_it02_02_ci_service_fallbacks` covers both cases (with the old SQL, s1 gave ci_class NULL and
  s2 gave name NULL).
- M2 split: the sn_ci and absent-entity tests moved to
  `tests/integration/model/test_model_stg_servicenow_ci.py` (160 lines: test_it02_02_ci_one_row_per_sys_id,
  test_it02_02_ci_service_fallbacks, test_it02_08_absent_optional_entities,
  test_it02_08_empty_lake_same_columns); `test_model_stg_servicenow.py` is now 316 lines.
- M3 `120_stg_jira.sql`: added a SQL comment on the NULL-key link filter, marking it as a
  clarification beyond U02-110.
- T02-14's `_stg_lake.py` (the `extra_entities` kwarg) and the 130-160 files and tests are untouched.
- Sizes: 110 is 221/400 lines, 120 is 150/400. `PYTHONUTF8=1 uv run pytest tests/integration/model -q -p no:logging`
  gives 33 passed. ruff format and ruff check are clean.
