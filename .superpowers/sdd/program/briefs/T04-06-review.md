# T04-06 review (verify agent), head a1a0fa0, base 84384c1

### Spec Compliance
- ✅ Spec compliant, within the controller rulings: FACT_TABLES has all five names, `_SHIPPED_TABLES = FACT_TABLES[:3]` carries the `# T04-07:` marker, 3 query IDs until T04-07, and the ST04-04 scan always includes 400_facts.sql.

Per unit:
- ✅ U04-41 org_closure. Seed is self at depth 0. The step joins on `p.org_id = w.ancestor_org_id` and emits `parent_org_id`, with `parent_org_id IS NOT NULL AND depth < 20`. GROUP BY with min(depth), and depth is CAST to INTEGER (400_facts.sql:8-23).
- ✅ U04-42 work_item_closure. The step joins on `p.key = a.parent_key` with `depth < 10` and keeps min-depth pairs. Candidate: type IN (initiative, epic, feature) AND status_category IN (todo, in_progress), picked with `row_number() ORDER BY depth, ancestor_record_id`. The LEFT JOIN gives NULL when there is no candidate and the same value on every row of a record (400_facts.sql:24-54).
- ✅ U04-43 incident_fact. Every column checked against the table:
  - copied columns, org_id through team (LEFT JOIN) and criticality through service: ✓
  - cluster: `>= d_cluster_min_membership`, prob DESC, then cluster_id ASC: ✓
  - excluded: `list_contains(..., coalesce(state,''))` OR close_code: ✓, never NULL
  - resolve_h: resolved and opened NOT NULL, resolved >= opened, date_diff <= days*86400, `/3600.0`: ✓
  - resolve_bh: resolve_h NOT NULL AND business_duration_s >= 0: ✓
  - impact_h: excluded gives NULL (the CASE kind 'excluded' has no arm). Measured `>= 0` gives minutes/60.0. Fallback applies when enabled, `coalesce(priority,5) <= max` and resolve_h is NOT NULL, giving `least(resolve_h * lkp(outage, prio, 0), cap)`. Otherwise 0: ✓. impact_estimated is true only on the fallback branch: ✓
  - downtime_usd: DECIMAL(18,2) of impact_h × lkp(downtime, criticality, w_downtime_default) × lkp(prio_mult, prio, 0), NULL only when excluded: ✓
  - toil_h: has an explicit base-NULL guard, which is needed because DuckDB least() skips NULLs; least(base × lkp(effort), max): ✓
  - toil_usd = coalesce(toil_h,0) × engineer_hour as DECIMAL(18,2): ✓. total_usd is the sum, CAST again to DECIMAL(18,2): ✓
  - is_repeat: `lag() OVER (PARTITION BY excluded, cluster_id, service_id ORDER BY opened_at, record_id)`. For non-excluded rows (the only rows that can be true) this is exactly "a window over non-excluded incidents only": ✓. coalesce(..., false): ✓
  - is_reopened and is_reassigned use coalesce > 0: ✓. change_caused = `caused_by_change_id IS NOT NULL OR link score >= d_change_link_min_score`, and the DISTINCT link CTE keeps the row count: ✓
  - column order and types match design §4.2 (asserted by INCIDENT_COLUMNS): ✓. No text column is selected (TH04-04): ✓
- ✅ U04-46 split_statements and FACT_TABLES. The regex is the spec's verbatim `^-- @statement (metrics\.[a-z_]+)\s*$` (no digits, as the spec says). Only whitespace or Jinja comments may come before the first marker, bodies must be non-blank, the table list is compared, and violations raise ConfigError("400_facts.sql: <reason>") (facts.py:37, 45-81). The extra malformed-marker check is covered under Minor 5.
- ✅ U04-47 materialize_facts, steps in spec order:
  - Checks inputs through information_schema.tables, filtered to the current_database() catalog. Verified to work on a real `wh-<build_id>` build connection.
  - Reads the stage file with importlib.resources, splits it, and sets candidates = default_binds ∪ weight_binds.
  - BEGIN. For each statement: render, then run_recorded(..., "facts", into=IntoSpec(table,"replace","query_id")), then log `metrics.facts.materialized` with build_id, table, row_count, query_id, duration_ms. Then COMMIT.
  - On any error it runs ROLLBACK. QueryError becomes SchemaViolation("400_facts.sql statement <table> failed: <message>"). ConfigError propagates unchanged (facts.py:85-150).

Per test:
- ✅ UT04-30. The §10.1 values are asserted literally: resolve_h 2/4/6/NULL, I4 excluded, downtime 15000.00/0.00/5000.00, toil_h 3.0/0.8/3.0 (approx), toil_usd 300/80/300, sum(total_usd) = 20680.00. Other tests in this group:
  - flags and copied columns
  - shape and invariants
  - edge rules: fallback plus cap, invalid durations, unknown team or service (default cost), close-code exclusion, caused_by_change_id, negative business duration
  - repeat window with an excluded incident in between. The test would catch a wrong window: if I5 were not skipped, I3 would count as a repeat.
- ✅ UT04-31. The 0.4/0.6/0.6 tie picks C5. A record with only 0.4 is a noise point (NULL). The 0.5 boundary qualifies.
- ✅ UT04-34:
  - org tree; an org 2-cycle keeps min depth; a 25-level chain is capped at 20
  - work-item closure with candidate; a done epic is not a candidate
  - parent_key cycle capped at depth 10; a duplicate-key tie goes to the lowest ancestor_record_id
- ✅ UT04-35:
  - FACT_TABLES constant; the shipped file splits correctly
  - good and bad files: malformed marker, prefix text, `{% set %}` prefix, blank body, wrong or missing table list
  - evidence rows: producer facts, template name and statement, row_count matches, query_id joins
  - repeatable runs; only used binds are recorded
  - a rollback leaves no tables and no evidence
  - render errors raise ConfigError; a missing input raises SchemaViolation
- ✅ ST04-13. The 1,000-node cycle gives 11,000 rows, max depth 10, elapsed < 5 s (measured 0.63 s).
- ✅ FT04-05. DROP enrich.cluster_member raises SchemaViolation "stage 400 input enrich.cluster_member missing". No metrics table and no evidence row are written.
- ✅ Test ID naming, docstrings and pytestmark are correct. The unit file uses `unit`; the fault file uses `pytest.mark.fault`, like the existing fault files.

Verification run (worktree, PYTHONUTF8=1):
- `uv run pytest tests/unit/metrics tests/fault/metrics -q -p no:logging`: 509 passed, 1 xfailed (T04-08 metrics.yaml, known).
- Card test files with coverage: 31 passed. herness/metrics/facts.py has 100 % line and 100 % branch coverage (87 stmts, 18 branches), so ≥ 90/85: ✓
- ruff check and ruff format --check on the touched files are clean. `mypy herness/metrics/facts.py` is clean (the mypy config covers only herness and tools).
- Budgets: facts.py 150/150, 400_facts.sql 159/400: ✓
- Extra focused check for drift: built 000-299 on an empty lake with BuildHarness and compared information_schema.columns of every core.* table with metrics_tiny CORE_DDL. They are identical: 12 tables, same names, order and types. Then ran materialize_facts on that real build connection: 3 query IDs, 0-row tables, no error.

- ⚠️ Cannot verify from the diff, or out of card scope:
  - BT04-09 (< 25 s parallel hashing of the real incident_fact at 5M rows). The builder measured about 35-38 s on the real 25-column table; the committed bench uses a 7-column stand-in. This is carried over from T04-05. BT04-01 (< 90 s for stage 400) is met for the 3 statements (about 39 s), but the margin will shrink when change_fact and work_item_fact land. The controller should route this to the T04-05 owner or a perf card.
  - T02-19 integration. The build runner does not call materialize_facts yet. If it opens its own transaction on `con` before the call, `BEGIN TRANSACTION` (facts.py:143) raises a raw duckdb TransactionException. Check this when T02-19 wires the "facts" stage.
  - No drift-guard test for CORE_DDL (see Minor 3). There is no drift today (checked above), and IT04-01 on lake_small is the later backstop.

### Strengths
- Every U04-43 NULL rule is implemented as written, including the non-obvious ones: the toil_h guard against DuckDB `least()` skipping NULLs, the excluded-only window via the partition key, and the DECIMAL re-cast of the sum.
- Tests assert the design §10.1 numbers verbatim and cover each edge branch, not only the happy path. The repeat-window test is built so that a wrong implementation fails.
- Rendering reuses render.py's helpers, so behaviour is identical to render_named (same guard, macro module and used-bind pick).
- Transaction behaviour is covered by a test: after a failure there are no tables and no evidence.

### Issues
#### Critical (Must Fix)
- none

#### Important (Should Fix)
- none

#### Minor (Nice to Have)
1. herness/metrics/facts.py:21 imports the render.py private helpers `_guard`, `_macros`, `_pick`, and facts.py:84-90 repeats the body of render_named (render.py:287-299) without the name lookup. This is acceptable now: render.py is at 299/300, facts.py at 150/150, behaviour is identical, and the import is commented. Follow-up when budget allows: add a public `render_source(source, context, candidates)` to render.py and have render_named call it too (this removes lines from render.py). Then the U04-39 contract has one implementation.
2. herness/metrics/facts.py:145-148 uses `except BaseException: con.execute("ROLLBACK"); raise`. If ROLLBACK itself fails (for example on a closed or interrupted connection), its error replaces the original one, which survives only as __context__. Wrap the ROLLBACK in `contextlib.suppress(duckdb.Error)`. This needs 1 line and facts.py has no spare lines, so fold the `_log` line or a constant line to make room.
3. tests/support/metrics_tiny.py:29-66: CORE_DDL copies the output shapes of files 200-280, and no test guards it against drift. It matches exactly today (verified), but a later impl 02 column change would leave the metrics unit tests green. Suggest a small integration test: run BuildHarness 000-299 on an empty lake and compare information_schema.columns of core.* with CORE_DDL. About 20 lines; test files have no budget.
4. herness/model/sql/400_facts.sql:27-35 (and :11-19): the recursive walks use UNION ALL. If duplicate `key` values sit inside a parent_key cycle, paths branch k^depth before the GROUP BY. The spec algorithm allows this and the depth cap still bounds it. `UNION` (set semantics) would merge converging paths, so the TH04-13 bounds hold even with duplicate keys. Hardening only; the current SQL follows the spec.
5. herness/metrics/facts.py:73 adds a malformed-marker rule: a line starting with `-- @statement` that does not match the regex. U04-46 does not list this step. Without it, such a line would silently join the previous body and run_recorded would reject it later for `--`; with it, split_statements raises a clear ConfigError. Keep it. Tests cover it (the Incident_fact and core.incident cases).

### Assessment
**Task quality:** Approved
**Reasoning:** All five units match the verbatim spec: every incident_fact column and NULL rule, the closure caps and tie rules, and the split and materialize contracts. Every card test passes, including the literal §10.1 values, with 100 % line and branch coverage. The open items are hardening and a follow-up refactor, plus the out-of-scope BT04-09 perf carry-over for the controller.

## Scoped re-review, fix round 1 (7b704fd), by the sub-controller
- Minor 2: ROLLBACK is now wrapped in contextlib.suppress(duckdb.Error), so the original error or its SchemaViolation mapping still propagates. The new test test_ut04_35_rollback_error_keeps_original covers it. facts.py stays at 150/150. RESOLVED.
- Minor 3: tests/integration/metrics/test_metrics_tiny_ddl.py::test_ut04_35_core_ddl_matches_model_build compares CORE_DDL with a real 000-299 empty-lake build, checking both the table set and the columns and types. RESOLVED.
- Minors 1, 4 and 5 are parked or kept, per the ledger rulings.
Verdict: Approved.
