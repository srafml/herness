# T04-06 report: Stage 400 closures and incident facts

Worktree: D:\herness\.claude\worktrees\agent-a634ec9959ed1690a (branch worktree-agent-a634ec9959ed1690a, base 84384c1)
Status: DONE_WITH_CONCERNS (concerns: BT04-09 on the real incident_fact, see below)
Commits: 2d172e9 wip(T04-06): stage 400 facts SQL, facts.py and metrics_tiny fixture; a1a0fa0 feat(metrics): stage 400 closures and incident facts (T04-06)

## Files (lines vs budget)
- herness/model/sql/400_facts.sql: 159 / 400 (U04-41 org_closure, U04-42 work_item_closure, U04-43 incident_fact)
- herness/metrics/facts.py: 150 / 150 (U04-46 split_statements, FACT_TABLES; U04-47 materialize_facts)
- tests/support/metrics_tiny.py (115, no budget): build_metrics_tiny, load_csvs, CORE_DDL, tiny_weights, patch_facts_config, BUILD_ID
- tests/fixtures/metrics_tiny/*.csv (7 files, 26 rows): core.org, core.team, core.service, core.incident (I1-I4 of design 04 §10.1), core.work_item, enrich.cluster_member, enrich.incident_change_link
- tests/unit/metrics/test_metrics_facts.py (UT04-30, UT04-31, UT04-34, UT04-35, ST04-13), tests/fault/metrics/test_metrics_facts_fault.py (FT04-05)
- tests/unit/metrics/test_metrics_catalog.py: ST04-04 scan now includes 400_facts.sql unconditionally (asserts it exists; `# T04-06:` marker and conditional dropped)
- pyproject.toml: herness.metrics.facts added to the "settings modules are leaves" forbidden list (like the other metrics modules)

## Units
U04-41, U04-42, U04-43, U04-46, U04-47 done. Per controller ruling: FACT_TABLES is the full five-tuple; split_statements validates against private `_SHIPPED_TABLES = FACT_TABLES[:3]` with a `# T04-07:` marker; materialize_facts returns three query IDs until T04-07; no placeholder statements. `_INPUT_TABLES` also carries a T04-07 note (add core.change etc.).

## Tests
- RED: with facts.py absent, `uv run pytest tests/unit/metrics/test_metrics_facts.py tests/fault/metrics/test_metrics_facts_fault.py` -> 2 collection errors (ImportError).
- GREEN: same command -> 31 passed; coverage herness/metrics/facts.py 100 % line, 100 % branch (87 stmts, 18 branches).
- tests/unit/metrics + tests/unit/model + tests/unit/test_sql_coverage.py + tests/fault/metrics: 669 passed, 1 skipped (symlinks), 1 xfailed (T04-08 metrics.yaml) .
- Fixture check (design 04 §10.1) reproduced: resolve_h 2/4/6/NULL, I4 excluded, downtime 15000.00/0.00/5000.00, toil_h 3.0/0.8/3.0, total 20680.00.
- Gates: ruff format/check clean, mypy clean, lint-imports 13 kept, check_module_size 0, check_type_ownership 0.
- Commits with SKIP=pytest-unit (KNOWN-RED per controller notes); all other hooks passed.

## Choices / deviations
1. Rendering "as U04-39": render_named cannot take a body (it loads named templates from herness/metrics/sql) and render.py is at 299/300, so facts.py renders with make_environment + RenderState and reuses render's private `_guard`, `_macros`, `_pick` (import with a comment) so behaviour is identical to render_named (context {"filters": {}, "rs": rs}; used binds only; ConfigError on unknown bind / no candidate / render error).
2. split_statements adds one check beyond U04-46's list: a line starting with `-- @statement` that does not match the marker regex is "malformed statement marker" (else it would silently fall into a body). Reasons: malformed marker; prefix not whitespace/Jinja comments; empty statement body; "statements must be exactly <FACT_TABLES[:3]> in order" (also covers "no markers").
3. Input check filters information_schema.tables by `table_catalog = current_database()`; checks the U04-41..43 inputs plus meta.build and meta.evidence, first missing -> SchemaViolation("stage 400 input <table> missing") before any write.
4. Transaction: BEGIN; per statement render + run_recorded(into replace); any exception -> ROLLBACK and re-raise; QueryError -> SchemaViolation("400_facts.sql statement <table> failed: <message>").
5. incident_fact: `is_repeat` window is `lag(opened_at) OVER (PARTITION BY excluded, cluster_id, service_id ORDER BY opened_at, record_id)` - partitioning by `excluded` is the one-scan equivalent of "window over non-excluded incidents only" (tested with an excluded C1 incident between two others). `least()` in DuckDB skips NULLs, so toil_h is guarded by an explicit base-IS-NULL case. `total_usd` is CAST to DECIMAL(18,2) (DuckDB would widen the sum). All SQL prose is in Jinja comments (run_recorded rejects `--`), and the file avoids the ST04-04 forbidden words even in comments.
6. metrics_tiny core DDL: files 200-280 are CTAS over staged lake data and cannot create empty tables in an in-memory DB, so CORE_DDL repeats the 12 core.* shapes; they were copied from `information_schema.columns` of a real empty-lake 000-299 build (BuildHarness), not hand-typed from the SQL. 000_settings.sql is executed as-is. CSVs are `<schema>.<table>.csv`, inserted BY NAME with all_varchar. Timestamps are New York wall times (-05). Session TimeZone UTC. patch_facts_config monkeypatches facts.catalog_from_config (FakeCatalog: shipped defaults+scoring) and facts.get_config (tiny weights) because config/metrics.yaml does not load until T04-08.

## Spec notes
- 400_facts.sql must NOT go through the spec 02 generic render_sql (it uses the metric sandbox macros p()/lkp()); T02-19 `_stage_score`/stage "facts" must call materialize_facts instead. No impl 02 unit/integration test renders files >= 300 today, so nothing broke.
- U04-47 says "log metrics.facts.materialized" with §8.1 fields build_id, table, row_count, query_id, duration_ms: implemented at INFO.

## Benchmarks (ad hoc, local, not a committed test)
- 5M synthetic core.incident rows (half with cluster memberships): incident_fact statement CTAS alone 1.3 s; full run_recorded (store + parallel hash + evidence) 36-39 s; stage 400 total (3 statements) ~39 s -> BT04-01 (< 90 s) currently met for these three statements.
- BT04-09 (< 25 s parallel hashing of metrics.incident_fact, 5M rows) re-run on the REAL 25-column incident_fact: ~35-38 s (hashing dominates), i.e. over target. The committed BT04-09 bench uses a 7-column stand-in. Flag for T04-05 owner / controller; not in this card's scope.

## Concerns
- BT04-09 above.
- facts.py is exactly at its 150-line budget; T04-07 adds only the constant switch and input tables, but there is no slack.
- facts.py imports three private helpers of render.py (render.py has 1 line of budget left, so a public wrapper could not be added there).

## Fix round 1 (review minors 2-3)
- Minor 2: facts.py wraps ROLLBACK in `contextlib.suppress(duckdb.Error)` so the original error (incl. the QueryError -> SchemaViolation mapping) propagates. Compacted two comment/import lines; facts.py still 150/150. New test `test_ut04_35_rollback_error_keeps_original` (proxy connection whose ROLLBACK raises TransactionException): RED with the suppress removed (TransactionException escaped), GREEN with it.
- Minor 3: new `tests/integration/metrics/test_metrics_tiny_ddl.py::test_ut04_35_core_ddl_matches_model_build` (integration): builds 000-299 on an empty lake via BuildHarness/build_core and asserts every core.* table's (column, type) list equals metrics_tiny's (built from CORE_DDL), and CORE_DDL names exactly those tables. Passes.
- Tests: card tests 32 passed (facts.py 100 % line/branch); tests/unit/metrics + tests/fault/metrics + new integration test: 511 passed, 1 xfailed (T04-08). Gates: ruff format/check, mypy, lint-imports (13 kept), check_module_size, check_type_ownership all clean.
