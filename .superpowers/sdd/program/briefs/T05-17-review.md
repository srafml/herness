# T05-17 review — Warehouse tools, part 1 (verify agent)

Worktree agent-a9e496468db262876, base a46205f, head 40c692a. Read-only; mutation probes reverted, `git status` clean.

**Task quality: Approved** (no Critical / Important; 3 Minor).

### Spec Compliance
- ✅ U05-39 `ListTables`: constant `LIST_TABLES_SQL` over `duckdb_tables()` filtered to the five schemas + `$schema`, ordered schema/table; `guard=False`; header `query_id=<qid> tables=<n>`, lines `<s>.<t> | rows=<n> | <comment ≤120>`; `data.tables`; `query_ids=[qid]`; second Python filter on ALLOWED_SCHEMAS (warehouse_tools.py:56-87).
- ✅ U05-40 `DescribeTable`: schema pattern/maxLength; unknown table → `ToolInputError("table <t> does not exist", hint="closest: …")` with 3 rapidfuzz WRatio names drawn only from allowed schemas (probe: `core.incidnt`, `secret.incidnt` with a real `secret.incidnt` table → hints name only core/metrics/score tables); `DESCRIBE_COLUMNS_SQL` constant; sample SELECT identifiers = duckdb_columns order ∩ `schema()` allow-list − blocked, DuckDB-quoted (probe: columns `a"b`, `Foo`, `x; DROP TABLE core.incident; --` quoted correctly, sample ran); blocked marked `BLOCKED (use enrich.text_redacted)`; redact-on-read cells redacted, untrusted wrapped (warehouse_tools.py:93-165).
- ✅ U05-41 `RunSql`: only `execute_recorded(ctx, sql, {}, guard=True)`; redact → `format_result` → data {columns, rows, row_count, truncated}; `truncated = shown < row_count` (warehouse_tools.py:186-189, _warehouse_tools_sql.py:151-175). No own cursor use of model SQL; no HTTP client in the new modules.
- ✅ U05-43 `GetScores`: constant SQL per kind with exactly the spec column lists, "or null" filters, `LIMIT $top`, orderings `rank, candidate_id` / `rank, entity_id, metric` / `delta_usd DESC, entity_id, metric` / `scenario, order_rank NULLS LAST, candidate_id` (_warehouse_tools_sql.py:39-60); scenario rule → ToolInputError; `top` bounds enforced by dispatch (probe: top=101/0 → ToolInputError, schema hint); `score.funding.title` redacted via guard lineage.
- ✅ Carry-overs: (a) ST05-07 uses real `RunSql` via `register_warehouse_tools`, stand-in removed; (b) `open_warehouse` closes the connection on any failure after connect (warehouse.py:178-188, 220/220) with a 3-case regression test (tests/unit/harness/test_warehouse.py:114-151); (c) cold-schema concurrency test for describe/list via dispatch (test_warehouse_tools.py:236-245).
- ⚠️ Stand-in build (tests/support/warehouse_tools_build.py) instead of spec 11 `tiny_build`/`full` — per controller ruling; re-point later.
- ⚠️ warehouse_tools.py 240/400 leaves ~160 lines for T05-18's four tools (tight; builder already moved helpers to the private sibling).

### Verification evidence
- Tests (card files, with coverage): 66 passed, 2 skipped (Windows symlink privilege), no warnings. Coverage: `_warehouse_tools_sql.py` 100 %, `warehouse_tools.py` 99 % (line 75 = defensive non-allowed-schema skip), `warehouse.py` 96 % (line+branch).
- Gates: ruff check clean, ruff format clean (668 files), mypy 0 issues (263 files), lint-imports 13 kept, check_module_size 0, check_type_ownership 0. Complexity/args enforced by ruff config (max-complexity 10, max-args 6).
- Mutations (reverted):
  - RunSql `guard=True`→`False`: ST05-07 and UT05-82 guarded test FAIL (internal guard → fatal ConfigError).
  - RunSql bypassing execute_recorded with a raw cursor: ST05-07 + 3 UT05-82 tests FAIL.
  - `table_result` without `redacted()`: UT05-81 redact, UT05-82 untrusted, UT05-84 title tests FAIL.
  - Removing the Python ALLOWED_SCHEMAS checks in describe/_missing_table: survives, because `DuckWarehouse.schema()` already loads only the five allowed schemas (warehouse.py:76-87) — defence in depth, not a gap.
- Guard probes via RunSql: `"DESCRIPTION"`, fullwidth `"ｄｅｓｃｒｉｐｔｉｏｎ"`, `x.*`, CTE alias of `description` → rule column; `secret.credentials` → table; `duckdb_tables()` → table_function; `read_csv` → function.
- Recording: one evidence + evidence_use row per recorded query; describe `query_ids=[cols, sample]`, others `[qid]`; result_hash only via execute_recorded/run_query.

### BT numbers (re-run once, local full-like build 500k incidents / 200 services / 36 months, 19.9 MB)
- BT05-04 run_sql, 200 distinct aggregates through dispatch: p95 23.3 ms (report 25.4 ms; threshold 2 s).
- BT05-05 list_tables p95 0.08 ms, describe_table p95 0.28 ms (report 0.10 / 0.41; threshold 50 ms). Plausible (warm result cache after call 1, as the spec defines).

### Builder deviations — rulings
1. `**kwargs: JsonValue` signature: accepted (the `Tool` protocol; typed helpers raise ToolInputError on direct calls).
2. `QueryError("table score.<kind> is not in this build")` when the score table is absent: accepted — QueryError is in the unit's Errors row, and it avoids a fatal internal-SQL ConfigError cancelling siblings.
3. Skip sample when every column is blocked: accepted (otherwise invalid `SELECT  FROM`); tested.
4. Content formats the spec leaves open: accepted.
5. Redaction tested via core.work_item.summary / score.funding.title (fixed redact-on-read list): accepted.
6. rapidfuzz import vs §2 row "none": accepted — U05-40 mandates rapidfuzz, already a project dependency (pyproject.toml:45); see Minor 2.
7. BT05-05 direct calls: accepted.

### Issues
#### Critical
None.
#### Important
None.
#### Minor
1. herness/harness/warehouse_tools.py:113 and :150-156 — describe_table's blocked-column test uses plain `str.lower()`, while the guard compares after NFKC + casefold. Probe: adding a column `"ｃｌｏｓｅ_ｎｏｔｅｓ"` to core.incident → it is listed without the BLOCKED mark, included in the sample SELECT, and the internal guard rejects it → fatal `ConfigError("internal tool SQL failed the SQL guard", rule="SQL guard: column")`. Fail-closed (no text leaks) and builds come from Herness's own DDL, so low risk; fix by normalising with the guard's `_norm` (NFKC+casefold) for marking and exclusion.
2. docs/impl/05-harness-core.impl.md:99 — the `warehouse_tools.py` §2 row still lists third-party deps "none" although it now imports `rapidfuzz` (U05-40 requires it); update the row (the guard row already lists rapidfuzz).
3. tests/unit/harness/test_warehouse_tools.py:351-367 — funding/action_lever fixtures have no ties, so the `candidate_id` / `entity_id, metric` tie-break orderings for those kinds are unexercised (org and portfolio do cover tie-breaks); `test_ut05_82_redact_on_read_non_text_cell_redacted_as_json` (line 426) sits under the UT05-84 section. Polish.

### Assessment
**Task quality:** Approved
**Reasoning:** All four tools match U05-39/40/41/43; agent SQL provably goes only through `execute_recorded(guard=True)` (mutations caught by ST05-07/UT05-82), identifiers are allow-listed and quoted, redaction/wrapping hold, carry-overs closed with real tests, and all gates and benchmarks pass. Remaining items are fail-closed hardening and docs polish.

---

## Re-review round 1 (head f83081e, base 40c692a)

**Task quality: Approved.**

- ✅ M1 fixed: `blocked_columns()` and both describe_table checks (marking `warehouse_tools.py` `_column_lines`, sample exclusion in `DescribeTable.__call__`) now use the guard's NFKC + casefold (`sql_guard._norm`). My probe was re-run with a fullwidth `"ｃｌｏｓｅ_ｎｏｔｅｓ"` column added to core.incident. The column is now marked BLOCKED and left out of the sample. The call succeeds with no internal-guard ConfigError, and the probe's column value `NFKCLEAK` appears nowhere in the content. The new regression test `test_ut05_81_fullwidth_lookalike_blocked_column` (core.change with a fullwidth `description`) covers it.
- ✅ M2 fixed: the §2 row for `warehouse_tools.py` now lists `rapidfuzz`.
- ✅ M3 fixed: in the funding data, rank now has ties that `candidate_id` must break. In the action_lever data (8 rows), there are ties on delta_usd and on (delta_usd, entity_id), so the metric tie-break is exercised. Both are asserted in the test. `test_ut05_82_redact_on_read_non_text_cell_redacted_as_json` moved to the UT05-82 section.
- No regressions:
  - Tests: test_warehouse_tools.py + ST05 tools/warehouse ran 50 passed, 1 skipped (symlink privilege).
  - Checks: ruff check/format clean, mypy 0 issues (263 files), lint-imports 13 kept, check_module_size 0.
- Minor (new, non-blocking): `_norm` is imported from `sql_guard` as a private name in two modules (warehouse_tools.py:28, _warehouse_tools_sql.py:26). Consider a public alias in sql_guard (e.g. `normalize_identifier`) when that module is next touched.
