# T01-12 review: Mapping check and health (verify agent)

Worktree agent-a7d231cb4d0815a5d, head b0d7115 (base 667fcf4). Read-only review.

**Verdict: Approved** (Minor findings only; spec notes for the controller.)

### Spec Compliance
- ✅ U01-56 `check_mapping`, `MappingIssue`, `STAGING_FILES`. SQL is rendered only through T02-11 `render_sql` (mapping_check.py:130-151). The connectors code builds no SQL text or identifiers; the only strings it passes in are the constant `raw` root and a constant build id. The sqlglot walk matches steps 2-5: `exp.ReadParquet` with a string-literal first argument matching `raw/<source>/(?P<entity>...)/`; the table alias, or the CTE name when the call is the CTE's only source; unqualified columns only when the SELECT has exactly one recorded source; `_` names dropped. Step 6 fetched sets are correct: servicenow fields + `_display` + sys_id/sys_updated_on/sys_class_name, all `to_snake`; mongodb and snowflake `to_snake`; dataverse literal. Step 7 sorts by (entity, column). Render, parse and missing-file failures raise `ConfigError("cannot parse <file>")` chained from the original error (mapping_check.py:148-150, 157-159). jira and monitoring are skipped behind the `# T01-17:` / `# T01-19:` markers per the binding ruling.
- ✅ U01-57 `source_health_report`, `SourceHealth`. It calls `list_watermarks()` once (health.py:76). It reads each stream key through `breaker(key).state()` (T08-06 public API, cached, bounded) and does no raw store reads. open → down "breaker open"; half_open → degraded "breaker probing"; files skips the watermark checks; a missing watermark gives "no watermark for <entity>"; a stale one gives "watermark stale for <entity>: <h> h" (24 h, monitoring 48 h). There is one row per stream key of each enabled source, `monitoring:<tool>` per enabled adapter (this matches the stream-key definition at impl 01 line 1159 and the UT01-95 keys). `now` is made aware through `ensure_utc` (naive → SchemaViolation). No source calls, no network I/O.
- ✅ UT01-58 (8 tests): fixture with `x.close_code` and unqualified columns; config missing close_code gives exactly one issue; the complete config gives `[]`. Extra edge cases: single vs multiple sources, CTE, another source's path, list-literal argument, render/parse failure, missing file.
- ✅ UT01-59 (4 tests): breaker open for jira → down, stale SN watermark → degraded, files → ok. Also covers half_open, missing watermark, the monitoring per-tool key with the 48 h limit, an empty config and a naive `now`. Uses a real migrated ops store bound as the resilience backend.
- ✅ IT01-07 (2 tests): real shipped staging SQL 110-170, real config loader with the shipped synth.yaml overlay, real renderer and sqlglot. `[]` for every source, plus a control test (dropping change_request.close_code gives exactly one issue). check_mapping does not touch the ops store, so no store is needed.
- ⚠️ Cannot verify / spec notes for the controller:
  1. Module-map row (impl 01 §2) allows mapping_check → `herness.model.sqlfiles` only. `render_sql` needs a `RenderContext` (render_context.build_render_context) and a `LakeInventory`/`EntityInventory` (lakeinfo), and sqlfiles has no factory or re-export (checked sqlfiles.py:9-34, 244). The three per-module edges (pyproject.toml:297-299) are the narrowest correct option; a wildcard or a package-level edge would be wider. The spec row should be amended to name render_context and lakeinfo.
  2. UT00-58 edit (tests/unit/repo/test_import_contracts.py:50-54) is sound. It keeps exact-list equality, is gated on the module existing (same pattern as `_config_sections`), and matches global-constraints ("a card that creates a module listed in a contract updates the contract in the same commit"). Cross-card touch of an impl-00 test: controller to acknowledge.
  3. U01-56 does not say which lake inventory the render uses. The builder's synthetic inventory (every configured entity present, `_EveryColumn` claims every column) is what makes the check meaningful: with a real inventory, `raw()` would emit typed NULLs for unseen columns and hide issues. I recommend adding this to the spec.
  4. U01-57 "watermark older than STALE_AFTER": the builder measures `value` (the data high-water mark). Watermarks only move when rows are committed (_write_loop.py:133-149), so a quiet entity (cmn_department, cmdb_rel_ci) will show "watermark stale" even when syncs are healthy. The same happens with `updated_at`, so this is a spec-level design question, not a builder defect. Also confirm with T01-19 that every enabled monitoring tool writes both `event` and `metric_daily` watermarks; otherwise a metrics-only tool stays degraded.
  5. For mongodb, snowflake and dataverse, check_mapping is structurally `[]`: 150-170 read only the lake's own columns (`lake.get(...).columns`, which is empty in the synthetic inventory), so no data column is ever required. This is faithful to the shipped SQL, not a hidden issue. The spec's step 6 fetched sets for these sources are dead until those templates name columns.

### Mutation evidence (run by reviewer, scratch probe over the IT01-07 config)
The baseline gives `[]`. Each dropped field produced exactly the expected issue:
- incident.priority → (incident, priority)
- change_request.state → (change_request, state) and (change_request, state_display)
- cmdb_ci_service.busines_criticality → (cmdb_ci_service, busines_criticality)
- task_sla.has_breached → (task_sla, has_breached)
- cmdb_rel_ci.child → (cmdb_rel_ci, child)
- incident.u_acknowledged_at (mappings custom field) → (incident, u_acknowledged_at)
- problem.known_error → (problem, known_error)
- sys_user_group.manager → (sys_user_group, manager)
- cmn_department.parent → (cmn_department, parent)
- cmdb_ci.company → (cmdb_ci, company)

Dropping the whole task_sla entity gives `[]` (deviation 4, below). The check really fires on the shipped SQL.

### Assessment of builder deviations
- Ignoring read_parquet option names (`hive_partitioning`, `union_by_name`, `filename`) and the virtual `filename` column: required. Otherwise every entity reports false issues, because `m.latest` orders by `filename`. It hides a real issue only if a source genuinely fetches a field named `filename` (Minor 2).
- Skipping unfetched entities: equivalent to what a real render does when the entity is absent (no read_parquet emitted; R-60 tolerance). An entity dropped from config is a coverage question, not a column-mapping one. It does let a config that omits a core entity (for example incident) pass `--check-mapping` (Minor 3).
- Extra `sql_dir` keyword-only parameter: harmless and default-preserving; spec note (Minor 4).
- Always-[] for mongodb, snowflake and dataverse: see ⚠️ 5; not hiding anything today.

### Strengths
- The `_EveryColumn` inventory is an elegant way to force the renderer to show every raw column the SQL wants, with no SQL text built in connectors.
- The IT control test guards the mechanism: if sqlfiles ever stops using `in`, the check turning vacuous would be caught.
- health.py is small and clear, and reads through public APIs only. Reasons carry only entity names and hours.
- Security: errors are "cannot parse <file>"; issues carry entity, column and file from config; health reasons carry entity names. No secrets, hosts or data, and neither module logs anything.
- Gates re-run by reviewer, all clean:
  - `pytest -k "UT01_58 or UT01_59"`: 12 passed.
  - `pytest -m integration -k IT01_07`: 2 passed.
  - UT00-58: passed.
  - ruff check and ruff format: clean.
  - mypy (both modules): clean.
  - lint-imports: 13 kept, 0 broken.
  - Budgets: mapping_check 230/260, health 95/130.
  - Coverage (card unit tests only): health 100 % line and branch; mapping_check 121/131 lines = 92.4 %, 33/36 branches = 91.7 % (combined 89 %). Both are at or above the 90/85 gate.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
1. herness/connectors/mapping_check.py:190-208: alias and CTE names are recorded once per statement, not per SELECT scope. If one statement reuses an alias for a raw source in one subquery and for another table elsewhere, the other table's columns are counted (false positives), and a reused alias across two raw entities is last-wins. The shipped SQL does not trigger this (IT01-07 `[]` plus the mutations above), and the spec algorithm does not ask for scoping.
2. herness/connectors/mapping_check.py:58,173: `_VIRTUAL = {"filename"}` drops any column named `filename`, including a genuine source field of that name (for example a ServiceNow custom field). Consider dropping only a `filename` column when the read_parquet call has `filename = true`, or accept this and document it in the spec.
3. herness/connectors/mapping_check.py:140: unfetched entities are not checked. A config missing a whole required entity (for example incident) passes `--check-mapping` with `[]`. Acceptable given R-60 and render semantics; record as a spec note.
4. herness/connectors/mapping_check.py:82: the extra `sql_dir` keyword-only parameter is not in the U01-56 signature. Add it to the spec or keep it as a test seam.
5. tests/integration/connectors/test_mapping_check_flow.py:108-112: the test mutates the module-level `SERVICENOW_FIELDS` (restored in `finally`). Safe today but fragile under reordering; build a copy instead. The same file (line 19) imports helpers from `tests.unit.connectors._settings_data`, a cross-tree dependency. Its non-ServiceNow sections are unit-test defaults, not synth values. This does not matter today because those sources are skipped or vacuous, but it should be replaced by the T11-16 synth sources config when that ships (as the builder notes). The ServiceNow overlay is a faithful stand-in: field lists match design 01 §4.2 (including `busines_criticality` on cmdb_ci_service only), and the custom columns match tools/synth/servicenow_incidents.py (`u_acknowledged_at`, `u_customer_impact_minutes`).
6. tests/unit/connectors/test_mapping_check.py: the unit fixture never calls `raw()`, so `_EveryColumn.__contains__` (mapping_check.py:78) and the mongodb, snowflake and dataverse `_fetched` branches (mapping_check.py:110-117) are covered only by IT01-07. There is no unit test pinning mongodb, snowflake or dataverse fetched sets.
7. T01-12-report.md "Coverage ... mapping_check.py 100 % line": inaccurate for the card unit tests (92.4 % line). The gate is still met.
8. No RED evidence for UT01-59 (the builder disclosed that health.py was written before its tests). This is process only; the tests assert real behaviour.

### Assessment
**Task quality:** Approved
**Reasoning:** Both units meet U01-56 and U01-57. Mutations show the mapping check fires on the real staging SQL, and health goes only through the breaker and watermark APIs with no source I/O. The import-linter edges are the narrowest workable option, but the module-map row, the render inventory choice and the staleness semantics need spec notes from the controller.
