# T05-17 report — Warehouse tools, part 1 (build agent)

Status: DONE_WITH_CONCERNS (minor; see Deviations). Worktree branch worktree-agent-a9e496468db262876, base a46205f.

## Implemented
- `herness/harness/warehouse_tools.py` (240 / 400; leaves ~160 lines for T05-18's four tools): `ListTables` (U05-39), `DescribeTable` (U05-40), `RunSql` (U05-41), `GetScores` (U05-43), `register_warehouse_tools(registry=None)` (owner "05", idempotent: module-level instances in `_TOOLS`, which T05-18 extends).
- `herness/harness/_warehouse_tools_sql.py` (175 / 200, NEW private sibling; §2 module-map row added in docs/impl/05-harness-core.impl.md same commit): `LIST_TABLES_SQL`, `DESCRIBE_COLUMNS_SQL`, `SCORES_SQL` (plain literal constants, re-exported by warehouse_tools), typed argument access (`text`, `opt_text`, `integer`), `strict_schema`, `one_line`, `quote`, `blocked_columns`, `redacted`, `table_result`.
- Safety: `run_sql` always `execute_recorded(..., guard=True)`; all other SQL is constant (`guard=False`) except the describe sample, whose identifiers are taken from `ctx.warehouse.schema()` (allow-list) intersected with the `duckdb_columns()` order, minus blocked columns, DuckDB-quoted. No SQL executed on a cursor directly; hashing only via execute_recorded. list_tables filters to ALLOWED_SCHEMAS in SQL and again in Python; describe_table refuses non-allowed schemas and its "closest" hint (rapidfuzz WRatio, 3) draws only from allowed tables. Redact-on-read cells (`RecordedResult.redact_columns`, i.e. guard lineage incl. `score.funding.title`, `core.work_item.summary`) pass `redact_text` before content and data; format_result wraps untrusted text.
- Import-linter: contracts list packages only (herness.harness), no change needed; 13 kept.

## Carry-overs closed
- (a) ST05-07 `test_st05_07_spec05_sql_executes_only_select` and `test_st05_07_concurrent_selects_on_cold_schema` now use the real `RunSql` (via `register_warehouse_tools(reg)` / `wt.RunSql()`), with `purpose`; `_RunSqlStandin` removed.
- (b) `open_warehouse` closes the connection on any failure after connect (try/except BaseException: close; raise). Regression test `test_ut05_49_failed_lock_down_closes_connection` (3 params: self-check false, SET external access fails, SET lock fails). warehouse.py 220 / 220.
- (c) `test_ut05_81_concurrent_tools_on_cold_warehouse`: 4 describe_table + 2 list_tables concurrently through dispatch on a cold warehouse, all ok.

## Tests
- `tests/support/warehouse_tools_build.py` (NEW): stand-in build (core.incident with all configured blocked columns, core.work_item (redact-on-read summary + injection text), enrich/metrics/meta tables, score.funding/org/action_lever/portfolio with spec columns incl. query_ids, and main/secret/stg tables that must never appear); sizes parameterised; docstring notes re-point to tiny_build/full when spec 11 lands. `patch_redaction` stubs redact_text in both call sites (test config has no redaction keys).
- `tests/unit/harness/test_warehouse_tools.py`: 24 tests (UT05-80 x4, UT05-81 x8 incl. params, UT05-82 x5, UT05-84 x7 incl. params).
- `tests/bench/test_harness_warehouse_tools_bench.py` [integration, slow]: BT05-04, BT05-05.
- Coverage: warehouse_tools.py 99 %, _warehouse_tools_sql.py 97 % (line+branch combined; only uncovered: defensive non-allowed-schema skip in list_tables).
- Card run: `pytest tests/unit/harness tests/security/test_st05_tools.py tests/security/test_st05_warehouse.py -q -p no:logging` -> 1239 passed, 2 skipped (symlink privilege on Windows).
- RED: test file collected with the module absent -> ImportError at collection; warehouse regression test 3 failed before the fix.
- Gates: ruff check/format clean, mypy 0 issues (263 files), lint-imports 13 kept, check_module_size 0, check_type_ownership 0.

## Benchmarks (local full-like build: 500,000 incidents, 200 services, 36 months; metrics.incident_monthly 7,200 rows; 19.9 MB)
- BT05-04 run_sql, 200 distinct typical aggregates through dispatch end to end: p95 25.4 ms (threshold 2 s).
- BT05-05 100 calls each, p95 over calls 2..100 (cache warm after the first): list_tables 0.10 ms, describe_table (core.incident) 0.41 ms (threshold 50 ms).

## Deviations / spec notes
1. Tool `__call__` signature is `(ctx, **kwargs: JsonValue)` (the `Tool` protocol) rather than the spec's keyword-only typed parameters, which mypy rejects as a `Tool`; arguments are read through typed helpers that raise `ToolInputError` on missing/mistyped values (direct calls).
2. `GetScores` raises `QueryError("table score.<kind> is not in this build")` when the score table is missing from `schema()`, instead of letting the internal-SQL guard raise a fatal `ConfigError` that would cancel sibling calls. Not in the spec; defensive.
3. `DescribeTable` with every column blocked skips the sample query (no `SELECT  FROM`); content/data carry no sample.
4. Content formats the spec leaves open: describe header `query_id=<cols qid> table=<t> columns=<n>`, then `name | type | yes/no | description`, then `sample rows:` and the format_result table (its own query_id header). describe `data = {table, columns[{name,type,nullable,description,blocked}], sample}`; `query_ids = [cols, sample]`. run_sql/get_scores `data.truncated` = rows dropped by return_rows; `ToolResult.truncated` = shown < row_count.
5. Redact-on-read columns are a fixed list (core.work_item.summary, score.funding.title); core.incident cannot hold one, so redaction for describe is tested on core.work_item (and get_scores on funding.title).
6. warehouse_tools imports `rapidfuzz` (spec §2 row says "none", but U05-40 requires rapidfuzz for the hint).
7. BT05-05 measures direct tool calls (BT05-04 goes through dispatch, "end to end").
8. `.secrets.baseline` regenerated by the hook: only a line-number shift for the existing docs/impl/05 entry (row added above it) and generated_at; no entries dropped; LF endings.
Final commit: 40c692a feat(harness): add warehouse tools part 1 (T05-17) — all pre-commit hooks passed.

## Fix round 1 (verifier Approved; controller Minors M1-M3)
- M1: describe_table now marks and excludes blocked columns under the guard's own normalisation (NFKC + casefold): `herness.harness.sql_guard._norm` imported as `norm_identifier` (no duplicated logic); `_warehouse_tools_sql.blocked_columns()` returns normalised names. New UT05-81 test `test_ut05_81_fullwidth_lookalike_blocked_column` (stand-in build gains `core.change` with a fullwidth `description` column): marked BLOCKED, absent from sample, call ok. RED on the old code: fatal ConfigError "internal tool SQL failed the SQL guard".
- M2: §2 row for warehouse_tools.py deps set to `rapidfuzz` (closes deviation 6).
- M3: stand-in funding ranks now tie in pairs and action_lever rows tie on delta_usd and on (delta_usd, entity_id); UT05-84 asserts ties exist and the tie-break orders hold. The UT05-82 non-text redaction test moved into the UT05-82 section.
- Lines: warehouse_tools.py 244/400, _warehouse_tools_sql.py 178/200. Tests: card + tests/unit/harness + ST05 files 1240 passed, 2 skipped; test_warehouse_tools.py 25 tests. Coverage 99 % / 100 %. ruff, mypy, lint-imports, module size clean. Benchmarks rerun: BT05-04 p95 22.9 ms; BT05-05 list 0.08 ms, describe 0.26 ms.
Fix round 1 commit: f83081e fix(harness): match blocked columns under guard normalisation (T05-17) — all hooks passed.
