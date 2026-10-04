# T02-13 review (ServiceNow and Jira staging), commits 7941c06..b338534

### Spec Compliance
- ✅ U02-109 `110_stg_servicenow.sql`: every column rule checked against the SQL.
  - sn_incident: typed ts_utc for opened/resolved/closed; `lead_int(priority,1,5)` SMALLINT; enum `servicenow.incident_state` on `coalesce(state, incident_state)`; to_int counts; to_bool made_sla; `sn_duration_s` → business_duration_s; ack/impact custom fields typed only when configured, else typed NULL with no flags or cast_stats row.
  - sn_change_request: enums `servicenow.change_type` (type) and `servicenow.change_close_code` (close_code → outcome); `coalesce(<f>_display, <f>)` for state and risk; opened_at/start_date/end_date/work_start/work_end map to the right columns.
  - sn_problem: the four-way coalesce order is exact; `known_error` typed; `cause_notes` → root_cause_text.
  - sn_ci: `sys_id` = `_source_key`; the union keeps one row per sys_id. `name` and `criticality` come from the cmdb_ci_service row. `busines_criticality` is read from cmdb_ci_service only; it is not even in the cmdb_ci column list. When cmdb_ci_service is absent, criticality is NULL. `ci_class` falls back to `cmdb_ci_service`. The window has a total order.
  - sn_rel_ci, sn_group, sn_department and sn_task_sla match the spec. Every table has record_id, source_key and source_updated_at. All raw reads go through `m.latest`, so the deletion and tombstone filter applies to every entity. An absent entity gives an empty table with the same columns.
  - `cast_stats` is emitted for every typed column. The file first deletes its own cast_stats rows (carry-over handled).
- ✅ U02-110 `120_stg_jira.sql`
  - jira_issue: all columns match the spec: `jstr` paths, the enums, `parent_key` coalesce with the epic_link fallback, VARCHAR[] for components and labels, status_category (key, then name, and a failure only when both miss), typed created and resolutiondate, story_points / DECIMAL(18,2) cost / team_value only when configured, jira_text description, and raw JSON passthrough.
  - jira_transition: accepts both the array and the `{histories}` object forms (`json_type` gives 'OBJECT'). Only items with field `status` are kept. `ts_utc(history.created)` is used, and both categories use the `jira.status_category` enum.
  - jira_link: outward and inward orientation are correct, with `type.name`. The remotelinks regex `(INC|CHG|PRB)[0-9]{4,}` runs on object.url and object.title, and gives full matches. The output is `DISTINCT`.
- IT coverage: IT02-02…08 each have at least one function in both files; Jira uses the card-owned IDs per the controller ruling.
  - IT02-04 uses the real ops store: `running`, `done` and `pending` requests go through `deleted_record_ids`.
  - IT02-06 uses a real uncommitted LakeWriter temp file.
  - IT02-07 checks (100, 3).
  - IT02-08 compares the column lists and types of an empty build and a populated build.
- Evidence I ran: `pytest tests/integration/model` 25 passed; the two card files 20 passed. `ruff check` and `ruff format --check` are clean.
- ⚠️ Cannot verify from the diff:
  - The IT02-10 reading of the sn_ci merge belongs to T02-15.
  - core.service rebuilds IDs with `rid('servicenow:cmdb_ci', sys_id)` (U02-116), so the changing `stg.sn_ci.record_id` has no effect there. Confirm in T02-15.
  - A deletion request on `servicenow:cmdb_ci:X` does not filter a live `servicenow:cmdb_ci_service:X` row, because the filter works per entity by record_id. The CI then comes back in `stg.sn_ci`. CIs are not personal data, so this is low risk, but it is worth a note under TH02-16.
  - `stg.enum_map` (U02-87, T02-12) does not dedupe on `source_value_lc` alone. Two config keys that differ only in case would duplicate staging rows through the enum LEFT JOINs. This is a cross-card and config issue, not this card's.

### Builder concerns (judged)
1. Empty strings as NULL (nullif on non-free-text raw reads): **accepted**. It follows the "Empty strings are NULL" rule in §3.10, and it stops open-ticket empty dates from inflating cast failures.
2. The sn_ci merge reading: **accepted**. It matches the literal rule ("service row wins for name, criticality"); the newest row gives the other columns, and the tie-break is total. There is one small edge case, Minor finding 1.
3. Dropping jira_link rows with a NULL key: **accepted**. An edge with no endpoint is meaningless, and the spec only emits a row when the linked issue is present.
4. The quoted `"at"` column: **accepted**. It keeps the spec's name; T02-17 has to quote it.
5. CAST to VARCHAR on passthrough columns: **accepted**. It is needed for identical column types between absent and present entities, and IT02-08 asserts that.
6. Custom columns read once in the column list: **accepted**. It is a defensive dedupe that does not change behaviour.
- Test IDs: the controller ruling is applied.
- Test file size (417): Minor finding 2.

### Strengths
- The two SQL files are compact (219 and 148 lines, within the 400-line budget). They use the shared macros (`latest`, `typed`, `enum`, `cast_stats`) throughout and read no raw data except through `latest`.
- The tests use the real LakeWriter and the real ops-store deletion flow, and they assert exact rows, cast_stats tuples and column types, including an idempotent re-run with no duplicate cast_stats rows.

### Issues
#### Critical (Must Fix)
None.
#### Important (Should Fix)
None.
#### Minor (Nice to Have)
1. `herness/model/sql/110_stg_servicenow.sql:141,158`: `ci_class` comes from the newest row. If a newer cmdb_ci row has a NULL `sys_class_name` for a sys_id that also has a cmdb_ci_service row, `ci_class` is NULL, so core.service (filtered by `stg.service_ci_class`) would drop that service. Likewise, `name` is the service row's value even when that value is NULL. A suggested fix is `coalesce(b.ci_class, svc.ci_class)` and `coalesce(svc.name, b.name)`. This is unlikely with real CMDB data; flag it for IT02-10 in T02-15.
2. `tests/integration/model/test_model_stg_servicenow.py` is 417 lines, over the 400-line ENG limit per module. The checker does not budget tests, and other test files exceed 400 too. It could be split (for example, the sn_ci tests into their own file).
3. `herness/model/sql/120_stg_jira.sql:143`: dropping links with a NULL `from_key` or `to_key` is not in the spec. Record it in the spec as a clarification, or in a SQL comment (the header comment does not mention it).

### Assessment
**Task quality:** Approved
**Reasoning:** Both units implement every column rule in the U02-109 and U02-110 tables, including enum names, coalesce orders, the cmdb_ci_service-only criticality, the empty tables for absent entities, the deletion filter, both changelog forms and the DISTINCT regex links. IT02-02…08 are covered with real-behaviour assertions, and the findings are edge-case polish only.

## Re-review 1 (fix round 1, 9e69e9a..136b067; T02-14 files ignored)

This round checked only the three Minor findings.

- **M1 fixed.** `110_stg_servicenow.sql`: the `svc` CTE now carries `ci_class`.
  - `name` is now `coalesce(svc.name, b.name)`: the service row still wins unless its name is NULL.
  - `ci_class` is now `coalesce(b.ci_class, svc.ci_class)`.
  - Criticality still comes from `cmdb_ci_service` only (unchanged). The header comment was updated to match.
  - The new test `test_it02_02_ci_service_fallbacks` covers both fallbacks: s1 takes the service class, and s2 keeps the cmdb_ci name while criticality 2 still comes from the service row. The builder says the old SQL fails this test; that claim is plausible from the case data.
- **M2 fixed.** `test_model_stg_servicenow.py` is now 316 lines and `test_model_stg_servicenow_ci.py` is 160. There are 12 functions in the two files: the 11 original ones plus 1 new, so none were lost. The IDs and `pytestmark` are in place.
- **M3 fixed.** `120_stg_jira.sql`: a `--` comment before the NULL-key `WHERE` marks the filter as a clarification beyond U02-110. It contains no `;`, so it does not affect statement splitting, and the tests pass.
- **Evidence I ran:** `pytest tests/integration/model` gave 33 passed. `ruff check` is clean.
- **New findings:** none.

**Task quality:** Approved
